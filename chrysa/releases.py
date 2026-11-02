"""Understand what a torrent title is, so Browse can match it to the title the user meant.

Every Browse result passes through :func:`classify`, which drops titles carrying piracy-tool words (crack, keygen,
and so on) and reads the show or film name, year, season and episode out of the rest. :func:`relevance` then scores
how well that name matches the query.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

MOVIE, SEASON, EPISODE, OTHER = "movie", "season", "episode", "other"

# File extensions a download may contain; everything else is never fetched (see torrentguard).
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".flv", ".ogv"}
HARMLESS_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".nfo", ".txt", ".jpg", ".jpeg", ".png"}

_QUALITY = re.compile(
    r"(?i)(?<![a-z0-9])(?:480p|576p|720p|1080p|1080i|2160p|4k|uhd|bluray|blu-ray|bdrip|brrip|bdremux|remux|web-?dl|web-?rip|"
    r"webrip|hdrip|dvdrip|dvdscr|hdtv|pdtv|hdcam|hd-?ts|camrip|x264|x265|h\.?264|h\.?265|hevc|xvid|divx|av1)(?![a-z0-9])"
)
_SEASON = re.compile(
    r"(?i)(?<![a-z0-9])(?:s(\d{1,2})(?![\dxe])|season[\s._-]*(\d{1,2})|(\d{1,2})(?:st|nd|rd|th)[\s._-]*season|"
    r"s(\d{1,2})[\s._-]*-[\s._-]*s?(\d{1,2}))"
)
_SEASON_RANGE = re.compile(r"(?i)(?<![a-z0-9])(?:s|seasons?[\s._-]*)(\d{1,2})[\s._-]*(?:-|to|&)[\s._-]*s?(\d{1,2})(?![\d])")
_EPISODE = re.compile(
    r"(?i)(?<![a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})(?:[\s._-]*-?[\s._-]*e?\d{1,3})?(?![a-z0-9])"
    r"|(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])"
    r"|(?<![a-z0-9])season[\s._-]*(\d{1,2})[\s._-]*(?:episode|ep)[\s._-]*(\d{1,3})(?![\d])"
)
_COMPLETE = re.compile(r"(?i)(?<![a-z0-9])(?:complete[\s._-]*(?:series|collection|seasons?)|all[\s._-]*seasons|the[\s._-]*complete)(?![a-z0-9])")
_YEAR = re.compile(r"(?<![\d])((?:19|20)\d{2})(?![\d])")

# Anything here makes a title unsafe or off-topic, whatever else it says.
_BANNED = re.compile(
    r"(?i)(?<![a-z0-9])(?:crack(?:ed)?|keygen|activator|fitgirl|skidrow|nulled|warez|trainer|pc[\s._-]*game)(?![a-z0-9])"
)


@dataclass(frozen=True)
class Release:
    kind: str  # MOVIE, SEASON or EPISODE
    title: str  # the show / film name found in the release name, normalised
    year: int | None = None
    season: int | None = None  # first season of a season pack, or the episode's season
    last_season: int | None = None  # last season of a multi-season pack
    episode: int | None = None
    quality: str = ""  # "2160p", "1080p", "720p", "480p" or ""


def normalise(text: str) -> str:
    """Lower-case, strip punctuation and accents-free separators: ``"Spider-Man: No Way Home"`` -> ``"spider man no way home"``."""
    text = text.lower().replace("&", " and ").replace("'", "").replace("’", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _quality(name: str) -> str:
    low = name.lower()
    if re.search(r"2160p|(?<![a-z0-9])4k|(?<![a-z0-9])uhd", low):
        return "2160p"
    for res in ("1080p", "1080i", "720p", "576p", "480p"):
        if res in low:
            return "1080p" if res == "1080i" else res
    return ""


def classify(name: str) -> Release | None:
    """Return what *name* is (movie, season, episode, or other), or ``None`` for piracy-tool titles."""
    if not name or _BANNED.search(name):
        return None
    has_quality = bool(_QUALITY.search(name))
    spaced = re.sub(r"[._]+", " ", name)

    episode = _EPISODE.search(spaced)
    if episode:
        season, number = _season_episode(episode)
        head = spaced[: episode.start()]
        return Release(EPISODE, _title_of(head), _year_of(head), int(season), None, int(number), _quality(name))

    season_range = _SEASON_RANGE.search(spaced)
    season = _SEASON.search(spaced)
    complete = _COMPLETE.search(spaced)
    if season_range or season or complete:
        match = season_range or season or complete
        head = spaced[: match.start()]
        first = last = None
        if season_range:
            first, last = int(season_range.group(1)), int(season_range.group(2))
        elif season:
            number = next((g for g in season.groups()[:4] if g), None)
            first = int(number) if number else None
        return Release(SEASON, _title_of(head), _year_of(head), first, last, None, _quality(name))

    year = _YEAR.search(spaced)
    if year and has_quality:
        head = spaced[: year.start()]
        title = _title_of(head)
        if title:
            return Release(MOVIE, title, int(year.group(1)), None, None, None, _quality(name))
    quality = _QUALITY.search(spaced)
    head = spaced[: quality.start()] if quality else spaced
    return Release(OTHER, _title_of(head) or normalise(spaced), _year_of(spaced), None, None, None, _quality(name))


def _season_episode(match: re.Match) -> tuple[int, int]:
    groups = [g for g in match.groups() if g is not None]
    return int(groups[0]), int(groups[1])


def _year_of(head: str) -> int | None:
    found = _YEAR.findall(head)
    return int(found[-1]) if found else None


def _title_of(head: str) -> str:
    head = re.sub(r"\[[^\]]*\]|\([^)]*\)|^\s*www\.\S+\s*[-–]?", " ", head)
    head = _YEAR.sub(" ", head)
    return normalise(head)


# --------------------------------------------------------------------------- query understanding


@dataclass(frozen=True)
class Title:
    name: str  # canonical, e.g. "Breaking Bad"
    year: int | None
    is_series: bool
    imdb_id: str = ""


@dataclass(frozen=True)
class Query:
    raw: str
    text: str  # what the user typed minus season/episode/year qualifiers
    year: int | None = None
    season: int | None = None
    episode: int | None = None


def parse_query(raw: str) -> Query:
    """Split ``"breaking bad s02"`` into the title words and the season/episode/year qualifiers."""
    text = raw.strip()
    season = episode = year = None
    match = _EPISODE.search(text)
    if match:
        season, episode = _season_episode(match)
        text = text[: match.start()] + " " + text[match.end():]
    else:
        match = _SEASON.search(text)
        if match:
            number = next((g for g in match.groups()[:4] if g), None)
            season = int(number) if number else None
            text = text[: match.start()] + " " + text[match.end():]
    years = _YEAR.findall(text)
    if years and len(normalise(_YEAR.sub(" ", text)).split()) >= 1:
        year = int(years[-1])
        text = _YEAR.sub(" ", text)
    text = re.sub(r"(?i)\b(?:complete|season|series|movie|film|full|download)\b", " ", text)
    return Query(raw.strip(), re.sub(r"\s+", " ", text).strip(), year, season, episode)


def lookup_titles(text: str, timeout: float = 6.0, limit: int = 3) -> list[Title]:
    """Resolve a partial title ("breaking") to real ones ("Breaking Bad") through IMDb's public suggestion API."""
    key = normalise(text)
    if len(key) < 2:
        return []
    url = f"https://v3.sg.media-imdb.com/suggestion/{key[0]}/{urllib.parse.quote(text.strip())}.json"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrysa"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return []
    titles = []
    for item in payload.get("d", []):
        kind, name = item.get("qid") or "", item.get("l")
        if not name or kind not in ("movie", "tvSeries", "tvMiniSeries", "tvMovie", "video"):
            continue
        titles.append(Title(name, item.get("y"), kind in ("tvSeries", "tvMiniSeries"), item.get("id") or ""))
    return titles[:limit]


def target_titles(query: Query, candidates: list[Title]) -> list[Title]:
    """Which candidates the user plausibly means: names that start with, or contain, every query word."""
    words = normalise(query.text).split()
    keep = []
    for title in candidates:
        name = normalise(title.name).split()
        if not words or not all(any(n.startswith(w) for n in name) for w in words):
            continue
        if query.year and title.year and abs(query.year - title.year) > 1:
            continue
        if query.season is not None and not title.is_series:
            continue
        keep.append(title)
    return keep


# --------------------------------------------------------------------------- relevance + ranking


def relevance(release: Release, query: Query, targets: list[Title]) -> int:
    """0 = unrelated (drop). Higher is a closer match. Season/episode/year qualifiers must agree."""
    if query.season is not None:
        if release.kind in (MOVIE, OTHER):
            return 0
        if release.season is not None:
            last = release.last_season or release.season
            if not release.season <= query.season <= last:
                return 0
        if query.episode is not None and release.kind == EPISODE and release.episode != query.episode:
            return 0
    if query.year and release.year and abs(release.year - query.year) > 1:
        return 0
    name = release.title
    words = normalise(query.text).split()
    if targets:
        best = 0
        for title in targets:
            canonical = normalise(title.name)
            if name == canonical:
                best = max(best, 5 if title is targets[0] else 4)
            elif name.startswith(canonical + " ") or name.endswith(" " + canonical):
                best = max(best, 2)  # "Breaking Bad Something" / "The Making of ..." : weak
        if best:
            return best
    # No canonical title matched (offline, or the user typed something IMDb does not know).
    if words and all(w in name.split() or any(t.startswith(w) for t in name.split()) for w in words):
        return 3 if name == " ".join(words) else 1
    return 0


_FOREIGN = re.compile(
    r"(?i)(?<![a-z0-9])(?:italian|french|german|spanish|latino|castellano|portuguese|brazilian|russian|hindi|tamil|telugu|"
    r"korean|japanese|chinese|turkish|polish|dutch|swedish|norwegian|danish|finnish|czech|hungarian|greek|arabic|thai|"
    r"ita|fra|fre|ger|spa|rus|hin|kor|jap|vff|vostfr|truefrench)(?![a-z0-9])"
)
_ENGLISH = re.compile(r"(?i)(?<![a-z0-9])(?:english|eng|multi|dual)(?![a-z0-9])")


def is_foreign(name: str) -> bool:
    """True when the name advertises another language and does not also say English, multi-language or dual audio."""
    return bool(_FOREIGN.search(name)) and not _ENGLISH.search(name)


def rank_key(release: Release, relevance_score: int, seeders: int, name: str = "") -> tuple:
    """Sort key, best first when reversed: closest title match, then English before foreign, then the most seeders."""
    return (relevance_score, 0 if is_foreign(name) else 1, seeders)


def describe(release: Release) -> str:
    """A short label for the result row: ``Movie 2010 · 1080p`` / ``Season 3 · 720p`` / ``S03E04``."""
    quality = f" · {release.quality}" if release.quality else ""
    if release.kind == OTHER:
        return f"Other{quality}"
    if release.kind == MOVIE:
        return f"Movie{' ' + str(release.year) if release.year else ''}{quality}"
    if release.kind == SEASON:
        if release.season is None:
            return f"Complete series{quality}"
        if release.last_season and release.last_season != release.season:
            return f"Seasons {release.season}-{release.last_season}{quality}"
        return f"Season {release.season}{quality}"
    return f"S{release.season:02d}E{release.episode:02d}{quality}"
