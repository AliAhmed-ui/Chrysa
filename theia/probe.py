"""Read duration and frame size with ``ffprobe`` (no decoding, a few ms per file)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float = 0.0  # seconds; 0 when unknown
    width: int = 0
    height: int = 0

    @property
    def ok(self) -> bool:
        return self.duration > 0


@cache
def find_tool(name: str) -> str | None:
    """Absolute path of ``ffmpeg`` / ``ffprobe``, or None if it is not installed."""
    return shutil.which(name)


def probe(path: str, timeout: float = 30.0) -> MediaInfo:
    """Probe one file. Never raises: unreadable files yield an empty :class:`MediaInfo`."""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return MediaInfo()
    command = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=codec_type,width,height,duration:stream_disposition=attached_pic",
        path,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
        )
        data = json.loads(done.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return MediaInfo()

    duration = _number(data.get("format", {}).get("duration"))
    width = height = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        if stream.get("disposition", {}).get("attached_pic"):
            continue  # embedded cover art, not the picture
        if not width:
            width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        duration = duration or _number(stream.get("duration"))
    return MediaInfo(duration=duration, width=width, height=height)


def probe_many(
    paths: Iterable[str],
    on_result: Callable[[str, MediaInfo], None],
    should_stop: Callable[[], bool] | None = None,
    workers: int | None = None,
) -> None:
    """Probe many files in parallel, reporting each result on the calling thread."""
    paths = list(paths)
    if not paths:
        return
    workers = workers or max(2, min(8, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="theia-probe") as pool:
        futures = {pool.submit(probe, path): path for path in paths}
        for future in as_completed(futures):
            if should_stop and should_stop():
                for pending in futures:
                    pending.cancel()
                break
            on_result(futures[future], future.result())


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def has_embedded_subtitles(path: str, timeout: float = 30.0) -> bool:
    """True when the container carries at least one subtitle stream. Never raises."""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return False
    command = [ffprobe, "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "csv=p=0", path]
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(done.stdout.strip())
