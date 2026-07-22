"""Small generated earcons used while a bounded deep task is running."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

SAMPLE_RATE = 48_000
CHIME_SECONDS = 0.36
PEAK_AMPLITUDE = 1800.0  # about -25 dBFS: present but safely below speech


@lru_cache(maxsize=1)
def processing_chime() -> bytes:
    """Return a soft two-note PCM16 chime with click-free attack and decay."""

    sample_count = int(SAMPLE_RATE * CHIME_SECONDS)
    time = np.arange(sample_count, dtype=np.float64) / SAMPLE_RATE
    attack = np.minimum(1.0, time / 0.018)
    decay = np.exp(-7.5 * time)
    first = np.sin(2.0 * np.pi * 520.0 * time)

    second_start = 0.13
    shifted = np.maximum(0.0, time - second_start)
    second_gate = (time >= second_start).astype(np.float64)
    second_attack = np.minimum(1.0, shifted / 0.014)
    second_decay = np.exp(-9.0 * shifted)
    second = np.sin(2.0 * np.pi * 720.0 * shifted) * second_gate

    signal = 0.62 * first * attack * decay + 0.48 * second * second_attack * second_decay
    return np.clip(signal * PEAK_AMPLITUDE, -32768, 32767).astype(np.int16).tobytes()
