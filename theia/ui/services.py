"""Background work wrapped in Qt signals: library scans and thumbnail generation."""

from __future__ import annotations

from collections.abc import Iterable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from ..library import Library
from ..models import Media, Show
from ..thumbnails import PRIORITY_BACKGROUND, PRIORITY_HOME, PRIORITY_VISIBLE, ThumbnailWorkers


class ScanWorker(QThread):
    """Runs one library rescan off the UI thread."""

    progress = pyqtSignal(str)
    finishedScan = pyqtSignal(object)  # ScanSummary
    failed = pyqtSignal(str)

    def __init__(self, library: Library, retry_failed: bool, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._retry_failed = retry_failed

    def run(self) -> None:
        try:
            summary = self._library.rescan(
                progress=self.progress.emit,
                should_stop=self.isInterruptionRequested,
                retry_failed=self._retry_failed,
            )
            self.finishedScan.emit(summary)
        except Exception as error:  # surfaced in the status line instead of killing the thread
            self.failed.emit(f"{type(error).__name__}: {error}")
        finally:
            self._library.db.close()


class ThumbnailService(QObject):
    """Feeds the ffmpeg workers and announces finished images on the UI thread."""

    thumbnailReady = pyqtSignal(int, str)  # media id, image path ('' when it failed)
    backdropReady = pyqtSignal(int, str)  # show id, image path

    def __init__(self, library: Library, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._workers = ThumbnailWorkers(library.paths.thumbnails, self._store, self._announce, workers=2)

    # Both callbacks run on worker threads; emitting a signal hops to the UI thread.
    def _store(self, kind: str, target_id: int, path: str | None) -> None:
        if kind == "backdrop":
            self._library.set_backdrop(target_id, path)
        else:
            self._library.set_thumbnail(target_id, path)

    def _announce(self, kind: str, target_id: int, path: str | None) -> None:
        signal = self.backdropReady if kind == "backdrop" else self.thumbnailReady
        signal.emit(target_id, path or "")

    def queue_missing(self) -> int:
        """Queue every item that still lacks a picture; home-screen items go first."""
        jobs = self._library.thumbnail_jobs()
        seen_shows: set[int] = set()
        for media in jobs:
            front = not media.is_episode or media.show_id not in seen_shows
            if media.is_episode:
                seen_shows.add(media.show_id)
            self._enqueue(media, PRIORITY_HOME if front else PRIORITY_BACKGROUND)
        return len(jobs)

    def prioritise(self, items: Iterable[Media]) -> None:
        """Move these (now visible) items to the front of the queue."""
        for media in items:
            if media.thumb_state == 0 and not media.cover_path:
                self._enqueue(media, PRIORITY_VISIBLE)

    def request_backdrop(self, show: Show) -> None:
        """Grab a wide frame for a show's banner from its first episode."""
        if show.first_media_id is None:
            return
        episode = self._library.media(show.first_media_id)
        if episode is not None:
            self._workers.enqueue(
                "backdrop", show.id, episode.path, episode.duration, episode.size, episode.mtime, PRIORITY_VISIBLE
            )

    def _enqueue(self, media: Media, priority: int) -> None:
        self._workers.enqueue("thumb", media.id, media.path, media.duration, media.size, media.mtime, priority)

    def pause(self) -> None:
        self._workers.pause()

    def resume(self) -> None:
        self._workers.resume()

    def stop(self) -> None:
        self._workers.stop()
