"""Online search for downloadable videos. Pluggable: a provider turns a query into results.

Bundled providers: torrent indexes (via :mod:`chrysa.bd`) and the Internet Archive
(public-domain and freely licensed films, each with an official torrent). Other providers can be added by
subclassing :class:`SearchProvider` and listing them in :data:`PROVIDERS`.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

USER_AGENT = "Chrysa (offline media center)"
TIMEOUT = 15


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
    """YTS, The Pirate Bay, 1337x, EZTV, Nyaa, SubsPlease and BitTorrented, via :mod:`chrysa.bd`."""

    name = "Torrent sources"

    def search(self, query: str, limit: int = 50) -> list[SearchResult]:
        from . import bd

        try:
            found = bd.search_all(query)
        except Exception as error:  # bd isolates each source, but never let one bug hide other providers
            raise SearchError(f"Torrent search failed: {error}") from error
        return [
            SearchResult(
                item["name"],
                f"[{item['source']}] {bd.human(item['size'])} · Seeders: {item['seeders']} · Leechers: {item['leechers']}",
                item["magnet"],
            )
            for item in found[:limit]
        ]


PROVIDERS: list[SearchProvider] = [TorrentSourcesProvider(), InternetArchiveProvider()]


def search(query: str, limit: int = 50) -> list[SearchResult]:
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
