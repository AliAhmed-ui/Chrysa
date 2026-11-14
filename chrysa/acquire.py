"""From a chosen result to verified media in the Library.

    search → ranking → duplicate check → download into <folder>/.chrysa-incoming → performance monitoring
    → validation (validate.py) → move into <folder> → Library rescan → history

:class:`Pipeline` is what :class:`chrysa.downloads.DownloadClient` calls at each step. The incoming folder is hidden,
so the Library never sees a file before it has been validated, and a rejected download is deleted from it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from . import releases, validate
from .downloads import Download, remove_partial_files
from .history import History

log = logging.getLogger("chrysa.acquire")


class Pipeline:
    def __init__(self, history: History | None = None, on_admitted: Callable[[list[str]], None] | None = None,
                 malware_scan: bool = True) -> None:
        self.history = history or History(None)
        self.on_admitted = on_admitted or (lambda paths: None)
        self.malware_scan = malware_scan

    # -------------------------------------------------------------- history

    def started(self, download: Download) -> None:
        meta = {**download.meta, "info_hash": download.info_hash, "title": download.meta.get("title") or download.name}
        download.meta["history_id"] = self.history.start_download(meta)
        log.info("[DOWNLOAD] starting %s (%s)", download.name or download.target[:60], download.info_hash or "no hash")

    def ended(self, download: Download, status: str, detail: str = "") -> None:
        monitor = download.monitor
        stats = {"detail": detail[:300], "info_hash": download.info_hash}
        if monitor is not None:
            stats.update(avg_speed=monitor.average(), peak_speed=monitor.peak, ttfd=monitor.ttfd,
                         avg_peers=monitor.average_peers)
        self.history.finish_download(download.meta.pop("history_id", None), status, **stats)
        if monitor is not None and monitor.first_data is not None:
            log.info("[DOWNLOAD] %s %s, average %s, peak %s", download.name, status, _rate(monitor.average()),
                     _rate(monitor.peak))

    # ----------------------------------------------------------- validation

    def finalize(self, download: Download, emit: Callable[..., None]) -> None:
        """Validate a finished download, then admit it to the library folder or delete it."""
        root = download.directory
        emit(download, state="validating", message="Checking contents…", checks=[], speed=0, eta="")
        report = validate.validate(
            root, download.files, malware_scan=self.malware_scan, should_stop=download._stop.is_set,
            step=lambda r: emit(download, checks=list(r.checks),
                                message="Scanning for malware…" if r.phase == "scanning" else "Checking contents…"),
        )
        if download._stop.is_set():
            return  # cancelled while checking; cancel_and_delete cleans up and reports
        if report.verdict == validate.REJECTED:
            log.warning("[VALIDATE] %s rejected: %s", download.name, report.reason)
            remove_partial_files(root, download.files, download.target)
            self.ended(download, "rejected", detail=report.reason)
            emit(download, state="rejected", checks=report.checks,
                 message=f"Validation failed: {report.reason}. The file was NOT added to the Library and was deleted.")
            return
        log.info("[VALIDATE] %s %s (%s)", download.name, report.verdict, report.media or "no stream info")
        log.info("[SCAN] %s", report.scan)
        final = download.final_dir or root
        admitted = admit(root, final, download.files) if final != root else [str(root / n) for n in download.files]
        remove_partial_files(root, [], download.target)  # aria2's saved <hash>.torrent
        self.ended(download, "verified")
        log.info("[LIBRARY] media admitted: %s", ", ".join(admitted))
        label = "Verified media" if report.verdict == validate.VERIFIED else "Container checked (ffprobe unavailable)"
        emit(download, state="verified", checks=report.checks, admitted=admitted,
             message=f"{label} — added to {final}")
        self.on_admitted(admitted)


def admit(incoming: Path, final: Path, names: list[str]) -> list[str]:
    """Move each top-level *name* from *incoming* to *final*, never overwriting; returns the new paths."""
    final.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in names:
        source = incoming / name
        if not source.exists() or source.resolve().parent != incoming.resolve():
            continue
        target = final / name
        n = 2
        while target.exists():
            stem, suffix = (name, "") if source.is_dir() else (Path(name).stem, Path(name).suffix)
            target = final / f"{stem} ({n}){suffix}"
            n += 1
        try:
            os.replace(source, target)
        except OSError:  # another file system
            shutil.move(str(source), str(target))
        moved.append(str(target))
    return moved


# --------------------------------------------------------------------------- duplicates


def library_match(db, release: releases.Release | None) -> str:
    """Title of a Library item that is the same movie / episode as *release*, or ``""``."""
    if db is None or release is None or release.kind not in (releases.MOVIE, releases.EPISODE):
        return ""
    con = db.connection()
    if release.kind == releases.EPISODE:
        rows = con.execute(
            "SELECT s.title AS title, m.season, m.episode FROM media m JOIN shows s ON s.id = m.show_id "
            "WHERE m.kind = 'episode' AND m.season = ? AND (m.episode = ? OR (m.episode <= ? AND m.episode_end >= ?))",
            (release.season, release.episode, release.episode, release.episode),
        ).fetchall()
        for row in rows:
            if releases.normalise(row["title"]) == release.title:
                return f"{row['title']} S{row['season']:02d}E{row['episode']:02d}"
        return ""
    rows = con.execute("SELECT title, year FROM media WHERE kind = 'movie'").fetchall()
    for row in rows:
        if releases.normalise(row["title"]) == release.title and (
                not release.year or not row["year"] or abs(row["year"] - release.year) <= 1):
            return f"{row['title']} ({row['year']})" if row["year"] else row["title"]
    return ""


def duplicate_reason(client, history: History, db, target: str, release: releases.Release | None,
                     info_hash: str) -> tuple[str, bool]:
    """Why starting *target* would duplicate something: ``(message, hard)``.

    Hard reasons (the same torrent is already running) block the download; soft ones (already verified before, or
    the same movie/episode is already in the Library) only ask for confirmation.
    """
    running = client.find_active(target)
    if running is not None:
        return f"Already downloading this torrent (“{running.name}”).", True
    for download in client.downloads:
        if download.active and release is not None and download.meta.get("release_key") == release_key(release):
            return f"An equivalent release is already downloading (“{download.name}”).", True
    if history.was_verified(info_hash):
        return "This exact torrent was downloaded and verified before.", False
    match = library_match(db, release)
    if match:
        return f"Already in your Library: {match}.", False
    return "", False


def release_key(release: releases.Release | None) -> str:
    """Same movie / same episode / same season, whatever the quality."""
    if release is None:
        return ""
    parts = [release.kind, release.title, release.year if release.kind == releases.MOVIE else "",
             release.season, release.last_season, release.episode]
    return re.sub(r"\s+", " ", "|".join("" if p is None else str(p) for p in parts))


def _rate(bps: float) -> str:
    return f"{bps / (1 << 20):.1f} MB/s" if bps >= 1 << 20 else f"{bps / 1024:.0f} KB/s"
