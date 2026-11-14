"""What Browse remembers between runs: how each source behaved and how past downloads performed.

Two small tables in the library database (see :mod:`chrysa.database`): ``source_health`` (one row per source) and
``download_history`` (one row per download). The ranking reads them through :meth:`History.evidence`, so a torrent,
release group or source that downloaded fast before is preferred next time, whatever its advertised seeders.

``History(None)`` remembers nothing and answers every lookup with "no evidence", which keeps tests and the
stand-alone Browse page free of a database.
"""

from __future__ import annotations

import functools
import logging
import math
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# How much one past download counts towards confidence, by how closely it matches the candidate.
_WEIGHT = {"torrent": 3.0, "group": 1.0, "source": 0.4}


@dataclass(frozen=True)
class Evidence:
    """Past performance relevant to one candidate."""

    speed: float = 0.0  # weighted mean of average download speeds, bytes/s
    samples: float = 0.0  # weighted number of downloads behind it
    success: float = 1.0  # share of those downloads that finished and validated
    verified_before: bool = False  # this exact torrent was downloaded and verified
    scope: str = ""  # the closest kind of match that contributed ("torrent", "group" or "source")

    @property
    def confidence(self) -> float:
        """0 with no history, approaching 0.75 as evidence piles up."""
        return 0.75 * self.samples / (self.samples + 3.0) if self.samples else 0.0


_ENDED = ("verified", "rejected", "stalled", "failed", "completed")


def _soft(default):
    """History is advisory: a database problem degrades it to "no evidence", never breaks Browse."""
    def wrap(method):
        @functools.wraps(method)
        def safe(self, *args, **kwargs):
            if self.db is None:
                return default() if callable(default) else default
            try:
                return method(self, *args, **kwargs)
            except sqlite3.Error as error:
                log.warning("history unavailable in %s: %s", method.__name__, error)
                return default() if callable(default) else default
        return safe
    return wrap


class History:
    def __init__(self, db=None) -> None:
        self.db = db

    # ------------------------------------------------------------ sources

    @_soft(dict)
    def source_rows(self) -> dict[str, dict]:
        rows = self.db.connection().execute("SELECT * FROM source_health").fetchall()
        return {row["source"]: dict(row) for row in rows}

    @_soft(None)
    def save_source(self, row: dict) -> None:
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "source")
        self.db.connection().execute(
            f"INSERT INTO source_health ({columns}) VALUES ({marks}) ON CONFLICT(source) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    @_soft(None)
    def set_disabled(self, source: str, disabled: bool) -> None:
        con = self.db.connection()
        con.execute(
            "INSERT INTO source_health (source, state, disabled) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET disabled = excluded.disabled",
            (source, "disabled" if disabled else "healthy", int(disabled)),
        )

    # ---------------------------------------------------------- downloads

    @_soft(None)
    def start_download(self, meta: dict) -> int | None:
        cursor = self.db.connection().execute(
            "INSERT INTO download_history (info_hash, source, title, size, resolution, release_group, started_at, "
            "status, initial_seeders, initial_leechers) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (meta.get("info_hash"), meta.get("source"), meta.get("title"), meta.get("size"),
             meta.get("resolution"), meta.get("release_group"), time.time(),
             meta.get("seeders"), meta.get("leechers")),
        )
        return cursor.lastrowid

    @_soft(None)
    def finish_download(self, row_id: int | None, status: str, **stats) -> None:
        if row_id is None:
            return
        allowed = ("avg_speed", "peak_speed", "ttfd", "avg_peers", "detail", "info_hash")
        values = {k: v for k, v in stats.items() if k in allowed}
        sets = "".join(f", {k} = ?" for k in values)
        self.db.connection().execute(
            f"UPDATE download_history SET status = ?, completed_at = ?{sets} WHERE id = ?",
            (status, time.time(), *values.values(), row_id),
        )

    @_soft(False)
    def was_verified(self, info_hash: str) -> bool:
        if not info_hash:
            return False
        row = self.db.connection().execute(
            "SELECT 1 FROM download_history WHERE info_hash = ? AND status = 'verified' LIMIT 1", (info_hash,)
        ).fetchone()
        return row is not None

    @_soft(Evidence)
    def evidence(self, info_hash: str = "", source: str = "", group: str = "") -> Evidence:
        """Blend past downloads of this torrent, its release group and its source into one estimate."""
        con = self.db.connection()
        total_w = speed_w = ok_w = 0.0
        verified = False
        scope = ""
        for kind, column, value in (("torrent", "info_hash", info_hash), ("group", "release_group", group),
                                    ("source", "source", source)):
            if not value:
                continue
            rows = con.execute(
                f"SELECT status, avg_speed FROM download_history WHERE {column} = ? AND status IN "
                f"({', '.join('?' for _ in _ENDED)}) ORDER BY id DESC LIMIT 20",
                (value, *_ENDED),
            ).fetchall()
            for row in rows:
                weight = _WEIGHT[kind]
                total_w += weight
                good = row["status"] in ("verified", "completed")
                ok_w += weight if good else 0.0
                # A stalled or failed download did deliver *something*; count what it managed (or nothing).
                speed_w += weight * math.log10(max(row["avg_speed"] or 1.0, 1.0))
                verified = verified or (kind == "torrent" and row["status"] == "verified")
                scope = scope or kind
        if not total_w:
            return Evidence()
        return Evidence(10 ** (speed_w / total_w), total_w, ok_w / total_w, verified, scope)

    @_soft(dict)
    def source_reliability(self) -> dict[str, float]:
        """Share of each source's finished downloads that validated (with a neutral prior of 2 good in 3)."""
        rows = self.db.connection().execute(
            "SELECT source, SUM(status = 'verified') AS good, COUNT(*) AS n FROM download_history "
            "WHERE status IN ('verified', 'rejected', 'failed', 'stalled') GROUP BY source"
        ).fetchall()
        return {row["source"]: (row["good"] + 2) / (row["n"] + 3) for row in rows if row["source"]}
