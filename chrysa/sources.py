"""Browse sources as interchangeable adapters, each with a contract, a health state and diagnostics.

A :class:`Source` wraps one fetch function (most live in :mod:`chrysa.bd`) and turns whatever happens into an
:class:`Outcome`: the rows that passed the result contract, or *why* there are none — offline, changed, blocked,
timed out, invalid response, parser failure, or genuinely no results. Those are different situations and are
recorded as such, so "1337x's page layout changed" never shows up as an innocent "No results".

After every search :func:`update_health` folds the outcomes into each source's health
(healthy / degraded / changed / offline / disabled), stored in SQLite through :class:`chrysa.history.History`.

Adding a source = writing a fetch function that returns ``bd.result()`` dicts and calling :func:`register`.
Hosts, timeouts and on/off can be overridden without touching code through ``sources.json`` in the data folder
(see :func:`load_overrides`); it is plain data, versioned, and never contains code.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import bd
from .history import History

log = logging.getLogger("chrysa.sources")

HEALTHY, DEGRADED, CHANGED, OFFLINE, DISABLED, UNKNOWN = "healthy", "degraded", "changed", "offline", "disabled", "unknown"
STATE_LABEL = {HEALTHY: "Healthy", DEGRADED: "Degraded", CHANGED: "Changed", OFFLINE: "Offline",
               DISABLED: "Disabled", UNKNOWN: "Not checked yet"}
OK = "ok"

# Capabilities
TEXT = "text-search"  # answers free-text queries
BY_IMDB = "imdb-lookup"  # lists a series by IMDb id instead
SWARM = "swarm-figures"  # publishes seeders/leechers
DETAIL_PAGES = "detail-pages"  # needs a request per row, so it pre-filters with keep()

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_B32 = re.compile(r"^[A-Z2-7]{32}$")


def info_hash(target: str) -> str:
    """Lower-case hex BitTorrent v1 info hash of a magnet link (base32 ones converted), or ``""``."""
    match = re.search(r"xt=urn:btih:([0-9A-Za-z]+)", target or "")
    if not match:
        return ""
    value = match.group(1)
    if _HEX40.match(value.lower()):
        return value.lower()
    if _B32.match(value.upper()):
        return base64.b32decode(value.upper()).hex()
    return ""


def contract_problem(row: dict) -> str:
    """Why *row* breaks the result contract (empty string when it is fine)."""
    title = row.get("name")
    if not isinstance(title, str) or not title.strip():
        return "missing title"
    target = row.get("magnet") or ""
    if target.startswith("magnet:?"):
        if not info_hash(target):
            return "malformed magnet / info hash"
    elif not (target.startswith(("http://", "https://")) and ".torrent" in target):
        return "no magnet or .torrent link"
    if not isinstance(row.get("size"), int) or row["size"] < 0:
        return "size not parseable"
    for key in ("seeders", "leechers"):
        value = row.get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            return f"{key} not numeric"
    return ""


@dataclass
class Outcome:
    source: str
    status: str  # OK or one of bd's failure kinds (bd.OFFLINE, bd.CHANGED, ...)
    rows: list[dict] = field(default_factory=list)
    detail: str = ""
    elapsed: float = 0.0
    total: int = 0  # rows the parser produced
    rejected: int = 0  # rows that broke the contract
    filtered: int = 0  # rows the page listed but the search dropped as unrelated before fetching details

    @property
    def ok(self) -> bool:
        return self.status in (OK, bd.NO_RESULTS)

    @property
    def parse_rate(self) -> float:
        return (self.total - self.rejected) / self.total if self.total else 1.0


@dataclass
class Source:
    name: str
    fetch: Callable[..., list[dict]]
    capabilities: frozenset = frozenset({TEXT, SWARM})
    canary: str = "big buck bunny"  # a query that should always find something, for health_check()
    enabled: bool = True

    @property
    def schema_version(self) -> int:
        return bd.SCHEMA.get(self.name, {}).get("version", 0)

    def search(self, query: str, keep: Callable[[str], bool] | None = None) -> Outcome:
        """Run one query; never raises."""
        started = time.monotonic()
        bd._local.parsed = None
        try:
            if DETAIL_PAGES in self.capabilities and keep is not None:
                raw = self.fetch(query, keep=keep)
            else:
                raw = self.fetch(query)
        except Exception as error:
            status, detail = bd.classify_error(error)
            if status == bd.PARSE_FAILURE:
                log.warning("[SOURCE] %s parser crashed", self.name, exc_info=True)
            return Outcome(self.name, status, detail=detail, elapsed=time.monotonic() - started)
        outcome = self.check(raw, time.monotonic() - started)
        parsed = getattr(bd._local, "parsed", None)
        if parsed is not None:
            outcome.filtered = max(0, parsed - outcome.total)
        return outcome

    def check(self, raw: list[dict], elapsed: float = 0.0) -> Outcome:
        """Apply the result contract to parsed rows."""
        if not isinstance(raw, list):
            return Outcome(self.name, bd.CHANGED, detail="parser returned no list", elapsed=elapsed)
        good, problems = [], {}
        for row in raw:
            problem = contract_problem(row) if isinstance(row, dict) else "not a result"
            if problem:
                problems[problem] = problems.get(problem, 0) + 1
            else:
                if SWARM not in self.capabilities:
                    row = {**row, "seeders": None, "leechers": None}
                good.append(row)
        outcome = Outcome(self.name, OK if good else bd.NO_RESULTS, good, elapsed=elapsed, total=len(raw),
                          rejected=len(raw) - len(good))
        if problems:
            worst = max(problems, key=problems.get)
            outcome.detail = f"{outcome.rejected}/{outcome.total} rows broke the contract ({worst})"
            if outcome.total >= 3 and outcome.parse_rate < 0.5:
                outcome.status = bd.CHANGED  # SOURCE PARSER BROKEN: most of what it reads is garbage
                outcome.detail = "parser broken: " + outcome.detail
        return outcome

    def health_check(self) -> Outcome:
        """Search the canary query; a healthy source finds something for it."""
        outcome = self.search(self.canary)
        if outcome.status == bd.NO_RESULTS:
            outcome.detail = f"nothing for the canary query {self.canary!r}"
        return outcome


REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    REGISTRY[source.name] = source
    return source


register(Source("YTS", bd.yts, canary="big buck bunny"))
register(Source("TPB", bd.tpb))
register(Source("1337x", bd.x1337, frozenset({TEXT, SWARM, DETAIL_PAGES})))
register(Source("LimeTorrents", bd.limetorrents, frozenset({TEXT, SWARM, DETAIL_PAGES})))
register(Source("Nyaa", bd.nyaa, canary="one piece"))
register(Source("SubsPlease", bd.subsplease, frozenset({TEXT}), canary="one piece"))
register(Source("EZTV", bd.eztv_by_imdb, frozenset({BY_IMDB, SWARM}), canary="tt0903747"))


# --------------------------------------------------------------------------- health


_lock = threading.Lock()
store = History(None)  # replaced by configure()


def configure(history: History) -> None:
    global store
    store = history
    for name, row in history.source_rows().items():
        if name in REGISTRY and row.get("disabled"):
            REGISTRY[name].enabled = False


def set_enabled(name: str, enabled: bool) -> None:
    REGISTRY[name].enabled = enabled
    store.set_disabled(name, not enabled)
    log.info("[SOURCE] %s %s by the user", name, "enabled" if enabled else "disabled")


def next_state(previous: dict, outcome: Outcome, suspicious_empty: bool = False) -> tuple[str, str]:
    """Health after *outcome*, given the stored row before it. Returns ``(state, note)``."""
    failures = (previous.get("consecutive_failures") or 0) + (0 if outcome.ok else 1)
    if outcome.ok:
        if suspicious_empty:
            return DEGRADED, "returned nothing while other sources found plenty; its page may have changed"
        if outcome.parse_rate < 0.9:
            return DEGRADED, outcome.detail
        return HEALTHY, ""
    status = outcome.status
    if status in (bd.CHANGED, bd.PARSE_FAILURE):
        return CHANGED, outcome.detail
    if status == bd.INVALID_RESPONSE:
        return (CHANGED if failures >= 2 else DEGRADED), outcome.detail
    if status == bd.OFFLINE:
        return OFFLINE, outcome.detail
    return (OFFLINE if failures >= 2 else DEGRADED), outcome.detail  # timeouts and blocks may be passing


def update_health(outcomes: dict[str, Outcome], suspicious: set[str] = frozenset()) -> dict[str, dict]:
    """Record one search's outcomes; returns the new rows by source name."""
    now = time.time()
    with _lock:
        stored = store.source_rows()
        updated = {}
        for name, outcome in outcomes.items():
            prev = stored.get(name) or {}
            state, note = next_state(prev, outcome, name in suspicious)
            count = len(outcome.rows)
            avg = prev.get("avg_result_count") or 0.0
            row = {
                "source": name, "state": state,
                "last_status": bd.NO_RESULTS if name in suspicious else outcome.status,
                "last_error": note or (outcome.detail if not outcome.ok else ""),
                "last_checked": now,
                "last_success": now if outcome.ok else prev.get("last_success"),
                "last_failure": prev.get("last_failure") if outcome.ok else now,
                "consecutive_failures": 0 if outcome.ok else (prev.get("consecutive_failures") or 0) + 1,
                "last_result_count": count,
                "avg_result_count": (0.8 * avg + 0.2 * count) if outcome.ok else avg,
                "parse_rate": round(outcome.parse_rate, 3),
                "response_ms": int(outcome.elapsed * 1000),
                "schema_version": REGISTRY[name].schema_version if name in REGISTRY else 0,
                "disabled": int(not REGISTRY[name].enabled) if name in REGISTRY else 0,
            }
            store.save_source(row)
            updated[name] = row
            log.info("[SOURCE] %s → %s (%s%s, %d ms)", name, f"{count} results" if outcome.ok else outcome.status,
                     state, f": {row['last_error']}" if row["last_error"] else "", row["response_ms"])
        _memory.update(updated)
        return updated


_memory: dict[str, dict] = {}  # latest rows, so the UI has health even without a database


def status_table() -> list[dict]:
    """Every registered source with its state and diagnostics, for the Sources popover and debug logs."""
    stored = {**store.source_rows(), **_memory}
    table = []
    for name, source in REGISTRY.items():
        row = dict(stored.get(name) or {"source": name, "state": UNKNOWN})
        if not source.enabled:
            row["state"] = DISABLED
        row["capabilities"] = sorted(source.capabilities)
        row["schema_version"] = source.schema_version
        table.append(row)
    return table


def is_suspicious_empty(name: str, outcome: Outcome, others_found: int, previous: dict | None) -> bool:
    """A text source answering nothing while the others found plenty, when it normally finds things."""
    if outcome.status != bd.NO_RESULTS or outcome.filtered or others_found < 15:
        return False  # a page that listed rows, all unrelated, is working fine
    return (previous or {}).get("avg_result_count", 0) >= 3


# --------------------------------------------------------------------------- overrides

OVERRIDES_VERSION = 1
_HOST = re.compile(r"^(?=.{4,253}$)[a-z0-9-]+(\.[a-z0-9-]+)+$")


def load_overrides(path: Path) -> list[str]:
    """Apply ``sources.json`` (if present): per-source ``hosts``, ``timeout`` and ``disabled``.

    Format: ``{"version": 1, "sources": {"1337x": {"hosts": ["1337x.example"], "timeout": 10}}}``.
    Only those keys are read and every value is validated; anything else is ignored with a log line.
    Returns human-readable notes of what was applied.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        log.warning("[SOURCE] ignoring %s: %s", path, error)
        return [f"ignored {Path(path).name}: {error}"]
    if not isinstance(data, dict) or data.get("version") != OVERRIDES_VERSION:
        log.warning("[SOURCE] ignoring %s: unsupported version %r", path, data.get("version") if isinstance(data, dict) else None)
        return [f"ignored {Path(path).name}: unsupported version"]
    notes = []
    for name, conf in (data.get("sources") or {}).items():
        if name not in REGISTRY or not isinstance(conf, dict):
            notes.append(f"unknown source {name!r} ignored")
            continue
        hosts = conf.get("hosts")
        if hosts is not None:
            good = [h.strip().lower() for h in hosts if isinstance(h, str) and _HOST.match(h.strip().lower())]
            if good and name in bd.SCHEMA:
                bd.SCHEMA[name]["hosts"] = good
                notes.append(f"{name}: hosts → {', '.join(good)}")
        timeout = conf.get("timeout")
        if isinstance(timeout, (int, float)) and 2 <= timeout <= 60 and name in bd.SCHEMA:
            bd.SCHEMA[name]["timeout"] = timeout
            notes.append(f"{name}: timeout → {timeout}s")
        if isinstance(conf.get("disabled"), bool):
            REGISTRY[name].enabled = not conf["disabled"]
            notes.append(f"{name}: {'disabled' if conf['disabled'] else 'enabled'}")
    for note in notes:
        log.info("[SOURCE] override %s", note)
    return notes
