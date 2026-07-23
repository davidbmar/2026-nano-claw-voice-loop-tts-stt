"""Telnyx phone gateway: callers dial in and talk to the nano-claw agent.

Call flow (mirrors riff's proven shape, minus the flow engine):

    caller → Telnyx Call Control app → POST /api/phone/incoming (webhook)
           → answer_with_streaming() → Telnyx opens WS to /ws/phone-media
           → PCMU 8k or L16 16k frames in → UtteranceEndpointer → STT service
           → nano-claw /api/chat (knowledge persona, tools disabled)
           → TTS 48k PCM → configured phone codec → caller hears the answer

Enabled only when NANO_CLAW_PHONE=1. Required env:
    TELNYX_API_KEY                  answer/hangup Call Control commands
    NANO_CLAW_PHONE_WEBHOOK_BASE    public https base (e.g. https://nano.example.com)
    NANO_CLAW_PHONE_TOKEN           shared secret segment in webhook/media URLs;
                                    requests without it are rejected (we do not
                                    verify Telnyx Ed25519 signatures yet — the
                                    token-in-URL is the auth boundary)
Optional:
    NANO_CLAW_PHONE_GREETING        spoken on answer
    NANO_CLAW_PHONE_VOICE           TTS voice id (default af_heart; use a
                                    Piper voice on nodes where Kokoro/MPS
                                    is slow or unstable)
    NANO_CLAW_PHONE_STT_SIZE        Whisper size for phone turns (default
                                    base; "tiny" for low-powered nodes)
    NANO_CLAW_PHONE_CODEC           pcmu (default) or l16 (16 kHz wideband)
    NANO_CLAW_PHONE_RMS_MIN         minimum energy endpoint threshold
    NANO_CLAW_PHONE_RMS_RATIO       noise-floor multiplier for endpointing
    NANO_CLAW_PHONE_GAIN            off = bypass outbound peak normalization
    NANO_CLAW_PHONE_GAIN_TARGET_DB  target peak dBFS (default -3)
    NANO_CLAW_PHONE_PREBUFFER_MS    initial unpaced audio burst (default 200)
    NANO_CLAW_PHONE_PACE_FACTOR     frame interval multiplier (default 1.0)
    NANO_CLAW_PHONE_BARGE_IN        1 = caller can interrupt the agent
                                    mid-speech (buffer-flush via Telnyx
                                    "clear"); unset = half-duplex
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import numpy as np
from aiohttp import web

from voice import metrics_db, silero_vad
from voice.flow_session import FlowSession, get_flow_mode
from voice.phone_audio import (
    FRAME_MS,
    BargeInDetector,
    DEFAULT_PHONE_GAIN_TARGET_DBFS,
    SentencePeakNormalizer,
    UtteranceEndpointer,
    pcm48k_to_l16_frames,
    pcm48k_to_ulaw_frames,
    transcript_looks_incomplete,
    ulaw_decode,
)
from voice.phone_tap import CallTap
from voice.processing_audio import processing_chime
from voice.sentence_pipeline import SentencePipeline
from voice.speech_preparer import (
    SPEECH_COMPILER_VERSION,
    SpeechChunk,
    compile_speech,
)
from voice.text_chunker import TextChunker
from voice.tts import synthesize as tts_synthesize

log = logging.getLogger("nano-claw.phone")

NANO_CLAW_URL = os.environ.get("NANO_CLAW_URL", "http://localhost:3001")
TELNYX_API = "https://api.telnyx.com/v2"

DEFAULT_GREETING = (
    "You've reached Space Channel. Ask me about rocket launches, "
    "U F O cases, space news, podcasts, or live shows."
)
IDLE_PROMPT_TEXT = "Hi — are you still there?"
IDLE_GOODBYE_TEXT = "It sounds like you've stepped away. Thanks for calling Space Channel — goodbye!"
MAX_BUFFERED_INBOUND_FRAMES = 30_000 // FRAME_MS
FRAME_S = FRAME_MS / 1000.0
DEFAULT_PHONE_PREBUFFER_MS = 200.0
DEFAULT_PHONE_PACE_FACTOR = 1.0
PROCESSING_CUE_SENTINEL = "\0nano-claw-processing-cue\0"


class FramePacer:
    """Anchor real-time frame sends to monotonic absolute deadlines.

    Relative sleeps add each send's work and scheduler oversleep to every
    later frame, so jitter becomes permanent drift.  This pacer advances one
    absolute deadline by ``frame_s * pace_factor`` per frame; a late wake-up
    therefore shortens or skips following sleeps until the schedule catches
    up.

    The old phone loop used a 0.9 interval to keep Telnyx fed, but that made
    buffered surplus grow throughout a reply.  ``prebuffer_ms`` supplies the
    same safety headroom once, immediately after :meth:`reset`, while a 1.0
    factor holds the buffer steady.  Reuse one reset pacer for every sentence
    in a reply so sentence boundaries cannot trigger another prebuffer burst.
    """

    def __init__(
        self,
        frame_s: float = FRAME_S,
        *,
        prebuffer_ms: float = DEFAULT_PHONE_PREBUFFER_MS,
        pace_factor: float = DEFAULT_PHONE_PACE_FACTOR,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not math.isfinite(frame_s) or frame_s <= 0.0:
            raise ValueError("frame_s must be finite and positive")
        if not math.isfinite(prebuffer_ms) or prebuffer_ms < 0.0:
            raise ValueError("prebuffer_ms must be finite and non-negative")
        if not math.isfinite(pace_factor) or pace_factor <= 0.0:
            raise ValueError("pace_factor must be finite and positive")
        self.frame_s = float(frame_s)
        self.prebuffer_ms = float(prebuffer_ms)
        self.pace_factor = float(pace_factor)
        self._clock = clock or time.monotonic
        self._deadline: float | None = None

    @property
    def running(self) -> bool:
        """Whether :meth:`reset` has anchored this reply's schedule."""
        return self._deadline is not None

    def reset(self) -> None:
        """Anchor a new reply and make its configured audio headroom due now."""
        prebuffer_s = self.prebuffer_ms / 1000.0
        self._deadline = self._clock() - prebuffer_s * self.pace_factor

    def now(self) -> float:
        """Read the monotonic clock used by this deadline sequence."""
        return self._clock()

    def next_deadline(self) -> float:
        """Return the next frame's absolute monotonic send deadline."""
        if self._deadline is None:
            raise RuntimeError("FramePacer.reset() must be called before pacing")
        self._deadline += self.frame_s * self.pace_factor
        return self._deadline


@dataclass(frozen=True)
class _SynthesizedSpeech:
    pcm48k: bytes
    tap: CallTap | None
    sentence_index: int | None


def idle_action(idle_s: float, prompted: bool, prompt_after_s: float) -> str:
    """Pure idle-policy decision: '', 'prompt', or 'hangup'.

    One prompt per silence stretch; a further full stretch after the prompt
    (still nothing) means the caller is gone.
    """
    if idle_s < prompt_after_s:
        return ""
    return "hangup" if prompted else "prompt"


# Runtime overrides set from the web UI (/api/phone/config). Checked before
# the environment so changes apply live — voice mid-call on the next sentence,
# model on the next turn. In-memory only: a container restart falls back to
# the .env values.
_overrides: dict[str, str] = {}


def _cfg(name: str, default: str = "") -> str:
    if name in _overrides:
        return _overrides[name].strip()
    return os.environ.get(name, default).strip()


def _phone_pacing_value(name: str, default: float, *, allow_zero: bool) -> float:
    """Read one finite pacing value, falling back on unsafe input."""
    try:
        value = float(_cfg(name, str(default)))
    except ValueError:
        return default
    minimum_ok = value >= 0.0 if allow_zero else value > 0.0
    return value if math.isfinite(value) and minimum_ok else default


def _phone_frame_pacer(*, clock: Callable[[], float] | None = None) -> FramePacer:
    """Build a reply pacer from the live phone environment/override config."""
    return FramePacer(
        prebuffer_ms=_phone_pacing_value(
            "NANO_CLAW_PHONE_PREBUFFER_MS",
            DEFAULT_PHONE_PREBUFFER_MS,
            allow_zero=True,
        ),
        pace_factor=_phone_pacing_value(
            "NANO_CLAW_PHONE_PACE_FACTOR",
            DEFAULT_PHONE_PACE_FACTOR,
            allow_zero=False,
        ),
        clock=clock,
    )


def phone_codec() -> str:
    """'pcmu' (default, 8 kHz μ-law) or 'l16' (16 kHz wideband PCM)."""
    codec = _cfg("NANO_CLAW_PHONE_CODEC", "pcmu").lower()
    return "l16" if codec == "l16" else "pcmu"


def phone_rate() -> int:
    return 16000 if phone_codec() == "l16" else 8000


def phone_enabled() -> bool:
    return _cfg("NANO_CLAW_PHONE") in ("1", "true", "yes")


def barge_in_enabled() -> bool:
    """Caller can interrupt the agent mid-speech (NANO_CLAW_PHONE_BARGE_IN=1).
    Off by default: the phone leg is half-duplex unless opted in."""
    return _cfg("NANO_CLAW_PHONE_BARGE_IN") in ("1", "true", "yes")


def phone_speech_mode() -> str:
    """Prepared speech is the default; raw remains an instant rollback."""

    value = _cfg(
        "NANO_CLAW_PHONE_SPEECH_PREPARATION",
        _cfg("NANO_CLAW_SPEECH_PREPARATION", "1"),
    ).lower()
    return "raw" if value in ("0", "false", "off", "no", "raw") else "prepared"


def _phone_gain_normalizer() -> SentencePeakNormalizer:
    """Build one call-owned normalizer from the phone gain environment."""
    raw_target = _cfg(
        "NANO_CLAW_PHONE_GAIN_TARGET_DB",
        str(DEFAULT_PHONE_GAIN_TARGET_DBFS),
    )
    try:
        target_dbfs = float(raw_target)
    except ValueError:
        target_dbfs = DEFAULT_PHONE_GAIN_TARGET_DBFS
    if not np.isfinite(target_dbfs):
        target_dbfs = DEFAULT_PHONE_GAIN_TARGET_DBFS
    return SentencePeakNormalizer(
        target_dbfs=target_dbfs,
        enabled=_cfg("NANO_CLAW_PHONE_GAIN", "on").lower() != "off",
    )


VAD_MODES = ("energy", "silero")
_vad_mode: str | None = None  # resolved lazily; runtime-switchable via /api/phone/vad


def get_vad_mode() -> str:
    """Active VAD for NEW calls: runtime selection > env > energy default.
    Falls back to energy loudly if silero is selected but unavailable."""
    global _vad_mode
    if _vad_mode is None:
        want = _cfg("NANO_CLAW_PHONE_VAD", "energy").lower()
        _vad_mode = want if want in VAD_MODES else "energy"
    if _vad_mode == "silero" and not silero_vad.available():
        log.error("[phone] silero VAD selected but unavailable — using energy")
        return "energy"
    return _vad_mode


def set_vad_mode(mode: str) -> bool:
    global _vad_mode
    if mode not in VAD_MODES:
        return False
    _vad_mode = mode
    log.info("[phone] VAD switched to %s (applies to new calls)", mode)
    return True


def dynamic_endpoint_enabled() -> bool:
    """Two-stage endpointing (NANO_CLAW_PHONE_DYNAMIC_ENDPOINT=1): endpoint
    on a short pause, but if the transcript ends mid-thought ('...tell me
    about'), keep listening and merge the continuation instead of answering
    the fragment. Emulates the semantic half of LiveKit-style turn detection
    with a deterministic tail check."""
    return _cfg("NANO_CLAW_PHONE_DYNAMIC_ENDPOINT") in ("1", "true", "yes")


async def _telnyx_cmd(client: httpx.AsyncClient, cid: str, command: str, payload: dict) -> bool:
    """POST a Call Control command; never raises (a webhook must always 200)."""
    try:
        resp = await client.post(
            f"{TELNYX_API}/calls/{cid}/actions/{command}",
            headers={"Authorization": f"Bearer {_cfg('TELNYX_API_KEY')}"},
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
        log.info("[telnyx] %s OK cid=%s", command, cid[:16])
        return True
    except Exception as exc:
        log.error("[telnyx] %s failed cid=%s: %s", command, cid[:16], exc)
        return False


# Live call ids — lets /api/phone/config report whether a change lands
# mid-call or on the next call.
_active_calls: set[str] = set()


class PhoneCall:
    """One live call: endpointing → STT → agent → TTS, half-duplex."""

    def __init__(self, ws: web.WebSocketResponse, call_id: str) -> None:
        self.ws = ws
        self.call_id = call_id
        _active_calls.add(call_id)
        # The agent API validates session ids against ^[A-Za-z0-9_-]{1,64}$.
        # Telnyx call ids now carry a "v3:" prefix, and the colon (plus any
        # other punctuation) makes the id fail validation, so /api/chat returns
        # 400 and the caller hears only the fallback line. Strip to the safe
        # alphabet before slicing so the phone session id is always accepted.
        safe_call_id = re.sub(r"[^A-Za-z0-9_-]", "", call_id)
        self.session_id = f"phone-{safe_call_id[:24]}"
        codec = phone_codec()
        rate = 16000 if codec == "l16" else 8000
        self.tap = CallTap.create(call_id, codec, rate, rate)
        self._tap_sentence_index = 0
        self._active_tap_sentence_index: int | None = None
        if self.tap:
            self.tap.event(
                "call_start",
                codec=codec,
                voice=_cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
            )
        # Dynamic mode endpoints fast (450 ms) because the semantic tail
        # check can rescue fragments; fixed mode keeps the safer 700 ms.
        self.dynamic = dynamic_endpoint_enabled()
        self.endpointer = UtteranceEndpointer(
            end_silence_ms=450 if self.dynamic else 700,
            rate_hz=phone_rate(),
            codec=codec,
        )
        self._tail_extensions = 0
        self._primed_len = 0
        self._primed_text = ""
        self.barge = BargeInDetector(rate_hz=phone_rate())
        # Neural VAD (one streaming instance per call; None = energy mode)
        self.vad_mode = get_vad_mode()
        if self.vad_mode == "silero":
            self.vad = (
                silero_vad.SileroVAD(sample_rate=16000)
                if phone_codec() == "l16"
                else silero_vad.SileroVAD()
            )
        else:
            self.vad = None
        self._vad_frames = 0
        log.info("[phone %s] VAD: %s", call_id[:8], self.vad_mode)
        self.speaking = False
        self.interrupted = False
        self.closed = False
        self._playback_flush_sent = False
        self._turn_task: asyncio.Task | None = None
        self._sentence_pipelines: set[SentencePipeline] = set()
        self._gain_normalizer = _phone_gain_normalizer()
        self._frame_pacer: FramePacer | None = None
        self._inbound_buffer: deque[tuple[np.ndarray, bool | None]] = deque()
        self._inbound_buffer_drops = 0
        self._http = httpx.AsyncClient(timeout=120.0)
        self.flow = FlowSession.create() if get_flow_mode() == "scheduler" else None
        self._flow_create_failed = False
        self.default_greeting = self.flow.greeting if self.flow else DEFAULT_GREETING
        # Idle policy: clock runs from the last time the caller spoke or the
        # agent finished speaking; one "are you still there?" per stretch.
        self.last_activity = time.monotonic()
        self.idle_prompted = False
        self._idle_task = asyncio.create_task(self._idle_watchdog())

    async def close(self) -> None:
        was_speaking = self.speaking
        self.speaking = False
        self.closed = True
        for pipeline in tuple(self._sentence_pipelines):
            await pipeline.aclose()
        if was_speaking or self.interrupted:
            await self._flush_playback()
        _active_calls.discard(self.call_id)
        self._inbound_buffer.clear()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            await self._http.aclose()
        finally:
            tap, self.tap = self.tap, None
            if tap:
                tap.event("call_end")
                tap.close()

    def _sync_flow_mode(self) -> None:
        """Re-evaluate the Flow dropdown at each turn boundary so a change in
        the web UI applies to the caller's next utterance, mid-call.

        Off → scheduler joins the flow cold (no flow greeting; it engages
        with whatever the caller says next). Scheduler → off abandons the
        negotiation state and returns to persona chat. A failed FlowSession
        create (availability missing) falls back to persona chat and is not
        retried for the rest of the call."""
        want = get_flow_mode() == "scheduler"
        if want and self.flow is None and not self._flow_create_failed:
            self.flow = FlowSession.create()
            if self.flow is None:
                self._flow_create_failed = True
                log.warning("[phone %s] flow switch requested but FlowSession "
                            "unavailable — staying in persona chat", self.call_id[:8])
            else:
                log.info("[phone %s] flow joined mid-call (scheduler)", self.call_id[:8])
        elif not want and self.flow is not None:
            log.info("[phone %s] flow left mid-call (scheduler → persona)", self.call_id[:8])
            self.flow = None

    def _mark_activity(self) -> None:
        self.last_activity = time.monotonic()
        self.idle_prompted = False

    async def _idle_watchdog(self) -> None:
        """Prompt after NANO_CLAW_PHONE_IDLE_S of silence; hang up after a
        second full stretch with still no reply (default 30s → 60s total)."""
        prompt_after = float(_cfg("NANO_CLAW_PHONE_IDLE_S", "30") or 30)
        while not self.closed:
            await asyncio.sleep(2.5)
            if self.closed:
                return
            if self.speaking or (self._turn_task and not self._turn_task.done()):
                continue
            action = idle_action(
                time.monotonic() - self.last_activity, self.idle_prompted, prompt_after
            )
            if action == "prompt":
                log.info("[phone %s] idle %.0fs — prompting caller", self.call_id[:8], prompt_after)
                self.idle_prompted = True
                await self.speak(IDLE_PROMPT_TEXT)
            elif action == "hangup":
                log.info("[phone %s] idle after prompt — hanging up", self.call_id[:8])
                await self.speak(IDLE_GOODBYE_TEXT)
                await _telnyx_cmd(self._http, self.call_id, "hangup", {})
                self.closed = True
                return

    # ── Inbound audio ────────────────────────────────────────────

    def feed_media(self, payload_b64: str) -> None:
        if self.closed:
            return
        payload = base64.b64decode(payload_b64)
        if self.tap:
            self.tap.inbound_frame(payload)
        pcm = (
            np.frombuffer(payload, dtype=np.int16)
            if phone_codec() == "l16"
            else ulaw_decode(payload)
        )
        # Feed the neural VAD continuously (its recurrent state needs every
        # frame); both detectors then share one speech decision per frame.
        is_speech = self.vad.feed_speech(pcm) if self.vad else None
        if self.vad:
            self._vad_frames += 1
            if self._vad_frames % 250 == 0:  # every ~5s of call audio
                vmax, vmean = self.vad.take_stats()
                log.info(
                    "[phone %s] silero last5s: max=%.2f mean=%.2f in_speech=%s",
                    self.call_id[:8], vmax, vmean, is_speech,
                )

        if self.speaking:
            # Barge-in (NANO_CLAW_PHONE_BARGE_IN=1): listen for the caller
            # talking over us; otherwise stay half-duplex.
            if barge_in_enabled() and self.barge.feed(pcm, is_speech=is_speech):
                self._interrupt()
            return

        # A completed task's callback normally replays first, but finish it
        # here too so a newly arrived frame can never overtake older audio.
        if self._turn_task and self._turn_task.done():
            self._turn_finished(self._turn_task)

        # While a turn is still thinking (STT/LLM), hold audio for ordered
        # replay — unless we just interrupted, in which case the caller's
        # speech is already feeding the barge-in-primed endpointer.
        if self._turn_task and not self._turn_task.done() and not self.interrupted:
            self._buffer_inbound(pcm, is_speech)
            return

        utterance = self._feed_endpointer(pcm, is_speech)
        if utterance:
            self._mark_activity()
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()  # interrupted turn still unwinding
                self._inbound_buffer.clear()
            self.interrupted = False
            self._start_turn(utterance)

    def _feed_endpointer(
        self, pcm: np.ndarray, is_speech: bool | None
    ) -> bytes | None:
        """Feed one decoded frame and capture endpoint state transitions."""
        tap = self.tap
        if tap is None:
            return self.endpointer.feed(pcm, is_speech=is_speech)
        was_in_utterance = self.endpointer.in_utterance
        utterance = self.endpointer.feed(pcm, is_speech=is_speech)
        is_in_utterance = self.endpointer.in_utterance
        rms = self.endpointer.current_rms
        floor = self.endpointer.noise_floor
        if not was_in_utterance and is_in_utterance:
            tap.event("utterance_start", rms=rms, floor=floor)
        if was_in_utterance and not is_in_utterance:
            tap.event(
                "utterance_end",
                rms=rms,
                floor=floor,
                accepted=utterance is not None,
            )
        return utterance

    def _buffer_inbound(self, pcm: np.ndarray, is_speech: bool | None) -> None:
        if len(self._inbound_buffer) >= MAX_BUFFERED_INBOUND_FRAMES:
            self._inbound_buffer.popleft()
            self._inbound_buffer_drops += 1
            if self._inbound_buffer_drops == 1 or self._inbound_buffer_drops % 250 == 0:
                log.warning(
                    "[phone %s] inbound buffer capped at %d frames — dropped %d oldest",
                    self.call_id[:8], MAX_BUFFERED_INBOUND_FRAMES, self._inbound_buffer_drops,
                )
        self._inbound_buffer.append((pcm, is_speech))

    def _start_turn(self, utterance: bytes) -> None:
        task = asyncio.create_task(self._run_turn(utterance))
        self._turn_task = task
        task.add_done_callback(self._turn_finished)

    def _turn_finished(self, task: asyncio.Task) -> None:
        if task is not self._turn_task:
            return
        self._turn_task = None
        if self.closed or task.cancelled() or self.interrupted:
            # Barge-in has already primed the endpointer; stale thinking audio
            # must neither precede that interruption nor reset it via replay.
            self._inbound_buffer.clear()
            return
        self._replay_inbound()

    def _replay_inbound(self) -> None:
        while self._inbound_buffer and not self.closed:
            pcm, is_speech = self._inbound_buffer.popleft()
            utterance = self._feed_endpointer(pcm, is_speech)
            if utterance:
                self._mark_activity()
                self._start_turn(utterance)
                return

    def _interrupt(self) -> None:
        """Caller talked over the agent: stop speaking and turn the
        interruption itself into the next utterance."""
        log.info("[phone %s] barge-in — caller interrupted", self.call_id[:8])
        if self.tap:
            self.tap.event(
                "barge_in", sentence_index=self._active_tap_sentence_index
            )
        self._mark_activity()
        self.interrupted = True
        self.speaking = False  # speak() loop sees this and aborts
        frames = self.barge.take_frames()
        self.endpointer.prime(frames)
        asyncio.create_task(self._flush_playback())

    async def _flush_playback(self) -> None:
        """Clear the bounded prebuffer surplus queued by frame pacing.

        Playback starts with a small one-time burst so Telnyx does not starve.
        Telnyx can still hold that audio after a local interruption; one clear
        drops the buffered tail.
        """
        if self._playback_flush_sent or getattr(self.ws, "closed", False):
            return
        self._playback_flush_sent = True
        try:
            await self.ws.send_json({"event": "clear"})
        except Exception:
            log.exception("[phone %s] clear failed", self.call_id[:8])
        else:
            if self.tap:
                self.tap.event("clear_sent")

    # ── One conversational turn ──────────────────────────────────

    async def _run_turn(self, pcm: bytes) -> None:
        try:
            # If we extended the window and the caller stayed quiet (<600 ms
            # of new audio), don't re-transcribe near-identical audio — they
            # trailed off; answer what we already heard.
            new_audio_bytes = len(pcm) - self._primed_len
            if self._tail_extensions and new_audio_bytes < int(phone_rate() * 2 * 0.6):
                text = self._primed_text
                self._tail_extensions = 2  # no further extensions
            else:
                text = await self._transcribe(pcm)
            if not text.strip():
                self._tail_extensions = 0
                return
            # Semantic tail check: a transcript ending mid-thought means the
            # short pause was a breath, not a turn end. Re-prime the
            # endpointer with the same audio and keep listening; the next
            # endpoint re-transcribes the MERGED utterance. Bounded to 2
            # extensions so a trailing-off caller still gets an answer.
            if (
                self.dynamic
                and self._tail_extensions < 2
                and transcript_looks_incomplete(text)
            ):
                self._tail_extensions += 1
                self._primed_len = len(pcm)
                self._primed_text = text
                log.info(
                    "[phone %s] tail-incomplete (%r…) — extending listen window (%d)",
                    self.call_id[:8], text[-30:], self._tail_extensions,
                )
                pcm_samples = np.frombuffer(pcm, dtype=np.int16)
                frame = phone_rate() * FRAME_MS // 1000
                self.endpointer.prime(
                    [
                        pcm_samples[i : i + frame]
                        for i in range(0, len(pcm_samples), frame)
                    ]
                )
                return
            self._tail_extensions = 0
            log.info("[phone %s] caller: %s", self.call_id[:8], text)
            metrics_db.bump_call_turns(_metrics_conn, self.call_id)
            self._sync_flow_mode()
            if self.flow:
                agent_started = time.monotonic() if self.tap else None
                reply = await self.flow.reply(text)
                if self.tap and agent_started is not None:
                    self.tap.event(
                        "agent_done", ms=(time.monotonic() - agent_started) * 1000.0
                    )
                log.info(
                    "[phone %s] flow outcome=%s slots=%s",
                    self.call_id[:8],
                    reply.outcome or "continue",
                    reply.slots,
                )
                await self.speak(reply.text)
                if reply.done:
                    await _telnyx_cmd(self._http, self.call_id, "hangup", {})
                    self.closed = True
                return
            await self._stream_reply(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[phone %s] turn failed", self.call_id[:8])
            await self.speak("Sorry, something went wrong on my end. Try asking again.")

    async def _stream_reply(self, text: str) -> None:
        """Stream the agent's reply (SSE) and speak each sentence as it
        completes — the caller hears the first sentence while the model is
        still writing the rest. Falls back to the non-stream JSON shape when
        the API has streaming disabled (NANO_CLAW_STREAM=0)."""
        t0 = time.monotonic()
        tap = self.tap
        agent_done_recorded = False

        def record_agent_done() -> None:
            nonlocal agent_done_recorded
            if agent_done_recorded:
                return
            agent_done_recorded = True
            if tap:
                tap.event("agent_done", ms=(time.monotonic() - t0) * 1000.0)

        self._playback_flush_sent = False
        self.speaking = True
        self.barge.reset()
        chunker = TextChunker()
        first_spoken_at: float | None = None
        reply_complete = False
        try:
            payload: dict = {
                "message": text,
                "sessionId": self.session_id,
                "responseMode": "voice",
            }
            model = _cfg("NANO_CLAW_PHONE_MODEL")
            if model:
                payload["model"] = model  # else: server's configured default
            async with self._http.stream(
                "POST",
                f"{NANO_CLAW_URL}/api/chat",
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if "text/event-stream" not in resp.headers.get("content-type", ""):
                    body = json.loads(await resp.aread())
                    reply = body.get("response", "") or "I didn't catch that — could you say it again?"
                    record_agent_done()
                    log.info("[phone %s] agent non-stream (%.1fs)", self.call_id[:8], time.monotonic() - t0)
                    await self._speak_sentences(self._speech_units(reply))
                    return

                async def stream_sentences():
                    nonlocal first_spoken_at, reply_complete
                    event = ""
                    data_lines: list[str] = []
                    last_processing_cue = 0.0
                    prepared = phone_speech_mode() == "prepared"
                    prepared_parts: list[str] = []
                    async for raw in resp.aiter_lines():
                        if self.closed or not self.speaking:
                            return  # hangup or barge-in: stop consuming the stream
                        if raw == "":
                            event_payload = "\n".join(data_lines)
                            data_lines = []
                            ev, event = event, ""
                            if not event_payload:
                                continue
                            obj = json.loads(event_payload)
                            if ev == "delta":
                                delta = obj.get("text", "")
                                if prepared:
                                    if isinstance(delta, str):
                                        prepared_parts.append(delta)
                                    continue
                                for chunk in chunker.push(delta):
                                    if first_spoken_at is None:
                                        first_spoken_at = time.monotonic()
                                        log.info(
                                            "[phone %s] first sentence at %.1fs",
                                            self.call_id[:8], first_spoken_at - t0,
                                        )
                                    yield chunk
                            elif ev == "deep_started":
                                acknowledgement = obj.get(
                                    "acknowledgement",
                                    "Let me think deeply about this.",
                                )
                                if (
                                    isinstance(acknowledgement, str)
                                    and acknowledgement.strip()
                                ):
                                    if first_spoken_at is None:
                                        first_spoken_at = time.monotonic()
                                    yield acknowledgement.strip()
                                last_processing_cue = time.monotonic()
                            elif ev == "deep_progress":
                                now = time.monotonic()
                                if (
                                    obj.get("phase")
                                    not in {"completed", "failed", "cancelled"}
                                    and now - last_processing_cue >= 2.6
                                ):
                                    last_processing_cue = now
                                    yield PROCESSING_CUE_SENTINEL
                            elif ev == "final":
                                record_agent_done()
                                reply_complete = True
                                if prepared:
                                    response_text = obj.get("response", "")
                                    source_text = (
                                        response_text.strip()
                                        if isinstance(response_text, str)
                                        and response_text.strip()
                                        else "".join(prepared_parts).strip()
                                    )
                                    for unit in self._speech_units(source_text):
                                        yield unit
                                else:
                                    tail = chunker.flush()
                                    if tail:
                                        yield tail
                            elif ev == "tool_pending":
                                yield (
                                    "I can't take actions over the phone, but I'm happy "
                                    "to answer questions."
                                )
                            elif ev == "error":
                                record_agent_done()
                                yield "Sorry, something went wrong. Try asking again."
                        elif raw.startswith("event:"):
                            event = raw[6:].strip()
                        elif raw.startswith("data:"):
                            data_lines.append(raw[5:].strip())

                await self._speak_sentences(stream_sentences())
                if reply_complete:
                    log.info(
                        "[phone %s] reply complete (%.1fs total)",
                        self.call_id[:8], time.monotonic() - t0,
                    )
                if self.closed or not self.speaking:
                    return
                record_agent_done()
        finally:
            self.speaking = False
            if not self.interrupted:
                self.endpointer.reset()
            self.last_activity = time.monotonic()

    @staticmethod
    def _sentences(text: str) -> list[str]:
        chunker = TextChunker()
        out = chunker.push(text)
        tail = chunker.flush()
        if tail:
            out.append(tail)
        return out

    @staticmethod
    def _speech_units(text: str) -> list[str | SpeechChunk]:
        """Compile one complete phone response, retaining the raw rollback."""

        if phone_speech_mode() != "prepared":
            return PhoneCall._sentences(text)
        try:
            max_words = int(_cfg("NANO_CLAW_SPEECH_MAX_WORDS", "18"))
        except ValueError:
            max_words = 18
        try:
            max_duration = int(_cfg("NANO_CLAW_SPEECH_MAX_CHUNK_MS", "2500"))
        except ValueError:
            max_duration = 2500
        try:
            plan = compile_speech(
                text,
                max_words_per_chunk=max_words,
                max_chunk_duration_ms=max_duration,
            )
        except Exception:
            log.exception("[phone] speech preparation failed; using raw text")
            return PhoneCall._sentences(text)
        metadata = plan.public_metadata()
        log.info(
            "[phone] speech plan compiled: version=%s chunks=%d normalizations=%d",
            metadata["compilerVersion"],
            metadata["chunkCount"],
            metadata["normalizationCount"],
        )
        return list(plan.chunks)

    async def _transcribe(self, pcm: bytes) -> str:
        stt_url = os.environ.get("STT_SERVICE_URL", "http://host.docker.internal:8200")
        started = time.monotonic() if self.tap else None
        resp = await self._http.post(
            f"{stt_url}/transcribe",
            content=pcm,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sample-Rate": str(phone_rate()),
                # Lower-powered nodes (M1 failover) run "tiny" for speed.
                "X-Model-Size": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
            },
        )
        text = resp.json().get("text", "")
        if self.tap and started is not None:
            self.tap.event(
                "stt_done",
                ms=(time.monotonic() - started) * 1000.0,
                text_len=len(text),
            )
        return text

    # ── Outbound audio ───────────────────────────────────────────

    async def _synthesize_sentence(
        self, sentence: str | SpeechChunk
    ) -> _SynthesizedSpeech:
        """Synthesize one sentence and retain its tap correlation fields."""
        if sentence == PROCESSING_CUE_SENTINEL:
            return _SynthesizedSpeech(processing_chime(), self.tap, None)
        spoken_text = sentence.text if isinstance(sentence, SpeechChunk) else sentence
        pause_after_ms = (
            sentence.pause_after_ms if isinstance(sentence, SpeechChunk) else None
        )
        tap = self.tap
        sentence_index: int | None = None
        if tap:
            self._tap_sentence_index += 1
            sentence_index = self._tap_sentence_index
            tap.event("synth_start", sentence_index=sentence_index)
        synth_started = time.monotonic() if tap else None
        voice = _cfg("NANO_CLAW_PHONE_VOICE", "af_heart")
        try:
            speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
        except ValueError:
            speed = 1.0
        loop = asyncio.get_running_loop()
        synth_args = (spoken_text, voice, speed)
        if pause_after_ms is not None:
            synth_args = (*synth_args, pause_after_ms)
        pcm48k = await loop.run_in_executor(None, tts_synthesize, *synth_args)
        if tap and synth_started is not None:
            tap.tts_pcm48k(pcm48k)
            tap.event(
                "synth_done",
                sentence_index=sentence_index,
                ms=(time.monotonic() - synth_started) * 1000.0,
                samples=len(pcm48k) // 2,
            )
        return _SynthesizedSpeech(pcm48k, tap, sentence_index)

    def _synthesis_failed(
        self, sentence: str | SpeechChunk, error: Exception
    ) -> None:
        log.error(
            "[phone %s] sentence synthesis failed",
            self.call_id[:8],
            exc_info=(type(error), error, error.__traceback__),
        )

    def _record_synth_ahead(self, ready: bool, wait_s: float) -> None:
        tap = self.tap
        if tap:
            tap.event(
                "synth_ahead_hit" if ready else "synth_ahead_miss",
                sentence_index=self._tap_sentence_index,
                wait_ms=wait_s * 1000.0,
            )

    async def _play_synthesized(
        self,
        speech: _SynthesizedSpeech,
        pacer: FramePacer | None = None,
    ) -> None:
        """Pace one already-synthesized sentence to the phone transport."""
        pacer = pacer or getattr(self, "_frame_pacer", None) or _phone_frame_pacer()
        tap = speech.tap
        sentence_index = speech.sentence_index
        send_started: float | None = None
        send_times: list[float] | None = [] if tap else None
        audio_s_sent = 0.0
        last_frame_audio_ms = 0.0
        if tap:
            self._active_tap_sentence_index = sentence_index
        try:
            codec = phone_codec()
            gain = self._gain_normalizer.normalize(speech.pcm48k)
            if tap:
                tap.event(
                    "gain_applied",
                    sentence_index=sentence_index,
                    measured_peak_dbfs=gain.measured_peak_dbfs,
                    applied_gain_db=gain.applied_gain_db,
                )
            frames = (
                pcm48k_to_l16_frames(gain.pcm16)
                if codec == "l16"
                else pcm48k_to_ulaw_frames(gain.pcm16)
            )
            if frames and not pacer.running:
                # The first sentence's synthesis must not consume prebuffer
                # time. Anchor only once its transport frames are ready.
                pacer.reset()
            if tap:
                outbound_rate = 16000 if codec == "l16" else 8000
                sample_width = 2 if codec == "l16" else 1
                send_started = pacer.now()
            for frame in frames:
                if self.closed or not self.speaking:
                    break  # hung up or barged in
                deadline = pacer.next_deadline()
                await asyncio.sleep(max(0.0, deadline - pacer.now()))
                if self.closed or not self.speaking:
                    break  # interruption may have landed during the sleep
                await self.ws.send_json(
                    {"event": "media", "media": {"payload": base64.b64encode(frame).decode()}}
                )
                if tap and send_times is not None:
                    sent_at = pacer.now()
                    tap.outbound_frame(frame)
                    send_times.append(sent_at)
                    frame_samples = len(frame) // sample_width
                    audio_s_sent += frame_samples / outbound_rate
                    last_frame_audio_ms = frame_samples * 1000.0 / outbound_rate
        except Exception:
            log.exception("[phone %s] speak failed", self.call_id[:8])
        finally:
            if tap and send_times is not None:
                elapsed_s = (
                    pacer.now() - send_started if send_started is not None else 0.0
                )
                intervals_ms = np.diff(send_times) * 1000.0
                if len(intervals_ms):
                    interval_p50_ms, interval_p95_ms = np.percentile(
                        intervals_ms, [50, 95]
                    )
                    interval_max_ms = float(np.max(intervals_ms))
                else:
                    interval_p50_ms = interval_p95_ms = interval_max_ms = 0.0
                fields = {
                    "sentence_index": sentence_index,
                    "count": len(send_times),
                    "interval_p50_ms": float(interval_p50_ms),
                    "interval_p95_ms": float(interval_p95_ms),
                    "interval_max_ms": interval_max_ms,
                    "audio_s": audio_s_sent,
                    "elapsed_s": elapsed_s,
                    "surplus_s": audio_s_sent - elapsed_s,
                }
                if send_times:
                    fields.update(
                        first_frame_t=send_times[0],
                        last_frame_t=send_times[-1],
                        last_frame_audio_ms=last_frame_audio_ms,
                    )
                tap.event("frames_sent", **fields)
                if self._active_tap_sentence_index == sentence_index:
                    self._active_tap_sentence_index = None

    async def _speak_sentences(self, sentences) -> None:
        """Synthesize and play a sync or async sentence source in order."""
        if self.closed or not self.speaking:
            return
        self._gain_normalizer.reset()
        previous_pacer = getattr(self, "_frame_pacer", None)
        self._frame_pacer = _phone_frame_pacer()
        pipeline = SentencePipeline(
            sentences,
            self._synthesize_sentence,
            on_error=self._synthesis_failed,
            on_ahead=self._record_synth_ahead,
        )
        self._sentence_pipelines.add(pipeline)
        try:
            async with pipeline:
                async for synthesized in pipeline:
                    if self.closed or not self.speaking:
                        return
                    await self._play_synthesized(synthesized.audio)
                    if self.closed or not self.speaking:
                        return
        finally:
            self._frame_pacer = previous_pacer
            self._sentence_pipelines.discard(pipeline)

    async def _speak_chunk(self, sentence: str) -> None:
        """TTS one sentence → paced phone frames. Caller manages `speaking`."""
        if self.closed or not self.speaking or not sentence:
            return
        await self._speak_sentences((sentence,))

    async def speak(self, text: str) -> None:
        """Speak a complete text (greeting, idle prompts, error lines)."""
        if self.closed or not text:
            return
        self._playback_flush_sent = False
        self.speaking = True
        self.barge.reset()
        try:
            await self._speak_sentences(self._speech_units(text))
        finally:
            self.speaking = False
            if not self.interrupted:
                self.endpointer.reset()  # drop anything "heard" while talking
            # else: the endpointer was primed with the interruption — keep it
            # Idle clock restarts when we stop talking — but only the clock;
            # clearing idle_prompted here would make the idle prompt reset
            # itself and re-prompt forever instead of hanging up.
            self.last_activity = time.monotonic()


# ── HTTP handlers ────────────────────────────────────────────────

_answered: dict[str, float] = {}  # call_control_id → answer time (webhook retries dedup)
_metrics_conn = None  # set in register_phone_routes; every write is best-effort


def _node() -> str:
    return _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE").replace("https://", "").rstrip("/")


def _token_ok(request: web.Request) -> bool:
    expected = _cfg("NANO_CLAW_PHONE_TOKEN")
    return bool(expected) and request.query.get("token") == expected


async def incoming_handler(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return web.Response(status=403, text="bad token")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")

    data = body.get("data", {})
    event = data.get("event_type", "")
    payload = data.get("payload", {})
    cid = payload.get("call_control_id", "")

    if event == "call.initiated" and cid:
        now = time.monotonic()
        for k, t in list(_answered.items()):  # keep the dedup map bounded
            if now - t > 3600:
                _answered.pop(k, None)
        if cid in _answered:
            return web.json_response({"ok": True, "dedup": True})
        _answered[cid] = now

        base = _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE").rstrip("/")
        ws_url = (
            base.replace("https://", "wss://", 1)
            + f"/ws/phone-media?token={_cfg('NANO_CLAW_PHONE_TOKEN')}"
        )
        caller = payload.get("from", "?")
        log.info("[phone] incoming call from %s → answering", caller)
        metrics_db.record_call_start(
            _metrics_conn, cid, caller, payload.get("to", "?"), _node()
        )
        codec = phone_codec()
        async with httpx.AsyncClient() as client:
            await _telnyx_cmd(client, cid, "answer", {
                "command_id": f"answer-{cid}",
                "stream_url": ws_url,
                "stream_track": "inbound_track",
                "stream_codec": "L16" if codec == "l16" else "PCMU",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "L16" if codec == "l16" else "PCMU",
                "stream_bidirectional_sampling_rate": phone_rate(),
            })
    elif event == "call.hangup":
        log.info("[phone] hangup cid=%s", cid[:16])
        _answered.pop(cid, None)
        metrics_db.record_call_end(_metrics_conn, cid)

    return web.json_response({"ok": True})


async def media_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    if not _token_ok(request):
        raise web.HTTPForbidden(text="bad token")
    await ws.prepare(request)

    call: PhoneCall | None = None
    try:
        async for raw in ws:
            if raw.type != web.WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                continue
            event = msg.get("event", "")

            if event == "start":
                meta = msg.get("start") or {}
                cid = meta.get("call_control_id") or msg.get("stream_id") or "unknown"
                call = PhoneCall(ws, cid)
                log.info("[phone %s] media stream started", cid[:8])
                greeting = _cfg("NANO_CLAW_PHONE_GREETING") or call.default_greeting
                asyncio.create_task(call.speak(greeting))
            elif event == "media" and call:
                call.feed_media((msg.get("media") or {}).get("payload", ""))
            elif event == "stop":
                log.info("[phone] media stream stopped")
                break
    finally:
        if call:
            await call.close()
    return ws


async def calls_handler(request: web.Request) -> web.Response:
    """Recent call log — token-gated: caller numbers are not public data."""
    if not _token_ok(request):
        return web.Response(status=403, text="bad token")
    try:
        conn = metrics_db.connect()
    except Exception:
        return web.json_response(
            {"node": _node(), "vad": get_vad_mode(), "calls": [], "error": "db unavailable"}
        )
    try:
        return web.json_response(
            {"node": _node(), "vad": get_vad_mode(), "calls": metrics_db.recent_calls(conn)}
        )
    finally:
        conn.close()


async def vad_get_handler(request: web.Request) -> web.Response:
    """Pipeline-settings surface: which VAD is active, what's selectable."""
    return web.json_response({
        "active": get_vad_mode(),
        "options": list(VAD_MODES),
        "silero_available": silero_vad.available(),
    })


async def vad_set_handler(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")
    mode = str(body.get("mode", "")).lower()
    if not set_vad_mode(mode):
        return web.Response(status=400, text=f"unknown mode: {mode}")
    return web.json_response({"active": get_vad_mode()})


async def config_get_handler(request: web.Request) -> web.Response:
    """Pipeline-settings surface: the phone line's live-tunable config."""
    try:
        speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
    except ValueError:
        speed = 1.0
    return web.json_response({
        "voice": _cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
        "model": _cfg("NANO_CLAW_PHONE_MODEL", ""),  # "" → server default
        "speed": speed,
        "stt_size": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
        "active_calls": len(_active_calls),
        "speech_mode": phone_speech_mode(),
        "speech_version": SPEECH_COMPILER_VERSION,
    })


async def config_set_handler(request: web.Request) -> web.Response:
    """Set runtime overrides from the web UI. Voice applies to the next
    spoken sentence (even mid-call); model applies to the next agent turn.
    Overrides live in memory — a restart returns to the .env values."""
    from voice import voice_catalog

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")

    if "voice" in body:
        voice = str(body["voice"])
        if voice_catalog.lookup(voice) is None:
            return web.Response(status=400, text=f"unknown voice: {voice}")
        _overrides["NANO_CLAW_PHONE_VOICE"] = voice
    if "model" in body:
        model = str(body["model"]).strip()
        if model:
            _overrides["NANO_CLAW_PHONE_MODEL"] = model
        else:
            _overrides.pop("NANO_CLAW_PHONE_MODEL", None)  # back to server default
    if "speed" in body:
        try:
            speed = float(body["speed"])
        except (TypeError, ValueError):
            return web.Response(status=400, text="bad speed")
        if not 0.5 <= speed <= 2.0:
            return web.Response(status=400, text="speed out of range (0.5-2.0)")
        _overrides["NANO_CLAW_PHONE_SPEED"] = str(speed)
    if "stt_size" in body:
        size = str(body["stt_size"])
        if size not in ("tiny", "base", "small", "medium"):
            return web.Response(status=400, text=f"unknown stt size: {size}")
        # Read per transcription request, so this applies to the caller's
        # next utterance even mid-call.
        _overrides["NANO_CLAW_PHONE_STT_SIZE"] = size

    log.info("phone config updated: voice=%s model=%s speed=%s (%d active call(s))",
             _cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
             _cfg("NANO_CLAW_PHONE_MODEL") or "(default)",
             _cfg("NANO_CLAW_PHONE_SPEED", "1.0"), len(_active_calls))
    return await config_get_handler(request)


def register_phone_routes(app: web.Application) -> None:
    """Attach gateway routes when NANO_CLAW_PHONE=1 (no-op otherwise)."""
    global _metrics_conn
    if not phone_enabled():
        return
    missing = [
        name
        for name in ("TELNYX_API_KEY", "NANO_CLAW_PHONE_WEBHOOK_BASE", "NANO_CLAW_PHONE_TOKEN")
        if not _cfg(name)
    ]
    if missing:
        log.error("[phone] NANO_CLAW_PHONE=1 but missing env: %s — gateway NOT registered", missing)
        return
    _metrics_conn = metrics_db.init_db()
    app.router.add_post("/api/phone/incoming", incoming_handler)
    app.router.add_get("/ws/phone-media", media_ws_handler)
    app.router.add_get("/api/calls", calls_handler)
    app.router.add_get("/api/phone/vad", vad_get_handler)
    app.router.add_post("/api/phone/vad", vad_set_handler)
    app.router.add_get("/api/phone/config", config_get_handler)
    app.router.add_post("/api/phone/config", config_set_handler)
    log.info("[phone] Telnyx gateway registered (webhook base: %s, VAD: %s)",
             _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE"), get_vad_mode())
