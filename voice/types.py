"""Shared data types for the voice pipeline."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlaybackToken:
    """Opaque identity for one playable utterance generation."""

    conversation_id: str
    utterance_id: str
    generation: int


@dataclass
class AudioChunk:
    """A chunk of raw PCM audio data."""
    samples: bytes          # 16-bit signed LE PCM
    sample_rate: int = 48000
    channels: int = 1
    payload_bytes: int = 0
    utterance_id: str = ""
    generation: int = 0
    frame_sequence: int = 0
