"""Decide whether a plain folder of videos is a TV show, and put its files in order.

Names from yt-dlp and friends (``Show Ep 01 (Sub) 23 Sep 26 | Channel [id].webm``) carry
no season and no ``S01E02``, so the release-name parser cannot place them. This module
looks at the *folder as a whole*: do the files share one title template, do they carry
distinct episode numbers or dates, are they about the same length, and does the folder
look like a movie release or a camera dump?

The scorer is a fixed-weight logistic model over those features. It needs no training
data, no model file and no extra dependency, and it is a pure function of the names and
sizes, so it can never hallucinate a title.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

TV_SHOW = "tv_show"
MOVIE = "movie"
OTHER = "other"

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
# "Ep 01", "Episode 5", "EP.12", "E07", "Part 3": an explicit counter in the name.
_EP_RE = re.compile(r"(?i)(?<![a-z0-9])(?:episode|ep|e|part|pt)[\s._#-]*(\d{1,4})(?![\d])")
_ISO_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])(?!\d)")
_TEXT_DATE_RE = re.compile(
    r"(?i)(?<!\d)(\d{1,2})[\s._-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s._,-]+((?:19|20)?\d{2})(?!\d)"
)
_BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|｜.*$|\|.*$")
_PREFIX_CUT_RE = re.compile(r"[\d\[(｜|]")
_RECORDING_RE = re.compile(
    r"(?i)^(?:vid|pxl|img|mov|mvi|dsc|gopr|gx\d|screencast|screen[\s_-]?(?:record|cast|shot)|zoom|obs|"
    r"recording|whatsapp|signal|telegram|trim|capture|clip)(?![a-z])"
)
_TECH_RE = re.compile(r"(?i)\b(?:2160p|1080p|720p|480p|bluray|brrip|web-?dl|webrip|hdrip|x26[45]|hevc|yify|yts)\b")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

# Weights of the logistic model, set by hand against the layouts in tests/test_classify.py.
_BIAS = -2.6
_W = {
    "explicit": 3.0,  # share of files with a distinct episode counter
    "dated": 1.6,  # share of files with a distinct date
    "template": 2.2,  # share of files that start with the same title text
    "uniform": 0.8,  # files are of similar size
    "many": 0.5,  # three or more files
    "recording": -3.5,  # camera / screen-recorder names
    "release": -3.0,  # the folder itself looks like a movie release
}
THRESHOLD = 0.6  # probability needed to call a folder a TV show


@dataclass(frozen=True, slots=True)
class FolderVerdict:
    label: str
    probability: float
    features: dict[str, float]


def episode_number(name: str) -> int | None:
    """The explicit episode counter in *name* (``Ep 01`` → 1), ignoring dates and ids."""
    text = _BRACKETS_RE.sub(" ", name)
    match = _EP_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def air_date(name: str) -> date | None:
    """A calendar date written in *name*, ISO (``2026-09-08``) or ``23 Sep 26``."""
    try:
        match = _ISO_DATE_RE.search(name)
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        match = _TEXT_DATE_RE.search(name)
        if match:
            year = int(match.group(3))
            year += 2000 if year < 100 else 0
            return date(year, _MONTHS[match.group(2).lower()], int(match.group(1)))
    except ValueError:
        return None
    return None


def _template(stem: str) -> str:
    """Leading title text of a file name, before the first number, bracket or bar."""
    cut = _PREFIX_CUT_RE.search(stem)
    head = stem[: cut.start()] if cut else stem
    return re.sub(r"[\s._-]+", " ", head).strip().casefold()


def _distinct_share(values: Sequence[object]) -> float:
    present = [v for v in values if v is not None]
    if len(present) < 2 or len(set(present)) != len(present):
        return 0.0
    return len(present) / len(values)


def classify_folder(folder: str, stems: Sequence[str], sizes: Sequence[int]) -> FolderVerdict:
    """Classify a folder from its name and the stems and sizes of the videos directly inside."""
    count = len(stems)
    if count < 2:
        return FolderVerdict(OTHER, 0.0, {})
    templates = Counter(t for t in map(_template, stems) if len(t) >= 3)
    top = templates.most_common(1)[0][1] if templates else 0
    mean = statistics.fmean(sizes)
    spread = statistics.pstdev(sizes) / mean if mean else 1.0

    features = {
        "explicit": _distinct_share([episode_number(s) for s in stems]),
        "dated": _distinct_share([air_date(s) for s in stems]),
        "template": top / count,
        "uniform": 1.0 if spread < 0.6 else 0.0,
        "many": 1.0 if count >= 3 else 0.0,
        "recording": sum(bool(_RECORDING_RE.match(s)) for s in stems) / count,
        "release": 1.0 if _TECH_RE.search(folder) or (_YEAR_RE.search(folder) and count < 4) else 0.0,
    }
    logit = _BIAS + sum(_W[k] * v for k, v in features.items())
    probability = 1 / (1 + math.exp(-logit))
    return FolderVerdict(TV_SHOW if probability >= THRESHOLD else OTHER, probability, features)


def order_episodes(stems: Sequence[str], mtimes: Sequence[float]) -> list[int]:
    """Episode number for each file: its own counter, else date order, else modification time.

    Counters are used as written when they are distinct; otherwise files are ranked
    1..n by date, then by modification time, then by their position in *stems*.
    """
    numbers = [episode_number(s) for s in stems]
    if all(n is not None for n in numbers) and len(set(numbers)) == len(numbers):
        return [int(n) for n in numbers if n is not None]
    dates = [air_date(s) for s in stems]
    key = [(d or date.max, m, i) for i, (d, m) in enumerate(zip(dates, mtimes))]
    rank = {idx: pos for pos, idx in enumerate(sorted(range(len(stems)), key=key.__getitem__), 1)}
    return [rank[i] for i in range(len(stems))]
