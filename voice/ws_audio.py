"""PCM16 browser audio transport carried by the main application WebSocket.

This mirrors the media-WebSocket structure proven by ``voice.phone`` while
keeping browser audio on the already-authenticated ``/ws`` connection.  The
mic remains at the STT-native 16 kHz rate while agent TTS stays at its native
48 kHz rate for full-band browser playback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voice.webrtc import QueuedGenerator, Session


log = logging.getLogger("ws-audio")

PCM_FORMAT = "pcm_s16le"
CHANNELS = 1
FRAME_DURATION_SECONDS = 0.020
MIC_SAMPLE_RATE = 16_000
MIC_FRAME_SAMPLES = int(MIC_SAMPLE_RATE * FRAME_DURATION_SECONDS)
MIC_FRAME_BYTES = MIC_FRAME_SAMPLES * 2
AGENT_SAMPLE_RATE = 48_000
AGENT_FRAME_SAMPLES = int(AGENT_SAMPLE_RATE * FRAME_DURATION_SECONDS)
AGENT_FRAME_BYTES = AGENT_FRAME_SAMPLES * 2
OUTBOUND_PREBUFFER_FRAMES = 5


class WsAudioFormatError(ValueError):
    """The browser announced or sent an unsupported microphone format."""


def wire_format() -> dict[str, dict[str, int | str]]:
    """Return the fixed mic and agent PCM formats advertised to the browser."""

    mic = {
        "format": PCM_FORMAT,
        "sampleRate": MIC_SAMPLE_RATE,
        "channels": CHANNELS,
        "frameSamples": MIC_FRAME_SAMPLES,
    }
    agent = {
        "format": PCM_FORMAT,
        "sampleRate": AGENT_SAMPLE_RATE,
        "channels": CHANNELS,
        "frameSamples": AGENT_FRAME_SAMPLES,
    }
    return {"mic": mic, "agent": agent}


class WsAudioTransport:
    """Bidirectional PCM transport over one authenticated application socket.

    Incoming binary messages are handed to ``Session.receive_mic_pcm``.  For
    output, the class presents the same ``set_generator`` / ``clear_generator``
    interface as ``WebRTCAudioSource`` so the rest of ``Session`` does not care
    which transport drains its TTS queue.
    """

    sample_rate = MIC_SAMPLE_RATE
    frame_samples = MIC_FRAME_SAMPLES
    playback_sample_rate = AGENT_SAMPLE_RATE
    playback_frame_samples = AGENT_FRAME_SAMPLES

    def __init__(self, ws: Any):
        self.ws = ws
        self._session: Session | None = None
        self._mic_started = False
        self._generator: QueuedGenerator | None = None
        self._pump_task: asyncio.Task | None = None
        self._closed = False

    def attach_session(self, session: Session) -> None:
        """Bind PCM ingress to the server-created session for this socket."""

        if self._session is not None and self._session is not session:
            raise RuntimeError("WebSocket audio transport is already attached")
        self._session = session
        # Session's legacy transport seam initializes both directions from
        # sample_rate. Correct its playback clock while leaving mic/STT at 16 kHz.
        session._playback_sample_rate = self.playback_sample_rate

    def start_mic(self, message: dict[str, Any]) -> None:
        """Validate the mandatory format announcement before accepting PCM."""

        expected = wire_format()["mic"]
        announced = {
            "format": message.get("format"),
            "sampleRate": message.get("sampleRate"),
            "channels": message.get("channels"),
            "frameSamples": message.get("frameSamples"),
        }
        if announced != expected:
            raise WsAudioFormatError(
                f"unsupported mic audio format: received {announced!r}, "
                f"expected {expected!r}"
            )
        self._mic_started = True
        log.info(
            "WebSocket mic ready: %s mono %d Hz, %d samples/frame",
            PCM_FORMAT,
            MIC_SAMPLE_RATE,
            MIC_FRAME_SAMPLES,
        )

    def receive_mic_frame(self, data: bytes | bytearray | memoryview) -> None:
        """Feed one browser PCM frame into Session's shared mic accumulator."""

        if not self._mic_started:
            raise WsAudioFormatError("mic audio received before mic_audio_start")
        if self._session is None:
            raise RuntimeError("WebSocket audio transport has no session")
        pcm = bytes(data)
        if not pcm or len(pcm) % 2:
            raise WsAudioFormatError(
                "mic audio frame must contain non-empty, complete PCM16 samples"
            )
        if len(pcm) != MIC_FRAME_BYTES:
            raise WsAudioFormatError(
                f"mic audio frame must be exactly {MIC_FRAME_BYTES} bytes"
            )
        self._session.receive_mic_pcm(pcm)

    def prepare_tts(self, pcm_48k: bytes) -> bytes:
        """Keep synthesized 48 kHz PCM16 unchanged for full-band playback."""

        return pcm_48k

    def set_generator(self, generator: QueuedGenerator) -> None:
        """Attach Session's queue and start its paced binary-frame pump."""

        if self._closed:
            return
        self.clear_generator()
        self._generator = generator
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pump_task = loop.create_task(self._pump(generator))

    def clear_generator(self) -> None:
        """Detach output immediately, retaining any unread Session queue bytes."""

        self._generator = None
        task = self._pump_task
        self._pump_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _pump(self, generator: QueuedGenerator) -> None:
        """Drain audio in 20 ms frames with a small browser scheduling lead."""

        loop = asyncio.get_running_loop()
        paced_frames = 0
        next_deadline: float | None = None
        try:
            while self._generator is generator and not self._closed:
                if generator.queue.available <= 0:
                    # A later synthesized sentence starts a fresh browser
                    # prebuffer instead of trying to catch up to stale pacing.
                    paced_frames = 0
                    next_deadline = None
                    await asyncio.sleep(0.005)
                    continue

                if next_deadline is None:
                    next_deadline = loop.time()
                if paced_frames >= OUTBOUND_PREBUFFER_FRAMES:
                    next_deadline += FRAME_DURATION_SECONDS
                    await asyncio.sleep(max(0.0, next_deadline - loop.time()))

                if self._generator is not generator or self._closed:
                    break
                chunk = generator.next_chunk()
                if not generator.session.is_playback_current(generator.token):
                    generator.session.record_late_audio_drop(generator.token)
                    break
                await self.ws.send_bytes(chunk.samples)
                generator.confirm(chunk.payload_bytes)
                paced_frames += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WebSocket agent audio pump failed")

    async def close(self) -> None:
        """Stop both directions and await cancellation of the output pump."""

        if self._closed:
            return
        self._closed = True
        self._mic_started = False
        task = self._pump_task
        self.clear_generator()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
