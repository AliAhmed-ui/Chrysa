"""A portable watch log: where you stopped in every movie and episode.

The log is one small JSON file in a folder the user picks (for example on a
partition shared by both systems of a dual boot). Chrysa keeps it up to date
while you watch and silently merges it into the library on start-up and after
each scan, so progress follows you between operating systems.

Entries are keyed by *what* was watched (movie title and year, or show plus
season and episode), never by file path, because paths differ between systems.
Each entry carries a timestamp and the newest one wins on a merge. Entries for
videos this machine does not have are kept untouched so the other system still
finds them.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import naming
from .config import FINISHED_FRACTION
from .models import KIND_EPISODE, KIND_MOVIE, Media

FILENAME = "chrysa-watch-log.json"
FORMAT_VERSION = 1


def identity(media: Media) -> str:
    """A key that names the same movie or episode on any machine."""
    if media.kind == KIND_EPISODE and media.show_title:
        season = media.season if media.season is not None else -1
        episode = media.episode if media.episode is not None else -1
        return f"episode:{naming.show_key(media.show_title)}:s{season}:e{episode}"
    if media.kind == KIND_MOVIE:
        return f"movie:{naming.show_key(media.title)}:{media.year or 0}"
    return f"other:{os.path.basename(media.path).lower()}"


def describe(media: Media, position: float, duration: float, updated: float) -> dict:
    """The entry stored for *media* (readable on its own, e.g. "Friends S02E05")."""
    entry: dict = {
        "kind": media.kind,
        "title": media.show_title if media.kind == KIND_EPISODE and media.show_title else media.title,
        "position": round(max(0.0, position), 1),
        "duration": round(max(0.0, duration), 1),
        "updated": round(updated, 3),
    }
    if media.year:
        entry["year"] = media.year
    if media.kind == KIND_EPISODE:
        entry["season"] = media.season
        entry["episode"] = media.episode
    if duration > 0:
        entry["finished"] = position / duration > FINISHED_FRACTION
    return entry


class WatchLog:
    """The entries of one log file; safe to use from several threads."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory).expanduser()
        self.path = self.directory / FILENAME
        self.entries: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._seen: tuple[int, int] | None = None  # (mtime_ns, size) of the file when last read
        self._unreadable = False  # the file exists but could not be parsed the last time it was read
        self.load()

    # ----------------------------------------------------------- reading

    def load(self) -> None:
        """Merge the file's content into memory (newest entry per key wins)."""
        with self._lock:
            try:
                stat = self.path.stat()
                with open(self.path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except FileNotFoundError:
                return  # no log here yet: a new one is created on the first save
            except (OSError, ValueError):
                self._unreadable = True  # unreadable or half-written: keep what we have
                return
            self._unreadable = False
            self._seen = (stat.st_mtime_ns, stat.st_size)
            items = data.get("entries") if isinstance(data, dict) else None
            if isinstance(items, dict):
                self.merge(items)

    def merge(self, items: dict[str, dict]) -> None:
        with self._lock:
            for key, entry in items.items():
                if not isinstance(entry, dict) or not isinstance(entry.get("updated"), (int, float)):
                    continue
                current = self.entries.get(key)
                if current is None or entry["updated"] > current.get("updated", 0):
                    self.entries[key] = entry

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self.entries.get(key)

    # ----------------------------------------------------------- writing

    def put(self, key: str, entry: dict) -> None:
        with self._lock:
            self.entries[key] = entry

    def save(self) -> None:
        """Write the log atomically. Raises ``OSError`` when the folder is not writable."""
        with self._lock:
            if self._changed_on_disk():
                self.load()  # the other system wrote meanwhile: merge before overwriting
            if not self.directory.is_dir():
                # Never create the folder: an SD card that is not inserted must not be faked
                # by an empty directory of the same name on the internal disk.
                raise FileNotFoundError(2, "The log folder is not available (is the card inserted?)", str(self.directory))
            if self._unreadable:
                self._keep_unreadable_copy()
            payload = {"chrysa_watch_log": FORMAT_VERSION, "saved": time.time(), "entries": self.entries}
            handle, temp = tempfile.mkstemp(prefix=".chrysa-log-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    json.dump(payload, out, ensure_ascii=False, indent=1, sort_keys=True)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, self.path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
            stat = self.path.stat()
            self._seen = (stat.st_mtime_ns, stat.st_size)

    def _keep_unreadable_copy(self) -> None:
        """Set aside a log we could not read before the next save replaces it."""
        try:
            shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
        except OSError:
            pass
        self._unreadable = False

    def _changed_on_disk(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return False
        return (stat.st_mtime_ns, stat.st_size) != self._seen
