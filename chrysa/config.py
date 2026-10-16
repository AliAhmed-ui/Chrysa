"""Paths, thresholds and file-type tables shared by every module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Chrysa"
APP_ID = "chrysa"  # desktop-file id / X11 instance name

# --- playback thresholds -------------------------------------------------
MIN_PLAYED_SECONDS = 5  # less than this is a glance, not watching: not remembered
FINISHED_FRACTION = 0.92  # above this an item counts as "watched"
SAVE_INTERVAL_MS = 1000  # how often the playback position is persisted
SKIP_SECONDS = 10  # Left / Right arrow jump

# --- thumbnails ----------------------------------------------------------
THUMBNAIL_FRACTION = 0.15  # first frame grab is taken this far into the file
THUMBNAIL_FALLBACK_FRACTIONS = (0.30, 0.45, 0.60)  # tried when a grab is too dark/flat
THUMBNAIL_WIDTH = 640
BACKDROP_WIDTH = 1280

# --- classification ------------------------------------------------------
# A file that only *might* be a movie (bare year, no release tags) is demoted
# to "Other Videos" when it is shorter than this, and an untagged file is
# promoted to a movie when it is at least feature length.
SHORT_CLIP_SECONDS = 20 * 60
FEATURE_LENGTH_SECONDS = 60 * 60

VIDEO_EXTENSIONS = frozenset(
    {
        ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg",
        ".m2ts", ".mts", ".ts", ".3gp", ".ogv", ".ogm", ".vob", ".divx", ".asf", ".rm",
        ".rmvb", ".f4v", ".mxf", ".m2v",
    }
)
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def default_media_dir() -> Path:
    """The library folder used on first launch: ``~/Videos``."""
    return Path.home() / "Videos"


@dataclass(frozen=True, slots=True)
class Paths:
    """Where Chrysa keeps its state (``~/.local/share/chrysa`` unless overridden)."""

    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "chrysa.db"

    @property
    def thumbnails(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "chrysa.log"

    def ensure(self) -> Paths:
        self.thumbnails.mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> Paths:
    """Resolve the data directory. ``CHRYSA_DATA_DIR`` overrides it (used by tests)."""
    override = os.environ.get("CHRYSA_DATA_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".local" / "share" / "chrysa"
    return Paths(base)
