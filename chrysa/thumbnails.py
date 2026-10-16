"""Offline artwork: grab a frame from each video with ``ffmpeg`` and cache it.

Frames are taken 15 % into the file. A frame that turns out to be nearly black
or featureless (fades, title cards, night scenes) is retried further in, and the
best attempt wins, so cards rarely end up as black rectangles.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import queue
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import BACKDROP_WIDTH, THUMBNAIL_FALLBACK_FRACTIONS, THUMBNAIL_FRACTION, THUMBNAIL_WIDTH
from .probe import find_tool

# Brightness probe: ffmpeg also emits the frame as a tiny greyscale image on stdout.
_PROBE_W, _PROBE_H = 32, 18
_MIN_MEAN = 30.0  # 0-255; darker than this counts as "too dark"
_MIN_SPREAD = 12.0  # standard deviation; flatter than this counts as "featureless"
_GRAB_TIMEOUT = 45.0

PRIORITY_VISIBLE = 0  # something on screen is waiting for it
PRIORITY_HOME = 10  # movies, other videos and the first episode of each show
PRIORITY_BACKGROUND = 20  # every remaining episode


def cache_file(directory: Path, video: str, size: int, mtime: float, suffix: str = "") -> Path:
    """Cache location for a video's frame; changes whenever the file itself changes."""
    identity = f"{video}|{size}|{int(mtime)}".encode("utf-8", "surrogateescape")
    return directory / f"{hashlib.sha1(identity, usedforsecurity=False).hexdigest()[:24]}{suffix}.jpg"


def grab_frame(video: str, out: Path, seconds: float, width: int) -> tuple[float, float] | None:
    """Write the frame at *seconds* to *out*; return its ``(brightness, spread)`` or None."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    graph = (
        "[0:v:0]scale=trunc(iw*sar/2)*2:ih,setsar=1,split=2[full][tiny];"
        f"[full]scale='min({width},iw)':-2:flags=bicubic[thumb];"
        f"[tiny]scale={_PROBE_W}:{_PROBE_H},format=gray[probe]"
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{max(0.0, seconds):.3f}", "-noaccurate_seek", "-i", video,
        "-filter_complex", graph,
        "-map", "[thumb]", "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "4", "-f", "image2", "-update", "1", str(out),
        "-map", "[probe]", "-frames:v", "1", "-f", "rawvideo", "pipe:1",
    ]
    nice = find_tool("nice")
    if nice:
        command = [nice, "-n", "10", *command]
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=_GRAB_TIMEOUT, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pixels = done.stdout
    if done.returncode != 0 or not out.is_file() or out.stat().st_size == 0 or not pixels:
        return None
    mean = sum(pixels) / len(pixels)
    spread = (sum((p - mean) ** 2 for p in pixels) / len(pixels)) ** 0.5
    return mean, spread


def make_thumbnail(video: str, out: Path, duration: float, width: int = THUMBNAIL_WIDTH) -> bool:
    """Create *out* from *video*. Returns False if no frame could be decoded."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if duration > 0:
        offsets = [duration * f for f in (THUMBNAIL_FRACTION, *THUMBNAIL_FALLBACK_FRACTIONS)]
    else:
        offsets = [10.0, 0.0]  # length unknown: a little way in, else the very first frame
    best: tuple[float, Path] | None = None
    attempts: list[Path] = []
    try:
        for index, seconds in enumerate(offsets):
            attempt = out.with_name(f"{out.stem}.{index}-{os.getpid()}-{threading.get_ident()}.tmp.jpg")
            attempts.append(attempt)
            stats = grab_frame(video, attempt, seconds, width)
            if stats is None:
                continue
            mean, spread = stats
            if mean >= _MIN_MEAN and spread >= _MIN_SPREAD:
                os.replace(attempt, out)
                return True
            score = min(mean, 90.0) + spread
            if best is None or score > best[0]:
                best = (score, attempt)
        if best is not None:
            os.replace(best[1], out)
            return True
        return False
    finally:
        for attempt in attempts:
            try:
                attempt.unlink()
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class _Job:
    kind: str  # "thumb" (media) or "backdrop" (show)
    target_id: int
    video: str
    duration: float
    size: int
    mtime: float


class ThumbnailWorkers:
    """A small pool of background threads that turn queued videos into images.

    ``store(kind, target_id, path_or_None)`` is called on the worker thread to
    persist a result; ``notify`` is called afterwards with the same arguments so
    the UI can refresh. Neither may assume it runs on the main thread.
    """

    def __init__(
        self,
        directory: Path,
        store: Callable[[str, int, str | None], None],
        notify: Callable[[str, int, str | None], None] | None = None,
        workers: int = 2,
    ) -> None:
        self._directory = directory
        self._store = store
        self._notify = notify or (lambda *_args: None)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._order = itertools.count()
        self._jobs: dict[tuple[str, int], _Job] = {}
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()
        self._stopped = False
        self._threads = [
            threading.Thread(target=self._run, name=f"chrysa-thumb-{i}", daemon=True) for i in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    # ------------------------------------------------------------- control

    def enqueue(
        self, kind: str, target_id: int, video: str, duration: float, size: int, mtime: float,
        priority: int = PRIORITY_BACKGROUND,
    ) -> None:
        """Queue (or re-prioritise) a frame grab."""
        key = (kind, target_id)
        with self._lock:
            self._jobs[key] = _Job(kind, target_id, video, duration, size, mtime)
        self._queue.put((priority, next(self._order), key))

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)

    def pause(self) -> None:
        """Stop starting new grabs (used while a video is playing)."""
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        self._stopped = True
        self._running.set()
        for _ in self._threads:
            self._queue.put((-1, next(self._order), None))

    # -------------------------------------------------------------- worker

    def _run(self) -> None:
        while True:
            _priority, _order, key = self._queue.get()
            if key is None or self._stopped:
                return
            self._running.wait()
            if self._stopped:
                return
            with self._lock:
                job = self._jobs.pop(key, None)
            if job is None:
                continue  # already handled through a higher-priority entry
            try:
                path = self._produce(job)
            except OSError:
                path = None
            try:
                self._store(job.kind, job.target_id, path)
                self._notify(job.kind, job.target_id, path)
            except Exception:  # a UI that has gone away must not kill the worker
                if self._stopped:
                    return

    def _produce(self, job: _Job) -> str | None:
        backdrop = job.kind == "backdrop"
        out = cache_file(self._directory, job.video, job.size, job.mtime, "-bd" if backdrop else "")
        if out.is_file() and out.stat().st_size > 0:
            return str(out)
        width = BACKDROP_WIDTH if backdrop else THUMBNAIL_WIDTH
        return str(out) if make_thumbnail(job.video, out, job.duration, width) else None
