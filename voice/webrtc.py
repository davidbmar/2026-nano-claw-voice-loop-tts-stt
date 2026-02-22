"""WebRTC session — PeerConnection lifecycle, mic recording, TTS playback."""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration

from voice.types import AudioChunk
from voice.audio.audio_queue import AudioQueue
from voice.audio.webrtc_audio_source import WebRTCAudioSource

FRAME_SAMPLES = 960  # 20ms at 48kHz
SAMPLE_RATE = 48000

log = logging.getLogger("webrtc")


class QueuedGenerator:
    """Reads PCM from an AudioQueue FIFO in 20ms chunks."""

    def __init__(self, queue: AudioQueue):
        self.queue = queue

    def next_chunk(self) -> AudioChunk:
        pcm = self.queue.read(FRAME_SAMPLES * 2)  # 2 bytes per int16 sample
        return AudioChunk(samples=pcm, sample_rate=SAMPLE_RATE, channels=1)


class Session:
    """Manages one WebRTC peer connection and its audio track."""

    def __init__(self):
        self._pc = RTCPeerConnection(configuration=RTCConfiguration())
        self._audio_source = WebRTCAudioSource()

        self._audio_queue = AudioQueue()
        self._tts_generator = QueuedGenerator(self._audio_queue)

        # Mic recording state
        self._recording = False
        self._mic_frames: list[bytes] = []
        self._mic_track = None
        self._mic_recv_task: asyncio.Task | None = None
        self._closed = False

        @self._pc.on("connectionstatechange")
        async def on_conn_state():
            log.info("Connection state: %s", self._pc.connectionState)

        @self._pc.on("track")
        async def on_track(track):
            if track.kind != "audio":
                return
            log.info("Received remote audio track from browser mic")
            self._mic_track = track
            self._mic_recv_task = asyncio.ensure_future(self._recv_mic_audio(track))

    async def handle_offer(self, sdp: str) -> str:
        """Process client SDP offer, return SDP answer."""
        self._pc.addTrack(self._audio_source)

        offer = RTCSessionDescription(sdp=sdp, type="offer")
        await self._pc.setRemoteDescription(offer)

        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        log.info("SDP answer created")
        return self._pc.localDescription.sdp

    def start_recording(self):
        """Start buffering incoming mic audio frames."""
        self._mic_frames.clear()
        self._recording = True
        log.info("Mic recording started (mic_track=%s)",
                 "attached" if self._mic_track else "MISSING")

    async def stop_recording(self) -> tuple[str, float]:
        """Stop recording and transcribe all captured audio.

        Returns:
            Tuple of (transcribed_text, audio_duration_seconds).
        """
        self._recording = False

        if not self._mic_frames:
            log.warning("No mic frames captured")
            return "", 0.0

        pcm_data = b"".join(self._mic_frames)
        self._mic_frames.clear()
        audio_duration_s = len(pcm_data) / (SAMPLE_RATE * 2)

        log.info("Mic recording stopped: %d bytes, %.2fs", len(pcm_data), audio_duration_s)

        stt_url = os.environ.get("STT_SERVICE_URL", "http://host.docker.internal:8200")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{stt_url}/transcribe",
                    content=pcm_data,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Sample-Rate": str(SAMPLE_RATE),
                    },
                )
                result = resp.json()
                text = result.get("text", "")
        except Exception:
            log.exception("STT service call failed (is stt-service running on %s?)", stt_url)
            text = ""
        return text, audio_duration_s

    def stop_speaking(self):
        """Stop TTS playback — clear the audio queue."""
        self._audio_queue.clear()
        self._audio_source.clear_generator()
        log.info("TTS playback stopped")

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """Strip markdown formatting so TTS reads clean prose."""
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
        text = re.sub(r'\*{1,3}', '', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        text = re.sub(r'https?://\S+', '', text)
        text = re.sub(r'`(.+?)`', r'\1', text)
        text = re.sub(r'\n{2,}', '. ', text)
        text = re.sub(r'\n', ' ', text)
        text = re.sub(r'\s{2,}', ' ', text)
        text = re.sub(r'\.{2,}', '.', text)
        return text.strip()

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences for incremental TTS."""
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        return [p for p in parts if p.strip()]

    async def speak_text(self, text: str, voice_id: str = ""):
        """Run TTS sentence-by-sentence and enqueue audio."""
        from voice.tts import synthesize

        self._audio_source.set_generator(self._tts_generator)

        text = self._clean_for_speech(text)
        sentences = self._split_sentences(text)
        log.info("TTS: %d sentences to synthesize", len(sentences))

        loop = asyncio.get_running_loop()
        for i, sentence in enumerate(sentences):
            pcm_48k = await loop.run_in_executor(None, synthesize, sentence, voice_id)
            if pcm_48k:
                self._audio_queue.enqueue(pcm_48k)
                log.debug("TTS sentence %d/%d enqueued: %d bytes",
                          i + 1, len(sentences), len(pcm_48k))

    async def _recv_mic_audio(self, track):
        """Background task: continuously receive audio frames from browser mic."""
        logged_format = False
        while True:
            try:
                frame = await track.recv()
            except Exception:
                log.info("Mic track ended")
                break

            if not logged_format:
                arr = frame.to_ndarray()
                log.info("Mic frame format=%s rate=%d samples=%d shape=%s",
                         frame.format.name, frame.sample_rate, frame.samples, arr.shape)
                logged_format = True

            if self._recording:
                arr = frame.to_ndarray()
                if arr.dtype in (np.float32, np.float64):
                    arr = (arr * 32767).clip(-32768, 32767).astype(np.int16)
                flat = arr.flatten()
                channels = flat.shape[0] // frame.samples
                if channels > 1:
                    flat = flat[::channels]
                pcm = flat.astype(np.int16).tobytes()
                self._mic_frames.append(pcm)

    async def close(self):
        """Tear down the peer connection."""
        if self._closed:
            return
        self._closed = True
        self._recording = False
        self._audio_source.clear_generator()
        if self._mic_recv_task:
            self._mic_recv_task.cancel()
        await self._pc.close()
        log.info("Session closed")
