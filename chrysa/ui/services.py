"""Background work wrapped in Qt signals: library scans and thumbnail generation."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .. import intro
from ..library import Library
from ..models import Media, Show
from ..subtitles import fetch_missing_subtitles
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


class SubtitleWorker(QThread):
    """Downloads missing subtitles off the UI thread, reporting each line as a signal."""

    logLine = pyqtSignal(str, str)  # level, message
    finishedFetch = pyqtSignal(object)  # FetchSummary

    def __init__(self, library: Library, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library

    def run(self) -> None:
        try:
            summary = fetch_missing_subtitles(
                self._library.subtitle_candidates(), self.logLine.emit, self.isInterruptionRequested
            )
            self.finishedFetch.emit(summary)
        except Exception as error:
            self.logLine.emit("WARN", f"Subtitle search failed: {type(error).__name__}: {error}")
            self.finishedFetch.emit(None)
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


class IntroWorker(QThread):
    """Looks for opening titles season by season, off the UI thread, and caches what it finds."""

    introFound = pyqtSignal(int)  # media id

    def __init__(self, library: Library, resume: threading.Event, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._resume = resume  # cleared while a video plays so ffmpeg does not compete with it

    def run(self) -> None:
        try:
            for episodes, done in self._library.intro_jobs():
                if self.isInterruptionRequested():
                    break
                intro.analyse_season(episodes, done, self._store, self._should_stop)
        except Exception:  # a damaged file must never take the app down
            pass
        finally:
            self._library.db.close()

    def _should_stop(self) -> bool:
        while not self._resume.wait(0.25):  # paused: wait here, between files
            if self.isInterruptionRequested():
                return True
        return self.isInterruptionRequested()

    def _store(self, media: Media, found: intro.Intro | None) -> None:
        self._library.save_intro(media, (found.start, found.end) if found else None)
        if found:
            self.introFound.emit(media.id)


class IntroService(QObject):
    """Runs one :class:`IntroWorker` at a time; asking again while it runs queues one more pass."""

    introFound = pyqtSignal(int)

    def __init__(self, library: Library, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._worker: IntroWorker | None = None
        self._again = False
        self._resume = threading.Event()
        self._resume.set()
        self._stopped = False

    def queue_missing(self) -> None:
        if self._stopped or not intro.available():
            return
        if self._worker is not None:
            self._again = True
            return
        worker = self._worker = IntroWorker(self._library, self._resume, self)
        worker.introFound.connect(self.introFound)
        worker.finished.connect(self._done)
        worker.start()

    def _done(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        if self._again:
            self._again = False
            self.queue_missing()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def stop(self) -> None:
        self._stopped = True
        if self._worker is not None:
            self._worker.requestInterruption()
            self._resume.set()
            self._worker.wait(5000)
