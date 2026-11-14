"""Online search for downloadable videos: every source in parallel, then understood, de-duplicated and ranked.

The pipeline for one query (see :class:`TorrentSourcesProvider`):

1. the query is resolved to real titles ("breaking" -> "Breaking Bad") and an intent (movie, episode, season...);
2. every enabled source (:mod:`chrysa.sources`: YTS, The Pirate Bay, 1337x, LimeTorrents, Nyaa, SubsPlease, EZTV and
   the Internet Archive) is asked in parallel, each with its own timeout; one slow or broken source never blocks or
   hides the others, and how each one fared is recorded as its health;
3. :mod:`chrysa.ranking` drops unrelated and unsafe names, merges the same torrent found on several sites, scores
   what is left (match, quality, safety, source, swarm, *measured* past performance) and explains each score.

Recent per-source answers are cached for a few minutes, so repeating a search does not hit every site again.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from . import bd, ranking, releases, sources
from .history import History

log = logging.getLogger("chrysa.search")

USER_AGENT = "Chrysa (offline media center)"
TIMEOUT = 15

last_summary = ""  # one line about how the most recent search went, shown under the results
last_report: SearchReport | None = None
history = History(None)  # replaced by configure()


def configure(store: History) -> None:
    """Give search and the sources their persistent memory (the library database)."""
    global history
    history = store
    sources.configure(store)


@dataclass(frozen=True)
class SearchResult:
    title: str
    metadata: str  # one display line, e.g. "Peers/Seeders: 150"
    target_url: str  # magnet link or .torrent URL handed to start_download()
    candidate: ranking.Candidate | None = field(default=None, compare=False, repr=False)  # everything Browse knows


class SearchError(Exception):
    pass


@dataclass
class SearchReport:
    """How one search went, for the status line under the search box and for diagnostics."""

    query: str
    intent: str = ranking.INTENT_ANY
    targets: list[str] = field(default_factory=list)
    outcomes: dict[str, sources.Outcome] = field(default_factory=dict)
    health: dict[str, dict] = field(default_factory=dict)
    raw: int = 0
    unique: int = 0
    high_quality: int = 0
    unrelated: int = 0
    unsafe: int = 0
    elapsed: float = 0.0

    @property
    def answered(self) -> int:
        return sum(o.ok for o in self.outcomes.values())

    def line(self) -> str:
        parts = [f"{self.raw} results", f"{self.unique} unique releases"]
        if self.high_quality:
            parts.append(f"{self.high_quality} high-quality matches")
        hidden = self.unrelated + self.unsafe
        if hidden:
            parts.append(f"{hidden} unrelated or suspicious hidden")
        return " · ".join(parts)


class SearchProvider:
    name = "provider"

    def search(self, query: str, limit: int = 50) -> list[SearchResult]:
        raise NotImplementedError


class InternetArchiveProvider(SearchProvider):
    name = "Internet Archive"
    ENDPOINT = "https://archive.org/advancedsearch.php"

    def search(self, query: str, limit: int = 50) -> list[SearchResult]:
        try:
            return self.parse(self.fetch(query, limit))
        except (OSError, ValueError) as error:
            raise SearchError(f"Search failed: {error}") from error

    def fetch(self, query: str, limit: int = 50) -> dict:
        # Quote the query so user text cannot inject Lucene operators.
        quoted = '"' + query.replace("\\", " ").replace('"', " ").strip() + '"'
        params = [
            ("q", f"title:({quoted}) AND mediatype:movies"),
            ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "downloads"), ("fl[]", "item_size"),
            ("sort[]", "downloads desc"), ("rows", str(limit)), ("output", "json"),
        ]
        url = f"{self.ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)

    @staticmethod
    def parse(payload: dict) -> list[SearchResult]:
        results = []
        for doc in payload.get("response", {}).get("docs", []):
            identifier = doc.get("identifier")
            if not identifier:
                continue
            ident = urllib.parse.quote(str(identifier), safe="")
            title = doc.get("title") or identifier
            if isinstance(title, list):
                title = title[0]
            size = doc.get("item_size")
            parts = [f"Downloads: {doc.get('downloads', 0)}"]
            if isinstance(size, int) and size > 0:
                parts.append(f"Size: {size / 1e9:.2f} GB" if size >= 1e9 else f"Size: {size / 1e6:.0f} MB")
            results.append(SearchResult(
                str(title), " · ".join(parts), f"https://archive.org/download/{ident}/{ident}_archive.torrent",
            ))
        return results

    @classmethod
    def rows(cls, query: str) -> list[dict]:
        """The archive's films as source rows (no swarm figures: it seeds them itself)."""
        payload = cls().fetch(query, 30)
        if not isinstance(payload, dict) or "response" not in payload:
            raise bd.SourceError(bd.CHANGED, "Internet Archive answer has no 'response'")
        docs = {d.get("identifier"): d for d in payload["response"].get("docs", [])}
        out = []
        for result in cls.parse(payload):
            ident = urllib.parse.unquote(result.target_url.split("/download/", 1)[1].split("/", 1)[0])
            size = docs.get(ident, {}).get("item_size")
            out.append(bd.result("Internet Archive", "", result.title, size if isinstance(size, int) else 0,
                                 None, None, result.target_url))
        return out


sources.register(sources.Source("Internet Archive", InternetArchiveProvider.rows, frozenset({sources.TEXT}),
                                canary="night of the living dead"))


# --------------------------------------------------------------------------- caching

_CACHE_TTL = {sources.OK: 600.0, bd.NO_RESULTS: 120.0}  # failures are never cached
_TITLE_TTL = 1800.0
_cache: dict[tuple[str, str], tuple[float, sources.Outcome]] = {}
_titles: dict[str, tuple[float, list[releases.Title]]] = {}
_cache_lock = threading.Lock()


def _cached_titles(text: str) -> list[releases.Title]:
    key = releases.normalise(text)
    with _cache_lock:
        hit = _titles.get(key)
    if hit and time.monotonic() - hit[0] < _TITLE_TTL:
        return hit[1]
    found = releases.lookup_titles(text)
    if found:  # an empty answer may just be a network blip; do not remember it
        with _cache_lock:
            _titles[key] = (time.monotonic(), found)
    return found


def _cached_search(source: sources.Source, term: str, keep) -> sources.Outcome:
    key = (source.name, releases.normalise(term))
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL.get(hit[1].status, 0):
        return hit[1]
    outcome = source.search(term, keep)
    if outcome.status in _CACHE_TTL:
        with _cache_lock:
            _cache[key] = (time.monotonic(), outcome)
    return outcome


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _titles.clear()


def _merge(name: str, outcomes: list[sources.Outcome]) -> sources.Outcome:
    """One source asked several spellings: it answered if any spelling answered."""
    good = [o for o in outcomes if o.ok]
    if good:
        rows = [r for o in good for r in o.rows]
        merged = sources.Outcome(name, sources.OK if rows else bd.NO_RESULTS, rows,
                                 elapsed=max(o.elapsed for o in outcomes), total=sum(o.total for o in good),
                                 rejected=sum(o.rejected for o in good), filtered=sum(o.filtered for o in good))
        merged.detail = next((o.detail for o in good if o.detail), "")
        if any(o.status == bd.CHANGED for o in outcomes):
            merged.detail = merged.detail or next(o.detail for o in outcomes if o.status == bd.CHANGED)
        return merged
    worst = min(outcomes, key=lambda o: bd._rank(o.status))
    return sources.Outcome(name, worst.status, detail=worst.detail, elapsed=max(o.elapsed for o in outcomes))


# --------------------------------------------------------------------------- the search


class TorrentSourcesProvider(SearchProvider):
    """Every registered source, matched to the title the user meant and ranked by :mod:`chrysa.ranking`."""

    name = "Torrent sources"
    DEADLINE = 14.0  # seconds to wait for all sources

    def search(self, query: str, limit: int = 60, progress: Callable[[dict], None] | None = None) -> list[SearchResult]:
        started = time.monotonic()
        progress = progress or (lambda event: None)
        parsed = releases.parse_query(query)
        if not parsed.text:
            raise SearchError("Type a movie or show name.")
        log.info('[SEARCH] query="%s"', query)
        targets = releases.target_titles(parsed, _cached_titles(parsed.text))
        terms = self._terms(parsed, targets)
        intent = ranking.detect_intent(parsed, targets)

        def accepts(name: str) -> bool:
            release = releases.classify(name)
            return release is not None and releases.relevance(release, parsed, targets) > 0

        active = [s for s in sources.REGISTRY.values() if s.enabled and sources.TEXT in s.capabilities]
        jobs = [(source, term) for source in active for term in terms]
        series = next((t for t in targets if t.is_series and t.imdb_id), None)
        eztv = sources.REGISTRY.get("EZTV")
        if series and eztv and eztv.enabled:
            jobs.append((eztv, series.imdb_id))
        names = list(dict.fromkeys(source.name for source, _ in jobs))
        progress({"kind": "start", "sources": names})

        per_source: dict[str, list[sources.Outcome]] = {name: [] for name in names}
        expected = {name: sum(1 for s, _ in jobs if s.name == name) for name in names}
        merged: dict[str, sources.Outcome] = {}
        pool = cf.ThreadPoolExecutor(min(16, len(jobs) + 1), thread_name_prefix="chrysa-search")
        try:
            futures = {pool.submit(_cached_search, source, term, accepts): source.name for source, term in jobs}
            try:
                for future in cf.as_completed(futures, timeout=self.DEADLINE):
                    name = futures[future]
                    per_source[name].append(future.result())
                    if len(per_source[name]) == expected[name]:
                        merged[name] = _merge(name, per_source[name])
                        progress({"kind": "source", "source": name, "status": merged[name].status,
                                  "count": len(merged[name].rows)})
            except cf.TimeoutError:
                pass  # a slow site is dropped rather than making everyone wait
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for name in names:
            if name not in merged:
                outcomes = per_source[name] or []
                answered = [o for o in outcomes if o.ok]
                merged[name] = _merge(name, answered) if answered else sources.Outcome(
                    name, bd.TIMEOUT, detail=f"no answer within {self.DEADLINE:.0f} s", elapsed=self.DEADLINE)
                progress({"kind": "source", "source": name, "status": merged[name].status,
                          "count": len(merged[name].rows)})

        stored = history.source_rows()
        suspicious = {
            name for name, outcome in merged.items()
            if sources.is_suspicious_empty(name, outcome, sum(len(o.rows) for n, o in merged.items() if n != name),
                                           stored.get(name))
        }
        health = sources.update_health(merged, suspicious)
        rows = [row for outcome in merged.values() for row in outcome.rows]
        ranked = ranking.rank(rows, parsed, targets, history, {n: r["state"] for n, r in health.items()})
        log.info("[FILTER] removed %d unrelated and %d suspicious results", ranked.unrelated, ranked.unsafe)
        log.info("[DEDUP] %d → %d unique", len(rows), ranked.unique)
        if ranked.candidates:
            best = ranked.candidates[0]
            log.info("[RANK] best: %s (%.2f; %s)", best.name, best.overall, best.scores)

        report = SearchReport(
            query, intent, [t.name for t in targets[:2]], merged, health, len(rows), ranked.unique,
            sum(c.high_quality for c in ranked.candidates), ranked.unrelated, ranked.unsafe,
            time.monotonic() - started,
        )
        results = [SearchResult(c.name, self._metadata(c), c.target, c) for c in ranked.candidates[:limit]]
        global last_summary, last_report
        last_report = report
        last_summary = report.line()
        return results

    @staticmethod
    def _metadata(c: ranking.Candidate) -> str:
        swarm = f"Seeders: {c.seeders} · Leechers: {c.leechers}" if c.seeders is not None else "Seeders: n/a"
        return f"{releases.describe(c.release)} · [{', '.join(c.sources)}] {bd.human(c.size)} · {swarm}"

    @staticmethod
    def _terms(parsed: releases.Query, targets: list[releases.Title]) -> list[str]:
        """The search strings to try: what was typed, then each canonical title (with the season when asked)."""
        terms = [parsed.text]
        for title in targets[:2]:
            name = title.name
            if parsed.season is not None and title.is_series:
                name += f" S{parsed.season:02d}"
            elif parsed.year:
                name += f" {parsed.year}"
            terms.append(name)
        unique, seen = [], set()
        for term in terms:
            key = releases.normalise(term)
            if key and key not in seen:
                seen.add(key)
                unique.append(term)
        return unique[:3]


PROVIDERS: list[SearchProvider] = [TorrentSourcesProvider()]


def search(query: str, limit: int = 60, progress: Callable[[dict], None] | None = None) -> list[SearchResult]:
    """Run *query* against every source. Raises :class:`SearchError` only when nothing could be searched at all."""
    provider = PROVIDERS[0]
    results = provider.search(query, limit, progress) if isinstance(provider, TorrentSourcesProvider) \
        else provider.search(query, limit)
    report = last_report
    if not results and report and report.outcomes and not report.answered:
        raise SearchError("No source could be reached. Check the internet connection, or the Sources list for details.")
    return results
