"""Custom audio track for aiortc that serves PCM frames over WebRTC."""

import asyncio
import time
from fractions import Fraction

import numpy as np
from av import AudioFrame
from aiortc import MediaStreamTrack

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960  # 20ms at 48kHz
PTIME = FRAME_SAMPLES / SAMPLE_RATE  # 0.02 seconds


class WebRTCAudioSource(MediaStreamTrack):
    """A server-side audio track that streams silence or generator output.

    aiortc calls recv() on this track roughly every 20ms. We either
    return silence or pull a chunk from the attached audio generator.
    """

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._generator = None
        self._start_time = None
        self._frame_count = 0

    def set_generator(self, generator):
        """Attach an audio generator (must have a next_chunk() method)."""
        self._generator = generator

    def clear_generator(self):
        """Detach the generator — track reverts to silence."""
        self._generator = None

    async def recv(self) -> AudioFrame:
        """Called by aiortc to get the next audio frame."""
        if self._start_time is None:
            self._start_time = time.monotonic()

        target_time = self._start_time + self._frame_count * PTIME
        now = time.monotonic()
        if target_time > now:
            await asyncio.sleep(target_time - now)

        self._frame_count += 1

        if self._generator:
            chunk = self._generator.next_chunk()
            samples = np.frombuffer(chunk.samples, dtype=np.int16)
        else:
            samples = np.zeros(FRAME_SAMPLES, dtype=np.int16)

        frame = AudioFrame.from_ndarray(
            samples.reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = SAMPLE_RATE
        frame.pts = (self._frame_count - 1) * FRAME_SAMPLES
        frame.time_base = Fraction(1, SAMPLE_RATE)

        return frame
