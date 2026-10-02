"""Walk library folders and classify every video file.

The scanner is pure filesystem + name logic: it never touches the database and
never opens a video. It understands the layouts found in real download folders:

* **Movie folders** — ``Heat (1995) [1080p]/Heat.1995.1080p.BRrip.x264.YIFY.mp4``.
  The feature is the largest video; samples, trailers and site promos are ignored.
* **Shows with season folders** — ``Friends/S01/…``, ``BEEF/Season 2/…``.
* **Season packs and flat show folders** — ``This.Is.Us.S01.COMPLETE.720p…/`` with
  the episodes directly inside.
* **Loose files** — anything else, classified on its own name: an episode if it
  carries ``S01E02``, a movie if it carries a year or release tags, otherwise an
  "other" video (``ES_Project_Demo.mp4``).

Plain folders (``Movies/``, ``Camera/``…) are treated as containers and walked
recursively, so the same rules apply at any depth.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import naming
from .config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

KIND_MOVIE = "movie"
KIND_EPISODE = "episode"
KIND_OTHER = "other"

# Sub-folders of a release that never hold the feature itself.
_JUNK_DIRS = frozenset(
    {
        "sample", "samples", "subs", "sub", "subtitles", "extras", "extra", "featurettes",
        "featurette", "behind the scenes", "deleted scenes", "trailers", "trailer",
        "interviews", "bonus", "bonus features", "special features", "screens",
        "screenshots", "proof", "nfo", "covers", "artwork", "scans",
    }
)
_SYSTEM_DIRS = frozenset({"$recycle.bin", "system volume information", "lost+found", "@eadir"})

# Artwork file names, most specific first.
_COVER_STEMS = (
    "poster", "cover", "folder", "movie", "show", "front", "default", "thumb",
    "fanart", "backdrop", "background", "landscape", "banner",
)
# Images dropped into releases by torrent sites: adverts, not artwork.
_PROMO_IMAGE_RE = re.compile(
    r"(?i)(www\.|https?[:_]|yts|yify|rarbg|torrent|eztv|ettv|1337x|tgx|galaxy|psarips|"
    r"official\s*site|downloaded\s*from|\.(?:com|net|org|to|am|ag|mx|lt|bz|gg|is|se|ws|cc|tv|io|me|link|site)\b)"
)
_MIN_COVER_BYTES = 6 * 1024
# Names produced by cameras, phones and screen recorders: never feature films.
_RECORDING_RE = re.compile(
    r"(?i)^(?:vid|pxl|img|mov|mvi|dsc|gopr|gx\d|screencast|screen[\s_-]?(?:record|cast|shot)|zoom|obs|"
    r"recording|whatsapp|signal|telegram|trim|vlc-record|simplescreenrecorder|kazam|capture|clip)(?![a-z])"
)
# A second video must be at least this fraction of the largest to count as a feature.
_FEATURE_SIZE_RATIO = 0.5
# A file tagged "sample" that also carries S01E02 is only skipped below this size.
_SAMPLE_MAX_BYTES = 60 * 1024 * 1024


@dataclass(slots=True)
class ScannedVideo:
    """One playable file and everything its name and location say about it."""

    path: str
    root: str
    size: int
    mtime: float
    kind: str
    title: str
    year: int | None = None
    # ``tentative`` marks a classification made from a bare file name; the library
    # may flip movie <-> other once the duration is known, using ``alt_title``.
    tentative: bool = False
    alt_title: str | None = None
    show_key: str | None = None
    show_title: str | None = None
    show_year: int | None = None
    show_dir: str | None = None
    show_cover: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    cover: str | None = None


@dataclass(slots=True)
class ScanResult:
    videos: list[ScannedVideo] = field(default_factory=list)
    scanned_roots: list[str] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)
    ignored: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    def of_kind(self, kind: str) -> list[ScannedVideo]:
        return [v for v in self.videos if v.kind == kind]


@dataclass(slots=True)
class _File:
    path: str
    name: str
    size: int
    mtime: float

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]


@dataclass(slots=True)
class _Listing:
    videos: list[_File] = field(default_factory=list)
    images: list[_File] = field(default_factory=list)
    subdirs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Show:
    key: str
    title: str
    year: int | None
    directory: str | None
    cover: str | None


def natural_key(text: str) -> list:
    """Sort key that orders ``Season 2`` before ``Season 10``."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def list_dir(path: str) -> _Listing:
    """List videos, images and sub-folders of *path*, skipping hidden and system entries."""
    listing = _Listing()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name.casefold() in _SYSTEM_DIRS:
                    continue
                try:
                    if entry.is_dir():
                        listing.subdirs.append(name)
                        continue
                    if not entry.is_file():
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext in VIDEO_EXTENSIONS or ext in IMAGE_EXTENSIONS:
                        stat = entry.stat()
                        if stat.st_size <= 0:
                            continue  # placeholder left behind by an aborted download
                        item = _File(entry.path, name, stat.st_size, stat.st_mtime)
                        (listing.videos if ext in VIDEO_EXTENSIONS else listing.images).append(item)
                except OSError:
                    continue
    except OSError:
        return listing
    listing.subdirs.sort(key=natural_key)
    listing.videos.sort(key=lambda f: natural_key(f.name))
    listing.images.sort(key=lambda f: natural_key(f.name))
    return listing


def pick_cover(images: list[_File], video_stem: str | None = None) -> tuple[str | None, bool]:
    """Choose artwork among *images*.

    Returns ``(path, generic)``. ``generic`` is True when the image was accepted
    only because nothing better exists; such picks are later dropped if the very
    same image turns up in several unrelated folders (a sure sign of an advert).
    """
    if not images:
        return None, False
    by_stem = {img.stem.casefold(): img for img in images}
    if video_stem:
        base = video_stem.casefold()
        for suffix in ("", "-poster", ".poster", "-cover", "-thumb", ".thumb", "-fanart", "-landscape"):
            hit = by_stem.get(base + suffix)
            if hit:
                return hit.path, False
    for stem in _COVER_STEMS:
        hit = by_stem.get(stem)
        if hit:
            return hit.path, False
    candidates = [
        img for img in images if img.size >= _MIN_COVER_BYTES and not _PROMO_IMAGE_RE.search(img.name)
    ]
    if not candidates:
        return None, False
    return max(candidates, key=lambda img: img.size).path, True


def pick_episode_cover(images: list[_File], video_stem: str) -> str | None:
    """Artwork for a single episode: only an image named after the episode file."""
    base = video_stem.casefold()
    for img in images:
        stem = img.stem.casefold()
        if stem == base or stem in (base + "-thumb", base + ".thumb"):
            return img.path
    return None


class Scanner:
    """Classify every video below a set of library roots."""

    def __init__(
        self,
        progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._progress = progress or (lambda _path: None)
        self._should_stop = should_stop or (lambda: False)
        self._videos: list[ScannedVideo] = []
        self._ignored: dict[str, str] = {}
        self._seen_dirs: set[str] = set()
        self._generic_covers: set[str] = set()
        self._root = ""

    # ------------------------------------------------------------------ API

    def scan(self, roots: Iterable[str | os.PathLike]) -> ScanResult:
        self._videos, self._ignored = [], {}
        self._seen_dirs, self._generic_covers = set(), set()
        result = ScanResult()
        for raw in roots:
            root = os.path.abspath(os.path.expanduser(os.fspath(raw)))
            if not os.path.isdir(root):
                result.missing_roots.append(root)
                continue
            self._root = root
            self._scan_container(root, list_dir(root))
            result.scanned_roots.append(root)
        self._drop_shared_artwork()
        self._unify_shows()
        self._videos.sort(key=lambda v: natural_key(v.path))
        result.videos = self._videos
        result.ignored = sorted(self._ignored.items())
        return result

    # --------------------------------------------------------------- walking

    def _enter(self, path: str) -> bool:
        """Guard against symlink loops and honour cancellation."""
        if self._should_stop():
            return False
        real = os.path.realpath(path)
        if real in self._seen_dirs:
            return False
        self._seen_dirs.add(real)
        self._progress(path)
        return True

    def _scan_container(self, path: str, listing: _Listing) -> None:
        """A plain folder: classify its loose files one by one, then descend."""
        if path == self._root and not self._enter(path):
            return
        for video in listing.videos:
            self._emit_loose(video, listing)
        for sub in listing.subdirs:
            self._scan_dir(os.path.join(path, sub))

    def _scan_dir(self, path: str) -> None:
        if not self._enter(path):
            return
        name = os.path.basename(path)
        listing = list_dir(path)
        info = naming.parse_dir(name)
        title_key = naming.show_key(info.title) if info.title else None

        # A. A show whose seasons live in sub-folders: Friends/S01, BEEF/Season 2.
        seasons = {}
        for sub in listing.subdirs:
            number = naming.season_of_dir(sub, title_key)
            if number is not None:
                seasons[sub] = number
        if seasons and self._scan_show_with_seasons(path, name, listing, info, seasons):
            return

        direct = [v for v in listing.videos if not self._is_extra(v)]
        pack_season = naming.season_pack_number(name)

        # B. A season pack named after the show: This.Is.Us.S01.COMPLETE.720p…/
        if pack_season is not None and info.title:
            files = self._collect(path, listing)
            if files:
                show = self._make_show(info.title, info.year, path, listing, files)
                for video in files:
                    self._emit_episode(video, show, pack_season, listing)
                return

        # C. A flat folder of numbered episodes: Chernobyl/Chernobyl.S01E01.mkv …
        if len(direct) >= 2:
            show = self._detect_flat_show(path, name, info, listing, direct)
            if show:
                default_season = pack_season if pack_season is not None else 1
                for video in direct:
                    self._emit_episode(video, show, default_season, listing)
                for sub in listing.subdirs:
                    if sub.casefold() not in _JUNK_DIRS:
                        self._scan_dir(os.path.join(path, sub))
                return

        # D. A release folder holding one feature (and maybe samples or promos).
        #    A bare "2025/" archive folder has a year but no title: that is a container.
        if direct and (info.technical or (info.year is not None and info.title)):
            main = self._feature_of(direct)
            if main is not None:
                own = naming.parse(main.name)
                if own.is_episode and own.title:
                    self._emit_loose(main, listing)  # Show.S01E01.1080p-GRP/show.s01e01.mkv
                else:
                    self._emit_movie(main, info, listing)
            else:  # several features side by side: a collection folder
                for video in direct:
                    self._emit_loose(video, listing)
            for sub in listing.subdirs:
                if sub.casefold() not in _JUNK_DIRS:
                    self._scan_dir(os.path.join(path, sub))
            return

        # E. Anything else is a container: Movies/, Camera/, Downloads/ …
        self._scan_container(path, listing)

    def _collect(self, path: str, listing: _Listing | None = None) -> list[_File]:
        """All features below *path*, skipping junk folders, samples and extras."""
        listing = listing if listing is not None else list_dir(path)
        files = [v for v in listing.videos if not self._is_extra(v)]
        for sub in listing.subdirs:
            if sub.casefold() in _JUNK_DIRS:
                continue
            child = os.path.join(path, sub)
            if self._enter(child):
                files.extend(self._collect(child))
        return files

    def _is_extra(self, video: _File) -> bool:
        """Samples and trailers inside a release are noted and skipped."""
        parsed = naming.parse(video.name)
        if parsed.sample:
            # "Show.S01E05.The.Sample.mkv" is a real episode; a sample clip is small.
            if parsed.marker != "strong" or video.size < _SAMPLE_MAX_BYTES:
                self._ignored[video.path] = "sample"
                return True
        if parsed.extra:
            self._ignored[video.path] = "trailer/extra"
            return True
        return False

    def _feature_of(self, videos: list[_File]) -> _File | None:
        """The one feature-sized file among *videos*, or None if there are several."""
        largest = max(videos, key=lambda v: v.size)
        features = [v for v in videos if v.size >= largest.size * _FEATURE_SIZE_RATIO]
        if len(features) != 1:
            return None
        for video in videos:
            if video is not largest:
                self._ignored[video.path] = "smaller companion file"
        return largest

    # ----------------------------------------------------------------- shows

    def _scan_show_with_seasons(
        self, path: str, name: str, listing: _Listing, info: naming.ParsedName, seasons: dict[str, int]
    ) -> bool:
        episodes: list[tuple[_File, int, _Listing]] = []
        season_listings: list[_Listing] = []
        for sub, number in seasons.items():
            season_path = os.path.join(path, sub)
            if not self._enter(season_path):
                continue
            season_listing = list_dir(season_path)
            season_listings.append(season_listing)
            for video in self._collect(season_path, season_listing):
                episodes.append((video, number, season_listing))
        if not episodes:
            return False

        title = info.title or naming.clean_stem(name)
        show = self._make_show(title, info.year, path, listing, [e[0] for e in episodes], season_listings)
        for video, number, season_listing in episodes:
            self._emit_episode(video, show, number, season_listing)

        # Videos beside the season folders are specials unless they carry S01E02.
        for video in listing.videos:
            if not self._is_extra(video):
                self._emit_episode(video, show, 0, listing)
        for sub in listing.subdirs:
            if sub in seasons or sub.casefold() in _JUNK_DIRS:
                continue
            child = os.path.join(path, sub)
            if self._enter(child):
                child_listing = list_dir(child)
                for video in self._collect(child, child_listing):
                    self._emit_episode(video, show, 0, child_listing)
        return True

    def _detect_flat_show(
        self, path: str, name: str, info: naming.ParsedName, listing: _Listing, direct: list[_File]
    ) -> _Show | None:
        parsed = [naming.parse(video.name) for video in direct]
        numbered = [p for p in parsed if p.is_numbered]
        if len(numbered) < max(2, math.ceil(0.6 * len(direct))):
            return None

        named = [p.title for p in numbered if p.names_show]
        if named:
            keys = Counter(naming.show_key(t) for t in named)
            key, count = keys.most_common(1)[0]
            if count < 0.6 * len(named):
                return None  # several different shows share this folder
            if info.title and naming.show_key(info.title) == key:
                title, year = info.title, info.year
            else:
                spellings = Counter(t for t in named if naming.show_key(t) == key)
                title, year = spellings.most_common(1)[0][0], None
        else:
            title, year = info.title or self._parent_title(path) or naming.clean_stem(name), info.year
        return self._make_show(title, year, path, listing, direct)

    def _parent_title(self, path: str) -> str | None:
        """Title for a bare ``Season 1`` folder: the name of the folder above it."""
        parent = os.path.basename(os.path.dirname(path))
        return naming.parse_dir(parent).title if parent else None

    def _make_show(
        self,
        title: str,
        year: int | None,
        directory: str | None,
        listing: _Listing | None,
        files: list[_File],
        season_listings: list[_Listing] | None = None,
    ) -> _Show:
        if year is None:
            years = Counter(
                y for y in (naming.parse(f.name, "episode").year for f in files[:30]) if y is not None
            )
            if years:
                year = years.most_common(1)[0][0]
        cover = None
        for candidate in [listing, *(season_listings or [])]:
            if candidate is None:
                continue
            cover, generic = pick_cover(candidate.images)
            if cover:
                if generic:
                    self._generic_covers.add(cover)
                break
        return _Show(naming.show_key(title), title, year, directory, cover)

    def _emit_episode(self, video: _File, show: _Show, default_season: int | None, listing: _Listing) -> None:
        parsed = naming.parse(video.name, "episode")
        season = parsed.season if parsed.season is not None else default_season
        if parsed.episode_title:
            title = parsed.episode_title
        elif parsed.title and not parsed.names_show and naming.show_key(parsed.title) != show.key:
            # "01 - Pilot.mkv" / "Friends.The.Reunion.2021.mkv": the name is the episode's own.
            title = naming.strip_show_prefix(parsed.title, show.title)
        elif parsed.episodes:
            title = f"Episode {parsed.episodes[0]}"
        else:
            title = naming.strip_show_prefix(naming.clean_stem(video.stem), show.title)
        self._videos.append(
            ScannedVideo(
                path=video.path,
                root=self._root,
                size=video.size,
                mtime=video.mtime,
                kind=KIND_EPISODE,
                title=title,
                show_key=show.key,
                show_title=show.title,
                show_year=show.year,
                show_dir=show.directory,
                show_cover=show.cover,
                season=season,
                episode=parsed.episode,
                episode_end=parsed.episode_end,
                cover=pick_episode_cover(listing.images, video.stem),
            )
        )

    # ---------------------------------------------------- movies and others

    def _emit_movie(self, video: _File, info: naming.ParsedName, listing: _Listing) -> None:
        own = naming.parse(video.name, "movie")
        if info.title and info.year is not None:
            title, year = info.title, info.year
        elif own.title and own.year is not None:
            title, year = own.title, own.year
        else:
            title = info.title or own.title or naming.clean_stem(video.stem)
            year = info.year if info.year is not None else own.year
        cover, generic = pick_cover(listing.images, video.stem)
        if cover and generic:
            self._generic_covers.add(cover)
        self._videos.append(
            ScannedVideo(
                path=video.path, root=self._root, size=video.size, mtime=video.mtime,
                kind=KIND_MOVIE, title=title, year=year, cover=cover,
            )
        )

    def _emit_loose(self, video: _File, listing: _Listing) -> None:
        """Classify a file by its own name alone."""
        parsed = naming.parse(video.name)
        plain = naming.clean_stem(video.stem)

        if parsed.is_episode and parsed.title:
            show = _Show(naming.show_key(parsed.title), parsed.title, parsed.year, None, None)
            self._emit_episode(video, show, parsed.season, listing)
            return

        cover = pick_episode_cover(listing.images, video.stem)
        if parsed.technical or (parsed.year is not None and not parsed.dated):
            self._videos.append(
                ScannedVideo(
                    path=video.path, root=self._root, size=video.size, mtime=video.mtime,
                    kind=KIND_MOVIE, title=parsed.title or plain, year=parsed.year,
                    tentative=True, alt_title=plain, cover=cover,
                )
            )
            return

        promotable = not parsed.dated and not _RECORDING_RE.match(video.stem)
        self._videos.append(
            ScannedVideo(
                path=video.path, root=self._root, size=video.size, mtime=video.mtime,
                kind=KIND_OTHER, title=plain, tentative=promotable, alt_title=plain, cover=cover,
            )
        )

    # ------------------------------------------------------- post-processing

    def _drop_shared_artwork(self) -> None:
        """Discard 'artwork' that is byte-identical across unrelated folders.

        Torrent sites drop the same advert image into every release. Only images
        accepted on the weakest rule are checked, and only when sizes collide.
        """
        if len(self._generic_covers) < 2:
            return
        by_size: dict[int, list[str]] = {}
        for cover in self._generic_covers:
            try:
                by_size.setdefault(os.path.getsize(cover), []).append(cover)
            except OSError:
                continue
        shared: set[str] = set()
        for paths in by_size.values():
            if len(paths) < 2:
                continue
            by_hash: dict[str, list[str]] = {}
            for cover in paths:
                try:
                    with open(cover, "rb") as handle:
                        digest = hashlib.md5(handle.read(), usedforsecurity=False).hexdigest()
                except OSError:
                    continue
                by_hash.setdefault(digest, []).append(cover)
            for same in by_hash.values():
                if len(same) > 1:
                    shared.update(same)
        if not shared:
            return
        for video in self._videos:
            if video.cover in shared:
                video.cover = None
            if video.show_cover in shared:
                video.show_cover = None

    def _unify_shows(self) -> None:
        """Give every episode of a show the same title, year, folder and artwork.

        One show can be assembled from several folders (``Show.S01…/``,
        ``Show.S02…/`` and loose files); they must agree on its identity.
        """
        groups: dict[str, list[ScannedVideo]] = {}
        for video in self._videos:
            if video.kind == KIND_EPISODE and video.show_key:
                groups.setdefault(video.show_key, []).append(video)
        for episodes in groups.values():
            titles = Counter(e.show_title for e in episodes if e.show_title)
            title = titles.most_common(1)[0][0]
            year = next((e.show_year for e in episodes if e.show_year is not None), None)
            directory = next((e.show_dir for e in episodes if e.show_dir), None)
            cover = next((e.show_cover for e in episodes if e.show_cover), None)
            for episode in episodes:
                episode.show_title, episode.show_year = title, year
                episode.show_dir, episode.show_cover = directory, cover
