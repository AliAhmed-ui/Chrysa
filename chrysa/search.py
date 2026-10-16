"""Online search for downloadable videos. Pluggable: a provider turns a query into results.

The bundled provider searches the Internet Archive (public-domain and freely
licensed films, each with an official torrent). Other providers can be added by
subclassing :class:`SearchProvider` and listing them in :data:`PROVIDERS`.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
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


PROVIDERS: list[SearchProvider] = [InternetArchiveProvider()]


def search(query: str, limit: int = 50) -> list[SearchResult]:
    """Run *query* against every provider; one provider failing does not hide the others."""
    results: list[SearchResult] = []
    errors: list[str] = []
    for provider in PROVIDERS:
        try:
            results.extend(provider.search(query, limit))
        except SearchError as error:
            errors.append(str(error))
    if errors and not results:
        raise SearchError("; ".join(errors))
    return results
