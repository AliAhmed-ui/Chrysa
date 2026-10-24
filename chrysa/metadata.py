"""Fetch plots, ratings and artwork for the Details pages from free, keyless web services.

Nothing here runs unless the user presses *Fetch Details*; the library itself works
offline. TV shows and episodes come from TVMaze (summaries, ratings, air dates, posters,
backdrops, episode stills); movies come from Wikipedia (the article's opening paragraph and
its poster). No account or API key is involved and only titles are sent. Images are
downloaded into ``Paths.metadata`` so the detail views never touch the network.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .library import Library
from .models import Details, Media, Show
from .subtitles import LogFn, display_name, is_online

TVMAZE = "https://api.tvmaze.com"
WIKIPEDIA_SEARCH = "https://en.wikipedia.org/w/rest.php/v1/search/page"
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_TIMEOUT = 15.0
_USER_AGENT = "Chrysa/1.0 (offline media centre; local plots and artwork lookup)"
_TVMAZE_PAUSE = 0.6  # TVMaze allows 20 calls per 10 seconds
_WIKIPEDIA_PAUSE = 0.2

JsonGet = Callable[[str, dict[str, str]], Any]
Download = Callable[[str, str], bool]


class MetadataError(Exception):
    """A web service gave an answer that could not be used."""


class NetworkError(MetadataError):
    """The service could not be reached."""


@dataclass(slots=True)
class DetailsSummary:
    shows: int = 0
    episodes: int = 0
    movies: int = 0
    not_found: int = 0
    failed: int = 0
    cancelled: bool = False
    offline: bool = False

    @property
    def fetched(self) -> int:
        return self.shows + self.episodes + self.movies


# --------------------------------------------------------------------- transport


def get_json(url: str, params: dict[str, str]) -> Any:
    """Decoded JSON from *url*, or None for a 404. Raises MetadataError subclasses."""
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    request = urllib.request.Request(full, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 429 and attempt == 1:  # rate limited: wait as asked, once
                time.sleep(min(10.0, float(error.headers.get("Retry-After", "3") or 3)))
                continue
            raise MetadataError(f"HTTP {error.code} from {urllib.parse.urlsplit(url).netloc}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise NetworkError(str(getattr(error, "reason", error))) from error
        except ValueError as error:
            raise MetadataError("unreadable answer from " + urllib.parse.urlsplit(url).netloc) from error
    return None


def download_image(url: str, target: str) -> bool:
    """Save *url* as *target* (kept if it is already there). False if it could not be fetched."""
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    partial = f"{target}.part"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response, open(partial, "wb") as handle:
            handle.write(response.read())
        os.replace(partial, target)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        try:
            os.unlink(partial)
        except OSError:
            pass
        return False


# ------------------------------------------------------------------------ parsing


def clean_text(markup: str | None) -> str | None:
    """TVMaze summaries are HTML (``<p>…<b>…</b></p>``); return plain text or None if empty."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup or ""))
    return re.sub(r"\s+", " ", text).strip() or None


def _rating(block: Any) -> float | None:
    try:
        return round(float((block or {}).get("average")), 1) or None
    except (TypeError, ValueError):
        return None


def _year(date: str | None) -> int | None:
    try:
        return int((date or "")[:4])
    except ValueError:
        return None


def _picture(images: Any) -> str | None:
    return (images or {}).get("original") or (images or {}).get("medium")


def best_show(hits: list[dict], year: int | None) -> dict | None:
    """From TVMaze's ranked search results, the first show premiered in *year* (else the top hit)."""
    shows = [hit["show"] for hit in hits or [] if isinstance(hit, dict) and hit.get("show")]
    if year is not None:
        for show in shows:
            if _year(show.get("premiered")) == year:
                return show
    return shows[0] if shows else None


def _normal(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()


def best_film(pages: list[dict], title: str, year: int | None) -> str | None:
    """The Wikipedia article key of the film among search *pages*, or None.

    A page must be described as a film; with a known year the description must carry it
    (``1995 crime drama film by Michael Mann``), or carry no year at all and the article must be
    named exactly like the title. A different year in the description rules the page out.
    """
    wanted = _normal(title)
    for page in pages or []:
        description = (page.get("description") or "").casefold()
        if "film" not in description and "movie" not in description:
            continue
        base = _normal(re.sub(r"\(.*?\)", "", (page.get("title") or "")))
        years = re.findall(r"\b(?:18|19|20)\d\d\b", description)
        if year is None or str(year) in years or (not years and base == wanted):
            return page.get("key")
    return None


# ------------------------------------------------------------------------ fetching


class _Fetcher:
    def __init__(
        self, library: Library, get: JsonGet, download: Download, log: LogFn, should_stop: Callable[[], bool],
        pause: float,
    ) -> None:
        self.library, self.get, self.download, self.log, self.should_stop = library, get, download, log, should_stop
        self.pause = pause
        self.summary = DetailsSummary()
        self.folder = str(library.paths.metadata)

    def image(self, stem: str, remote: str | None) -> str | None:
        """Download *remote* as ``<stem>.jpg`` into the metadata folder; the local path, or None."""
        if not remote:
            return None
        target = os.path.join(self.folder, f"{stem}.jpg")
        return target if self.download(remote, target) else None

    def _wait(self, seconds: float) -> None:
        time.sleep(seconds * self.pause)

    def show(self, show: Show) -> None:
        stored = self.library.show_details(show.id)
        source_id = stored.source_id if stored and stored.source_id else None
        if stored is None or (stored.plot is None and not source_id):
            found = best_show(self.get(f"{TVMAZE}/search/shows", {"q": show.title}), show.year)
            self._wait(_TVMAZE_PAUSE)
            if found is None:
                self.summary.not_found += 1
                self.log("WARN", f"{show.title}: not found on TVMaze.")
                self.library.save_show_details(show.id, Details(fetched_at=time.time()))
                for episode in self.library.episodes_missing_details(show.id):
                    self.library.save_media_details(episode.id, Details(fetched_at=time.time()))
                return
            source_id = found["id"]
            backdrop = self._backdrop(source_id)
            self.library.save_show_details(show.id, Details(
                source_id=source_id, plot=clean_text(found.get("summary")), rating=_rating(found.get("rating")),
                image_path=self.image(f"show-{show.id}-poster", _picture(found.get("image"))),
                backdrop_path=self.image(f"show-{show.id}-backdrop", backdrop),
                fetched_at=time.time(),
            ))
            self.summary.shows += 1
            self.log("OK", f"{show.title}: plot and artwork saved.")
        elif source_id is None:
            return  # looked up before and nothing matched; the monthly retry will come back to it

        missing = self.library.episodes_missing_details(show.id)
        if not missing or self.should_stop():
            return
        listing = self.get(f"{TVMAZE}/shows/{source_id}/episodes", {"specials": "1"}) or []
        self._wait(_TVMAZE_PAUSE)
        by_number = {(e.get("season"), e.get("number")): e for e in listing if e.get("number") is not None}
        for episode in missing:
            self._episode(episode, by_number.get((episode.season, episode.episode)))
        self.log("OK", f"{show.title}: {len(missing)} episodes checked.")

    def _backdrop(self, source_id: int) -> str | None:
        """A wide picture for the banner, when TVMaze has one (it often has only posters)."""
        pictures = self.get(f"{TVMAZE}/shows/{source_id}/images", {}) or []
        self._wait(_TVMAZE_PAUSE)
        wide = [p for p in pictures if p.get("type") == "background"]
        wide.sort(key=lambda p: not p.get("main"))
        return (wide[0].get("resolutions", {}).get("original", {}) or {}).get("url") if wide else None

    def _episode(self, media: Media, found: dict | None) -> None:
        if found is None:
            self.library.save_media_details(media.id, Details(fetched_at=time.time()))
            return
        self.library.save_media_details(media.id, Details(
            source_id=found.get("id"), title=found.get("name") or None, plot=clean_text(found.get("summary")),
            rating=_rating(found.get("rating")), air_date=found.get("airdate") or None,
            image_path=self.image(f"media-{media.id}", _picture(found.get("image"))),
            fetched_at=time.time(),
        ))
        self.summary.episodes += 1

    def movie(self, media: Media) -> None:
        query = f"{media.title} {media.year} film" if media.year else f"{media.title} film"
        found = self.get(WIKIPEDIA_SEARCH, {"q": query, "limit": "6"}) or {}
        key = best_film(found.get("pages", []), media.title, media.year)
        self._wait(_WIKIPEDIA_PAUSE)
        article = self.get(WIKIPEDIA_SUMMARY + urllib.parse.quote(key, safe="()_,'"), {}) if key else None
        self._wait(_WIKIPEDIA_PAUSE)
        if not article or article.get("type") == "disambiguation" or not article.get("extract"):
            self.summary.not_found += 1
            self.log("WARN", f"{display_name(media)}: no article found on Wikipedia.")
            self.library.save_media_details(media.id, Details(fetched_at=time.time()))
            return
        poster = (article.get("originalimage") or article.get("thumbnail") or {}).get("source")
        self.library.save_media_details(media.id, Details(
            plot=clean_text(article["extract"]),
            image_path=self.image(f"media-{media.id}", poster), fetched_at=time.time(),
        ))
        self.summary.movies += 1
        self.log("OK", f"{display_name(media)}: overview and poster saved.")


def fetch_missing_details(
    library: Library,
    log: LogFn,
    should_stop: Callable[[], bool] = lambda: False,
    get: JsonGet = get_json,
    download: Download = download_image,
    online: Callable[[], bool] = is_online,
    pause: float = 1.0,
) -> DetailsSummary:
    """Look up every show, episode and movie that has no plot yet and store what the web knows.

    Never raises: no connection ends the run with ``offline`` set and a log line; trouble with a
    single title is logged and skipped. ``pause`` scales the delay between requests (0 in tests).
    """
    shows, movies = library.shows_missing_details(), library.movies_missing_details()
    if not shows and not movies:
        log("DONE", "Every title already has its details.")
        return DetailsSummary()
    if not online():
        log("WARN", "No internet connection - fetching details skipped.")
        return DetailsSummary(offline=True)
    fetcher = _Fetcher(library, get, download, log, should_stop, pause)
    summary = fetcher.summary
    log("INFO", f"Fetching details for {len(shows)} shows and {len(movies)} movies...")
    jobs = [(show.title, lambda s=show: fetcher.show(s)) for show in shows]
    jobs += [(display_name(movie), lambda m=movie: fetcher.movie(m)) for movie in movies]
    try:
        for name, job in jobs:
            if should_stop():
                summary.cancelled = True
                break
            try:
                job()
            except NetworkError:
                raise
            except Exception as error:  # one odd answer or full disk must not stop the rest
                summary.failed += 1
                log("WARN", f"{name}: {type(error).__name__}: {error}")
    except NetworkError as error:
        summary.offline = True
        log("WARN", f"Lost the connection ({error}). Run Fetch Details again later.")
        return summary
    if summary.cancelled:
        log("INFO", "Fetching details cancelled.")
    else:
        log("DONE", f"Details saved: {summary.shows} shows, {summary.episodes} episodes, {summary.movies} movies.")
    return summary
