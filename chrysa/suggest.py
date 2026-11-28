"""Title suggestions while typing in Browse, and spelling correction for the search itself.

Suggestions come from IMDb's public, keyless suggestion feed (the one its own search box
uses), which tolerates typos: "brking abd" already answers Breaking Bad. TVmaze is the
fallback when IMDb cannot be reached. Only the typed text is sent, never anything from
the library, and only once the user has allowed internet access in Browse.
"""

from __future__ import annotations

import concurrent.futures as cf
import difflib
import http.client
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

IMDB = "https://v3.sg.media-imdb.com/suggestion/x/{}.json"
TVMAZE = "https://api.tvmaze.com/search/shows"
TIMEOUT = 3.0
USER_AGENT = "Chrysa/1.0 (offline media centre; title suggestions)"
LIMIT = 8
KINDS = {  # IMDb title types worth suggesting -> how they are labelled
    "movie": "Movie", "tvSeries": "TV series", "tvMiniSeries": "Mini-series",
    "tvMovie": "TV movie", "tvSpecial": "TV special",
}

# Words that narrow a search rather than name the title: kept when the title is corrected.
_EXTRA = re.compile(r"^(s\d{1,2}(e\d{1,3})?|e\d{1,3}|season|episode|ep|complete|\d{1,2}x\d{1,3}|"
                    r"(19|20)\d{2}|\d{3,4}p|4k|uhd|hdr|x26[45]|hevc|bluray|web(-?dl|rip)?|remux)$")


@dataclass(frozen=True)
class Suggestion:
    title: str
    year: int | None = None
    kind: str = ""

    @property
    def label(self) -> str:
        """What the drop-down shows: Breaking Bad (2008) · TV series."""
        text = f"{self.title} ({self.year})" if self.year else self.title
        return f"{text}  ·  {self.kind}" if self.kind else text


_cache: dict[str, list[Suggestion]] = {}
_lock = threading.Lock()
_local = threading.local()  # each pool thread keeps its connections open between keystrokes
_pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="chrysa-suggest")


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.casefold()).split()


def _get(url: str) -> object:
    """Decoded JSON from *url* over a kept-alive connection, so a keystroke skips the TLS handshake."""
    parts = urllib.parse.urlsplit(url)
    connections = _local.__dict__.setdefault("connections", {})
    path = f"{parts.path}?{parts.query}" if parts.query else parts.path
    for attempt in (1, 2):
        connection = connections.get(parts.netloc)
        if connection is None:
            connection = connections[parts.netloc] = http.client.HTTPSConnection(parts.netloc, timeout=TIMEOUT)
        try:
            connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
        except (http.client.HTTPException, OSError):
            connection.close()
            connections.pop(parts.netloc, None)
            if attempt == 2:  # the server may simply have closed an idle connection: retry once on a new one
                raise
            continue
        if response.status != 200:
            raise urllib.error.URLError(f"HTTP {response.status} from {parts.netloc}")
        return json.loads(body)
    return None


def warm() -> None:
    """Open the IMDb connections now, so the first suggestions do not wait for the handshake."""
    def connect() -> None:
        try:
            _get(IMDB.format("a"))
        except (urllib.error.URLError, OSError, ValueError):
            pass
    for _ in range(_pool._max_workers):  # one connection per pool thread
        _pool.submit(connect)


def fetch(query: str, done) -> None:
    """Look *query* up on a pool thread and call ``done(suggestions)`` there."""
    _pool.submit(lambda: done(suggestions(query)))


def guess(query: str) -> list[Suggestion]:
    """An instant answer from earlier lookups: the titles found for the longest prefix already asked
    that still fit what is typed ("breaking" was looked up, "breaking b" is typed). [] if none fit."""
    words = _words(query)
    key = " ".join(words)
    with _lock:
        if key in _cache:
            return _cache[key]
        prefix = next((key[:end] for end in range(len(key) - 1, 1, -1) if key[:end] in _cache), None)
        earlier = _cache.get(prefix, []) if prefix else []
    def fits(suggestion: Suggestion) -> bool:  # each typed word starts (or nearly starts) the next title word
        title = iter(_words(suggestion.title))
        return all(any(t.startswith(w) or difflib.SequenceMatcher(None, w, t[:len(w)]).ratio() >= 0.75
                       for t in title) for w in words)
    return [s for s in earlier if fits(s)]


def _from_imdb(query: str) -> list[Suggestion]:
    payload = _get(IMDB.format(urllib.parse.quote(query.casefold())))
    found = []
    for entry in (payload or {}).get("d", []) if isinstance(payload, dict) else []:
        kind = KINDS.get(entry.get("qid", ""))
        if kind and entry.get("l"):
            found.append(Suggestion(entry["l"], entry.get("y"), kind))
    return found


def _from_tvmaze(query: str) -> list[Suggestion]:
    payload = _get(f"{TVMAZE}?{urllib.parse.urlencode({'q': query})}")
    found = []
    for hit in payload if isinstance(payload, list) else []:
        show = hit.get("show") or {}
        premiered = show.get("premiered") or ""
        if show.get("name"):
            found.append(Suggestion(show["name"], int(premiered[:4]) if premiered[:4].isdigit() else None, "TV series"))
    return found


def suggestions(query: str) -> list[Suggestion]:
    """Up to LIMIT titles for what the user has typed so far, best first. Never raises."""
    key = " ".join(_words(query))
    if len(key) < 2:
        return []
    with _lock:
        if key in _cache:
            return _cache[key]
    found: list[Suggestion] = []
    for source in (_from_imdb, _from_tvmaze):
        try:
            found = source(key)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError,
                AttributeError):
            continue
        if found:
            break
    unique, seen = [], set()
    for suggestion in found:  # the same title as a film and a remake, say, is offered once per year
        mark = (suggestion.title.casefold(), suggestion.year)
        if mark not in seen:
            seen.add(mark)
            unique.append(suggestion)
    unique = unique[:LIMIT]
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = unique
    return unique


def split(query: str) -> tuple[str, str]:
    """(title words, narrowing words): "brking abd s01 1080p" -> ("brking abd", "s01 1080p")."""
    words = query.split()
    return (" ".join(w for w in words if not _EXTRA.match(w.casefold())),
            " ".join(w for w in words if _EXTRA.match(w.casefold())))


def corrected(query: str, found: list[Suggestion] | None = None) -> str | None:
    """The query with its title spelled properly ("brking abd s01" -> "Breaking Bad s01"), or None.

    Nothing is changed when every word typed is already in the best suggestion (so "batman"
    stays "batman" rather than becoming "The Batman"), or when the best suggestion is not
    close enough to what was typed to be a misspelling of it.
    """
    core, extra = split(query)
    if not core:
        return None
    found = suggestions(core) if found is None else found
    if not found:
        return None
    title = found[0].title
    typed, real = _words(core), _words(title)
    if not typed or set(typed) <= set(real):
        return None
    if difflib.SequenceMatcher(None, " ".join(typed), " ".join(real)).ratio() < 0.6:
        return None
    return f"{title} {extra}".strip()
