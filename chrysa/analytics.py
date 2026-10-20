"""Watch-habit statistics computed from the local playback table (read-only)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import FINISHED_FRACTION, MIN_PLAYED_SECONDS
from .database import Database

DAY = 86400.0
CATEGORY_LABELS = {"movie": "Movies", "episode": "TV Shows", "other": "Other videos"}


@dataclass
class Analytics:
    total_seconds: float = 0.0
    week_seconds: float = 0.0
    month_seconds: float = 0.0
    started: int = 0
    completed: int = 0
    weekly: list[tuple[str, float]] = field(default_factory=list)  # oldest first, seconds per week
    categories: list[tuple[str, float]] = field(default_factory=list)  # seconds, largest first
    top_shows: list[tuple[str, float]] = field(default_factory=list)

    @property
    def completion_rate(self) -> float:
        return self.completed / self.started if self.started else 0.0


def compute(db: Database, weeks: int = 8, now: float | None = None) -> Analytics:
    """Aggregate watch history.

    Chrysa keeps one row per file (the last position), not a log of sessions, so time is
    what each started file has been watched up to, bucketed by when it was last watched.
    """
    now = time.time() if now is None else now
    result = Analytics()
    rows = db.connection().execute(
        """
        SELECT p.position_seconds AS position, p.duration_seconds AS duration, p.last_watched AS last,
               m.kind AS kind, s.title AS show
        FROM playback p
        JOIN media m ON m.id = p.media_id
        LEFT JOIN shows s ON s.id = m.show_id
        WHERE p.position_seconds >= ?
        """,
        (MIN_PLAYED_SECONDS,),
    ).fetchall()

    buckets = [0.0] * weeks
    categories: dict[str, float] = {}
    shows: dict[str, float] = {}
    for row in rows:
        duration = row["duration"] or 0.0
        seconds = min(row["position"], duration) if duration > 0 else row["position"]
        result.started += 1
        if duration > 0 and row["position"] / duration > FINISHED_FRACTION:
            result.completed += 1
        result.total_seconds += seconds
        age = now - row["last"]
        if age <= 7 * DAY:
            result.week_seconds += seconds
        if age <= 30 * DAY:
            result.month_seconds += seconds
        week = int(age // (7 * DAY))
        if 0 <= week < weeks:
            buckets[weeks - 1 - week] += seconds
        label = CATEGORY_LABELS.get(row["kind"], "Other videos")
        categories[label] = categories.get(label, 0.0) + seconds
        if row["show"]:
            shows[row["show"]] = shows.get(row["show"], 0.0) + seconds

    result.weekly = [("this week" if i == weeks - 1 else f"{weeks - 1 - i}w ago", s) for i, s in enumerate(buckets)]
    result.categories = sorted(categories.items(), key=lambda item: -item[1])
    result.top_shows = sorted(shows.items(), key=lambda item: -item[1])[:6]
    return result


def format_hours(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours:.1f} h" if hours >= 10 or hours == 0 else f"{hours:.2f} h"
