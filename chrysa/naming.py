"""Turn messy release names into clean titles, years, seasons and episodes.

All real parsing is delegated to `guessit`_; this module only normalises its
output into a small immutable record, adds a few structural sanity checks that
a media *library* needs (guessit happily reads ``Blade Runner 2049 (2017)`` as
season 2017, episode 2049 when it is not told the name is a movie), and caches
results because guessit costs a few milliseconds per name.

.. _guessit: https://github.com/guessit-io/guessit
"""

from __future__ import annotations

import json
import re
import string
import threading
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from typing import Any

from guessit import guessit

# Bump when the meaning of a cached ParsedName changes, to invalidate the cache.
PARSER_VERSION = 2

# guessit keys that only ever appear on real releases (as opposed to home videos).
_TECHNICAL_KEYS = ("screen_size", "source", "video_codec", "streaming_service", "video_profile", "color_depth")

# An explicit episode marker: S01E02, 1x02, "Season 1 Episode 2".
_EPISODE_MARKER_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:s\d{1,4}[\s._-]*e\d{1,4}|\d{1,2}x\d{2,3}(?!\d)"
    r"|season[\s._-]*\d{1,2}[\s._-]*(?:episode|ep)[\s._-]*\d{1,4})"
)
# Fansub style numbering: "[Group] Show - 05 [1080p]".
_ABSOLUTE_MARKER_RE = re.compile(r"\s-\s\d{1,4}(?:v\d)?(?=\s*(?:[\[(]|\.[a-z0-9]{2,4}$|$))", re.I)
# Bare leading counter: "01 - Pilot", "2. Setup", "03 Finale".
_LEADING_MARKER_RE = re.compile(r"^\d{1,3}(?!\d)(?:\s*[-–.)]\s*|\s+)(?=\S)")
# A season marker in a *folder* name: "S01", "S01-S05", "Season 2", "Series 3".
_SEASON_TOKEN_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[(])(?:s(\d{1,2})(?:[\s._-]*-?[\s._-]*s\d{1,2})?(?=$|[\s._\-\])])"
    r"|(?:season|series|saison|staffel|temporada|stagione)[\s._-]*(\d{1,2})(?!\d))"
)
# A folder that *is* a season: "Season 1", "season1", "S01", "Series 2", "Season 01 (1080p)".
_SEASON_DIR_RE = re.compile(
    r"(?i)^(?:season|series|saison|staffel|temporada|stagione|s)[\s._-]*(\d{1,3})(?=$|[\s._\-(\[])(?![\s._-]*e\d)"
)
_SPECIALS_DIR_RE = re.compile(r"(?i)^(?:specials?|season[\s._-]*0+|s0+)$")
_SAMPLE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])sample(?:[^a-z0-9]|$)")
_DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}[-_. ]?(?:0[1-9]|1[0-2])[-_. ]?(?:0[1-9]|[12]\d|3[01])(?!\d)")
_TITLE_YEAR_RE = re.compile(r"^\s*(?P<title>.+?)\s*[(\[]\s*(?P<year>(?:19|20)\d{2})\s*[)\]]")
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.I)


@dataclass(frozen=True, slots=True)
class ParsedName:
    """What a file or folder name tells us."""

    title: str | None = None
    year: int | None = None
    season: int | None = None
    episodes: tuple[int, ...] = ()
    episode_title: str | None = None
    technical: bool = False  # carries resolution / source / codec tags
    sample: bool = False
    extra: bool = False  # trailer, featurette, bonus material
    special: bool = False
    dated: bool = False  # the name contains a calendar date (recordings, phone clips)
    marker: str = ""  # "strong" (S01E02 / 1x02), "absolute" (" - 05 "), "leading" ("01 - ") or ""

    @property
    def episode(self) -> int | None:
        return self.episodes[0] if self.episodes else None

    @property
    def episode_end(self) -> int | None:
        return self.episodes[-1] if len(self.episodes) > 1 else None

    @property
    def is_episode(self) -> bool:
        """True only for an unambiguous episode: explicit marker, season and number."""
        return self.marker == "strong" and self.season is not None and bool(self.episodes)

    @property
    def is_numbered(self) -> bool:
        """Episode-like enough to count inside a folder that looks like a show."""
        if not self.episodes:
            return False
        if self.marker in ("strong", "absolute"):
            return True
        return self.marker == "leading" and self.year is None and not self.technical

    @property
    def names_show(self) -> bool:
        """Whether ``title`` is the show's name (as opposed to the episode's)."""
        return self.marker in ("strong", "absolute") and bool(self.title)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str], ParsedName] = {}
_fresh: set[tuple[str, str]] = set()
_lock = threading.Lock()


def load_cache(rows: Iterable[tuple[str, str, str]]) -> int:
    """Prime the cache with ``(name, hint, json)`` rows saved by :func:`drain_new_entries`."""
    loaded = 0
    with _lock:
        for name, hint, data in rows:
            try:
                fields = json.loads(data)
                fields["episodes"] = tuple(fields.get("episodes", ()))
                _cache[(name, hint)] = ParsedName(**fields)
                loaded += 1
            except (TypeError, ValueError):
                continue
    return loaded


def drain_new_entries() -> Iterator[tuple[str, str, str]]:
    """Yield entries parsed since the last call, as ``(name, hint, json)``."""
    with _lock:
        keys = list(_fresh)
        _fresh.clear()
        entries = [(key, _cache[key]) for key in keys if key in _cache]
    for (name, hint), parsed in entries:
        yield name, hint, json.dumps(asdict(parsed), separators=(",", ":"))


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _fresh.clear()


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse(name: str, hint: str | None = None) -> ParsedName:
    """Parse a file or folder *name* (not a path).

    ``hint`` is ``"movie"``, ``"episode"`` or ``None`` to let guessit decide.
    """
    key = (name, hint or "")
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    parsed = _parse(name, hint)
    with _lock:
        _cache[key] = parsed
        _fresh.add(key)
    return parsed


def parse_dir(name: str) -> ParsedName:
    """Parse a folder name: as a season pack if it carries a season marker, else as a movie."""
    return parse(name, "episode" if _SEASON_TOKEN_RE.search(name) else "movie")


def _parse(name: str, hint: str | None) -> ParsedName:
    options: dict[str, Any] = {"name_only": True}
    if hint:
        options["type"] = hint
    try:
        guess = dict(guessit(name, options))
    except Exception:  # guessit is robust, but a bad name must never abort a scan
        guess = {}

    other = _strings(guess.get("other"))
    details = _strings(guess.get("episode_details"))
    year = _first_int(guess.get("year"))
    title = _clean_title(_first_str(guess.get("title")))
    if title:
        title = _restore_verbatim_title(name, title, year)

    if _EPISODE_MARKER_RE.search(name):
        marker = "strong"
    elif _ABSOLUTE_MARKER_RE.search(name):
        marker = "absolute"
    elif _LEADING_MARKER_RE.match(name):
        marker = "leading"
    else:
        marker = ""

    season = _first_int(guess.get("season"))
    episodes = _ints(guess.get("episode"))
    dated = "date" in guess or bool(_DATE_RE.search(name))
    if dated and marker != "strong":
        # "Screencast From 2026-09-08 18-45-12" is not season 2026, episode 45.
        season, episodes = None, ()

    episode_title = _first_str(guess.get("episode_title"))
    part = _first_int(guess.get("part"))
    if episode_title and part and "part" not in episode_title.lower():
        episode_title = f"{episode_title} (Part {part})"

    return ParsedName(
        title=title,
        year=year,
        season=season,
        episodes=episodes,
        episode_title=_clean_title(episode_title),
        technical=any(k in guess for k in _TECHNICAL_KEYS),
        sample="Sample" in other or bool(_SAMPLE_RE.search(name)),
        extra=bool({"Trailer", "Extras", "Bonus"} & set(other)) or bool({"Extras", "Bonus", "Trailer"} & set(details)),
        special="Special" in details,
        dated=dated,
        marker=marker,
    )


def season_of_dir(name: str, show_key_: str | None = None) -> int | None:
    """Return the season number if *name* is a season folder of the enclosing show.

    ``Season 1``, ``season1``, ``S01`` and ``Specials`` (season 0) always qualify;
    ``Friends Season 2`` or ``Friends.S02.720p`` qualify when the enclosing show is
    Friends (``show_key_``).
    """
    stripped = name.strip()
    if _SPECIALS_DIR_RE.match(stripped):
        return 0
    match = _SEASON_DIR_RE.match(stripped)
    if match:
        return int(match.group(1))
    if show_key_ and _SEASON_TOKEN_RE.search(stripped):
        parsed = parse(stripped, "episode")
        if parsed.season is not None and not parsed.episodes:
            if parsed.title is None or show_key(parsed.title) == show_key_:
                return parsed.season
    return None


def season_pack_number(name: str) -> int | None:
    """Season number of a pack folder such as ``This.Is.Us.S01.COMPLETE.720p``, else None."""
    if not _SEASON_TOKEN_RE.search(name):
        return None
    parsed = parse(name, "episode")
    return None if parsed.episodes else parsed.season


def is_sample(name: str) -> bool:
    return bool(_SAMPLE_RE.search(name))


# ---------------------------------------------------------------------------
# helpers for display / identity
# ---------------------------------------------------------------------------


def clean_stem(stem: str) -> str:
    """A readable title for a file we could not classify: ``ES_Project_Demo`` → ``ES Project Demo``."""
    text = stem.replace("_", " ")
    if " " not in text:
        text = text.replace(".", " ")
    return re.sub(r"\s+", " ", text).strip() or stem


def show_key(title: str) -> str:
    """Identity of a show regardless of punctuation and case: ``This.Is.Us`` == ``This Is Us``."""
    key = "".join(ch for ch in title.casefold() if ch.isalnum())
    return key or title.casefold().strip()


def sort_title(title: str) -> str:
    """Sort key that ignores a leading article and punctuation."""
    text = _ARTICLE_RE.sub("", title.strip()).casefold()
    return re.sub(r"^[^\w]+", "", text) or title.casefold()


def strip_show_prefix(title: str, show_title: str) -> str:
    """``Friends The Reunion`` inside the show Friends → ``The Reunion``."""
    if show_key(title).startswith(show_key(show_title)) and len(show_key(title)) > len(show_key(show_title)):
        pattern = r"[\W_]*".join(re.escape(ch) for ch in show_title if ch.isalnum())
        match = re.match(pattern, title, re.I)
        if match:
            rest = title[match.end():].lstrip(" -–—:._")
            if rest:
                return rest
    return title


def _restore_verbatim_title(name: str, title: str, year: int | None) -> str:
    """Prefer the literal text of a human-style ``Title (Year)`` name.

    guessit drops punctuation it treats as separators, turning
    ``When Harry Met Sally... (1989)`` into ``When Harry Met Sally`` and
    ``Mission - Impossible (1996)`` into ``Mission``. When the name is spelled
    with spaces and guessit agrees on the year, the text before it *is* the title.
    """
    match = _TITLE_YEAR_RE.match(name)
    if not match or year is None or int(match.group("year")) != year:
        return title
    verbatim = match.group("title").strip()
    if " " in verbatim or not re.search(r"[._]", verbatim):
        if show_key(verbatim).startswith(show_key(title)):
            return verbatim
    return title


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"\s+", " ", title).strip(" -–—._")
    if not title:
        return None
    if title.islower():
        title = string.capwords(title)
    return title


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _first_str(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return str(value) if value is not None else None


def _first_int(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ints(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(v for v in value if isinstance(v, int) and not isinstance(v, bool))
    return (value,) if isinstance(value, int) and not isinstance(value, bool) else ()
