"""Synthetic TV audio for the intro tests: tonal 'music' and speech-like 'dialogue', mixed into episodes."""

import numpy as np

from chrysa import intro

RATE = intro.RATE
SCALES = (np.array([0, 2, 4, 7, 9, 12, 14, 16, 19, 21]), np.array([0, 3, 5, 7, 10, 12, 15, 17, 19, 22]),
          np.array([0, 2, 3, 6, 7, 8, 11, 12, 14, 15]), np.array([0, 1, 5, 7, 8, 12, 13, 17, 19, 20]))


def music(seed: int, seconds: float, tempo: float = 0.34) -> np.ndarray:
    """A tune with its own key, scale and timbre (harmonic weights), over a bass line."""
    rng = np.random.default_rng(seed)
    root = 55 * 2 ** (rng.integers(0, 12) / 12)
    scale = SCALES[rng.integers(0, len(SCALES))]
    weights = rng.uniform(0.1, 1.0, 5)
    decay = rng.uniform(1.0, 4.0)
    tempo *= rng.uniform(0.7, 1.4)
    out = np.zeros(int(seconds * RATE), np.float32)
    t = 0.0
    while t < seconds:
        length = tempo * rng.choice([1, 1, 2, 3])
        note = root * 2 ** ((rng.choice(scale) + 12) / 12)
        start, count = int(t * RATE), int(min(length, seconds - t) * RATE)
        if count < 1:
            break
        x = np.arange(count) / RATE
        envelope = np.exp(-x * decay) * np.minimum(1, x * 80)
        tone = sum(weights[h - 1] * np.sin(2 * np.pi * note * h * x) / h for h in range(1, 6))
        out[start:start + count] += (0.18 * envelope * tone).astype(np.float32)
        t += length
    bass = root * 2 ** (rng.choice([0, 5, 7, 3], int(seconds / 2) + 1) / 12)
    for i, f in enumerate(bass):
        a, b = int(i * 2 * RATE), min(len(out), int((i * 2 + 2) * RATE))
        if a < b:
            out[a:b] += 0.12 * np.sin(2 * np.pi * f * np.arange(b - a) / RATE).astype(np.float32)
    return out


def dialogue(seed: int, seconds: float) -> np.ndarray:
    """Speech-like noise: a few formant-ish tones whose frequencies change every 0.25 s, over breath noise."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    segments = int(seconds / 0.25) + 2
    signal = rng.normal(0, 0.03, n).astype(np.float32)
    for _ in range(3):
        freq = np.repeat(rng.uniform(180, 3200, segments), RATE // 4)[:n]
        amp = np.repeat(rng.uniform(0.0, 0.14, segments), RATE // 4)[:n]
        signal += (amp * np.sin(2 * np.pi * np.cumsum(freq) / RATE)).astype(np.float32)
    return signal


def episode(seed: int, minutes: float, intro_at: float | None = None, theme=None, extras=(), gain: float = 1.0,
            degrade: bool = False, bed: float = 0.02) -> np.ndarray:
    """An episode: dialogue, the *theme* at *intro_at* seconds, other recurring pieces ``extras=[(audio, at)]``."""
    audio = dialogue(seed, minutes * 60)
    pieces = list(extras) + ([(theme, intro_at)] if theme is not None and intro_at is not None else [])
    for piece, at in pieces:
        a = int(at * RATE)
        end = min(len(audio), a + len(piece))
        audio[a:end] = piece[:end - a] + bed * audio[a:end]
    audio *= gain
    if degrade:  # a lossy encode: dull highs, a noise floor, coarse quantisation
        audio = np.convolve(audio, np.ones(3) / 3, mode="same")
        audio += np.random.default_rng(seed + 1000).normal(0, 0.01, len(audio)).astype(np.float32)
        audio = np.round(audio * 64) / 64
    return audio.astype(np.float32)
