"""Transport frames are always whole 20ms — no sub-frame payloads.

Chunk PCM lengths are arbitrary (speech + jittered pause), so the final
frame used to be a random partial (measured 0.7-19.7ms on a live call; a
0.7ms l16 payload is 22 bytes). Sub-frame payloads are carrier-hostile:
playout pads or glitches them into audible ticks at every chunk end —
exactly the clicking reported on 2026-07-27 calls, absent from our own
outbound tap. Chunk ends are declick-faded to silence, so zero-padding
the last frame is inaudible by construction.
"""

import numpy as np

from voice.phone_audio import pcm48k_to_l16_frames, pcm48k_to_ulaw_frames


def _pcm48(ms):
    return (np.ones(48_000 * ms // 1000, dtype=np.int16) * 100).tobytes()


def test_l16_frames_are_all_full_size():
    frames = pcm48k_to_l16_frames(_pcm48(47))  # 47ms → 2 full + partial
    assert frames
    assert all(len(f) == 320 * 2 for f in frames)
    # The pad is silence, not garbage.
    tail = np.frombuffer(frames[-1], dtype=np.int16)
    assert tail[-1] == 0


def test_ulaw_frames_are_all_full_size():
    frames = pcm48k_to_ulaw_frames(_pcm48(47))
    assert frames
    assert all(len(f) == 160 for f in frames)


def test_exact_multiple_needs_no_pad():
    frames = pcm48k_to_l16_frames(_pcm48(40))  # exactly 2 frames
    assert len(frames) == 2
    assert all(len(f) == 320 * 2 for f in frames)


def test_empty_input_yields_no_frames():
    assert pcm48k_to_l16_frames(b"") == []
    assert pcm48k_to_ulaw_frames(b"") == []
