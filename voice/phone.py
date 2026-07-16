"""Telnyx phone gateway: callers dial in and talk to the nano-claw agent.

Call flow (mirrors riff's proven shape, minus the flow engine):

    caller → Telnyx Call Control app → POST /api/phone/incoming (webhook)
           → answer_with_streaming() → Telnyx opens WS to /ws/phone-media
           → μ-law 8k frames in → UtteranceEndpointer → STT service
           → nano-claw /api/chat (knowledge persona, tools disabled)
           → TTS 48k PCM → μ-law 8k frames out → caller hears the answer

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

import httpx
import numpy as np
from aiohttp import web

from voice import metrics_db
from voice.phone_audio import (
    FRAME_MS,
    BargeInDetector,
    UtteranceEndpointer,
    pcm48k_to_ulaw_frames,
    ulaw_decode,
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


def idle_action(idle_s: float, prompted: bool, prompt_after_s: float) -> str:
    """Pure idle-policy decision: '', 'prompt', or 'hangup'.

    One prompt per silence stretch; a further full stretch after the prompt
    (still nothing) means the caller is gone.
    """
    if idle_s < prompt_after_s:
        return ""
    return "hangup" if prompted else "prompt"


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def phone_enabled() -> bool:
    return _cfg("NANO_CLAW_PHONE") in ("1", "true", "yes")


def barge_in_enabled() -> bool:
    """Caller can interrupt the agent mid-speech (NANO_CLAW_PHONE_BARGE_IN=1).
    Off by default: the phone leg is half-duplex unless opted in."""
    return _cfg("NANO_CLAW_PHONE_BARGE_IN") in ("1", "true", "yes")


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


class PhoneCall:
    """One live call: endpointing → STT → agent → TTS, half-duplex."""

    def __init__(self, ws: web.WebSocketResponse, call_id: str) -> None:
        self.ws = ws
        self.call_id = call_id
        self.session_id = f"phone-{call_id[:24]}"
        self.endpointer = UtteranceEndpointer()
        self.barge = BargeInDetector()
        self.speaking = False
        self.interrupted = False
        self.closed = False
        self._turn_task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=120.0)
        # Idle policy: clock runs from the last time the caller spoke or the
        # agent finished speaking; one "are you still there?" per stretch.
        self.last_activity = time.monotonic()
        self.idle_prompted = False
        self._idle_task = asyncio.create_task(self._idle_watchdog())

    async def close(self) -> None:
        self.closed = True
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        await self._http.aclose()

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
        pcm = ulaw_decode(base64.b64decode(payload_b64))

        if self.speaking:
            # Barge-in (NANO_CLAW_PHONE_BARGE_IN=1): listen for the caller
            # talking over us; otherwise stay half-duplex.
            if barge_in_enabled() and self.barge.feed(pcm):
                self._interrupt()
            return

        # While a turn is still thinking (STT/LLM), ignore audio — unless we
        # just interrupted, in which case the caller's speech IS the new turn.
        if self._turn_task and not self._turn_task.done() and not self.interrupted:
            return

        utterance = self.endpointer.feed(pcm)
        if utterance:
            self._mark_activity()
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()  # interrupted turn still unwinding
            self.interrupted = False
            self._turn_task = asyncio.create_task(self._run_turn(utterance))

    def _interrupt(self) -> None:
        """Caller talked over the agent: flush Telnyx's audio buffer, stop
        speaking, and turn the interruption itself into the next utterance."""
        log.info("[phone %s] barge-in — caller interrupted", self.call_id[:8])
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

    async def _run_turn(self, pcm8k: bytes) -> None:
        try:
            text = await self._transcribe(pcm8k)
            if not text.strip():
                return
            log.info("[phone %s] caller: %s", self.call_id[:8], text)
            t0 = time.monotonic()
            reply = await self._ask_agent(text)
            log.info(
                "[phone %s] agent (%.1fs): %s",
                self.call_id[:8], time.monotonic() - t0, reply[:120],
            )
            metrics_db.bump_call_turns(_metrics_conn, self.call_id)
            await self.speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[phone %s] turn failed", self.call_id[:8])
            await self.speak("Sorry, something went wrong on my end. Try asking again.")

    async def _transcribe(self, pcm8k: bytes) -> str:
        stt_url = os.environ.get("STT_SERVICE_URL", "http://host.docker.internal:8200")
        resp = await self._http.post(
            f"{stt_url}/transcribe",
            content=pcm8k,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sample-Rate": "8000",
                # Lower-powered nodes (M1 failover) run "tiny" for speed.
                "X-Model-Size": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
            },
        )
        return resp.json().get("text", "")

    async def _ask_agent(self, text: str) -> str:
        resp = await self._http.post(
            f"{NANO_CLAW_URL}/api/chat",
            json={"message": text, "sessionId": self.session_id},
        )
        data = resp.json()
        # Tools are disabled in phone deployments, so responses are final;
        # if a tool somehow pends, decline rather than dead-air the caller.
        if data.get("type") == "tool_pending":
            return "I can't take actions over the phone, but I'm happy to answer questions."
        return data.get("response", "") or "I didn't catch that — could you say it again?"

    # ── Outbound audio ───────────────────────────────────────────

    async def speak(self, text: str) -> None:
        """Sentence-chunked TTS → μ-law frames, paced near real time."""
        if self.closed or not text:
            return
        self.speaking = True
        self.barge.reset()
        voice = _cfg("NANO_CLAW_PHONE_VOICE", "af_heart")
        loop = asyncio.get_running_loop()
        chunker = TextChunker()
        sentences = chunker.push(text)
        tail = chunker.flush()
        if tail:
            sentences.append(tail)
        try:
            for sentence in sentences:
                if self.closed or not self.speaking:
                    return  # hung up or barged in
                pcm48k = await loop.run_in_executor(None, tts_synthesize, sentence, voice, 1.0)
                for frame in pcm48k_to_ulaw_frames(pcm48k):
                    if self.closed or not self.speaking:
                        return
                    await self.ws.send_json(
                        {"event": "media", "media": {"payload": base64.b64encode(frame).decode()}}
                    )
                    # Pace slightly faster than real time: keeps Telnyx's
                    # jitter buffer fed without flooding it.
                    await asyncio.sleep(FRAME_MS / 1000 * 0.9)
        except Exception:
            log.exception("[phone %s] speak failed", self.call_id[:8])
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
        async with httpx.AsyncClient() as client:
            await _telnyx_cmd(client, cid, "answer", {
                "command_id": f"answer-{cid}",
                "stream_url": ws_url,
                "stream_track": "inbound_track",
                "stream_codec": "PCMU",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "stream_bidirectional_sampling_rate": 8000,
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
                greeting = _cfg("NANO_CLAW_PHONE_GREETING") or DEFAULT_GREETING
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
        return web.json_response({"node": _node(), "calls": [], "error": "db unavailable"})
    try:
        return web.json_response({"node": _node(), "calls": metrics_db.recent_calls(conn)})
    finally:
        conn.close()


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
    log.info("[phone] Telnyx gateway registered (webhook base: %s)",
             _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE"))
