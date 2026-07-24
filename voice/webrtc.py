"""Voice session — shared mic recording/STT and transport-backed TTS playback."""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import re
import threading
import time
import uuid

import httpx
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration

from voice.types import AudioChunk, PlaybackToken
from voice.audio.audio_queue import AudioQueue
from voice.audio.webrtc_audio_source import WebRTCAudioSource
from voice.text_chunker import clean_for_speech

FRAME_SAMPLES = 960  # 20ms at 48kHz
SAMPLE_RATE = 48000
MIC_PREROLL_FRAMES = 30  # 600ms: preserves the first word while VAD opens

log = logging.getLogger("webrtc")


class QueuedGenerator:
    """Reads PCM from an AudioQueue FIFO in 20ms chunks."""

    def __init__(self, queue: AudioQueue, session: "Session", token: PlaybackToken):
        self.queue = queue
        self.session = session
        self.token = token
        self._frame_sequence = 0

    def next_chunk(self) -> AudioChunk:
        pcm, payload_bytes = self.session.read_playback_frame(
            self.token, FRAME_SAMPLES * 2
        )
        chunk = AudioChunk(
            samples=pcm,
            sample_rate=SAMPLE_RATE,
            channels=1,
            payload_bytes=payload_bytes,
            utterance_id=self.token.utterance_id,
            generation=self.token.generation,
            frame_sequence=self._frame_sequence,
        )
        self._frame_sequence += 1
        return chunk

    def confirm(self, payload_bytes: int) -> None:
        """Confirm bytes handed to the active transport."""

        self.session.confirm_playback_bytes(self.token, payload_bytes)


class Session:
    """Manage one conversation's shared audio and turn-pipeline state.

    With no argument this constructs the original WebRTC source and peer
    connection.  A transport with the same source interface can instead drain
    TTS and feed PCM through :meth:`receive_mic_pcm`.
    """

    def __init__(self, audio_transport=None):
        self._pc = None
        self._audio_transport = audio_transport
        self._audio_source = audio_transport or WebRTCAudioSource()
        self._mic_sample_rate = getattr(audio_transport, "sample_rate", SAMPLE_RATE)
        self._playback_sample_rate = getattr(audio_transport, "sample_rate", SAMPLE_RATE)

        self._audio_queue = AudioQueue()
        self._tts_generator: QueuedGenerator | None = None
        # The Session is the sole allocator for a monotonically increasing,
        # per-conversation playback generation. Synthesis may finish on a
        # worker after cancellation, but only the event-loop admission step
        # can attach its PCM to the current token.
        self._playback_lock = threading.RLock()
        self._playback_generation = 0
        self._active_playback: PlaybackToken | None = None
        self._playback_chunks: list[dict] = []
        self._playback_enqueued_bytes = 0
        self._playback_confirmed_bytes = 0
        self._playback_receipts: dict[int, dict] = {}
        self._last_playback_receipt: dict | None = None
        self._late_audio_drops = 0

        # Mic recording state
        self._recording = False
        self._mic_frames: list[bytes] = []
        self._mic_preroll: deque[bytes] = deque(maxlen=MIC_PREROLL_FRAMES)
        self._mic_track = None
        self._mic_recv_task: asyncio.Task | None = None
        self._closed = False

        if audio_transport is not None:
            audio_transport.attach_session(self)

        # Selected voice for this session (browser default: Kokoro af_heart).
        self.voice_id = "af_heart"
        self.speed = 1.0

        # Pipeline settings: model + STT (Whisper) size for this session.
        self.model = ""       # "" → server uses its default
        self.stt_size = "base"
        self.analysis_style = "topic_map"
        self.speech_mode = (
            "prepared"
            if os.environ.get("NANO_CLAW_SPEECH_PREPARATION", "1").strip().lower()
            not in {"0", "false", "off", "no", "raw"}
            else "raw"
        )
        self.last_speech_plan = None

        self._paused = False
        self._stream_task: asyncio.Task | None = None
        self._turn: dict = {}
        # Deep analysis has a quiet handoff between terminal progress and the
        # first synthesized projection sentence. Browser VAD must not cancel
        # the completed result during that gap; manual Stop remains available.
        self._deep_projection_pending = False

        if audio_transport is None:
            self._pc = RTCPeerConnection(configuration=RTCConfiguration())

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
        if self._pc is None:
            raise RuntimeError("WebRTC offer received for a non-WebRTC session")
        self._pc.addTrack(self._audio_source)

        offer = RTCSessionDescription(sdp=sdp, type="offer")
        await self._pc.setRemoteDescription(offer)

        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        log.info("SDP answer created")
        return self._pc.localDescription.sdp

    def start_recording(self):
        """Start buffering mic audio, including the VAD trigger pre-roll."""
        self._mic_frames = list(self._mic_preroll)
        self._mic_preroll.clear()
        self._recording = True
        log.info(
            "Mic recording started (mic_track=%s, preroll_frames=%d)",
            "attached" if self._mic_track else "MISSING",
            len(self._mic_frames),
        )

    def cancel_recording(self):
        """Discard a partial hands-free turn without invoking STT or Claude."""
        self._recording = False
        self._mic_frames.clear()
        log.info("Mic recording cancelled")

    async def stop_recording(self) -> tuple[str, float, int | None]:
        """Stop recording and transcribe all captured audio.

        Returns:
            Tuple of (transcribed_text, audio_duration_seconds, stt_ms).
        """
        self._recording = False

        if not self._mic_frames:
            log.warning("No mic frames captured")
            return "", 0.0, None

        pcm_data = b"".join(self._mic_frames)
        self._mic_frames.clear()
        audio_duration_s = len(pcm_data) / (self._mic_sample_rate * 2)

        log.info("Mic recording stopped: %d bytes, %.2fs", len(pcm_data), audio_duration_s)

        stt_url = os.environ.get("STT_SERVICE_URL", "http://host.docker.internal:8200")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{stt_url}/transcribe",
                    content=pcm_data,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Sample-Rate": str(self._mic_sample_rate),
                        "X-Model-Size": self.stt_size,
                    },
                )
                result = resp.json()
                text = result.get("text", "")
                stt_ms = result.get("processing_ms")
        except Exception:
            log.exception("STT service call failed (is stt-service running on %s?)", stt_url)
            return "", 0.0, None
        return text, audio_duration_s, stt_ms

    def _conversation_id(self) -> str:
        value = getattr(self, "conversation_id", "")
        return value if isinstance(value, str) and value else "unbound"

    def _allocate_playback(self) -> PlaybackToken:
        """Allocate the only valid token for a new utterance."""

        with self._playback_lock:
            self._playback_generation += 1
            token = PlaybackToken(
                conversation_id=self._conversation_id(),
                utterance_id=f"utt-{uuid.uuid4().hex}",
                generation=self._playback_generation,
            )
            self._active_playback = token
            self._playback_chunks = []
            self._playback_enqueued_bytes = 0
            self._playback_confirmed_bytes = 0
            return token

    @property
    def active_playback(self) -> PlaybackToken | None:
        with self._playback_lock:
            return self._active_playback

    @property
    def last_playback_receipt(self) -> dict | None:
        with self._playback_lock:
            return self._last_playback_receipt

    @property
    def late_audio_drops(self) -> int:
        with self._playback_lock:
            return self._late_audio_drops

    def is_playback_current(self, token: PlaybackToken | None) -> bool:
        if token is None:
            return False
        with self._playback_lock:
            return token == self._active_playback

    def record_late_audio_drop(self, token: PlaybackToken | None) -> None:
        with self._playback_lock:
            self._late_audio_drops += 1
        if token is not None:
            log.info(
                "Dropped stale audio: utterance=%s generation=%d active=%s",
                token.utterance_id,
                token.generation,
                getattr(self._active_playback, "generation", None),
            )

    def read_playback_frame(
        self, token: PlaybackToken, frame_bytes: int
    ) -> tuple[bytes, int]:
        """Read one frame only while ``token`` is still current."""

        with self._playback_lock:
            if token != self._active_playback:
                self._late_audio_drops += 1
                return bytes(frame_bytes), 0
            return self._audio_queue.read_with_count(frame_bytes)

    def confirm_playback_bytes(
        self, token: PlaybackToken, payload_bytes: int
    ) -> bool:
        """Record PCM handed to the active transport, ignoring late confirms."""

        if payload_bytes <= 0:
            return False
        with self._playback_lock:
            if token != self._active_playback:
                self._late_audio_drops += 1
                return False
            self._playback_confirmed_bytes = min(
                self._playback_enqueued_bytes,
                self._playback_confirmed_bytes + payload_bytes,
            )
            return True

    def _build_playback_receipt(
        self, token: PlaybackToken, status: str, reason: str
    ) -> dict:
        confirmed = self._playback_confirmed_bytes
        chunks = []
        finished_count = 0
        started_count = 0
        for item in self._playback_chunks:
            delivered = max(
                0,
                min(confirmed, item["byte_end"]) - item["byte_start"],
            )
            if delivered >= item["byte_end"] - item["byte_start"]:
                chunk_status = "finished"
                finished_count += 1
                started_count += 1
            elif delivered > 0:
                chunk_status = "partial"
                started_count += 1
            else:
                chunk_status = "not_delivered"
            chunks.append(
                {
                    "chunk_id": item["chunk_id"],
                    "sequence": item["sequence"],
                    "audio_role": item["audio_role"],
                    "status": chunk_status,
                    "played_audio_ms": round(
                        delivered / (self._playback_sample_rate * 2) * 1000
                    ),
                }
            )
            if item.get("plan_sequence") is not None:
                chunks[-1]["plan_sequence"] = item["plan_sequence"]
        # Delivery is a playback fact, not a planner/cancellation assertion.
        # A cancellation after all bytes crossed the transport boundary is a
        # completed delivery; otherwise expose the exact partial boundary.
        if chunks and finished_count == len(chunks):
            status = "completed"
        elif started_count:
            status = "partial"
        else:
            status = "not_delivered"
        return {
            "event_type": "utterance_delivery_receipt",
            "conversation_id": token.conversation_id,
            "utterance_id": token.utterance_id,
            "generation": token.generation,
            "status": status,
            "reason": reason,
            "chunks": chunks,
            # NanoClaw's first slice is text_only. Riff will populate typed
            # act receipts when its FSM adopts SpeechEnvelopeV1.
            "acts": [],
        }

    def _finish_playback(
        self, token: PlaybackToken, status: str, reason: str
    ) -> dict:
        with self._playback_lock:
            prior = self._playback_receipts.get(token.generation)
            if prior is not None:
                return prior
            receipt = self._build_playback_receipt(token, status, reason)
            self._playback_receipts[token.generation] = receipt
            self._last_playback_receipt = receipt
            if token == self._active_playback:
                self._active_playback = None
            log.info(
                "Playback receipt: utterance=%s generation=%d status=%s "
                "reason=%s confirmed_bytes=%d enqueued_bytes=%d duration_ms=%d",
                token.utterance_id,
                token.generation,
                receipt["status"],
                reason,
                self._playback_confirmed_bytes,
                self._playback_enqueued_bytes,
                self._playback_enqueued_bytes // 2 * 1000 // 48000,
            )
            return receipt

    def _cancel_playback(self, reason: str) -> dict | None:
        """Invalidate the current generation before clearing queued PCM."""

        with self._playback_lock:
            token = self._active_playback
            receipt = (
                self._build_playback_receipt(token, "cancelled", reason)
                if token is not None
                else None
            )
            if token is not None:
                self._playback_receipts[token.generation] = receipt
                self._last_playback_receipt = receipt
                log.info(
                    "Playback receipt: utterance=%s generation=%d status=%s "
                    "reason=%s confirmed_bytes=%d enqueued_bytes=%d duration_ms=%d",
                    token.utterance_id,
                    token.generation,
                    receipt["status"],
                    reason,
                    self._playback_confirmed_bytes,
                    self._playback_enqueued_bytes,
                    self._playback_enqueued_bytes // 2 * 1000 // 48000,
                )
            # Advancing even on a stop with no active token makes cancellation
            # a tombstone that any racing producer necessarily fails.
            self._playback_generation += 1
            self._active_playback = None
            self._audio_source.clear_generator()
            self._audio_queue.clear()
            self._tts_generator = None
            self._paused = False
            return receipt

    def stop_speaking(self, reason: str = "stopped") -> dict | None:
        """Hard-stop TTS playback and invalidate its generation."""

        receipt = self._cancel_playback(reason)
        self._deep_projection_pending = False
        log.info("TTS playback stopped: reason=%s", reason)
        return receipt

    def set_stream_task(self, task) -> None:
        """Remember the task running the current streamed reply (for cancel)."""
        self._stream_task = task

    def is_paused(self) -> bool:
        return self._paused

    def pause_speaking(self) -> None:
        """Barge-in pause: go silent but KEEP the queued audio for resume."""
        if self.active_playback is None:
            return
        self._paused = True
        self._audio_source.clear_generator()
        log.info("Barge-in: paused (%d bytes retained)", self._audio_queue.available)

    def resume_speaking(self) -> None:
        """Resume a paused reply from where it stopped."""
        generator = self._tts_generator
        if generator is None or not self.is_playback_current(generator.token):
            self._paused = False
            log.info("Barge-in resume ignored: no current playback generation")
            return
        self._paused = False
        self._audio_source.set_generator(generator)
        log.info("Barge-in: resumed (%d bytes queued)", self._audio_queue.available)

    def cancel_stream(self, reason: str = "barge_in") -> dict | None:
        """Committed barge-in: discard the reply audio + abort its stream task."""
        receipt = self._cancel_playback(reason)
        self._deep_projection_pending = False
        if (self._stream_task and self._stream_task is not asyncio.current_task()
                and not self._stream_task.done()):
            self._stream_task.cancel()
        self._stream_task = None
        log.info("Playback cancelled: reason=%s", reason)
        return receipt

    def set_voice(self, voice_id: str, speed: float):
        """Update the voice + speed used for subsequent replies."""
        if voice_id:
            self.voice_id = voice_id
        try:
            self.speed = max(0.5, min(2.0, float(speed)))
        except (TypeError, ValueError):
            pass
        log.info("Voice set: %s (speed=%.2f)", self.voice_id, self.speed)

    def set_speech_mode(self, mode: str) -> None:
        """Select the reversible raw/prepared path for subsequent turns."""

        if mode not in {"raw", "prepared"}:
            raise ValueError("speech mode must be raw or prepared")
        self.speech_mode = mode
        log.info("Speech preparation mode set: %s", mode)

    def prepare_speech(self, text: str):
        """Compile a complete text-only plan when prepared mode is active."""

        if self.speech_mode != "prepared":
            self.last_speech_plan = None
            return None
        from voice.speech_preparer import compile_speech

        try:
            max_words = int(os.environ.get("NANO_CLAW_SPEECH_MAX_WORDS", "18"))
        except ValueError:
            max_words = 18
        try:
            max_duration = int(
                os.environ.get("NANO_CLAW_SPEECH_MAX_CHUNK_MS", "2500")
            )
        except ValueError:
            max_duration = 2500
        self.last_speech_plan = compile_speech(
            text,
            max_words_per_chunk=max_words,
            max_chunk_duration_ms=max_duration,
        )
        return self.last_speech_plan

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """Use the same canonical cleanup as incremental streaming speech."""
        text = clean_for_speech(text)
        text = re.sub(r'\n{2,}', '. ', text)
        text = re.sub(r'\n', ' ', text)
        text = re.sub(r'\.{2,}', '.', text)
        return text.strip()

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences for incremental TTS."""
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        return [p for p in parts if p.strip()]

    def begin_stream(self, audio_role: str = "answer") -> PlaybackToken:
        """Start one playable utterance and return its generation token."""

        if self.active_playback is not None:
            self._cancel_playback("superseded")
        else:
            self._audio_source.clear_generator()
            self._audio_queue.clear()
            self._paused = False
        token = self._allocate_playback()
        generator = QueuedGenerator(self._audio_queue, self, token)
        with self._playback_lock:
            self._tts_generator = generator
            self._default_audio_role = audio_role
        self._audio_source.set_generator(generator)
        log.info(
            "Playback started: utterance=%s generation=%d role=%s",
            token.utterance_id,
            token.generation,
            audio_role,
        )
        return token

    @staticmethod
    def synthesize_chunk(
        text: str,
        voice_id: str = "",
        speed: float = 1.0,
        pause_after_ms: int | None = None,
    ) -> bytes:
        """Synthesize PCM only; this worker-safe step never mutates playback."""

        from voice.tts import synthesize

        return synthesize(text, voice_id, speed, pause_after_ms)

    def enqueue_synthesized_chunk(
        self,
        token: PlaybackToken,
        pcm_48k: bytes,
        *,
        audio_role: str | None = None,
        chunk_id: str | None = None,
        plan_sequence: int | None = None,
    ) -> int:
        """Atomically admit synthesized PCM only for the active generation."""

        prepare_tts = getattr(self._audio_source, "prepare_tts", None)
        pcm = prepare_tts(pcm_48k) if prepare_tts is not None else pcm_48k
        if not pcm:
            return 0
        with self._playback_lock:
            if token != self._active_playback:
                self._late_audio_drops += 1
                log.info(
                    "Rejected late synthesis: utterance=%s generation=%d",
                    token.utterance_id,
                    token.generation,
                )
                return 0
            byte_start = self._playback_enqueued_bytes
            byte_end = byte_start + len(pcm)
            sequence = len(self._playback_chunks)
            requested_chunk_id = chunk_id or f"chunk_{sequence}"
            used_chunk_ids = {
                item.get("chunk_id") for item in self._playback_chunks
            }
            resolved_chunk_id = requested_chunk_id
            if resolved_chunk_id in used_chunk_ids:
                resolved_chunk_id = f"{requested_chunk_id}_{sequence}"
            self._playback_chunks.append(
                {
                    "chunk_id": resolved_chunk_id,
                    "sequence": sequence,
                    "plan_sequence": plan_sequence,
                    "audio_role": audio_role
                    or getattr(self, "_default_audio_role", "answer"),
                    "byte_start": byte_start,
                    "byte_end": byte_end,
                }
            )
            self._playback_enqueued_bytes = byte_end
            self._audio_queue.enqueue(pcm)
            return len(pcm)

    def enqueue_generated_pcm(
        self,
        token: PlaybackToken,
        pcm_48k: bytes,
        *,
        audio_role: str = "processing_earcon",
    ) -> int:
        """Admit generated non-speech PCM under the same generation fence."""

        return self.enqueue_synthesized_chunk(
            token, pcm_48k, audio_role=audio_role
        )

    def enqueue_chunk(self, text: str, voice_id: str = "", speed: float = 1.0) -> int:
        """Compatibility path; production callers should split work/admission."""

        token = self.active_playback
        if token is None:
            return 0
        pcm_48k = self.synthesize_chunk(text, voice_id, speed)
        return self.enqueue_synthesized_chunk(token, pcm_48k)

    def enqueue_pcm(self, pcm_48k: bytes) -> int:
        """Compatibility path for generated PCM on the current generation."""

        token = self.active_playback
        if token is None:
            return 0
        return self.enqueue_generated_pcm(token, pcm_48k)

    async def end_stream(
        self, total_bytes: int, token: PlaybackToken | None = None
    ) -> dict | None:
        """Wait for transport confirmation, then emit a delivery receipt."""

        token = token or self.active_playback
        if token is None:
            return self.last_playback_receipt
        loop = asyncio.get_running_loop()
        playback_seconds = total_bytes / (self._playback_sample_rate * 2)
        budget = max(5.0, min(120.0, playback_seconds + 5.0))
        deadline = loop.time() + budget
        timed_out = False
        while not self._closed and self.is_playback_current(token):
            with self._playback_lock:
                queue_empty = self._audio_queue.available <= 0
                transport_confirmed = (
                    self._playback_confirmed_bytes >= self._playback_enqueued_bytes
                )
            if queue_empty and transport_confirmed:
                break
            await asyncio.sleep(0.02)
            if self._paused:
                # Freeze the countdown while paused (extend the deadline).
                deadline += 0.02
                continue
            if loop.time() >= deadline:
                timed_out = True
                break
        if not self.is_playback_current(token):
            return self._playback_receipts.get(token.generation)
        if timed_out and not self._paused:
            log.warning(
                "TTS playback confirmation timed out with %d bytes queued",
                self._audio_queue.available,
            )
        # The last WebRTC frame has left the queue but can still be in the
        # browser audio buffer. Keep the mic gate closed through that tail.
        await asyncio.sleep(0.15)
        if self.is_playback_current(token) and not self._paused:
            self._audio_source.clear_generator()
        receipt = self._finish_playback(
            token,
            "completed" if not timed_out else "partial",
            "playback_finished" if not timed_out else "delivery_timeout",
        )
        if timed_out:
            self._audio_queue.clear()
        return receipt

    async def speak_text(
        self,
        text: str,
        voice_id: str = "",
        speed: float = 1.0,
        token: PlaybackToken | None = None,
    ) -> float | None:
        """Whole-text path (non-streaming fallback): clean, split, enqueue, drain."""
        token = token or self.begin_stream()
        try:
            plan = self.prepare_speech(text)
        except Exception:
            self.last_speech_plan = None
            plan = None
            log.exception("Speech preparation failed; using the raw fallback")
        if plan is not None:
            return await self.speak_plan(plan, voice_id, speed, token=token)
        text = self._clean_for_speech(text)
        sentences = self._split_sentences(text)
        loop = asyncio.get_running_loop()
        total_bytes = 0
        first_audio = None
        for sentence in sentences:
            pcm = await loop.run_in_executor(
                None, self.synthesize_chunk, sentence, voice_id, speed
            )
            queued_bytes = self.enqueue_synthesized_chunk(token, pcm)
            total_bytes += queued_bytes
            if queued_bytes and first_audio is None:
                first_audio = time.monotonic()
        await self.end_stream(total_bytes, token)
        return first_audio

    async def speak_plan(
        self,
        plan,
        voice_id: str = "",
        speed: float = 1.0,
        *,
        token: PlaybackToken | None = None,
    ) -> float | None:
        """Synthesize a complete validated plan in its declared order."""

        token = token or self.begin_stream()
        loop = asyncio.get_running_loop()
        total_bytes = 0
        first_audio = None
        for chunk in plan.chunks:
            pcm = await loop.run_in_executor(
                None,
                self.synthesize_chunk,
                chunk.text,
                voice_id,
                speed,
                chunk.pause_after_ms,
            )
            queued_bytes = self.enqueue_synthesized_chunk(
                token,
                pcm,
                chunk_id=chunk.chunk_id,
                plan_sequence=chunk.sequence,
            )
            total_bytes += queued_bytes
            if queued_bytes and first_audio is None:
                first_audio = time.monotonic()
        await self.end_stream(total_bytes, token)
        return first_audio

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

            arr = frame.to_ndarray()
            if arr.dtype in (np.float32, np.float64):
                arr = (arr * 32767).clip(-32768, 32767).astype(np.int16)
            flat = arr.flatten()
            channels = flat.shape[0] // frame.samples
            if channels > 1:
                flat = flat[::channels]
            pcm = flat.astype(np.int16).tobytes()
            self.receive_mic_pcm(pcm)

    def receive_mic_pcm(self, pcm: bytes) -> None:
        """Accumulate transport-provided PCM through the shared mic path."""

        if not pcm:
            return
        if len(pcm) % 2:
            raise ValueError("mic PCM16 must contain complete samples")
        frame = bytes(pcm)
        if self._recording:
            self._mic_frames.append(frame)
        else:
            self._mic_preroll.append(frame)

    async def close(self):
        """Tear down the peer connection."""
        if self._closed:
            return
        self._closed = True
        self._recording = False
        self._cancel_playback("disconnect")
        if self._mic_recv_task:
            self._mic_recv_task.cancel()
        if self._audio_transport is not None:
            await self._audio_transport.close()
        elif self._pc is not None:
            self._audio_source.clear_generator()
            await self._pc.close()
        log.info("Session closed")
