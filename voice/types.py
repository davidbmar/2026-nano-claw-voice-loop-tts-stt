"""Shared data types for the voice pipeline."""

from dataclasses import dataclass


@dataclass
class AudioChunk:
    """A chunk of raw PCM audio data."""
    samples: bytes          # 16-bit signed LE PCM
    sample_rate: int = 48000
    channels: int = 1
