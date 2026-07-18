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
    NANO_CLAW_PHONE_BARGE_IN        1 = caller can interrupt the agent
                                    mid-speech (buffer-flush via Telnyx
                                    "clear"); unset = half-duplex
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque

import httpx
import numpy as np
from aiohttp import web

from voice import metrics_db, silero_vad
from voice.flow_session import FlowSession, get_flow_mode
from voice.phone_audio import (
    FRAME_MS,
    BargeInDetector,
    UtteranceEndpointer,
    pcm48k_to_l16_frames,
    pcm48k_to_ulaw_frames,
    transcript_looks_incomplete,
    ulaw_decode,
)
from voice.phone_tap import CallTap
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
        self.session_id = f"phone-{call_id[:24]}"
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
        self._turn_task: asyncio.Task | None = None
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
        self.closed = True
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
        was_in_utterance = self.endpointer._in_utterance
        rms = (
            float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
            if len(pcm)
            else 0.0
        )
        utterance = self.endpointer.feed(pcm, is_speech=is_speech)
        is_in_utterance = self.endpointer._in_utterance
        if not was_in_utterance and is_in_utterance:
            tap.event("utterance_start", rms=rms)
        if was_in_utterance and not is_in_utterance:
            tap.event("utterance_end", rms=rms, accepted=utterance is not None)
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
        """Caller talked over the agent: flush Telnyx's audio buffer, stop
        speaking, and turn the interruption itself into the next utterance."""
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
        # Telnyx buffers outbound media ahead of playback; without a clear
        # the caller keeps hearing the old answer for seconds after we stop.
        asyncio.create_task(self._send_clear())

    async def _send_clear(self) -> None:
        try:
            await self.ws.send_json({"event": "clear"})
        except Exception:
            log.exception("[phone %s] clear failed", self.call_id[:8])

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

        self.speaking = True
        self.barge.reset()
        chunker = TextChunker()
        first_spoken_at: float | None = None
        try:
            payload: dict = {"message": text, "sessionId": self.session_id}
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
                    for chunk in self._sentences(reply):
                        await self._speak_chunk(chunk)
                    return

                event = ""
                data_lines: list[str] = []
                async for raw in resp.aiter_lines():
                    if self.closed or not self.speaking:
                        return  # hangup or barge-in: stop consuming the stream
                    if raw == "":
                        payload = "\n".join(data_lines)
                        data_lines = []
                        ev, event = event, ""
                        if not payload:
                            continue
                        obj = json.loads(payload)
                        if ev == "delta":
                            for chunk in chunker.push(obj.get("text", "")):
                                if first_spoken_at is None:
                                    first_spoken_at = time.monotonic()
                                    log.info(
                                        "[phone %s] first sentence at %.1fs",
                                        self.call_id[:8], first_spoken_at - t0,
                                    )
                                await self._speak_chunk(chunk)
                        elif ev == "final":
                            record_agent_done()
                            tail = chunker.flush()
                            if tail:
                                await self._speak_chunk(tail)
                            log.info(
                                "[phone %s] reply complete (%.1fs total)",
                                self.call_id[:8], time.monotonic() - t0,
                            )
                        elif ev == "tool_pending":
                            await self._speak_chunk(
                                "I can't take actions over the phone, but I'm happy to answer questions."
                            )
                        elif ev == "error":
                            record_agent_done()
                            await self._speak_chunk("Sorry, something went wrong. Try asking again.")
                    elif raw.startswith("event:"):
                        event = raw[6:].strip()
                    elif raw.startswith("data:"):
                        data_lines.append(raw[5:].strip())
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

    async def _speak_chunk(self, sentence: str) -> None:
        """TTS one sentence → paced phone frames. Caller manages `speaking`."""
        if self.closed or not self.speaking or not sentence:
            return
        tap = self.tap
        sentence_index: int | None = None
        if tap:
            self._tap_sentence_index += 1
            sentence_index = self._tap_sentence_index
            self._active_tap_sentence_index = sentence_index
            tap.event("synth_start", sentence_index=sentence_index)
        synth_started = time.monotonic() if tap else None
        send_started: float | None = None
        send_times: list[float] | None = [] if tap else None
        audio_s_sent = 0.0
        last_frame_audio_ms = 0.0
        voice = _cfg("NANO_CLAW_PHONE_VOICE", "af_heart")
        try:
            speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
        except ValueError:
            speed = 1.0
        loop = asyncio.get_running_loop()
        try:
            pcm48k = await loop.run_in_executor(None, tts_synthesize, sentence, voice, speed)
            if tap and synth_started is not None:
                tap.tts_pcm48k(pcm48k)
                tap.event(
                    "synth_done",
                    sentence_index=sentence_index,
                    ms=(time.monotonic() - synth_started) * 1000.0,
                    samples=len(pcm48k) // 2,
                )
            codec = phone_codec()
            frames = (
                pcm48k_to_l16_frames(pcm48k)
                if codec == "l16"
                else pcm48k_to_ulaw_frames(pcm48k)
            )
            if tap:
                outbound_rate = 16000 if codec == "l16" else 8000
                sample_width = 2 if codec == "l16" else 1
                send_started = time.monotonic()
            for frame in frames:
                if self.closed or not self.speaking:
                    break  # hung up or barged in
                await self.ws.send_json(
                    {"event": "media", "media": {"payload": base64.b64encode(frame).decode()}}
                )
                if tap and send_times is not None:
                    sent_at = time.monotonic()
                    tap.outbound_frame(frame)
                    send_times.append(sent_at)
                    frame_samples = len(frame) // sample_width
                    audio_s_sent += frame_samples / outbound_rate
                    last_frame_audio_ms = frame_samples * 1000.0 / outbound_rate
                # Pace slightly faster than real time: keeps Telnyx's
                # jitter buffer fed without flooding it.
                await asyncio.sleep(FRAME_MS / 1000 * 0.9)
        except Exception:
            log.exception("[phone %s] speak failed", self.call_id[:8])
        finally:
            if tap and send_times is not None:
                elapsed_s = (
                    time.monotonic() - send_started if send_started is not None else 0.0
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

    async def speak(self, text: str) -> None:
        """Speak a complete text (greeting, idle prompts, error lines)."""
        if self.closed or not text:
            return
        self.speaking = True
        self.barge.reset()
        try:
            for sentence in self._sentences(text):
                if self.closed or not self.speaking:
                    return
                await self._speak_chunk(sentence)
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
