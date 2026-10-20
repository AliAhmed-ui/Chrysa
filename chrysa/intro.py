"""Find a show's opening titles by comparing the audio of its episodes (fully offline).

The first minutes of two episodes are decoded to mono 8 kHz audio with ffmpeg and
reduced to a spectrogram. An FFT cross-correlation finds the time offset at which the
two line up; at that offset the spectra are compared frame by frame, and the longest
unbroken run of matching frames is the theme song. Nothing here touches Qt or the database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .probe import find_tool

try:
    import numpy as np
except ImportError:  # the feature switches itself off; the rest of the app is unaffected
    np = None

RATE = 8000  # samples per second after downsampling
ANALYSIS_SECONDS = 480  # the first 8 minutes of each file
ALGORITHM = 2  # bump when detection changes: stored results are then recomputed
FRAME = 1600  # samples per analysis frame (0.2 s) ...
HOP = 800  # ... advancing 0.1 s at a time
FRAME_SECONDS = HOP / RATE
BANDS = 32
SMOOTH_FRAMES = 10  # similarity is averaged over one second
MATCH_THRESHOLD = 0.45  # averaged cosine similarity above which two stretches count as the same audio
MAX_GAP_FRAMES = 15  # a brief mismatch (a sound effect, a cut) does not end the intro
MAX_OFFSET_FRAMES = 300 * 10  # the theme starts within 5 minutes of where it starts in the other episode
MIN_INTRO_SECONDS = 15.0
MAX_INTRO_SECONDS = 240.0


@dataclass(frozen=True, slots=True)
class Intro:
    start: float  # seconds into the file
    end: float


def available() -> bool:
    return np is not None and find_tool("ffmpeg") is not None


# ------------------------------------------------------------- extraction


def extract_audio(path: str, seconds: float = ANALYSIS_SECONDS, timeout: float = 180.0):
    """The first *seconds* of audio as a mono float32 array at :data:`RATE`, or None.

    Runs ffmpeg at low priority and keeps everything in memory (8 minutes is about 15 MB).
    """
    ffmpeg = find_tool("ffmpeg")
    if np is None or not ffmpeg:
        return None
    command = [
        ffmpeg, "-v", "error", "-nostdin", "-t", f"{seconds:g}", "-i", path,
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
        done = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL, **options
        )
    except (OSError, subprocess.SubprocessError):
        return None
    usable = len(done.stdout) - len(done.stdout) % 2
    if done.returncode != 0 or usable < RATE * 10:
        return None
    return np.frombuffer(done.stdout[:usable], dtype="<i2").astype(np.float32) / 32768.0


# ------------------------------------------------------------ correlation


def _features(samples):
    """A spectrogram summary: log energy in :data:`BANDS` frequency bands, one row per 0.1 s.

    Comparing spectral shape instead of raw waveforms survives the dialogue and effects
    that are mixed over the music differently in each episode, and different encodes.
    """
    count = (len(samples) - FRAME) // HOP + 1
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    spectrum = np.abs(np.fft.rfft(samples[index] * np.hanning(FRAME), axis=1)) ** 2
    frequencies = np.fft.rfftfreq(FRAME, 1 / RATE)
    edges = np.geomspace(150, 3800, BANDS + 1)
    bands = np.empty((count, BANDS), np.float32)
    for band in range(BANDS):
        bands[:, band] = spectrum[:, (frequencies >= edges[band]) & (frequencies < edges[band + 1])].sum(axis=1)
    bands = np.log(bands + 1e-8)
    bands -= bands.mean(axis=1, keepdims=True)  # loudness does not matter
    bands -= bands.mean(axis=0, keepdims=True)  # nor does the episode's overall sound
    return bands


def _best_lag(a, b) -> int:
    """Frames by which *a* trails *b*: ``a[t + lag] ≈ b[t]`` at the best alignment.

    The cross-correlation of the two feature sequences, summed over bands, computed with FFTs.
    """
    size = 1 << (len(a) + len(b) - 1).bit_length()
    spectrum = (np.fft.rfft(a, size, axis=0) * np.conj(np.fft.rfft(b, size, axis=0))).sum(axis=1)
    correlation = np.fft.irfft(spectrum, size)
    lags = np.arange(size)
    lags = np.where(lags < size // 2, lags, lags - size)
    correlation = np.where(np.abs(lags) <= MAX_OFFSET_FRAMES, correlation, -np.inf)
    return int(lags[int(np.argmax(correlation))])


def find_common_block(a, b, rate: int = RATE) -> tuple[Intro, Intro] | None:
    """The longest stretch of identical audio in *a* and *b*, as ``(intro in a, intro in b)``."""
    minimum = rate * MIN_INTRO_SECONDS
    if np is None or rate != RATE or len(a) < minimum or len(b) < minimum:
        return None
    fa, fb = _features(a), _features(b)
    lag = _best_lag(fa, fb)
    b_start = max(0, -lag)
    a_start = b_start + lag
    overlap = min(len(fb) - b_start, len(fa) - a_start)
    if overlap < SMOOTH_FRAMES:
        return None
    x, y = fa[a_start:a_start + overlap], fb[b_start:b_start + overlap]
    similarity = (x * y).sum(axis=1) / (np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1) + 1e-9)
    smoothed = np.convolve(similarity, np.ones(SMOOTH_FRAMES) / SMOOTH_FRAMES, mode="same")

    best = _longest_run(smoothed >= MATCH_THRESHOLD, MAX_GAP_FRAMES)
    if best is None:
        return None
    first, last = best  # frame indices, inclusive
    if not MIN_INTRO_SECONDS <= (last - first + 1) * FRAME_SECONDS <= MAX_INTRO_SECONDS:
        return None
    to_seconds = lambda frame: frame * FRAME_SECONDS  # noqa: E731
    in_a = Intro(to_seconds(a_start + first), to_seconds(a_start + last + 1))
    in_b = Intro(to_seconds(b_start + first), to_seconds(b_start + last + 1))
    return in_a, in_b


def _longest_run(matching, max_gap: int) -> tuple[int, int] | None:
    """Longest run of True values, bridging gaps of up to *max_gap* False values."""
    best: tuple[int, int] | None = None
    start = last = None
    gap = 0
    for index, hit in enumerate(matching):
        if hit:
            if start is None:
                start = index
            last, gap = index, 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                best = _better(best, start, last)
                start = last = None
                gap = 0
    if start is not None:
        best = _better(best, start, last)
    return best


def _better(best, start, last):
    return (start, last) if best is None or last - start > best[1] - best[0] else best


# -------------------------------------------------------------- per season

Store = Callable[[object, "Intro | None"], None]  # (media, intro or None when none was found)


def analyse_season(episodes, done: set[int], store: Store, should_stop: Callable[[], bool] = lambda: False) -> None:
    """Find and store the intro of every episode in *episodes* whose id is not in *done*.

    Each episode is compared with its neighbours; the episode and its partner both
    get a result when a theme is found. Episodes whose audio cannot be read are left
    alone so a later run can try again.
    """
    audio: dict[int, object] = {}  # a small cache: every file is decoded once per run

    def load(media):
        if media.id not in audio:
            if len(audio) >= 3:
                audio.pop(next(iter(audio)))
            audio[media.id] = extract_audio(media.path)
        return audio[media.id]

    for index, media in enumerate(episodes):
        if should_stop():
            return
        if media.id in done:
            continue
        samples = load(media)
        if samples is None:
            continue
        partners = [p for p in (episodes[index + 1:index + 2] + episodes[max(0, index - 1):index][::-1]) if os.path.isfile(p.path)]
        found = None
        for partner in partners[:2]:
            if should_stop():
                return
            other = load(partner)
            if other is None:
                continue
            found = find_common_block(samples, other)
            if found is not None:
                if partner.id not in done:
                    store(partner, found[1])
                    done.add(partner.id)
                break
        if not partners:
            continue  # a lone episode: wait until the season has more
        store(media, found[0] if found else None)
        done.add(media.id)
