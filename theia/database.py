"""SQLite storage: schema, migrations and per-thread connections.

The UI thread, the library scanner and the thumbnail workers all talk to the
same database file, each through its own connection (SQLite connections must
not be shared between threads). WAL mode lets the UI keep reading while a scan
writes.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Library roots chosen by the user.
CREATE TABLE IF NOT EXISTS folders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,
    added_at     REAL NOT NULL,
    last_scanned REAL
);

CREATE TABLE IF NOT EXISTS shows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,      -- normalised title, the show's identity
    title         TEXT NOT NULL,
    sort_title    TEXT NOT NULL,
    year          INTEGER,
    path          TEXT,                      -- main show folder, if it has one
    cover_path    TEXT,                      -- artwork found on disk
    backdrop_path TEXT,                      -- generated wide frame for the detail banner
    added_at      REAL NOT NULL
);

-- One row per playable file.
CREATE TABLE IF NOT EXISTS media (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id        INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    path             TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL CHECK (kind IN ('movie', 'episode', 'other')),
    title            TEXT NOT NULL,
    sort_title       TEXT NOT NULL,
    year             INTEGER,
    show_id          INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    season           INTEGER,
    episode          INTEGER,
    episode_end      INTEGER,                -- last episode of a double-length file
    size             INTEGER NOT NULL DEFAULT 0,
    mtime            REAL NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    width            INTEGER NOT NULL DEFAULT 0,
    height           INTEGER NOT NULL DEFAULT 0,
    probed           INTEGER NOT NULL DEFAULT 0,   -- ffprobe has run for this size/mtime
    cover_path       TEXT,                   -- artwork found next to the file
    thumb_path       TEXT,                   -- frame grabbed by ffmpeg
    thumb_state      INTEGER NOT NULL DEFAULT 0,   -- 0 pending, 1 ready, 2 failed
    added_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS media_by_kind ON media(kind, sort_title);
CREATE INDEX IF NOT EXISTS media_by_show ON media(show_id, season, episode);

-- Where the viewer stopped. Rows disappear with their media row.
CREATE TABLE IF NOT EXISTS playback (
    media_id         INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    position_seconds REAL NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    last_watched     REAL NOT NULL,
    audio_track      INTEGER,                -- libVLC track id chosen for this file
    subtitle_track   INTEGER,                -- libVLC track id, -1 = off
    subtitle_file    TEXT                    -- external subtitle chosen for this file
);
CREATE INDEX IF NOT EXISTS playback_recent ON playback(last_watched DESC);

-- Memoised guessit results (parsing a name costs several milliseconds).
CREATE TABLE IF NOT EXISTS name_cache (
    name TEXT NOT NULL,
    hint TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (name, hint)
);
"""


class Database:
    """A SQLite file with one lazily opened connection per thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migrate()

    def connection(self) -> sqlite3.Connection:
        """The calling thread's connection (opened on first use)."""
        con = getattr(self._local, "con", None)
        if con is None:
            # isolation_level=None: autocommit; transactions are explicit (see transaction()).
            con = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute("PRAGMA busy_timeout = 30000")
            self._local.con = con
        return con

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically, taking the write lock up front."""
        con = self.connection()
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")

    def close(self) -> None:
        """Close the calling thread's connection."""
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    def _migrate(self) -> None:
        con = self.connection()
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer Theia (schema {version}); "
                f"this version understands schema {SCHEMA_VERSION}."
            )
        if version < SCHEMA_VERSION:
            with self.transaction() as tx:
                for statement in _statements(_SCHEMA):
                    tx.execute(statement)
                tx.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _statements(script: str) -> list[str]:
    """Split a schema script into statements (``executescript`` would auto-commit)."""
    statements, current = [], []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0].rstrip()
        if not stripped:
            continue
        current.append(stripped)
        if stripped.endswith(";"):
            statements.append("\n".join(current))
            current = []
    return statements
