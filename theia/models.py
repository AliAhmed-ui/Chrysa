"""Plain data objects handed from the library to the UI, plus display formatting."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import FINISHED_FRACTION, RESUME_MIN_FRACTION

KIND_MOVIE = "movie"
KIND_EPISODE = "episode"
KIND_OTHER = "other"


@dataclass(slots=True)
class Media:
    """A playable file together with its saved playback state."""

    id: int
    path: str
    kind: str
    title: str
    year: int | None = None
    show_id: int | None = None
    show_title: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    duration: float = 0.0  # seconds, measured by ffprobe
    cover_path: str | None = None
    thumb_path: str | None = None
    thumb_state: int = 0
    size: int = 0
    mtime: float = 0.0
    position: float = 0.0  # seconds, where playback stopped
    played_duration: float = 0.0  # seconds, as reported by the player
    last_watched: float | None = None
    audio_track: int | None = None
    subtitle_track: int | None = None
    subtitle_file: str | None = None

    # ---- playback state -------------------------------------------------

    @property
    def total_duration(self) -> float:
        """Length of the file: the player's figure when known, else ffprobe's."""
        return self.played_duration if self.played_duration > 0 else self.duration

    @property
    def fraction(self) -> float:
        """How far through the file playback stopped, 0.0 – 1.0."""
        total = self.total_duration
        if total <= 0 or self.position <= 0:
            return 0.0
        return min(1.0, self.position / total)

    @property
    def is_finished(self) -> bool:
        return self.fraction > FINISHED_FRACTION

    @property
    def in_progress(self) -> bool:
        """Partially watched: belongs in *Continue Watching* and offers *Resume*."""
        return RESUME_MIN_FRACTION <= self.fraction <= FINISHED_FRACTION

    @property
    def resume_position(self) -> float:
        """Where playback should start: the saved spot if partially watched, else 0."""
        return self.position if self.in_progress else 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_duration - self.position)

    # ---- display --------------------------------------------------------

    @property
    def image(self) -> str | None:
        """Artwork from the folder if there is any, otherwise the grabbed frame."""
        return self.cover_path or self.thumb_path

    @property
    def is_episode(self) -> bool:
        return self.kind == KIND_EPISODE

    @property
    def episode_code(self) -> str:
        """``S1:E3``, ``S9:E23-24``, ``Special`` or '' for non-episodes."""
        if not self.is_episode:
            return ""
        if self.episode is None:
            return "Special" if not self.season else f"S{self.season}"
        number = f"E{self.episode}"
        if self.episode_end and self.episode_end != self.episode:
            number += f"-{self.episode_end}"
        return f"S{self.season}:{number}" if self.season else number

    @property
    def heading(self) -> str:
        """Primary line for a card: the show for an episode, else the title."""
        return self.show_title or self.title if self.is_episode else self.title

    @property
    def full_title(self) -> str:
        """Everything in one line, for the player's title bar."""
        if self.is_episode:
            parts = [self.show_title or "", self.episode_code, self.title]
            return "  ·  ".join(p for p in parts if p)
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)


@dataclass(slots=True)
class Show:
    """A TV show: one card on the home screen, however many episodes it has."""

    id: int
    title: str
    year: int | None = None
    path: str | None = None
    cover_path: str | None = None
    backdrop_path: str | None = None
    season_count: int = 0
    episode_count: int = 0
    episode_image: str | None = None  # frame from the first episode
    first_media_id: int | None = None
    last_watched: float | None = None

    @property
    def image(self) -> str | None:
        return self.cover_path or self.episode_image

    @property
    def summary(self) -> str:
        parts = []
        if self.season_count:
            parts.append(plural(self.season_count, "Season"))
        parts.append(plural(self.episode_count, "Episode"))
        return " · ".join(parts)


@dataclass(slots=True)
class Folder:
    """A library root."""

    id: int
    path: str
    media_count: int = 0
    last_scanned: float | None = None

    @property
    def available(self) -> bool:
        return os.path.isdir(self.path)


@dataclass(slots=True)
class NextUp:
    """What the big play button on a show does."""

    episode: Media
    action: str  # "start" | "resume" | "next" | "restart"

    @property
    def label(self) -> str:
        code = self.episode.episode_code
        verb = {"start": "Play", "resume": "Resume", "next": "Play", "restart": "Watch Again"}[self.action]
        return f"{verb} {code}".strip() if self.action != "restart" else verb

    @property
    def start_position(self) -> float:
        return self.episode.resume_position if self.action == "resume" else 0.0


@dataclass(slots=True)
class ScanSummary:
    """Outcome of a library rescan."""

    movies: int = 0
    shows: int = 0
    episodes: int = 0
    others: int = 0
    added: int = 0
    removed: int = 0
    moved: int = 0
    updated: int = 0  # rows whose title, kind, artwork… changed
    probed: int = 0
    ignored: list[tuple[str, str]] | None = None
    missing_roots: list[str] | None = None
    cancelled: bool = False
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return self.movies + self.episodes + self.others

    @property
    def changed(self) -> bool:
        """Whether the scan altered anything the UI shows."""
        return bool(self.added or self.removed or self.moved or self.updated)

    def describe(self) -> str:
        if self.cancelled:
            return "Scan cancelled"
        parts = [plural(self.movies, "movie"), plural(self.shows, "show"), plural(self.episodes, "episode")]
        if self.others:
            parts.append(plural(self.others, "other video"))
        return " · ".join(parts)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def format_clock(seconds: float) -> str:
    """Timestamp for the player: ``7:05`` or ``1:23:45``."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_duration(seconds: float) -> str:
    """Running time for cards and lists: ``2h 50m``, ``22m`` or ``14s``."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s" if seconds else ""
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"
