"""Find a season's opening titles from the audio of its episodes (fully offline).

The question is not "do these two episodes share audio?" but **"which recurring segment of this season is most
likely the opening, and where is it in each episode?"** The pipeline:

1. **Coarse features** (stage 1): every episode is decoded once, whole, to mono 8 kHz and reduced to a compact
   spectrogram (32 log-energy bands per 0.2 s, loudness- and mix-invariant). The features are cached in SQLite, so
   an episode is never decoded twice.
2. **Candidate segments** (stage 2): each pair of episodes is cross-correlated over *every* offset. Only the few
   strongest offsets are examined in detail; along each, runs of matching frames (hysteresis, tolerant of short
   dropouts and sub-frame timing jitter) are recurring segments. Nothing assumes where in the episode they are.
3. **Clustering**: occurrences are linked across episodes into clusters. A cluster is one recurring piece of audio
   with the set of episodes it occurs in. A season can have several (an outro, a recurring song, two intro variants).
4. **Judgement**: each cluster gets a confidence from recurrence (how many of the analysed episodes contain it),
   match quality, plausible duration, how much it behaves like an opening (position, with late intros allowed) and
   how little like credits music or background music. Clusters below :data:`MIN_CONFIDENCE` are discarded: a missing
   Skip Intro button is far better than one that skips story.
5. **Per-episode boundaries** (stage 3): each episode's interval is the median over everything it matched, so
   start, end and duration are independent per episode; an episode without the intro simply has none.

A confident cluster becomes a :class:`Profile` with a small *template* (the intro's features). Episodes added later
are matched against the templates alone, cheaply, and only an episode that matches none triggers a re-analysis of
the whole season (from cached features), which can also revise earlier conclusions.

Nothing here touches Qt or the database; :func:`analyse_season` talks to a store object (the Library).
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

from .probe import find_tool

try:
    import numpy as np
except ImportError:  # the feature switches itself off; the rest of the app is unaffected
    np = None

log = logging.getLogger("chrysa.intro")

# --- features ---------------------------------------------------------------
RATE = 8000  # samples per second after downsampling
FRAME = 3200  # samples per analysis frame (0.4 s) ...
HOP = 1600  # ... advancing 0.2 s at a time
HOP_SECONDS = HOP / RATE
BANDS = 32
FLOOR = 1e-3  # relative energy floor (-30 dB re the frame average)
LOCAL_FRAMES = 51  # the surrounding ~10 s is subtracted: what music of any kind shares with the speech around it goes
QSCALE = 12.0  # features are stored as int8 at 1/12 of a log unit
ALGORITHM = 4  # bump when detection changes: cached features and results are then recomputed
MAX_AUDIO_SECONDS = 4 * 3600

# --- matching ---------------------------------------------------------------
SMOOTH = 5  # similarity is averaged over one second
HIGH = 0.45  # smoothed similarity that seeds a matching run ...
LOW = 0.28  # ... and the lower level at which it may extend (fades, builds, tails)
RAW_LOW = 0.30  # run ends are trimmed back to frames at least this similar (and half the run's core level)
MIN_MEAN_SIM = 0.38
MAX_GAP = 8  # frames: a brief mismatch (a sound effect, a cut) does not end a segment
MIN_SEGMENT_SECONDS = 10.0
MIN_SEGMENT_FRAMES = int(MIN_SEGMENT_SECONDS / HOP_SECONDS)
REFINE_FRAMES = 40  # how far (8 s) boundaries may move when placed exactly
MAX_PEAKS = 12  # offsets examined per episode pair
PEAK_SEPARATION = int(1.6 / HOP_SECONDS)  # only a peak's own sharp neighbourhood: another recurring piece may sit a few seconds away

# --- judgement --------------------------------------------------------------
MIN_CONFIDENCE = 0.62  # below this nothing is shown
END_SAFETY = 0.3  # seconds: the skip lands this much before the matched end, never inside the story
SEED_EPISODES = 6  # episodes judged together before the rest are matched against the result


@dataclass(frozen=True, slots=True)
class Intro:
    start: float  # seconds into the file
    end: float


def available() -> bool:
    return np is not None and find_tool("ffmpeg") is not None


# ------------------------------------------------------------ extraction


class Features:
    """The coarse spectrogram of one episode (int8 frames × :data:`BANDS`) and its length in seconds.

    ``data`` is the plain view (used to place boundaries exactly); ``hp`` has the surrounding ten seconds
    subtracted, which removes what any music shares with the speech around it (used to find recurring audio,
    so that two different songs do not look alike). Only the plain view is stored; ``hp`` is derived on demand.
    """

    def __init__(self, q, duration: float, hp_q=None) -> None:
        self.q = q
        self.duration = float(duration)
        self._data = None
        self._hp_q = hp_q
        self._hp = None

    @property
    def frames(self) -> int:
        return len(self.q)

    @property
    def data(self):
        if self._data is None:
            self._data = self.q.astype(np.float32) / QSCALE
        return self._data

    @property
    def hp_q(self):
        if self._hp_q is None:
            high = self.data - _moving_mean(self.data, LOCAL_FRAMES)
            self._hp_q = np.clip(np.rint(high * QSCALE), -127, 127).astype(np.int8)
        return self._hp_q

    @property
    def hp(self):
        if self._hp is None:
            self._hp = self.hp_q.astype(np.float32) / QSCALE
        return self._hp

    @classmethod
    def from_rows(cls, rows, duration: float) -> Features:
        """Quantise raw band rows (each already centred across bands); removes the episode's overall sound."""
        rows = rows - rows.mean(axis=0, keepdims=True)
        return cls(np.clip(np.rint(rows * QSCALE), -127, 127).astype(np.int8), duration)

    def to_blob(self) -> bytes:
        return zlib.compress(self.q.tobytes(), 3)

    @classmethod
    def from_blob(cls, blob: bytes, frames: int, duration: float) -> Features:
        q = np.frombuffer(zlib.decompress(blob), dtype=np.int8).reshape(frames, BANDS).copy()
        return cls(q, duration)

    def slice(self, first: int, last: int) -> Features:
        """Frames ``[first, last)`` as their own :class:`Features`, keeping the high-passed view as it was in context."""
        return Features(self.q[first:last].copy(), (last - first) * HOP_SECONDS, self.hp_q[first:last].copy())


def _moving_mean(rows, width: int):
    """Mean of each band over a window of *width* frames around every frame (shrinking at the ends)."""
    total = np.concatenate([np.zeros((1, rows.shape[1]), np.float64), np.cumsum(rows, axis=0, dtype=np.float64)])
    index = np.arange(len(rows))
    low, high = np.maximum(index - width // 2, 0), np.minimum(index + width // 2 + 1, len(rows))
    return ((total[high] - total[low]) / (high - low)[:, None]).astype(np.float32)


@cache
def _band_matrix():
    frequencies = np.fft.rfftfreq(FRAME, 1 / RATE)
    edges = np.geomspace(150, 3800, BANDS + 1)
    matrix = np.zeros((len(frequencies), BANDS), np.float32)
    for band in range(BANDS):
        matrix[(frequencies >= edges[band]) & (frequencies < edges[band + 1]), band] = 1.0
    return matrix


@cache
def _window():
    return np.hanning(FRAME).astype(np.float32)


def _rows(samples):
    """Log energy in :data:`BANDS` frequency bands per frame, each frame centred (loudness does not matter)."""
    count = (len(samples) - FRAME) // HOP + 1
    if count <= 0:
        return np.empty((0, BANDS), np.float32)
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    spectrum = np.abs(np.fft.rfft(samples[index] * _window(), axis=1)) ** 2
    energy = spectrum @ _band_matrix()
    # Bands far below the frame's average carry only noise floor; a relative floor stops that randomness
    # from dominating the comparison (and keeps silence from looking like a sound).
    bands = np.log(energy + FLOOR * energy.mean(axis=1, keepdims=True) + 1e-10)
    bands -= bands.mean(axis=1, keepdims=True)
    return bands.astype(np.float32)


def features_from_samples(samples) -> Features:
    """Features of an in-memory mono signal at :data:`RATE` (tests, and short clips)."""
    blocks, step = [], HOP * 300
    for start in range(0, max(1, len(samples) - FRAME + 1), step):
        blocks.append(_rows(samples[start:start + step + FRAME - HOP]))
    rows = np.concatenate(blocks) if blocks else np.empty((0, BANDS), np.float32)
    return Features.from_rows(rows, len(samples) / RATE)


def extract_features(path: str, should_stop: Callable[[], bool] = lambda: False, timeout: float = 1800.0):
    """Decode the whole file and return its :class:`Features`, or None (no audio, unreadable, stopped).

    ffmpeg runs at low priority and its output is reduced block by block, so memory stays small however long the
    episode is.
    """
    ffmpeg = find_tool("ffmpeg")
    if np is None or not ffmpeg:
        return None
    command = [
        ffmpeg, "-v", "error", "-nostdin", "-t", f"{MAX_AUDIO_SECONDS}", "-i", path,
        "-vn", "-sn", "-dn", "-map", "0:a:0?", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-",
    ]
    options: dict = {}
    if sys.platform == "win32":
        options["creationflags"] = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
    elif shutil.which("nice"):
        command = ["nice", "-n", "10", *command]
        if shutil.which("ionice"):
            command = ["ionice", "-c", "3", *command]  # idle disk priority: never slow a video playing from the same drive
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **options)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    rows, pending, total = [], np.empty(0, np.float32), 0
    try:
        while True:
            if should_stop() or time.monotonic() > deadline:
                return None
            raw = process.stdout.read(RATE * 2 * 60)
            if not raw:
                break
            raw = raw[:len(raw) - len(raw) % 2]
            total += len(raw) // 2
            pending = np.concatenate([pending, np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0])
            if len(pending) >= FRAME:
                frames = (len(pending) - FRAME) // HOP + 1
                rows.append(_rows(pending))
                pending = pending[frames * HOP:]
    except OSError:
        return None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
    if process.returncode not in (0, None) and not rows:
        return None
    if total < RATE * 30 or not rows:
        return None
    return Features.from_rows(np.concatenate(rows), total / RATE)


# ------------------------------------------------------------ pair matching


@dataclass(frozen=True, slots=True)
class Segment:
    """Audio shared by two episodes: frames ``[a0, a1)`` of the first equal ``[b0, b1)`` of the second."""

    a0: int
    a1: int
    b0: int
    b1: int
    sim: float


def frame_time(frame: int) -> float:
    """Seconds at the start of the hop slot a frame stands for."""
    return frame * HOP_SECONDS + (FRAME - HOP) / 2 / RATE


def _cosine(x, y):
    dot = (x * y).sum(axis=1)
    return dot / (np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1) + 1e-3)  # silence counts as no match


def _lag_peaks(a, b) -> list[int]:
    """The offsets (frames by which ``a`` trails ``b``) with the strongest overall alignment, over every offset.

    A matched filter: the cross-correlation of the two feature sequences summed over bands (an FFT), scaled by the
    square root of the overlap so that neither long overlaps nor chance alignments of a few frames win by size.
    """
    na, nb = len(a), len(b)
    size = 1 << (na + nb - 1).bit_length()
    spectrum = (np.fft.rfft(a, size, axis=0) * np.conj(np.fft.rfft(b, size, axis=0))).sum(axis=1)
    correlation = np.fft.irfft(spectrum, size)
    lags = np.arange(-(nb - 1), na)
    overlap = np.minimum(min(na, nb), np.minimum(na - lags, nb + lags))
    score = np.where(overlap >= MIN_SEGMENT_FRAMES, correlation[lags % size] / np.sqrt(np.maximum(overlap, 1)), -np.inf)
    peaks = []
    for _ in range(MAX_PEAKS):
        best = int(np.argmax(score))
        if not np.isfinite(score[best]) or score[best] <= 0:
            break
        peaks.append(int(lags[best]))
        score[max(0, best - PEAK_SEPARATION):best + PEAK_SEPARATION + 1] = -np.inf
    return peaks


def _diagonal(x, y):
    """Per-frame similarity of two aligned feature stretches, tolerant of one frame of timing jitter."""
    similarity = _cosine(x, y)
    if len(x) > 1:
        similarity[1:] = np.maximum(similarity[1:], _cosine(x[1:], y[:-1]))
        similarity[:-1] = np.maximum(similarity[:-1], _cosine(x[:-1], y[1:]))
    return similarity


def _runs(mask) -> list[tuple[int, int]]:
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def _bridge(mask, max_gap: int):
    """*mask* with gaps of up to *max_gap* False values between True runs filled."""
    bridged = mask.copy()
    runs = _runs(mask)
    for (_, end), (start, _) in zip(runs, runs[1:]):
        if start - end <= max_gap:
            bridged[end:start] = True
    return bridged


def _matching_runs(similarity) -> list[tuple[int, int]]:
    """Runs of matching frames.

    Seeded where the smoothed similarity is high. A run may reach as far as the similarity stays at least half of
    its own core level (so a clean match is bounded tightly and a noisy one is not cut short), bridging short gaps;
    its ends are then trimmed to frames that really match.
    """
    smooth = np.convolve(similarity, np.ones(SMOOTH) / SMOOTH, mode="same")
    strong = smooth >= HIGH
    found = []
    for start, end in _runs(_bridge(smooth >= LOW, MAX_GAP)):
        seeds = strong[start:end]
        if not seeds.any():
            continue
        core = float(np.median(similarity[start:end][seeds]))
        level = max(LOW, 0.5 * core)
        floor = max(RAW_LOW, 0.5 * core)
        for s, e in _runs(_bridge(smooth[start:end] >= level, MAX_GAP)):
            if not seeds[s:e].any():
                continue
            s, e = start + s, start + e
            limit = 2 * SMOOTH
            while s < e and similarity[s] < floor and limit:
                s, limit = s + 1, limit - 1
            limit = 2 * SMOOTH
            while e > s and similarity[e - 1] < floor and limit:
                e, limit = e - 1, limit - 1
            if e - s >= MIN_SEGMENT_FRAMES and similarity[s:e].mean() >= MIN_MEAN_SIM:
                found.append((s, e))
    return found


def match_pair(fa: Features, fb: Features) -> list[Segment]:
    """Every stretch of audio shared by two episodes, wherever it sits in either.

    Stage 2 finds candidates on the high-passed features; stage 3 places each one's ends on the plain features.
    """
    if np is None or fa.frames < MIN_SEGMENT_FRAMES or fb.frames < MIN_SEGMENT_FRAMES:
        return []
    a, b = fa.hp, fb.hp
    segments = []
    for lag in _lag_peaks(a, b):
        b0 = max(0, -lag)
        a0 = b0 + lag
        length = min(len(b) - b0, len(a) - a0)
        similarity = _diagonal(a[a0:a0 + length], b[b0:b0 + length])
        plain = None
        for start, end in _matching_runs(similarity):
            if plain is None:
                plain = _diagonal(fa.data[a0:a0 + length], fb.data[b0:b0 + length])
            start, end = _refine(plain, start, end)
            if end - start >= MIN_SEGMENT_FRAMES:
                segments.append(Segment(a0 + start, a0 + end, b0 + start, b0 + end, float(plain[start:end].mean())))
    return _strongest(segments)


def _refine(plain, start: int, end: int) -> tuple[int, int]:
    """Stage 3: move the ends of a found segment to where the plain features stop matching.

    Next to an intro both episodes have unrelated speech, so on the plain features the similarity falls away
    sharply at the true edge; the high-passed features blur that edge by a couple of seconds.
    """
    low, high = max(0, start - REFINE_FRAMES), min(len(plain), end + REFINE_FRAMES)
    inside = plain[start:end]
    core = float(np.median(inside[inside >= HIGH])) if (inside >= HIGH).any() else float(np.median(inside))
    level = max(LOW, 0.5 * core)
    smooth = np.convolve(plain, np.ones(SMOOTH) / SMOOTH, mode="same")[low:high]
    runs = [(low + s, low + e) for s, e in _runs(_bridge(smooth >= level, MAX_GAP)) if low + e > start and low + s < end]
    if not runs:
        return start, end
    new_start, new_end = min(r[0] for r in runs), max(r[1] for r in runs)
    floor = max(RAW_LOW, 0.5 * core)
    limit = 2 * SMOOTH
    while new_start < new_end and plain[new_start] < floor and limit:
        new_start, limit = new_start + 1, limit - 1
    limit = 2 * SMOOTH
    while new_end > new_start and plain[new_end - 1] < floor and limit:
        new_end, limit = new_end - 1, limit - 1
    return new_start, new_end


def _strongest(segments: list[Segment]) -> list[Segment]:
    """Keep the best explanation of each stretch: a repetitive tune also matches itself at shifted offsets, and those
    weaker alignments overlap the true one in one of the two episodes."""
    kept: list[Segment] = []
    for seg in sorted(segments, key=lambda s: -(s.a1 - s.a0) * s.sim):
        length = seg.a1 - seg.a0
        clash = any(
            min(seg.a1, k.a1) - max(seg.a0, k.a0) > 0.3 * length or min(seg.b1, k.b1) - max(seg.b0, k.b0) > 0.3 * length
            for k in kept
        )
        if not clash:
            kept.append(seg)
    return kept


def find_common_block(a, b, rate: int = RATE) -> tuple[Intro, Intro] | None:
    """The longest audio two signals share, as ``(intro in a, intro in b)``; a thin convenience over :func:`match_pair`."""
    if np is None or rate != RATE:
        return None
    segments = match_pair(features_from_samples(a), features_from_samples(b))
    if not segments:
        return None
    best = max(segments, key=lambda s: (s.a1 - s.a0) * s.sim)
    return (Intro(frame_time(best.a0), frame_time(best.a1)), Intro(frame_time(best.b0), frame_time(best.b1)))


# ------------------------------------------------------------ clustering


@dataclass
class _Occurrence:
    episode: int
    start: int  # frames
    end: int
    sim: float
    partner: int  # the other episode of the pair it came from


@dataclass
class Member:
    """One episode's share of a recurring cluster."""

    episode: int
    start: float  # seconds
    end: float
    similarity: float
    partners: int  # distinct other episodes it matched
    spread: float  # seconds of disagreement between those partners about the boundaries
    duration: float  # the episode's own length


@dataclass
class Cluster:
    members: list[Member]
    confidence: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    medoid: int = 0  # episode whose audio becomes the template
    length: float = 0.0  # median intro length

    @property
    def episodes(self) -> set[int]:
        return {m.episode for m in self.members}


class _Union:
    def __init__(self, count: int) -> None:
        self.parent = list(range(count))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        self.parent[self.find(i)] = self.find(j)


def _median(values):
    return float(np.median(values)) if len(values) else 0.0


def _mad(values) -> float:
    return _median([abs(v - _median(values)) for v in values]) if len(values) > 1 else 0.0


def _overlap_ratio(a: _Occurrence, b: _Occurrence) -> float:
    """How much of the shorter occurrence lies inside the other (1 when one contains the other)."""
    inside = min(a.end, b.end) - max(a.start, b.start)
    return inside / min(a.end - a.start, b.end - b.start) if inside > 0 else 0.0


def find_clusters(features: dict[int, Features], should_stop: Callable[[], bool] = lambda: False) -> list[Cluster]:
    """Every recurring piece of audio in the season, each with the episodes it occurs in (unscored)."""
    ids = sorted(features)
    occurrences: list[_Occurrence] = []
    for position, first in enumerate(ids):
        for second in ids[position + 1:]:
            if should_stop():
                return []
            for seg in match_pair(features[first], features[second]):
                occurrences.append(_Occurrence(first, seg.a0, seg.a1, seg.sim, second))
                occurrences.append(_Occurrence(second, seg.b0, seg.b1, seg.sim, first))
                # occurrences come in linked pairs: (2n, 2n+1)
    union = _Union(len(occurrences))
    for index in range(0, len(occurrences), 2):
        union.union(index, index + 1)
    by_episode: dict[int, list[int]] = {}
    for index, occ in enumerate(occurrences):
        by_episode.setdefault(occ.episode, []).append(index)
    for indices in by_episode.values():  # the same stretch of one episode found against different partners
        for n, i in enumerate(indices):
            for j in indices[n + 1:]:
                if _overlap_ratio(occurrences[i], occurrences[j]) >= 0.8:
                    union.union(i, j)
    groups: dict[int, list[_Occurrence]] = {}
    for index, occ in enumerate(occurrences):
        groups.setdefault(union.find(index), []).append(occ)
    clusters = []
    for group in groups.values():
        members = _members(group, features)
        if len(members) >= 2:
            clusters.append(Cluster(members))
    return clusters


def _members(group: list[_Occurrence], features: dict[int, Features]) -> list[Member]:
    """Reduce an occurrence group to one interval per episode, dropping episodes only weakly tied to it."""
    group = list(group)
    while True:
        episodes = {o.episode for o in group}
        if len(episodes) < 2:
            return []
        need = max(1, math.ceil(0.34 * (len(episodes) - 1)))
        degree = {e: len({o.partner for o in group if o.episode == e and o.partner in episodes}) for e in episodes}
        weak = {e for e, d in degree.items() if d < need}
        if not weak:
            break
        group = [o for o in group if o.episode not in weak and o.partner not in weak]
    members = []
    for episode in sorted(episodes):
        mine = [o for o in group if o.episode == episode]
        starts = sorted(frame_time(o.start) for o in mine)
        ends = sorted(frame_time(o.end) for o in mine)
        # The extent of the opening in this episode is what matched other episodes: partners with shorter or longer
        # cuts each reveal only the part they share, so a high quantile of the ends and a low one of the starts, which
        # tends towards the union as partners increase yet is not decided by one partner's chance overextension
        # (with only one or two matches it is the more conservative intersection).
        members.append(Member(
            episode, starts[math.ceil(0.25 * (len(starts) - 1))], ends[int(0.75 * (len(ends) - 1))],
            _mean([o.sim for o in mine]), degree[episode],
            max(_mad(starts), _mad(ends)), features[episode].duration,
        ))
    return members


def _mean(values) -> float:
    return float(sum(values) / len(values)) if values else 0.0


# ------------------------------------------------------------ judgement


def _clip(x: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, x))


def _plausible_duration(seconds: float) -> float:
    """Openings run from about ten seconds to two minutes; anything far outside is something else."""
    if seconds < 10:
        return 0.0
    if seconds < 25:
        return 0.45 + 0.55 * (seconds - 10) / 15
    if seconds <= 150:
        return 1.0
    if seconds <= 300:
        return 1.0 - 0.65 * (seconds - 150) / 150
    return 0.1


def recurrence_score(occurrences: int, episodes: int) -> float:
    """How convincing it is that the audio recurs: more episodes is stronger, and a larger share is stronger."""
    if occurrences < 2:
        return 0.0
    evidence = 1 - math.exp(-(occurrences - 1) / 0.83)  # 2 episodes: .70, 3: .91, 4: .97
    return evidence * (0.55 + 0.45 * math.sqrt(min(1.0, occurrences / max(episodes, occurrences))))


def similarity_score(similarity: float) -> float:
    return 0.4 + 0.6 * _clip((similarity - 0.35) / 0.4)


def opening_score(starts, durations) -> float:
    """How much the cluster's *position* looks like an opening: early, with late intros still allowed.

    A soft function of where it starts as a fraction of the episode (not a cut-off), reduced when the start wanders
    a lot between episodes and the cluster is not early, which is how background music behaves.
    """
    fractions = [s / d for s, d in zip(starts, durations) if d > 0]
    fraction = _median(fractions)
    score = 1 / (1 + math.exp((fraction - 0.45) / 0.07))
    late = _clip((fraction - 0.12) / 0.2)
    wander = 1 - 1 / (1 + (_mad(starts) / 150) ** 2)
    return score * (1 - 0.7 * late * wander)


def outro_score(tails, durations) -> float:
    """1 when plenty of episode follows the segment; near 0 when it runs up to the end (credits music).

    "Plenty" is either several minutes or a good share of the episode, so a short episode with an opening near
    its start is not mistaken for one whose segment sits at the end.
    """
    shares = [t / d for t, d in zip(tails, durations) if d > 0]
    return max(0.02, _clip((_median(tails) - 90) / 420), _clip((_median(shares) - 0.05) / 0.45))


def boundary_score(spreads) -> float:
    return 1 / (1 + (_median(spreads) / 2.0) ** 2)


def judge(cluster: Cluster, analysed: int) -> Cluster:
    """Fill in a cluster's confidence and the factors behind it."""
    members = cluster.members
    starts = [m.start for m in members]
    lengths = [m.end - m.start for m in members]
    durations = [m.duration for m in members]
    cluster.length = _median(lengths)
    factors = {
        "recurrence": recurrence_score(len(members), analysed),
        "similarity": similarity_score(_mean([m.similarity for m in members])),
        "duration": _plausible_duration(cluster.length) ** 0.7,
        "opening": opening_score(starts, durations) ** 1.5,
        "not_outro": outro_score([m.duration - m.end for m in members], durations) ** 2,
        "boundaries": 0.7 + 0.3 * boundary_score([m.spread for m in members]),
    }
    cluster.factors = factors
    cluster.confidence = math.prod(factors.values())
    cluster.medoid = max(members, key=lambda m: (m.partners, m.similarity)).episode
    return cluster


# ------------------------------------------------------------ the season model


@dataclass
class Profile:
    """One recurring intro of a season (a *variant*), with a template for matching episodes seen later."""

    variant: int
    template: object  # int8 frames × 2·BANDS: the plain features, then the high-passed ones
    confidence: float
    quality: float  # confidence without the recurrence part
    occurrences: int
    episodes: int
    duration: float
    created: float = 0.0


@dataclass
class EpisodeResult:
    episode: int
    present: bool
    start: float = 0.0
    end: float = 0.0
    confidence: float = 0.0
    similarity: float = 0.0
    variant: int | None = None
    status: str = "absent"  # found | absent | unreadable


@dataclass
class SeasonAnalysis:
    profiles: list[Profile]
    results: dict[int, EpisodeResult]
    clusters: int = 0
    rejected: list[str] = field(default_factory=list)


def analyse(features: dict[int, Features], should_stop: Callable[[], bool] = lambda: False) -> SeasonAnalysis | None:
    """Judge a whole season from the features of its episodes. None when stopped."""
    clusters = find_clusters(features, should_stop)
    if should_stop():
        return None
    analysed = len(features)
    scored = sorted((judge(c, analysed) for c in clusters), key=lambda c: -c.confidence)
    results = {e: EpisodeResult(e, False) for e in features}
    profiles: list[Profile] = []
    assigned: set[int] = set()  # an episode has one opening: a better cluster that explains it wins
    rejected = []
    for cluster in scored:
        label = (f"{len(cluster.members)}/{analysed} episodes, {cluster.length:.0f} s, "
                 f"confidence {cluster.confidence:.2f} {_factor_text(cluster)}")
        if cluster.confidence < MIN_CONFIDENCE:
            rejected.append("rejected: " + label)
            continue
        free = [m for m in cluster.members if m.episode not in assigned]
        if len(free) < 2:
            rejected.append("episodes already explained by a better cluster: " + label)
            continue
        cluster.members = free
        judge(cluster, analysed)
        if cluster.confidence < MIN_CONFIDENCE:
            rejected.append("rejected: " + label)
            continue
        variant = len(profiles)
        medoid = next(m for m in free if m.episode == cluster.medoid)
        first = int(math.ceil((medoid.start - frame_time(0)) / HOP_SECONDS))
        last = int((medoid.end - frame_time(0)) / HOP_SECONDS)
        quality = cluster.confidence / max(recurrence_score(len(free), analysed), 1e-6)
        piece = features[medoid.episode].slice(first, last)
        profiles.append(Profile(variant, np.concatenate([piece.q, piece.hp_q], axis=1), cluster.confidence, quality,
                                len(free), analysed, cluster.length, time.time()))
        for m in free:
            assigned.add(m.episode)
            support = _clip(m.partners / max(1, len(free) - 1))
            results[m.episode] = EpisodeResult(
                m.episode, True, m.start, max(m.start, m.end - END_SAFETY),
                cluster.confidence * (0.85 + 0.15 * support * similarity_score(m.similarity)), m.similarity,
                variant, "found")
    return SeasonAnalysis(profiles, results, len(clusters), rejected)


def _factor_text(cluster: Cluster) -> str:
    return "(" + ", ".join(f"{k} {v:.2f}" for k, v in cluster.factors.items()) + ")"


def match_profiles(profiles: list[Profile], episode: int, features: Features) -> EpisodeResult | None:
    """Look for an already known intro in one new episode, using the templates only. None when none is there."""
    best = None
    for profile in profiles:
        template = Features(profile.template[:, :BANDS], profile.duration, profile.template[:, BANDS:])
        for seg in match_pair(features, template):
            covered = (seg.a1 - seg.a0) / template.frames
            if covered < 0.55 or seg.sim < 0.5:
                continue
            start, end = frame_time(seg.a0), frame_time(seg.a1)
            position = opening_score([start], [features.duration]) ** 1.5 * outro_score([features.duration - end], [features.duration]) ** 2
            confidence = profile.confidence * (0.85 + 0.15 * similarity_score(seg.sim)) * covered ** 0.5 * min(1.0, position + 0.15)
            if best is None or confidence > best.confidence:
                best = EpisodeResult(episode, True, start, max(start, end - END_SAFETY), confidence, seg.sim,
                                     profile.variant, "found")
    return best if best and best.confidence >= MIN_CONFIDENCE else None


# ------------------------------------------------------------ per season

Found = Callable[[int], None]


def clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def analyse_season(job, store, should_stop: Callable[[], bool] = lambda: False, on_found: Found | None = None) -> None:
    """Bring one season's intro knowledge up to date and save it through *store* (the Library).

    *job* has ``show_id``, ``season``, ``episodes`` (Media, in order) and ``done`` (ids already judged).

    * Known intros (templates) are looked for in new episodes first; that is cheap.
    * A season seen for the first time has its first :data:`SEED_EPISODES` judged together, so Skip Intro becomes
      useful before the whole season has been decoded; the rest are matched against that result.
    * Only episodes that match nothing known make the season be judged again, from cached features, which can
      also revise earlier conclusions.
    """
    on_found = on_found or (lambda media_id: None)
    episodes = [m for m in job.episodes if os.path.isfile(m.path)]
    if len(episodes) < 2:
        return
    tag = f"S{job.season:02d}"
    by_id = {m.id: m for m in episodes}
    features: dict[int, Features] = {}
    unreadable: set[int] = set()
    log.info("[INTRO] Season: %s (%d episodes, %d already judged)", tag, len(episodes), len(job.done))

    def load(media) -> Features | None:
        if media.id in features:
            return features[media.id]
        found = store.intro_features(media)
        if found is None:
            log.info("[INTRO] %s episode %s: listening to the audio…", tag, media.episode or f"#{media.id}")
            found = extract_features(media.path, should_stop)
            if found is None:
                if not should_stop():
                    unreadable.add(media.id)
                    log.warning("[INTRO] %s episode %s: the audio could not be read", tag, media.episode or f"#{media.id}")
                return None
            store.save_intro_features(media, found)
        features[media.id] = found
        return found

    def save(profiles: list[Profile], results: list[EpisodeResult]) -> None:
        items = [(by_id[r.episode], r) for r in results if r.episode in by_id]
        items += [(by_id[e], EpisodeResult(e, False, status="unreadable")) for e in unreadable]
        store.save_season_intros(job.show_id, job.season, profiles, items)
        for r in results:
            if r.present and r.confidence >= MIN_CONFIDENCE:
                on_found(r.episode)

    def judge_season(episode_list) -> bool:
        """Judge *episode_list* together and save the result. False when stopped."""
        for media in episode_list:
            if should_stop():
                return False
            load(media)
        analysis = analyse({m.id: features[m.id] for m in episode_list if m.id in features}, should_stop)
        if analysis is None:
            return False
        _log(tag, analysis, by_id, len(analysis.results))
        save(analysis.profiles, list(analysis.results.values()))
        return True

    def match_new(profiles: list[Profile], new: list) -> list | None:
        """Match *new* episodes against *profiles*; returns the ones that matched nothing (None when stopped)."""
        matched, left = [], []
        for media in new:
            if should_stop():
                return None
            feats = load(media)
            if feats is None:
                continue
            result = match_profiles(profiles, media.id, feats)
            if result is None:
                left.append(media)
                continue
            matched.append(result)
            log.info("[INTRO] %s episode %s: %s–%s (confidence %.2f, matched the known intro)", tag,
                     by_id[media.id].episode, clock(result.start), clock(result.end), result.confidence)
        if matched:
            for profile in profiles:
                profile.occurrences += sum(1 for r in matched if r.variant == profile.variant)
                profile.episodes += len(matched) + len(left)
                profile.confidence = profile.quality * recurrence_score(profile.occurrences, profile.episodes)
            save(profiles, matched)
        return left

    pending = [m for m in episodes if m.id not in job.done]
    known = store.intro_profiles(job.show_id, job.season) if job.done else []
    if known:
        left = match_new(known, pending)
        if left is None or not left:
            return
        judge_season(episodes)
        return
    if len(episodes) > SEED_EPISODES:
        order = sorted(episodes, key=lambda m: (-(m.last_watched or 0.0), m.episode or 0))
        seed = order[:SEED_EPISODES]
        if not judge_season(seed):
            return
        profiles = store.intro_profiles(job.show_id, job.season)
        if profiles:
            left = match_new(profiles, [m for m in episodes if m not in seed])
            if left is None or not left:
                return
    judge_season(episodes)


def _log(tag: str, analysis: SeasonAnalysis, media: dict, analysed: int) -> None:
    log.info("[INTRO] Season: %s", tag)
    log.info("[INTRO] Episodes analyzed: %d", analysed)
    log.info("[INTRO] Candidate clusters: %d", analysis.clusters)
    for line in analysis.rejected[:6]:
        log.info("[INTRO]   %s", line)
    for profile in analysis.profiles:
        log.info("[INTRO] Intro variant %d: recurrence %d/%d, length %.0f s, confidence %.2f", profile.variant + 1,
                 profile.occurrences, profile.episodes, profile.duration, profile.confidence)
    for episode, result in sorted(analysis.results.items(), key=lambda kv: (media[kv[0]].episode or 0) if kv[0] in media else 0):
        name = f"Episode {media[episode].episode}" if episode in media and media[episode].episode else f"#{episode}"
        if result.present:
            log.info("[INTRO] %s: %s–%s (confidence %.2f, similarity %.2f)", name, clock(result.start), clock(result.end),
                     result.confidence, result.similarity)
        else:
            log.info("[INTRO] %s: NO INTRO", name)
