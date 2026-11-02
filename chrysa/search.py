"""Online search for downloadable videos. Pluggable: a provider turns a query into results.

Bundled providers: torrent indexes (results are matched to the title you meant, see :mod:`chrysa.releases`) (via :mod:`chrysa.bd`) and the Internet Archive
(public-domain and freely licensed films, each with an official torrent). Other providers can be added by
subclassing :class:`SearchProvider` and listing them in :data:`PROVIDERS`.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass

from . import releases

USER_AGENT = "Chrysa (offline media center)"
TIMEOUT = 15

last_summary = ""  # one line about how the most recent search went, shown under the results


@dataclass(frozen=True)
class SearchResult:
    title: str
    metadata: str  # one display line, e.g. "Peers/Seeders: 150"
    target_url: str  # magnet link or .torrent URL handed to start_download()


class SearchError(Exception):
    pass


class SearchProvider:
    name = "provider"

    def search(self, query: str, limit: int = 50) -> list[SearchResult]:
        raise NotImplementedError


class InternetArchiveProvider(SearchProvider):
    name = "Internet Archive"
    ENDPOINT = "https://archive.org/advancedsearch.php"

    def search(self, query: str, limit: int = 50) -> list[SearchResult]:
        # Quote the query so user text cannot inject Lucene operators.
        quoted = '"' + query.replace("\\", " ").replace('"', " ").strip() + '"'
        params = [
            ("q", f"title:({quoted}) AND mediatype:movies"),
            ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "downloads"), ("fl[]", "item_size"),
            ("sort[]", "downloads desc"), ("rows", str(limit)), ("output", "json"),
        ]
        url = f"{self.ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.load(response)
        except (OSError, ValueError) as error:
            raise SearchError(f"Search failed: {error}") from error
        return self.parse(payload)

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


class TorrentSourcesProvider(SearchProvider):
    """YTS, The Pirate Bay, 1337x, LimeTorrents, EZTV, Nyaa and SubsPlease, via :mod:`chrysa.bd`.

    The query is first resolved to real titles ("breaking" -> "Breaking Bad"), every source is asked for each
    spelling in parallel, and only results matching that title survive, most seeded first.
    """

    name = "Torrent sources"
    DEADLINE = 14.0  # seconds to wait for all sources
    LAZY = {"1337x", "LimeTorrents"}  # sources that cost extra requests per row, so they pre-filter

    def search(self, query: str, limit: int = 60) -> list[SearchResult]:
        from . import bd

        parsed = releases.parse_query(query)
        if not parsed.text:
            raise SearchError("Type a movie or show name.")
        targets = releases.target_titles(parsed, releases.lookup_titles(parsed.text))
        terms = self._terms(parsed, targets)

        def accepts(name: str) -> bool:
            release = releases.classify(name)
            return release is not None and releases.relevance(release, parsed, targets) > 0

        jobs = [(source, term) for term in terms for source, _ in bd.SOURCES]
        fns = dict(bd.SOURCES)
        series = next((t for t in targets if t.is_series and t.imdb_id), None)

        def run(job):
            source, term = job
            try:
                if source in self.LAZY:
                    return source, fns[source](term, keep=accepts), None
                return source, fns[source](term), None
            except Exception as error:  # one dead site never hides the others
                return source, [], error

        found: list[dict] = []
        status: dict[str, list] = {}  # source -> [rows, answered]
        pool = ThreadPoolExecutor(min(16, len(jobs) + 1))
        try:
            futures = [pool.submit(run, job) for job in jobs]
            if series:
                futures.append(pool.submit(lambda: self._eztv(bd, series.imdb_id)))
            deadline = time.monotonic() + self.DEADLINE
            for future in futures:
                try:  # a slow site is dropped rather than making everyone wait
                    source, rows, error = future.result(timeout=max(0.1, deadline - time.monotonic()))
                except FuturesTimeout:
                    continue
                entry = status.setdefault(source, [0, False])
                entry[1] = entry[1] or error is None
                found += rows
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        accepted: dict[str, tuple] = {}
        dropped = 0
        for item in found:
            release = releases.classify(item["name"])
            score = releases.relevance(release, parsed, targets) if release else 0
            if release is None or not score:
                dropped += 1
                continue
            match = re.search(r"btih:([a-z0-9]+)", item["magnet"], re.I)
            key = match.group(1).lower() if match else item["magnet"]
            rank = releases.rank_key(release, score, item["seeders"], item["name"])
            if key not in accepted or accepted[key][0] < rank:
                accepted[key] = (rank, item, release)
        ranked = sorted(accepted.values(), key=lambda entry: entry[0], reverse=True)

        counts: dict[str, int] = {}
        results: list[SearchResult] = []
        for rank, item, release in ranked:
            counts[item["source"]] = counts.get(item["source"], 0) + 1
            results.append(SearchResult(
                item["name"],
                f"{releases.describe(release)} · [{item['source']}] {bd.human(item['size'])} · "
                f"Seeders: {item['seeders']} · Leechers: {item['leechers']}",
                item["magnet"],
            ))
        global last_summary
        parts = [f"{name} {counts.get(name, 0)}" if answered else f"{name} offline" for name, (_, answered) in status.items()]
        shown = " · ".join(parts)
        if targets:
            shown = f"{', '.join(t.name for t in targets[:2])} — " + shown
        last_summary = f"{shown} · {dropped} unrelated hidden" if dropped else shown
        return results[:limit]

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

    @staticmethod
    def _eztv(bd, imdb_id: str):
        try:
            return "EZTV", bd.eztv_by_imdb(imdb_id), None
        except Exception as error:
            return "EZTV", [], error


PROVIDERS: list[SearchProvider] = [TorrentSourcesProvider(), InternetArchiveProvider()]


def search(query: str, limit: int = 60) -> list[SearchResult]:
    """Run *query* against every provider; one provider failing does not hide the others."""
    def run(provider: SearchProvider) -> list[SearchResult] | str:
        try:
            return provider.search(query, limit)
        except SearchError as error:
            return str(error)

    results: list[SearchResult] = []
    errors: list[str] = []
    with ThreadPoolExecutor(len(PROVIDERS)) as pool:  # providers run side by side, results keep provider order
        for outcome in pool.map(run, PROVIDERS):
            if isinstance(outcome, str):
                errors.append(outcome)
            else:
                results.extend(outcome)
    if errors and not results:
        raise SearchError("; ".join(errors))
    return results
