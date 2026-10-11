"""The media library: syncing scans into SQLite, browsing queries, playback state.

Everything here is UI-agnostic and safe to call from any thread (each thread
uses its own SQLite connection, see :mod:`theia.database`).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable

from . import naming
from .config import FEATURE_LENGTH_SECONDS, SHORT_CLIP_SECONDS, Paths, default_media_dir
from .database import Database
from .models import KIND_EPISODE, KIND_MOVIE, KIND_OTHER, Folder, Media, NextUp, ScanSummary, Show
from .probe import MediaInfo, probe_many
from .scanner import ScannedVideo, Scanner, ScanResult
from .watchlog import WatchLog, describe, identity

WATCH_LOG_SETTING = "watchlog.dir"

THUMB_PENDING, THUMB_READY, THUMB_FAILED = 0, 1, 2

_MEDIA_SELECT = """
SELECT m.id, m.path, m.kind, m.title, m.year, m.show_id, s.title AS show_title,
       m.season, m.episode, m.episode_end, m.duration_seconds, m.cover_path,
       m.thumb_path, m.thumb_state, m.size, m.mtime,
       p.position_seconds, p.duration_seconds AS played_duration, p.last_watched,
       p.audio_track, p.subtitle_track, p.subtitle_file
FROM media m
LEFT JOIN shows s ON s.id = m.show_id
LEFT JOIN playback p ON p.media_id = m.id
"""
# Regular seasons in order, then specials (season 0), then files without a season.
_EPISODE_ORDER = (
    "ORDER BY (m.season IS NULL), (m.season = 0), m.season, "
    "(m.episode IS NULL), m.episode, m.sort_title, m.path"
)

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]


class Library:
    def __init__(self, db: Database, paths: Paths) -> None:
        self.db = db
        self.paths = paths
        self._scan_lock = threading.Lock()
        self._name_cache_loaded = False
        self._log: WatchLog | None = None
        self._log_lock = threading.RLock()
        self._log_media: dict[int, Media] = {}  # media id -> row, for naming log entries cheaply
        self.watch_log_error: str | None = None  # why the last write to the log failed, if it did

    @classmethod
    def open(cls, paths: Paths) -> Library:
        paths.ensure()
        return cls(Database(paths.database), paths)

    # ------------------------------------------------------------ settings

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.connection().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: object) -> None:
        self.db.connection().execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def flag(self, key: str, default: bool) -> bool:
        value = self.setting(key)
        return default if value is None else value == "1"

    def set_flag(self, key: str, value: bool) -> None:
        self.set_setting(key, "1" if value else "0")

    # ------------------------------------------------------------- folders

    def folders(self) -> list[Folder]:
        rows = self.db.connection().execute(
            "SELECT f.id, f.path, f.last_scanned, "
            "(SELECT COUNT(*) FROM media m WHERE m.folder_id = f.id) AS media_count "
            "FROM folders f ORDER BY f.path"
        )
        return [Folder(r["id"], r["path"], r["media_count"], r["last_scanned"]) for r in rows]

    def ensure_default_folder(self) -> None:
        """On the very first launch, start with ``~/Videos`` as the library."""
        if self.setting("initialized") is not None:
            return
        default = default_media_dir()
        if default.is_dir():
            self.add_folder(default)
        self.set_setting("initialized", "1")

    def add_folder(self, path: str | os.PathLike) -> tuple[Folder | None, str]:
        """Add a library root.

        Returns ``(folder, message)``; ``folder`` is None when nothing was added
        (not a directory, or already part of the library).
        """
        path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if not os.path.isdir(path):
            return None, f"{path} is not a folder."
        existing = self.folders()
        for folder in existing:
            if folder.path == path:
                return None, "That folder is already in your library."
            if _is_inside(path, folder.path):
                return None, f"That folder is already covered by {folder.path}."
        with self.db.transaction() as con:
            cursor = con.execute("INSERT INTO folders (path, added_at) VALUES (?, ?)", (path, time.time()))
            folder_id = cursor.lastrowid
            # A new parent folder takes over the roots (and the watch history) inside it.
            for folder in existing:
                if _is_inside(folder.path, path):
                    con.execute("UPDATE media SET folder_id = ? WHERE folder_id = ?", (folder_id, folder.id))
                    con.execute("DELETE FROM folders WHERE id = ?", (folder.id,))
        return Folder(folder_id, path), "Folder added."

    def remove_folder(self, folder_id: int) -> None:
        """Forget a library root together with its media and watch history."""
        with self.db.transaction() as con:
            con.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            _prune_shows(con)

    # ------------------------------------------------------------ scanning

    def rescan(
        self,
        progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
        retry_failed: bool = False,
    ) -> ScanSummary:
        """Walk every library folder and bring the database in line with the disk.

        ``retry_failed`` re-probes files whose duration could not be read and
        re-attempts failed thumbnails (used by the manual *Rescan Library* button).
        """
        progress = progress or (lambda _message: None)
        should_stop = should_stop or (lambda: False)
        started = time.time()
        with self._scan_lock:
            self._load_name_cache()
            folders = self.folders()
            progress("Scanning folders…")
            scanner = Scanner(
                progress=lambda path: progress(f"Scanning {os.path.basename(path) or path}…"),
                should_stop=should_stop,
            )
            result = scanner.scan([folder.path for folder in folders])
            self._save_name_cache()
            if should_stop():
                return ScanSummary(cancelled=True)

            plan = self._plan(result, folders, retry_failed)
            infos: dict[str, MediaInfo] = {}
            if plan.to_probe:
                total = len(plan.to_probe)

                def on_probed(path: str, info: MediaInfo) -> None:
                    infos[path] = info
                    if len(infos) % 5 == 0 or len(infos) == total:
                        progress(f"Reading video details… {len(infos)}/{total}")

                progress(f"Reading video details… 0/{total}")
                probe_many(plan.to_probe, on_probed, should_stop)
                if should_stop():
                    return ScanSummary(cancelled=True)

            summary = self._apply(result, plan, infos, retry_failed)
            summary.probed = len(infos)
            summary.ignored = result.ignored
            summary.missing_roots = result.missing_roots
            summary.seconds = time.time() - started
            self.prune_thumbnails()
            self._log_media.clear()
            try:
                self.sync_watch_log()  # pick up progress made on another system
            except Exception:  # never let a damaged log break a scan
                pass
            return summary

    def _plan(self, result: ScanResult, folders: list[Folder], retry_failed: bool) -> _SyncPlan:
        """Diff the scan against the database: what is new, gone, moved, or needs probing."""
        folder_ids = {folder.path: folder.id for folder in folders}
        online = [folder_ids[root] for root in result.scanned_roots if root in folder_ids]
        con = self.db.connection()
        marks = ",".join("?" * len(online))
        existing = (
            {row["path"]: row for row in con.execute(f"SELECT * FROM media WHERE folder_id IN ({marks})", online)}
            if online
            else {}
        )
        scanned = {video.path: video for video in result.videos}
        gone = set(existing) - set(scanned)
        new = set(scanned) - set(existing)
        moved = _match_moved(existing, gone, scanned, new)

        to_probe = []
        for path, video in scanned.items():
            row = existing.get(path) or moved.get(path)
            unknown = row is None or not row["probed"] or _changed(row, video)
            retry = retry_failed and row is not None and row["duration_seconds"] <= 0
            if unknown or retry:
                to_probe.append(path)
        return _SyncPlan(folder_ids, existing, scanned, gone, moved, to_probe)

    def _apply(
        self, result: ScanResult, plan: _SyncPlan, infos: dict[str, MediaInfo], retry_failed: bool
    ) -> ScanSummary:
        now = time.time()
        summary = ScanSummary()
        with self.db.transaction() as con:
            show_ids = _sync_shows(con, result.videos, now)
            for path, video in plan.scanned.items():
                row = plan.existing.get(path)
                was_moved = False
                if row is None and path in plan.moved:
                    row, was_moved = plan.moved[path], True
                info = infos.get(path)
                duration = info.duration if info is not None else (row["duration_seconds"] if row else 0.0)
                kind, title = final_kind(video, duration)
                values = {
                    "folder_id": plan.folder_ids[video.root],
                    "path": path,
                    "kind": kind,
                    "title": title,
                    "sort_title": naming.sort_title(title),
                    "year": video.year if kind != KIND_OTHER else None,
                    "show_id": show_ids.get(video.show_key) if kind == KIND_EPISODE else None,
                    "season": video.season,
                    "episode": video.episode,
                    "episode_end": video.episode_end,
                    "size": video.size,
                    "mtime": video.mtime,
                    "cover_path": video.cover,
                }
                if info is not None:
                    values.update(duration_seconds=info.duration, width=info.width, height=info.height, probed=1)
                if row is None:
                    values["added_at"] = now
                    columns = ", ".join(values)
                    marks = ", ".join("?" * len(values))
                    con.execute(f"INSERT INTO media ({columns}) VALUES ({marks})", list(values.values()))
                    summary.added += 1
                    continue
                if _changed(row, video):
                    # Same path, different file: the old frame grab no longer applies.
                    values.update(thumb_path=None, thumb_state=THUMB_PENDING)
                elif retry_failed and row["thumb_state"] == THUMB_FAILED:
                    values["thumb_state"] = THUMB_PENDING
                changes = {k: v for k, v in values.items() if row[k] != v}
                if changes:
                    assignments = ", ".join(f"{column} = ?" for column in changes)
                    con.execute(f"UPDATE media SET {assignments} WHERE id = ?", [*changes.values(), row["id"]])
                if was_moved:
                    summary.moved += 1
                elif changes:
                    summary.updated += 1

            kept = {row["id"] for row in plan.moved.values()}
            for path in plan.gone:
                row = plan.existing[path]
                if row["id"] not in kept:
                    con.execute("DELETE FROM media WHERE id = ?", (row["id"],))
                    summary.removed += 1
            _prune_shows(con)
            for root in result.scanned_roots:
                con.execute("UPDATE folders SET last_scanned = ? WHERE path = ?", (now, root))

            counts = {r["kind"]: r["n"] for r in con.execute("SELECT kind, COUNT(*) AS n FROM media GROUP BY kind")}
            summary.movies = counts.get(KIND_MOVIE, 0)
            summary.episodes = counts.get(KIND_EPISODE, 0)
            summary.others = counts.get(KIND_OTHER, 0)
            summary.shows = con.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
        return summary

    def _load_name_cache(self) -> None:
        if self._name_cache_loaded:
            return
        con = self.db.connection()
        if self.setting("parser_version") != str(naming.PARSER_VERSION):
            con.execute("DELETE FROM name_cache")
            self.set_setting("parser_version", naming.PARSER_VERSION)
        naming.load_cache((r["name"], r["hint"], r["data"]) for r in con.execute("SELECT name, hint, data FROM name_cache"))
        self._name_cache_loaded = True

    def _save_name_cache(self) -> None:
        entries = list(naming.drain_new_entries())
        if entries:
            with self.db.transaction() as con:
                con.executemany("INSERT OR REPLACE INTO name_cache (name, hint, data) VALUES (?, ?, ?)", entries)

    # ------------------------------------------------------------ browsing

    def _media(self, where: str = "", params: Iterable = (), order: str = "") -> list[Media]:
        rows = self.db.connection().execute(f"{_MEDIA_SELECT} {where} {order}", tuple(params))
        return [_media_from_row(row) for row in rows]

    def media(self, media_id: int) -> Media | None:
        found = self._media("WHERE m.id = ?", (media_id,))
        return found[0] if found else None

    def movies(self) -> list[Media]:
        return self._media("WHERE m.kind = 'movie'", order="ORDER BY m.sort_title, m.year")

    def subtitle_candidates(self) -> list[Media]:
        """Every movie and TV episode, in a stable order, for the subtitle downloader."""
        return self._media(
            "WHERE m.kind IN ('movie', 'episode')", order="ORDER BY s.title, m.season, m.episode, m.sort_title"
        )

    def others(self) -> list[Media]:
        return self._media("WHERE m.kind = 'other'", order="ORDER BY m.sort_title")

    def shows(self) -> list[Show]:
        return self._shows()

    def show(self, show_id: int) -> Show | None:
        found = self._shows("WHERE s.id = ?", (show_id,))
        return found[0] if found else None

    def _shows(self, where: str = "", params: Iterable = ()) -> list[Show]:
        first = "FROM media m WHERE m.show_id = s.id"
        rows = self.db.connection().execute(
            f"""
            SELECT s.id, s.title, s.year, s.path, s.cover_path, s.backdrop_path,
                   (SELECT COUNT(*) {first}) AS episode_count,
                   (SELECT COUNT(DISTINCT m.season) {first} AND m.season > 0) AS season_count,
                   (SELECT m.id {first} {_EPISODE_ORDER} LIMIT 1) AS first_media_id,
                   (SELECT COALESCE(m.cover_path, m.thumb_path) {first}
                           AND (m.cover_path IS NOT NULL OR m.thumb_state = {THUMB_READY})
                           {_EPISODE_ORDER} LIMIT 1) AS episode_image,
                   (SELECT MAX(p.last_watched) FROM playback p JOIN media m ON m.id = p.media_id
                     WHERE m.show_id = s.id) AS last_watched
            FROM shows s {where} ORDER BY s.sort_title
            """,
            tuple(params),
        )
        return [
            Show(
                id=r["id"], title=r["title"], year=r["year"], path=r["path"], cover_path=r["cover_path"],
                backdrop_path=r["backdrop_path"], season_count=r["season_count"],
                episode_count=r["episode_count"], episode_image=r["episode_image"],
                first_media_id=r["first_media_id"], last_watched=r["last_watched"],
            )
            for r in rows
        ]

    def episodes(self, show_id: int, season: int | None = None) -> list[Media]:
        """Episodes of a show in watching order, optionally limited to one season."""
        if season is None:
            return self._media("WHERE m.show_id = ?", (show_id,), _EPISODE_ORDER)
        return self._media("WHERE m.show_id = ? AND IFNULL(m.season, -1) = ?", (show_id, season), _EPISODE_ORDER)

    def seasons(self, show_id: int) -> list[int]:
        """Season numbers of a show: 1, 2, … then 0 (specials) and -1 (unnumbered) last."""
        rows = self.db.connection().execute(
            "SELECT DISTINCT IFNULL(season, -1) AS season FROM media WHERE show_id = ?", (show_id,)
        )
        return sorted((r["season"] for r in rows), key=lambda s: (s <= 0, -s if s <= 0 else s))

    def continue_watching(self, limit: int = 40) -> list[Media]:
        """Where you left off, most recently watched first.

        A show appears once, as the episode watched most recently, however far
        through it you got and whatever older episodes are still unfinished.
        Movies appear while partially watched.
        """
        listed, shows = [], set()
        for media in self._media("WHERE p.position_seconds > 0", order="ORDER BY p.last_watched DESC"):
            if media.show_id is not None:
                if media.show_id in shows:
                    continue
                shows.add(media.show_id)
            elif not media.in_progress:
                continue
            listed.append(media)
        return listed[:limit]

    def search(self, text: str, episode_limit: int = 60) -> dict[str, list]:
        """Case-insensitive match of every word in *text* against titles."""
        words = [w for w in text.casefold().split() if w]
        if not words:
            return {"shows": [], "movies": [], "others": [], "episodes": []}

        def hit(*fields: str | None) -> bool:
            haystack = " ".join(f for f in fields if f).casefold()
            return all(word in haystack for word in words)

        episodes = [
            e for e in self._media("WHERE m.kind = 'episode'", order=_EPISODE_ORDER)
            if hit(e.title, e.show_title, e.episode_code)
            and not hit(e.show_title)  # a show-name match is already covered by the show card
        ]
        return {
            "shows": [s for s in self.shows() if hit(s.title, str(s.year or ""))],
            "movies": [m for m in self.movies() if hit(m.title, str(m.year or ""))],
            "others": [m for m in self.others() if hit(m.title)],
            "episodes": episodes[:episode_limit],
        }

    def counts(self) -> dict[str, int]:
        con = self.db.connection()
        counts = {r["kind"]: r["n"] for r in con.execute("SELECT kind, COUNT(*) AS n FROM media GROUP BY kind")}
        counts["show"] = con.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
        return counts

    # ------------------------------------------------------------ playback

    def save_progress(self, media_id: int, position: float, duration: float) -> None:
        """Remember where playback of *media_id* stands (called every few seconds)."""
        now = time.time()
        try:
            self.db.connection().execute(
                """
                INSERT INTO playback (media_id, position_seconds, duration_seconds, last_watched)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    position_seconds = excluded.position_seconds,
                    duration_seconds = CASE WHEN excluded.duration_seconds > 0
                                            THEN excluded.duration_seconds
                                            ELSE playback.duration_seconds END,
                    last_watched = excluded.last_watched
                """,
                (media_id, max(0.0, position), max(0.0, duration), now),
            )
        except sqlite3.IntegrityError:
            return  # the file was removed from the library while it was playing
        if position > 0:
            self._log_progress(media_id, position, duration, now)

    def save_tracks(
        self, media_id: int, audio_track: int | None, subtitle_track: int | None, subtitle_file: str | None
    ) -> None:
        """Remember the audio/subtitle choice made for this file."""
        try:
            self.db.connection().execute(
                """
                INSERT INTO playback (media_id, last_watched, audio_track, subtitle_track, subtitle_file)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    audio_track = excluded.audio_track,
                    subtitle_track = excluded.subtitle_track,
                    subtitle_file = excluded.subtitle_file
                """,
                (media_id, time.time(), audio_track, subtitle_track, subtitle_file),
            )
        except sqlite3.IntegrityError:
            pass

    def clear_progress(self, media_id: int) -> None:
        """Mark as unwatched: forget the saved position entirely."""
        self.db.connection().execute("DELETE FROM playback WHERE media_id = ?", (media_id,))
        self._log_progress(media_id, 0.0, 0.0, time.time())  # a zero entry tells other systems it was reset

    def mark_watched(self, media_id: int) -> None:
        media = self.media(media_id)
        if media is None:
            return
        duration = media.total_duration or 1.0
        self.save_progress(media_id, duration, duration)

    # ----------------------------------------------------------- watch log

    def watch_log_dir(self) -> str | None:
        """The folder holding the watch log, or None until the user has chosen one."""
        return self.setting(WATCH_LOG_SETTING)

    def _watch_log(self) -> WatchLog | None:
        with self._log_lock:
            directory = self.watch_log_dir()
            if not directory:
                return None
            if self._log is None or str(self._log.directory) != directory:
                self._log = WatchLog(directory)
            return self._log

    def _log_progress(self, media_id: int, position: float, duration: float, now: float) -> None:
        log = self._watch_log()
        if log is None:
            return
        media = self._log_media.get(media_id)
        if media is None:
            media = self.media(media_id)
            if media is None:
                return
            self._log_media[media_id] = media
        log.put(identity(media), describe(media, position, duration, now))
        self._write_log(log)

    def _write_log(self, log: WatchLog) -> None:
        try:
            log.save()
            self.watch_log_error = None
        except OSError as error:
            self.watch_log_error = str(error)

    def set_watch_log_dir(self, directory: str | os.PathLike) -> tuple[bool, str]:
        """Move the log to *directory*, merging what is there with what we know.

        The old file is left where it was. Returns ``(ok, message)``.
        """
        with self._log_lock:
            fresh = WatchLog(directory)
            if self._watch_log() is not None:
                fresh.merge(self._watch_log().entries)
            try:
                fresh.save()
            except OSError as error:
                return False, f"Theia cannot write to that folder: {error.strerror or error}"
            self.set_setting(WATCH_LOG_SETTING, str(fresh.directory))
            self._log = fresh
        self.sync_watch_log()
        return True, str(fresh.path)

    def sync_watch_log(self) -> int:
        """Two-way merge between the log file and the library; returns how many items changed here.

        Newer progress in the log replaces older progress in the database, and
        newer local progress is written into the log. Matching is by title and
        season/episode, so it works whatever the file paths are on each system.
        """
        log = self._watch_log()
        if log is None:
            return 0
        with self._log_lock:
            log.load()
            con = self.db.connection()
            applied, pushed = 0, False
            for media in self._media():
                key = identity(media)
                entry = log.get(key)
                local = media.last_watched or 0.0
                remote = float(entry["updated"]) if entry else 0.0
                if entry is not None and remote > local + 0.01:
                    position = float(entry.get("position") or 0.0)
                    if position <= 0:
                        if media.last_watched is not None:
                            con.execute("DELETE FROM playback WHERE media_id = ?", (media.id,))
                            applied += 1
                        continue
                    con.execute(
                        """
                        INSERT INTO playback (media_id, position_seconds, duration_seconds, last_watched)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(media_id) DO UPDATE SET
                            position_seconds = excluded.position_seconds,
                            duration_seconds = CASE WHEN excluded.duration_seconds > 0
                                                    THEN excluded.duration_seconds
                                                    ELSE playback.duration_seconds END,
                            last_watched = excluded.last_watched
                        """,
                        (media.id, position, float(entry.get("duration") or 0.0), remote),
                    )
                    applied += 1
                elif media.position > 0 and local > remote + 0.01:
                    log.put(key, describe(media, media.position, media.total_duration, local))
                    pushed = True
            if pushed:
                self._write_log(log)
        return applied

    def next_episode(self, media: Media) -> Media | None:
        """The episode that follows *media* in its show, if any."""
        if not media.is_episode or media.show_id is None:
            return None
        episodes = self.episodes(media.show_id)
        for index, episode in enumerate(episodes):
            if episode.id == media.id:
                return episodes[index + 1] if index + 1 < len(episodes) else None
        return None

    def next_up(self, show_id: int) -> NextUp | None:
        """Decide what a show's main play button does."""
        return next_up(self.episodes(show_id))

    # ---------------------------------------------------------- thumbnails

    def thumbnail_jobs(self) -> list[Media]:
        """Media still waiting for a frame grab, the ones shown on the home screen first."""
        self._forget_missing_thumbnails()
        pending = self._media(
            f"WHERE m.thumb_state = {THUMB_PENDING} AND m.cover_path IS NULL",
            order=_EPISODE_ORDER,
        )
        first_of_show: set[int] = set()
        front, back = [], []
        for media in pending:
            if not media.is_episode:
                front.append(media)
            elif media.show_id not in first_of_show:
                first_of_show.add(media.show_id)
                front.append(media)
            else:
                back.append(media)
        return front + back

    def _forget_missing_thumbnails(self) -> None:
        """Requeue thumbnails whose cached file has been deleted from disk."""
        con = self.db.connection()
        rows = con.execute(f"SELECT id, thumb_path FROM media WHERE thumb_state = {THUMB_READY}").fetchall()
        lost = [(r["id"],) for r in rows if not (r["thumb_path"] and os.path.isfile(r["thumb_path"]))]
        if lost:
            with self.db.transaction() as tx:
                tx.executemany(
                    f"UPDATE media SET thumb_state = {THUMB_PENDING}, thumb_path = NULL WHERE id = ?", lost
                )

    def set_thumbnail(self, media_id: int, path: str | None) -> None:
        state = THUMB_READY if path else THUMB_FAILED
        self.db.connection().execute(
            "UPDATE media SET thumb_path = ?, thumb_state = ? WHERE id = ?", (path, state, media_id)
        )

    def set_backdrop(self, show_id: int, path: str | None) -> None:
        self.db.connection().execute("UPDATE shows SET backdrop_path = ? WHERE id = ?", (path, show_id))

    def prune_thumbnails(self) -> int:
        """Delete cached images that no library item refers to any more."""
        con = self.db.connection()
        used = {r[0] for r in con.execute("SELECT thumb_path FROM media WHERE thumb_path IS NOT NULL")}
        used |= {r[0] for r in con.execute("SELECT backdrop_path FROM shows WHERE backdrop_path IS NOT NULL")}
        removed = 0
        try:
            entries = list(os.scandir(self.paths.thumbnails))
        except OSError:
            return 0
        cutoff = time.time() - 3600  # leave files a worker may be writing right now
        for entry in entries:
            try:
                if entry.is_file() and entry.path not in used and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
        return removed


# ---------------------------------------------------------------------------
# pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------


class _SyncPlan:
    __slots__ = ("folder_ids", "existing", "scanned", "gone", "moved", "to_probe")

    def __init__(self, folder_ids, existing, scanned, gone, moved, to_probe) -> None:
        self.folder_ids: dict[str, int] = folder_ids
        self.existing: dict[str, sqlite3.Row] = existing
        self.scanned: dict[str, ScannedVideo] = scanned
        self.gone: set[str] = gone
        self.moved: dict[str, sqlite3.Row] = moved
        self.to_probe: list[str] = to_probe


def final_kind(video: ScannedVideo, duration: float) -> tuple[str, str]:
    """Settle a tentative classification once the running time is known.

    A file that merely *looked* like a movie (``Birthday 2024.mp4``) is not one if
    it runs a few minutes; an untagged file (``Inception.mkv``) is one if it is
    feature length.
    """
    if video.tentative and duration > 0:
        if video.kind == KIND_MOVIE and duration < SHORT_CLIP_SECONDS:
            return KIND_OTHER, video.alt_title or video.title
        if video.kind == KIND_OTHER and duration >= FEATURE_LENGTH_SECONDS:
            return KIND_MOVIE, video.alt_title or video.title
    return video.kind, video.title


def next_up(episodes: list[Media]) -> NextUp | None:
    """Pick the episode a show should play, given its episodes in watching order.

    * Nothing watched yet → the first episode.
    * The most recently watched episode is unfinished → resume it.
    * It is finished (> 92 %) → the next unfinished episode after it, wrapping
      around to earlier unfinished ones; if every episode is finished, start over.
    """
    if not episodes:
        return None
    watched = [e for e in episodes if e.last_watched]
    if not watched:
        return NextUp(episodes[0], "start")
    last = max(watched, key=lambda e: e.last_watched)
    if not last.is_finished:
        return NextUp(last, "resume" if last.in_progress else "start")
    index = next(i for i, e in enumerate(episodes) if e.id == last.id)
    for episode in episodes[index + 1:] + episodes[:index]:
        if not episode.is_finished:
            return NextUp(episode, "resume" if episode.in_progress else "next")
    return NextUp(episodes[0], "restart")


def _media_from_row(row: sqlite3.Row) -> Media:
    return Media(
        id=row["id"], path=row["path"], kind=row["kind"], title=row["title"], year=row["year"],
        show_id=row["show_id"], show_title=row["show_title"], season=row["season"],
        episode=row["episode"], episode_end=row["episode_end"], duration=row["duration_seconds"] or 0.0,
        cover_path=row["cover_path"], thumb_path=row["thumb_path"], thumb_state=row["thumb_state"],
        size=row["size"], mtime=row["mtime"], position=row["position_seconds"] or 0.0,
        played_duration=row["played_duration"] or 0.0, last_watched=row["last_watched"],
        audio_track=row["audio_track"], subtitle_track=row["subtitle_track"],
        subtitle_file=row["subtitle_file"],
    )


def _changed(row: sqlite3.Row, video: ScannedVideo) -> bool:
    """Whether the file at this path is no longer the one we indexed."""
    return row["size"] != video.size or abs(row["mtime"] - video.mtime) > 1e-3


def _is_inside(path: str, parent: str) -> bool:
    return path != parent and os.path.commonpath([path, parent]) == parent


def _match_moved(
    existing: dict[str, sqlite3.Row], gone: set[str], scanned: dict[str, ScannedVideo], new: set[str]
) -> dict[str, sqlite3.Row]:
    """Pair vanished rows with new files that are evidently the same file.

    A renamed or relocated video keeps its exact size and either its file name
    or its modification time, so its watch progress can follow it.
    """
    if not gone or not new:
        return {}
    by_name: dict[tuple, list[sqlite3.Row]] = {}
    by_time: dict[tuple, list[sqlite3.Row]] = {}
    for path in gone:
        row = existing[path]
        by_name.setdefault((row["size"], os.path.basename(path).casefold()), []).append(row)
        by_time.setdefault((row["size"], int(row["mtime"])), []).append(row)
    moved: dict[str, sqlite3.Row] = {}
    taken: set[int] = set()
    for path in sorted(new):
        video = scanned[path]
        keys = (
            (by_name, (video.size, os.path.basename(path).casefold())),
            (by_time, (video.size, int(video.mtime))),
        )
        for index, key in keys:
            candidates = [row for row in index.get(key, ()) if row["id"] not in taken]
            if len(candidates) == 1:
                moved[path] = candidates[0]
                taken.add(candidates[0]["id"])
                break
    return moved


def _sync_shows(con: sqlite3.Connection, videos: list[ScannedVideo], now: float) -> dict[str, int]:
    """Insert or update one row per show seen in the scan; return ``key → id``."""
    wanted: dict[str, ScannedVideo] = {}
    for video in videos:
        if video.kind == KIND_EPISODE and video.show_key:
            wanted.setdefault(video.show_key, video)
    known = {row["key"]: row for row in con.execute("SELECT * FROM shows")}
    ids: dict[str, int] = {}
    for key, video in wanted.items():
        values = {
            "title": video.show_title,
            "sort_title": naming.sort_title(video.show_title or ""),
            "year": video.show_year,
            "path": video.show_dir,
            "cover_path": video.show_cover,
        }
        row = known.get(key)
        if row is None:
            cursor = con.execute(
                "INSERT INTO shows (key, title, sort_title, year, path, cover_path, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, *values.values(), now),
            )
            ids[key] = cursor.lastrowid
            continue
        changes = {k: v for k, v in values.items() if row[k] != v}
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            con.execute(f"UPDATE shows SET {assignments} WHERE id = ?", [*changes.values(), row["id"]])
        ids[key] = row["id"]
    return ids


def _prune_shows(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM shows WHERE id NOT IN (SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL)")
