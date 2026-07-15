"""Randomized exponential backoff for barge-in false-alarm resumes.

Each consecutive false alarm waits longer (full jitter up to a growing
ceiling) so persistent noise backs off toward the cap instead of causing
rapid pause/resume stutter. A clean reply drain (or a committed barge-in)
calls reset().
"""

from __future__ import annotations

import random


class Backoff:
    def __init__(self, base: float = 0.5, factor: float = 2.0, cap: float = 8.0):
        self._base = base
        self._factor = factor
        self._cap = cap
        self._n = 0

    @property
    def attempts(self) -> int:
        return self._n

    def next(self) -> float:
        """Return a full-jitter delay in [0, ceiling] and advance the counter."""
        uncapped = self._base * (self._factor ** self._n)
        ceiling = min(self._cap, uncapped)
        if uncapped < self._cap:
            self._n += 1
        return random.uniform(0.0, ceiling)

    def reset(self) -> None:
        self._n = 0
