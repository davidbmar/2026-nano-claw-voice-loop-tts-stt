"""Voice server — aiohttp + WebSocket bridge between browser and nano-claw API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import httpx
from aiohttp import WSCloseCode, web

from voice import cost_ledger, metrics_db
from voice import voice_catalog
from voice.flow_session import (
    FLOW_MODES,
    REGION_MODELS,
    FlowSession,
    get_flow_mode,
    get_region_model,
    set_flow_mode,
    set_region_model,
)
from voice.text_chunker import TextChunker
from voice.tts import synthesize as tts_synthesize
from voice.wav import pcm_to_wav
from voice import kokoro_client
from voice import lux_client
from voice.backoff import Backoff
from voice.webauth.aiohttp_adapter import (
    AUTH_ADAPTER_KEY,
    SECURITY_HEADERS,
    SESSION_COOKIE_NAME,
    AiohttpAuthAdapter,
    WebSocketIdentity,
    close_auth_adapter,
    request_security_middleware,
)
from voice.webauth.sqlite_store import (
    MAX_CONVERSATION_ID_LENGTH,
    MAX_CONVERSATION_PAGE_SIZE,
    MAX_TURN_PAGE_SIZE,
    MAX_TURN_TEXT_LENGTH,
)

if TYPE_CHECKING:
    from voice.webrtc import Session

log = logging.getLogger("voice-server")


def _on_agent_task_done(task: asyncio.Task) -> None:
    """Log unexpected failures from a spawned agent-handler task.

    A cancellation is the expected outcome of a committed barge-in
    (`Session.cancel_stream` cancels this exact task), so it's silently
    swallowed here rather than logged as an error.
    """
    if task.cancelled():
        return  # committed barge-in cancels the task on purpose
    exc = task.exception()
    if exc is not None:
        log.error("Agent task failed", exc_info=exc)


NANO_CLAW_URL = os.environ.get("NANO_CLAW_URL", "http://localhost:3001")
STATIC_DIR = Path(__file__).resolve().parent / "web"
BARGE_IN_ENABLED = os.environ.get("NANO_CLAW_BARGE_IN", "0") not in ("0", "false", "")
METRICS = metrics_db.init_db()


# no-cache: browsers must revalidate the UI on every load, otherwise tabs
# opened before a deploy keep running the old app.js (stale controls that
# silently do nothing). FileResponse still serves 304s when unchanged.
_NO_CACHE = {"Cache-Control": "no-cache"}

AUTH_SWEEP_INTERVAL_SECONDS = 60.0
DEFAULT_CONVERSATION_PAGE_SIZE = 20
DEFAULT_TURN_PAGE_SIZE = 50
MAX_CURSOR_LENGTH = 1_024


@dataclass(slots=True)
class _ActiveHistorySocket:
    ws: Any
    tenant: str
    user_sub: str


class _HistoryRuntime:
    """Coordinate live capture with owner-requested history deletion."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.active: dict[str, _ActiveHistorySocket] = {}
        self.deleted: set[str] = set()
        self.blocked_owners: set[tuple[str, str]] = set()
        self.failures: set[tuple[str, str, str]] = set()


HISTORY_RUNTIME_KEY = web.AppKey("nano_claw_history_runtime", _HistoryRuntime)


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


async def costs_page_handler(request: web.Request) -> web.FileResponse:
    """Serve the live cost console separately from the voice-control UI."""

    return web.FileResponse(STATIC_DIR / "costs.html", headers=_NO_CACHE)


async def static_handler(request: web.Request) -> web.FileResponse:
    filename = request.match_info["filename"]
    path = (STATIC_DIR / filename).resolve()
    if not path.is_relative_to(STATIC_DIR.resolve()) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers=_NO_CACHE)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    # The browser never supplies this value. A fresh, unguessable id for each
    # socket keeps agent memory and pending tool approvals conversation-local.
    conversation_id = f"voice-{uuid.uuid4().hex}"
    ws = web.WebSocketResponse()

    # Every real route request has the adapter installed by create_app().  The
    # fallback keeps direct unit-level calls transport-free; it cannot occur on
    # the registered aiohttp route.  Origin/session resolution and registration
    # all happen before the WebSocket upgrade.
    try:
        auth_adapter = request.app.get(AUTH_ADAPTER_KEY)
    except AttributeError:
        auth_adapter = None
    socket_identity = WebSocketIdentity(None, None, conversation_id)
    if auth_adapter is not None:
        ws.headers.update(SECURITY_HEADERS)
        socket_identity = await auth_adapter.bind_websocket(
            request, ws, conversation_id, prepare=True
        )
    else:
        await ws.prepare(request)
    try:
        history_runtime = request.app.get(HISTORY_RUNTIME_KEY)
    except AttributeError:
        history_runtime = None
    history_registered = False
    if (
        history_runtime is not None
        and socket_identity.user_sub is not None
        and socket_identity.tenant is not None
    ):
        history_registered = await _register_history_socket(
            history_runtime, socket_identity, ws
        )
        if not history_registered:
            await ws.close(
                code=WSCloseCode.POLICY_VIOLATION,
                message=b"history deletion in progress",
            )
    log.info("WebSocket connected")

    session: Session | None = None
    # The browser pushes its persisted set_voice/set_model/set_stt right after
    # `hello`, but the session is only created when `webrtc_offer` arrives
    # (after mic permission). Buffer early settings and apply them at session
    # creation so saved choices survive a reconnect instead of being dropped.
    pending_settings: dict = {}
    http_client = httpx.AsyncClient(timeout=120.0)

    def _spawn_agent(coro, turn_state=None):
        # One active agent reply at a time. If a reply is still in flight,
        # drop the duplicate (the browser also gates new turns behind
        # agentSpeaking) so two tasks can't race on the audio queue / WS
        # and orphan each other past barge-in's reach.
        existing = session._stream_task if session else None
        if existing is not None and not existing.done():
            log.info("Agent reply already in flight; ignoring duplicate request")
            coro.close()  # avoid 'coroutine was never awaited' warning
            return
        task = asyncio.create_task(coro)
        if turn_state is not None:
            session._turn = turn_state
        session.set_stream_task(task)
        task.add_done_callback(_on_agent_task_done)

    try:
        async for raw_msg in ws:
            if raw_msg.type != web.WSMsgType.TEXT:
                continue

            try:
                msg = json.loads(raw_msg.data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "hello":
                await ws.send_json({"type": "hello_ack", "bargeIn": BARGE_IN_ENABLED})

            elif msg_type == "webrtc_offer":
                # aiortc is only needed once a browser actually starts WebRTC.
                from voice.webrtc import Session

                session = Session()
                session._agent_session_id = conversation_id
                # Identity is fixed at the HTTP upgrade.  Browser messages can
                # never replace these server-owned values; login/logout applies
                # when the page reconnects and receives a new socket.
                session.user_sub = socket_identity.user_sub
                session.tenant = socket_identity.tenant
                session.conversation_id = conversation_id
                session._user_sub = socket_identity.user_sub
                session._tenant_id = socket_identity.tenant
                session._history_store = (
                    auth_adapter.store
                    if auth_adapter is not None
                    and socket_identity.user_sub is not None
                    and socket_identity.tenant is not None
                    else None
                )
                session._history_runtime = history_runtime
                session._history_clock = (
                    auth_adapter._now if auth_adapter is not None else None
                )
                session._history_started = False
                session._history_warning_sent = False
                session._history_agent_active = False
                session._history_agent_parts = []
                session._history_agent_failed = False
                session._backoff = Backoff()          # per-session backoff
                session._resume_task = None            # pending false-alarm resume timer
                session._scheduler_flow_enabled = get_flow_mode() == "scheduler"
                session._scheduler_flow_attempted = False
                session._scheduler_flow = None
                # Apply any settings the browser pushed before the session existed.
                if "voice" in pending_settings:
                    v = pending_settings["voice"]
                    session.set_voice(v["voiceId"], v["speed"])
                if "model" in pending_settings:
                    session.model = pending_settings["model"]
                    log.info("Model set (pending): %s", session.model or "(default)")
                if "stt" in pending_settings:
                    session.stt_size = pending_settings["stt"]
                    log.info("STT size set (pending): %s", session.stt_size)
                pending_settings.clear()
                answer_sdp = await session.handle_offer(msg["sdp"])
                await ws.send_json({"type": "webrtc_answer", "sdp": answer_sdp})

            elif msg_type == "mic_start":
                if session:
                    session.start_recording()

            elif msg_type == "mic_stop":
                if not session:
                    continue

                t0 = time.monotonic()
                text, duration, stt_ms = await session.stop_recording()
                if not text:
                    await ws.send_json({"type": "transcription", "text": ""})
                    continue
                if not await _capture_user_utterance(ws, session, text):
                    continue
                turn_state = {"t0": t0, "stt_ms": stt_ms,
                              "stt_size": session.stt_size, "voice_id": session.voice_id,
                              "model": session.model}
                await ws.send_json({"type": "transcription", "text": text})
                _spawn_agent(_handle_agent_request(ws, session, http_client, text), turn_state)

            elif msg_type == "mic_cancel":
                if session:
                    session.cancel_recording()

            elif msg_type == "text_message":
                raw_text = msg.get("text", "")
                if not isinstance(raw_text, str):
                    await ws.send_json(
                        {"type": "input_error", "error": "invalid_message"}
                    )
                    continue
                text = raw_text.strip()
                if not text or not session:
                    continue
                if not await _capture_user_utterance(ws, session, text):
                    continue
                await ws.send_json({"type": "transcription", "text": text})
                turn_state = {"t0": time.monotonic(), "stt_ms": None,
                              "stt_size": session.stt_size, "voice_id": session.voice_id,
                              "model": session.model}
                _spawn_agent(_handle_agent_request(ws, session, http_client, text), turn_state)

            elif msg_type == "set_model":
                model_id = msg.get("modelId", "") or ""
                if session:
                    session.model = model_id
                    log.info("Model set: %s", session.model or "(default)")
                else:
                    pending_settings["model"] = model_id

            elif msg_type == "set_stt":
                size = msg.get("size", "base")
                size = size if size in ("tiny", "base", "small", "medium") else "base"
                if session:
                    session.stt_size = size
                    log.info("STT size set: %s", session.stt_size)
                else:
                    pending_settings["stt"] = size

            elif msg_type == "set_voice":
                if not session:
                    pending_settings["voice"] = {
                        "voiceId": msg.get("voiceId", ""),
                        "speed": msg.get("speed", 1.0),
                    }
                    continue
                voice_id = msg.get("voiceId", "")
                session.set_voice(voice_id, msg.get("speed", 1.0))
                # Proactively warn if a native-service voice was picked but the
                # service is down — the reply will still work (Piper fallback).
                entry = voice_catalog.lookup(voice_id)
                if entry and entry["engine"] in ("kokoro", "luxtts"):
                    probe = (kokoro_client.is_healthy if entry["engine"] == "kokoro"
                             else lux_client.is_healthy)
                    label = "Kokoro" if entry["engine"] == "kokoro" else "LuxTTS"
                    loop = asyncio.get_running_loop()
                    healthy = await loop.run_in_executor(None, probe)
                    if not healthy:
                        await ws.send_json({
                            "type": "voice_notice",
                            "text": f"{label} voice unavailable — using the fast voice.",
                        })

            elif msg_type == "tool_approve":
                request_id = msg.get("requestId", "")
                if not request_id or not session:
                    continue
                _spawn_agent(_handle_tool_decision(ws, session, http_client, "approve", request_id))

            elif msg_type == "tool_reject":
                request_id = msg.get("requestId", "")
                if not request_id or not session:
                    continue
                _spawn_agent(_handle_tool_decision(ws, session, http_client, "reject", request_id))

            elif msg_type == "stop_speaking":
                if session:
                    session.stop_speaking()

            elif msg_type == "barge_in":
                if BARGE_IN_ENABLED and session:
                    # Cancel any pending resume, then pause.
                    if getattr(session, "_resume_task", None):
                        session._resume_task.cancel()
                        session._resume_task = None
                    session.pause_speaking()

            elif msg_type == "barge_in_commit":
                if BARGE_IN_ENABLED and session:
                    if getattr(session, "_resume_task", None):
                        session._resume_task.cancel()
                        session._resume_task = None
                    session.cancel_stream()          # abort reply + clear audio
                    _abandon_agent_turn(session)
                    session._backoff.reset()
                    await ws.send_json({"type": "agent_audio_end"})   # re-arm mic for the user's turn

            elif msg_type == "barge_in_false":
                if BARGE_IN_ENABLED and session and session.is_paused():
                    delay = session._backoff.next()
                    log.info("Barge-in false alarm; resuming in %.2fs", delay)

                    async def _resume_after(d, sess=session, w=ws):
                        try:
                            await asyncio.sleep(d)
                            if sess.is_paused() and not w.closed:
                                sess.resume_speaking()
                        except asyncio.CancelledError:
                            pass

                    session._resume_task = asyncio.ensure_future(_resume_after(delay))

            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

    except Exception:
        log.exception("WebSocket error")
    finally:
        if session:
            _abandon_agent_turn(session)
        if session and session._stream_task and not session._stream_task.done():
            session._stream_task.cancel()
            try:
                await session._stream_task
            except BaseException:
                pass  # CancelledError (expected) or the task's own error — we're tearing down
        if session:
            try:
                await session.close()
            except Exception:
                # Memory deletion is the privacy boundary; a WebRTC teardown
                # failure must not prevent it from running.
                log.exception("WebRTC session close failed")
        preserve_agent_memory = bool(
            socket_identity.user_sub is not None
            and session is not None
            and getattr(session, "_history_started", False)
            and (
                history_runtime is None
                or conversation_id not in history_runtime.deleted
            )
        )
        try:
            if not preserve_agent_memory:
                await _delete_agent_session(http_client, conversation_id)
            await http_client.aclose()
        finally:
            if history_registered and history_runtime is not None:
                await _unregister_history_socket(
                    history_runtime, conversation_id, ws
                )
            if auth_adapter is not None:
                await auth_adapter.unbind_websocket(ws)
        log.info("WebSocket disconnected")

    return ws


async def _register_history_socket(
    runtime: _HistoryRuntime,
    identity: WebSocketIdentity,
    ws: Any,
) -> bool:
    """Register an authenticated socket unless delete-all is in progress."""

    assert identity.tenant is not None and identity.user_sub is not None
    owner = (identity.tenant, identity.user_sub)
    async with runtime.lock:
        if owner in runtime.blocked_owners:
            return False
        runtime.active[identity.conversation_id] = _ActiveHistorySocket(
            ws=ws,
            tenant=identity.tenant,
            user_sub=identity.user_sub,
        )
        return True


async def _unregister_history_socket(
    runtime: _HistoryRuntime, conversation_id: str, ws: Any
) -> None:
    async with runtime.lock:
        entry = runtime.active.get(conversation_id)
        if entry is not None and entry.ws is ws:
            runtime.active.pop(conversation_id, None)


async def _invoke_store_method(
    callable_: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run sync storage off-loop without abandoning a transaction on cancel."""

    thread_task = asyncio.create_task(
        asyncio.to_thread(callable_, *args, **kwargs)
    )
    try:
        result = await asyncio.shield(thread_task)
    except asyncio.CancelledError:
        # A barge-in may cancel the caller, but releasing the history gate while
        # SQLite is still committing would race an owner deletion. Finish the
        # bounded DB operation before propagating cancellation.
        await asyncio.shield(thread_task)
        raise
    if not inspect.isawaitable(result):
        return result
    awaitable_task = asyncio.ensure_future(result)
    try:
        return await asyncio.shield(awaitable_task)
    except asyncio.CancelledError:
        await asyncio.shield(awaitable_task)
        raise


def _history_now(session: Any) -> datetime:
    clock = getattr(session, "_history_clock", None)
    if callable(clock):
        return clock()
    return datetime.now(timezone.utc)


def _history_owner(session: Any) -> tuple[str, str] | None:
    tenant = getattr(session, "_tenant_id", None)
    user_sub = getattr(session, "_user_sub", None)
    if not isinstance(tenant, str) or not tenant:
        return None
    if not isinstance(user_sub, str) or not user_sub:
        return None
    return tenant, user_sub


async def _send_history_write_warning(ws: Any, session: Any) -> None:
    if getattr(session, "_history_warning_sent", False):
        return
    session._history_warning_sent = True
    if getattr(ws, "closed", False):
        return
    try:
        await ws.send_json(
            {
                "type": "history_error",
                "error": "history_write_failed",
                "text": "This conversation's saved history may be incomplete.",
            }
        )
    except Exception:
        log.warning("Could not report history write failure to socket", exc_info=True)


async def _mark_history_failure(session: Any) -> None:
    owner = _history_owner(session)
    store = getattr(session, "_history_store", None)
    conversation_id = getattr(session, "conversation_id", None)
    runtime = getattr(session, "_history_runtime", None)
    if owner is None or not isinstance(conversation_id, str):
        return
    tenant, user_sub = owner
    if runtime is not None:
        runtime.failures.add((tenant, user_sub, conversation_id))
    if store is None or not getattr(session, "_history_started", False):
        return
    marker = getattr(store, "mark_conversation_incomplete", None)
    if not callable(marker):
        return
    try:
        if runtime is None:
            await _invoke_store_method(
                marker, conversation_id, tenant, user_sub
            )
        else:
            async with runtime.lock:
                await _invoke_store_method(
                    marker, conversation_id, tenant, user_sub
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        # The live socket warning and in-process failure marker remain visible
        # even when the durable marker cannot be written during a DB outage.
        log.warning("Could not persist incomplete-history marker", exc_info=True)


async def _capture_user_utterance(ws: Any, session: Any, text: Any) -> bool:
    """Persist a final user transcription, while anonymous turns stay ephemeral."""

    if not isinstance(text, str):
        if not getattr(ws, "closed", False):
            await ws.send_json(
                {"type": "input_error", "error": "invalid_message"}
            )
        return False
    if len(text) > MAX_TURN_TEXT_LENGTH:
        if not getattr(ws, "closed", False):
            await ws.send_json(
                {
                    "type": "input_error",
                    "error": "message_too_long",
                    "maxLength": MAX_TURN_TEXT_LENGTH,
                }
            )
        return False

    store = getattr(session, "_history_store", None)
    owner = _history_owner(session)
    if store is None or owner is None:
        return True
    tenant, user_sub = owner
    conversation_id = getattr(session, "conversation_id", None)
    if not isinstance(conversation_id, str):
        return True
    runtime = getattr(session, "_history_runtime", None)

    async def write() -> bool:
        if runtime is not None:
            if conversation_id in runtime.deleted:
                return False
            if (tenant, user_sub) in runtime.blocked_owners:
                return False
        if getattr(session, "_history_started", False):
            await _invoke_store_method(
                store.append_conversation_turn,
                conversation_id,
                tenant,
                user_sub,
                "user",
                text,
                _history_now(session),
            )
        else:
            await _invoke_store_method(
                store.open_conversation,
                conversation_id,
                tenant,
                user_sub,
                text,
                _history_now(session),
            )
            session._history_started = True
        return True

    try:
        if runtime is None:
            return await write()
        async with runtime.lock:
            return await write()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("History write failed for a completed user turn")
        await _mark_history_failure(session)
        await _send_history_write_warning(ws, session)
        return True


def _begin_agent_turn(session: Any) -> None:
    session._history_agent_active = True
    session._history_agent_failed = False
    session._history_agent_parts = []


def _ensure_agent_turn(session: Any) -> None:
    if not getattr(session, "_history_agent_active", False):
        _begin_agent_turn(session)


def _append_agent_delta(session: Any, text: Any) -> None:
    if not isinstance(text, str) or not text:
        return
    _ensure_agent_turn(session)
    session._history_agent_parts.append(text)


def _abandon_agent_turn(session: Any) -> None:
    session._history_agent_active = False
    session._history_agent_failed = True
    session._history_agent_parts = []


async def _complete_agent_turn(
    ws: Any, session: Any, text: str | None = None
) -> None:
    """Persist only a completed agent turn; partial/error buffers are discarded."""

    if getattr(session, "_history_agent_failed", False):
        _abandon_agent_turn(session)
        return
    if text is None:
        text = "".join(getattr(session, "_history_agent_parts", []))
    _abandon_agent_turn(session)

    store = getattr(session, "_history_store", None)
    owner = _history_owner(session)
    if (
        store is None
        or owner is None
        or not getattr(session, "_history_started", False)
    ):
        return
    tenant, user_sub = owner
    conversation_id = getattr(session, "conversation_id", None)
    if not isinstance(conversation_id, str):
        return
    runtime = getattr(session, "_history_runtime", None)

    async def write() -> None:
        if runtime is not None:
            if conversation_id in runtime.deleted:
                return
            if (tenant, user_sub) in runtime.blocked_owners:
                return
        await _invoke_store_method(
            store.append_conversation_turn,
            conversation_id,
            tenant,
            user_sub,
            "agent",
            text,
            _history_now(session),
        )

    try:
        if runtime is None:
            await write()
        else:
            async with runtime.lock:
                await write()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("History write failed for a completed agent turn")
        await _mark_history_failure(session)
        await _send_history_write_warning(ws, session)


def _agent_session_id(session: Session) -> str:
    """Return the server-owned Node session id for a browser conversation."""

    session_id = getattr(session, "_agent_session_id", None)
    if not isinstance(session_id, str) or not session_id:
        # Direct unit-level callers do not pass through websocket_handler, so
        # lazily give their synthetic Session the same server-owned identity.
        session_id = f"voice-{uuid.uuid4().hex}"
        session._agent_session_id = session_id
    return session_id


async def _delete_agent_session(client: httpx.AsyncClient, session_id: str) -> None:
    """Best-effort deletion of one conversation's Node memory."""

    try:
        response = await client.request(
            "DELETE",
            f"{NANO_CLAW_URL}/api/session",
            json={"sessionId": session_id},
            timeout=10.0,
        )
        if response.status_code >= 300:
            log.warning(
                "nano-claw session cleanup failed: status=%d",
                response.status_code,
            )
    except Exception:
        # The Node service may be restarting. Its startup/TTL orphan sweep is
        # the fallback, so disconnect cleanup must never break WebSocket close.
        log.warning("nano-claw session cleanup request failed", exc_info=True)


async def _handle_agent_request(
    ws: web.WebSocketResponse,
    session: Session,
    client: httpx.AsyncClient,
    text: str,
) -> None:
    """Stream nano-claw's reply as SSE; synthesize + forward chunks as they arrive."""
    _begin_agent_turn(session)
    try:
        if await _handle_scheduler_request(ws, session, text):
            return
        req_start = time.monotonic()
        async with client.stream(
            "POST",
            f"{NANO_CLAW_URL}/api/chat",
            json={"message": text, "sessionId": _agent_session_id(session), **({"model": session.model} if session.model else {})},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                data = json.loads(await resp.aread())
                await _process_api_response(ws, session, data, req_start=req_start)
                return
            await _consume_sse(ws, session, resp, req_start=req_start)
    except asyncio.CancelledError:
        _abandon_agent_turn(session)
        raise
    except Exception:
        _abandon_agent_turn(session)
        log.exception("nano-claw streaming call failed")
        error_text = "Sorry, I couldn't reach the agent."
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


async def _handle_scheduler_request(
    ws: web.WebSocketResponse,
    session: Session,
    text: str,
) -> bool:
    """Handle an enabled scheduler turn before the normal API route."""

    if not getattr(session, "_scheduler_flow_enabled", False):
        return False

    flow = getattr(session, "_scheduler_flow", None)
    greeting = None
    if flow is None:
        if getattr(session, "_scheduler_flow_attempted", False):
            return False
        session._scheduler_flow_attempted = True
        flow = FlowSession.create()
        if flow is None:
            session._scheduler_flow_enabled = False
            return False
        session._scheduler_flow = flow
        greeting = flow.greeting
        await ws.send_json({"type": "agent_reply", "text": greeting})
        # One audio gate covers both the greeting and the pending first reply;
        # otherwise the greeting's audio_end rearms hands-free VAD too early.
        await ws.send_json({"type": "agent_audio_start"})
        await ws.send_json(_flow_state_message(flow))

    try:
        if greeting is not None:
            await session.speak_text(greeting, session.voice_id, session.speed)

        reply = await flow.reply(text)
        log.info(
            "Scheduler flow outcome=%s slots=%s",
            reply.outcome or "continue",
            reply.slots,
        )
        if reply.done:
            # WebSocket sends and playback are cancellable during barge-in.
            # Revert before either so a completed flow cannot be stranded.
            session._scheduler_flow = None
            session._scheduler_flow_enabled = False
        await ws.send_json(_flow_state_message(flow, reply))
        await ws.send_json({"type": "agent_reply", "text": reply.text})
        completed_text = (
            f"{greeting}\n\n{reply.text}" if greeting is not None else reply.text
        )
        await _complete_agent_turn(ws, session, completed_text)
        if greeting is not None:
            await session.speak_text(reply.text, session.voice_id, session.speed)
        else:
            await _speak_with_events(ws, session, reply.text)
        return True
    finally:
        if greeting is not None and not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})


def _flow_state_message(flow, reply=None) -> dict:
    """Build the browser's defensive, read-only goal-region snapshot."""

    slots = getattr(reply, "slots", None) if reply is not None else None
    if not isinstance(slots, dict):
        slots = getattr(flow, "slots", {})
    if not isinstance(slots, dict):
        slots = {}

    rejected = getattr(reply, "rejected", []) if reply is not None else []
    if not isinstance(rejected, (list, tuple)):
        rejected = []

    turns_used = getattr(reply, "turns_used", None) if reply is not None else None
    if not isinstance(turns_used, int) or isinstance(turns_used, bool):
        turns_used = getattr(flow, "turns_used", 0)
    if not isinstance(turns_used, int) or isinstance(turns_used, bool):
        turns_used = 0
    max_turns = getattr(reply, "max_turns", None) if reply is not None else None
    if not isinstance(max_turns, int) or isinstance(max_turns, bool):
        max_turns = getattr(flow, "max_turns", 0)
    if not isinstance(max_turns, int) or isinstance(max_turns, bool):
        max_turns = 0

    supervisor_ms = (
        getattr(reply, "supervisor_ms", None) if reply is not None else None
    )
    if not isinstance(supervisor_ms, (int, float)) or isinstance(supervisor_ms, bool):
        supervisor_ms = None

    return {
        "type": "flow_state",
        "goal": str(getattr(flow, "goal", "") or ""),
        "outcome": getattr(reply, "outcome", None) if reply is not None else None,
        "slots": dict(slots),
        "rejected": [str(item) for item in rejected],
        "turns_used": int(turns_used),
        "max_turns": int(max_turns),
        "supervisor_ms": supervisor_ms,
    }


async def _consume_sse(
    ws: web.WebSocketResponse,
    session: Session,
    resp: httpx.Response,
    req_start: float | None = None,
) -> None:
    """Parse SSE frames, speaking each chunk and forwarding text to the browser."""
    # Redundant with the spawn-time set_stream_task() in websocket_handler:
    # this coroutine now always runs inside that same spawned task, so
    # current_task() here IS the task already registered on the session.
    # Left in place as a harmless no-op / safety net.
    session.set_stream_task(asyncio.current_task())
    if req_start is None:
        req_start = time.monotonic()
    first_delta = None
    first_audio = None
    chunker = TextChunker()
    loop = asyncio.get_running_loop()
    total_bytes = 0
    event = ""
    data_lines: list[str] = []
    final_seen = False
    stream_failed = False

    _ensure_agent_turn(session)

    async def speak_chunk(chunk: str):
        nonlocal total_bytes, first_audio
        await ws.send_json({"type": "agent_reply_delta", "text": chunk})
        queued_bytes = await loop.run_in_executor(
            None, session.enqueue_chunk, chunk, session.voice_id, session.speed
        )
        total_bytes += queued_bytes
        if queued_bytes and first_audio is None:
            first_audio = time.monotonic()

    session.begin_stream()
    await ws.send_json({"type": "agent_audio_start"})
    try:
        async for raw in resp.aiter_lines():
            if raw == "":  # frame boundary
                payload = "\n".join(data_lines)
                data_lines = []
                ev, event = event, ""
                if not payload:
                    continue
                obj = json.loads(payload)
                if ev == "delta":
                    delta = obj.get("text", "")
                    _append_agent_delta(session, delta)
                    if first_delta is None:
                        first_delta = time.monotonic()
                    for chunk in chunker.push(delta):
                        await speak_chunk(chunk)
                elif ev == "tool_pending":
                    tail = chunker.flush()
                    if tail:
                        await speak_chunk(tail)
                    parts = getattr(session, "_history_agent_parts", None)
                    if isinstance(parts, list) and parts:
                        parts.append("\n\n")
                    _stash_turn_metrics(
                        session, req_start, first_delta, first_audio, obj.get("debug") or {}
                    )
                    await ws.send_json({"type": "tool_pending", "requestId": obj["requestId"], "tools": obj["tools"]})
                    await ws.send_json({"type": "agent_audio_end"})
                    return
                elif ev == "final":
                    final_seen = True
                    parts = getattr(session, "_history_agent_parts", None)
                    response_text = obj.get("response", "")
                    if (
                        isinstance(parts, list)
                        and not parts
                        and isinstance(response_text, str)
                    ):
                        parts.append(response_text)
                    tail = chunker.flush()
                    if tail:
                        await speak_chunk(tail)
                    debug = obj.get("debug") or {}
                    if debug:
                        await ws.send_json({"type": "debug", **debug})
                    _write_turn_metrics(session, req_start, first_delta, first_audio, debug)
                    if not stream_failed:
                        await _complete_agent_turn(ws, session)
                    await ws.send_json({"type": "agent_reply_done"})
                elif ev == "error":
                    stream_failed = True
                    _abandon_agent_turn(session)
                    await ws.send_json({"type": "agent_reply", "text": f"Error: {obj.get('error', 'agent error')}"})
                continue
            if raw.startswith("event:"):
                event = raw[6:].strip()
            elif raw.startswith("data:"):
                data_lines.append(raw[5:].strip())

        await session.end_stream(total_bytes)
        session._backoff.reset()   # clean drain — clear consecutive-false count
        if not final_seen:
            _abandon_agent_turn(session)
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})
    except Exception:
        _abandon_agent_turn(session)
        session.stop_speaking()
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})
        raise


def _ms(a, b):
    return int((b - a) * 1000) if (a is not None and b is not None) else None


def _sum_metric_values(*values):
    numbers = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return sum(numbers) if numbers else None


def _generation_ms(debug):
    total_ms = debug.get("durationMs")
    first_token_ms = debug.get("firstTokenMs")
    if not isinstance(total_ms, (int, float)) or not isinstance(first_token_ms, (int, float)):
        return None
    return max(1, total_ms - first_token_ms)


def _stash_turn_metrics(session, req_start, first_delta, first_audio, debug):
    """Best-effort accumulation for a turn paused on tool approval."""
    try:
        turn = getattr(session, "_turn", None)
        if not isinstance(turn, dict):
            return
        partial = turn.get("_metrics")
        if not isinstance(partial, dict):
            partial = {}
        usage = debug.get("tokenUsage") or {}
        tokens_out = usage.get("completion")
        current_gen_ms = _generation_ms(debug)
        turn["_metrics"] = {
            "tokens_in": _sum_metric_values(partial.get("tokens_in"), usage.get("prompt")),
            "tokens_out": _sum_metric_values(partial.get("tokens_out"), tokens_out),
            "llm_total_ms": _sum_metric_values(partial.get("llm_total_ms"), debug.get("durationMs")),
            "generation_ms": _sum_metric_values(partial.get("generation_ms"), current_gen_ms),
            "generation_complete": partial.get("generation_complete", True)
            and (not tokens_out or current_gen_ms is not None),
            "t0": partial.get("t0", turn.get("t0", req_start)),
            "req_start": partial.get("req_start", req_start),
            "first_delta": partial.get("first_delta") if partial.get("first_delta") is not None else first_delta,
            "first_audio": partial.get("first_audio") if partial.get("first_audio") is not None else first_audio,
        }
    except Exception:
        log.exception("metrics: failed to stash partial turn")


def _write_turn_metrics(session, req_start, first_delta, first_audio, debug):
    try:
        turn = getattr(session, "_turn", {}) or {}
        partial = turn.get("_metrics") or {}
        usage = debug.get("tokenUsage") or {}
        current_tokens_out = usage.get("completion")
        current_gen_ms = _generation_ms(debug)
        req_start = partial.get("req_start", req_start)
        first_delta = partial.get("first_delta") if partial.get("first_delta") is not None else first_delta
        first_audio = partial.get("first_audio") if partial.get("first_audio") is not None else first_audio
        t0 = partial.get("t0", turn.get("t0", req_start))
        tokens_in = _sum_metric_values(partial.get("tokens_in"), usage.get("prompt"))
        tokens_out = _sum_metric_values(partial.get("tokens_out"), current_tokens_out)
        total_ms = _sum_metric_values(partial.get("llm_total_ms"), debug.get("durationMs"))
        gen_ms = _sum_metric_values(partial.get("generation_ms"), current_gen_ms)
        generation_complete = partial.get("generation_complete", True) and (
            not current_tokens_out or current_gen_ms is not None
        )
        tok_per_sec = round(tokens_out / (gen_ms / 1000), 2) if (
            tokens_out and gen_ms and generation_complete
        ) else None
        model = turn.get("model") or debug.get("model") or ""
        provider = model.split("/")[0] if "/" in model else None
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_id": _agent_session_id(session), "provider": provider, "model": model,
            "model_version": debug.get("model"),
            "stt_size": turn.get("stt_size"), "voice_id": turn.get("voice_id"),
            "stt_ms": turn.get("stt_ms"),
            "llm_ttft_ms": _ms(req_start, first_delta),
            "llm_total_ms": total_ms,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "tok_per_sec": tok_per_sec,
            "tts_ms": _ms(first_delta, first_audio),
            "e2e_ms": _ms(t0, first_audio),
            "est_cost_usd": metrics_db.estimate_cost(METRICS, model, tokens_in, tokens_out) if METRICS else None,
        }
        metrics_db.record_turn(METRICS, rec)
        turn.pop("_metrics", None)
    except Exception:
        log.exception("metrics: failed to assemble turn record")


async def _handle_tool_decision(
    ws: web.WebSocketResponse,
    session: Session,
    client: httpx.AsyncClient,
    action: str,
    request_id: str,
) -> None:
    """POST approve/reject to nano-claw API and handle response."""
    _ensure_agent_turn(session)
    try:
        endpoint = f"{NANO_CLAW_URL}/api/chat/{action}"
        req_start = time.monotonic()
        async with client.stream(
            "POST",
            endpoint,
            json={"requestId": request_id, "sessionId": _agent_session_id(session)},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                data = json.loads(await resp.aread())
                await _process_api_response(ws, session, data, req_start=req_start)
                return
            await _consume_sse(ws, session, resp, req_start=req_start)
    except asyncio.CancelledError:
        _abandon_agent_turn(session)
        raise
    except Exception:
        _abandon_agent_turn(session)
        log.exception("nano-claw API %s call failed", action)
        error_text = "Sorry, tool execution failed."
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


async def _speak_with_events(
    ws: web.WebSocketResponse,
    session: Session,
    text: str,
) -> float | None:
    """Keep browser VAD muted until synthesized audio actually finishes."""
    await ws.send_json({"type": "agent_audio_start"})
    try:
        return await session.speak_text(text, session.voice_id, session.speed)
    finally:
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})


async def _process_api_response(
    ws: web.WebSocketResponse,
    session: Session,
    data: dict,
    req_start: float | None = None,
) -> None:
    """Route an API response to the browser and optionally TTS."""
    if req_start is None:
        req_start = time.monotonic()
    # Forward debug info if present
    debug = data.get("debug")
    if debug:
        log.info(
            "iter=%d msgs=%d model=%s tokens=%s duration=%dms finish=%s",
            debug.get("iteration", 0),
            debug.get("messageCount", 0),
            debug.get("model", "?"),
            debug.get("tokenUsage"),
            debug.get("durationMs", 0),
            debug.get("finishReason"),
        )
        await ws.send_json({"type": "debug", **debug})

    if data.get("type") == "final":
        reply = data.get("response", "")
        await ws.send_json({"type": "agent_reply", "text": reply})
        pending_parts = getattr(session, "_history_agent_parts", [])
        completed_reply = (
            "".join(pending_parts) + reply
            if isinstance(pending_parts, list) and pending_parts
            else reply
        )
        await _complete_agent_turn(ws, session, completed_reply)
        first_audio = None
        if reply:
            first_audio = await _speak_with_events(ws, session, reply)
        else:
            await ws.send_json({"type": "agent_audio_end"})
        _write_turn_metrics(session, req_start, None, first_audio, debug or {})
    elif data.get("type") == "tool_pending":
        _stash_turn_metrics(session, req_start, None, None, debug or {})
        await ws.send_json({
            "type": "tool_pending",
            "requestId": data["requestId"],
            "tools": data["tools"],
        })
    elif data.get("error"):
        _abandon_agent_turn(session)
        error_text = f"Error: {data['error']}"
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


def _history_json_error(error: str, status: int) -> web.Response:
    return web.json_response({"error": error}, status=status)


async def _resolve_history_request(
    request: web.Request,
) -> tuple[AiohttpAuthAdapter, Any, dict[str, str]] | web.Response:
    adapter = request.app.get(AUTH_ADAPTER_KEY)
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if adapter is None or not raw_token:
        return _history_json_error("unauthenticated", 401)
    try:
        identity = await adapter._resolve_raw_token(raw_token)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Authentication store failed for history request")
        return _history_json_error("auth_unavailable", 503)
    if identity is None:
        await adapter.close_bound_sockets(raw_token)
        response = _history_json_error("unauthenticated", 401)
        adapter._clear_cookie(response, SESSION_COOKIE_NAME)
        return response
    store = adapter.store
    if store is None:
        return _history_json_error("history_unavailable", 503)
    return adapter, store, dict(identity)


def _single_query_value(request: web.Request, name: str) -> str | None:
    values = request.query.getall(name, [])
    if len(values) > 1:
        raise ValueError(f"duplicate {name}")
    return values[0] if values else None


def _page_size(request: web.Request, *, default: int, maximum: int) -> int:
    raw = _single_query_value(request, "limit")
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("limit must be a positive decimal integer")
    value = int(raw)
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _encode_history_cursor(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _decode_history_cursor(raw: str | None, *, kind: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not raw or len(raw) > MAX_CURSOR_LENGTH or not raw.isascii():
        raise ValueError("invalid cursor")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(
            padded, altchars=b"-_", validate=True
        )
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid cursor") from None
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("kind") != kind
    ):
        raise ValueError("invalid cursor")
    return payload


def _conversation_cursor(
    request: web.Request,
) -> tuple[float | None, str | None]:
    payload = _decode_history_cursor(
        _single_query_value(request, "cursor"), kind="conversations"
    )
    if payload is None:
        return None, None
    if set(payload) != {"v", "kind", "startedAt", "id"}:
        raise ValueError("invalid cursor")
    started_at = payload["startedAt"]
    identifier = payload["id"]
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(float(started_at))
        or not isinstance(identifier, str)
        or not 1 <= len(identifier) <= MAX_CONVERSATION_ID_LENGTH
    ):
        raise ValueError("invalid cursor")
    return float(started_at), identifier


def _turn_cursor(request: web.Request, conversation_id: str) -> int:
    payload = _decode_history_cursor(
        _single_query_value(request, "cursor"), kind="turns"
    )
    if payload is None:
        return -1
    if set(payload) != {"v", "kind", "conversationId", "seq"}:
        raise ValueError("invalid cursor")
    sequence = payload["seq"]
    if (
        payload["conversationId"] != conversation_id
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
    ):
        raise ValueError("invalid cursor")
    return sequence


def _iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _history_incomplete(
    request: web.Request, identity: dict[str, str], row: dict[str, Any]
) -> bool:
    if bool(row.get("history_incomplete")):
        return True
    runtime = request.app.get(HISTORY_RUNTIME_KEY)
    return runtime is not None and (
        identity["tenant"], identity["sub"], str(row["id"])
    ) in runtime.failures


def _conversation_payload(
    request: web.Request, identity: dict[str, str], row: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "startedAt": _iso_timestamp(row.get("started_at")),
        "endedAt": _iso_timestamp(row.get("ended_at")),
        "turnCount": int(row.get("turn_count", 0)),
        "incomplete": _history_incomplete(request, identity, row),
    }


async def conversations_list_handler(request: web.Request) -> web.Response:
    resolved = await _resolve_history_request(request)
    if isinstance(resolved, web.Response):
        return resolved
    _, store, identity = resolved
    try:
        limit = _page_size(
            request,
            default=DEFAULT_CONVERSATION_PAGE_SIZE,
            maximum=MAX_CONVERSATION_PAGE_SIZE,
        )
        before_started_at, before_id = _conversation_cursor(request)
    except ValueError:
        return _history_json_error("invalid_pagination", 400)
    try:
        rows = await _invoke_store_method(
            store.list_conversations,
            identity["tenant"],
            identity["sub"],
            limit=limit + 1,
            before_started_at=before_started_at,
            before_id=before_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("History store failed while listing conversations")
        return _history_json_error("history_unavailable", 503)
    if not isinstance(rows, list):
        return _history_json_error("history_unavailable", 503)
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_history_cursor(
            {
                "v": 1,
                "kind": "conversations",
                "startedAt": last["started_at"],
                "id": last["id"],
            }
        )
    return web.json_response(
        {
            "conversations": [
                _conversation_payload(request, identity, row) for row in page
            ],
            "nextCursor": next_cursor,
        }
    )


async def conversation_detail_handler(request: web.Request) -> web.Response:
    conversation_id = request.match_info.get("id", "")
    if not 1 <= len(conversation_id) <= MAX_CONVERSATION_ID_LENGTH:
        return _history_json_error("invalid_conversation_id", 400)
    resolved = await _resolve_history_request(request)
    if isinstance(resolved, web.Response):
        return resolved
    _, store, identity = resolved
    try:
        limit = _page_size(
            request, default=DEFAULT_TURN_PAGE_SIZE, maximum=MAX_TURN_PAGE_SIZE
        )
        after_seq = _turn_cursor(request, conversation_id)
    except ValueError:
        return _history_json_error("invalid_pagination", 400)
    try:
        result = await _invoke_store_method(
            store.get_conversation_page,
            conversation_id,
            identity["tenant"],
            identity["sub"],
            limit=limit + 1,
            after_seq=after_seq,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("History store failed while reading a conversation")
        return _history_json_error("history_unavailable", 503)
    if result is None:
        return _history_json_error("not_found", 404)
    conversation, turns = result
    has_more = len(turns) > limit
    page = turns[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = _encode_history_cursor(
            {
                "v": 1,
                "kind": "turns",
                "conversationId": conversation_id,
                "seq": page[-1]["seq"],
            }
        )
    return web.json_response(
        {
            "conversation": _conversation_payload(
                request, identity, conversation
            ),
            "turns": [
                {
                    "seq": int(turn["seq"]),
                    "role": str(turn["role"]),
                    "text": str(turn["text"]),
                    "ts": _iso_timestamp(turn["ts"]),
                }
                for turn in page
            ],
            "nextCursor": next_cursor,
        }
    )


async def _close_history_socket(ws: Any) -> None:
    if getattr(ws, "closed", False):
        return
    try:
        await ws.close(
            code=WSCloseCode.POLICY_VIOLATION,
            message=b"conversation deleted",
        )
    except Exception:
        log.warning("Could not close deleted conversation socket", exc_info=True)


async def _delete_agent_memories(conversation_ids: list[str]) -> None:
    if not conversation_ids:
        return
    client = httpx.AsyncClient(timeout=10.0)
    try:
        for conversation_id in conversation_ids:
            await _delete_agent_session(client, conversation_id)
    finally:
        await client.aclose()


async def conversation_delete_handler(request: web.Request) -> web.Response:
    conversation_id = request.match_info.get("id", "")
    if not 1 <= len(conversation_id) <= MAX_CONVERSATION_ID_LENGTH:
        return _history_json_error("invalid_conversation_id", 400)
    resolved = await _resolve_history_request(request)
    if isinstance(resolved, web.Response):
        return resolved
    _, store, identity = resolved
    runtime = request.app[HISTORY_RUNTIME_KEY]

    active_ws = None
    async with runtime.lock:
        entry = runtime.active.get(conversation_id)
        if (
            entry is not None
            and entry.tenant == identity["tenant"]
            and entry.user_sub == identity["sub"]
        ):
            # The tombstone makes every later capture a no-op. A capture that
            # already held the gate has finished before this point and will be
            # removed by the cascading delete below.
            runtime.deleted.add(conversation_id)
            active_ws = entry.ws
    if active_ws is not None:
        await _close_history_socket(active_ws)

    try:
        deleted = await _invoke_store_method(
            store.delete_conversation,
            conversation_id,
            identity["tenant"],
            identity["sub"],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("History store failed while deleting a conversation")
        return _history_json_error("history_unavailable", 503)
    if not deleted:
        return _history_json_error("not_found", 404)
    async with runtime.lock:
        runtime.deleted.add(conversation_id)
        runtime.failures.discard(
            (identity["tenant"], identity["sub"], conversation_id)
        )
    await _delete_agent_memories([conversation_id])
    return web.json_response({"ok": True})


async def conversations_delete_all_handler(request: web.Request) -> web.Response:
    resolved = await _resolve_history_request(request)
    if isinstance(resolved, web.Response):
        return resolved
    _, store, identity = resolved
    runtime = request.app[HISTORY_RUNTIME_KEY]
    owner = (identity["tenant"], identity["sub"])

    async with runtime.lock:
        runtime.blocked_owners.add(owner)
        active = [
            (conversation_id, entry.ws)
            for conversation_id, entry in runtime.active.items()
            if (entry.tenant, entry.user_sub) == owner
        ]
        runtime.deleted.update(conversation_id for conversation_id, _ in active)
    try:
        await asyncio.gather(
            *(_close_history_socket(ws) for _, ws in active)
        )
        deleted_ids = await _invoke_store_method(
            store.delete_all_conversations,
            identity["tenant"],
            identity["sub"],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("History store failed while deleting all conversations")
        return _history_json_error("history_unavailable", 503)
    finally:
        async with runtime.lock:
            runtime.blocked_owners.discard(owner)

    if not isinstance(deleted_ids, list) or not all(
        isinstance(value, str) for value in deleted_ids
    ):
        return _history_json_error("history_unavailable", 503)
    async with runtime.lock:
        runtime.deleted.update(deleted_ids)
        owner_failures = {
            failure for failure in runtime.failures if failure[:2] == owner
        }
        runtime.failures.difference_update(owner_failures)
    memory_ids = list(
        dict.fromkeys(
            [*deleted_ids, *(conversation_id for conversation_id, _ in active)]
        )
    )
    await _delete_agent_memories(memory_ids)
    return web.json_response({"ok": True, "deleted": len(deleted_ids)})


async def _run_auth_sweep_once(
    adapter: AiohttpAuthAdapter, now: datetime | None = None
) -> None:
    current = adapter._now() if now is None else now
    if adapter.store is not None:
        try:
            await _invoke_store_method(adapter.store.sweep, current)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic auth session sweep failed")
    try:
        await adapter.close_expired_sockets(current)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Periodic expired-socket sweep failed")


async def _auth_sweep_loop(adapter: AiohttpAuthAdapter) -> None:
    try:
        while True:
            await _run_auth_sweep_once(adapter)
            await asyncio.sleep(AUTH_SWEEP_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


async def _auth_sweep_context(app: web.Application):
    adapter = app[AUTH_ADAPTER_KEY]
    task = asyncio.create_task(_auth_sweep_loop(adapter))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


_PREVIEW_SAMPLES = {
    "a": "Hi, this is how I sound.",
    "b": "Hi, this is how I sound.",
    "e": "Hola, así es como sueno.",
}


async def voices_handler(request: web.Request) -> web.Response:
    return web.json_response(voice_catalog.grouped_for_ui())


async def models_handler(request: web.Request) -> web.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{NANO_CLAW_URL}/api/models")
        return web.json_response(resp.json())


async def metrics_handler(request: web.Request) -> web.Response:
    if METRICS is None:
        return web.json_response({"recent": [], "byModel": []})
    recent = [
        {key: value for key, value in row.items() if key not in {"asked_text", "said_text"}}
        for row in metrics_db.recent(METRICS, 50)
    ]
    return web.json_response({
        "recent": recent,
        "byModel": metrics_db.aggregates(METRICS),
    })


async def costs_handler(request: web.Request) -> web.Response:
    """Return the privacy-safe cost ledger aggregation used by ``/costs``."""

    return web.json_response(cost_ledger.build_report(METRICS))


def _flow_api_payload() -> dict:
    return {
        "active": get_flow_mode(),
        "options": list(FLOW_MODES),
        "availability_ok": FlowSession.availability_ok(),
    }


async def flow_get_handler(request: web.Request) -> web.Response:
    """Report the flow used for new browser sessions and phone calls."""

    return web.json_response(_flow_api_payload())


async def flow_set_handler(request: web.Request) -> web.Response:
    """Set the flow used for new browser sessions and phone calls."""

    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.Response(status=400, text="bad json")
    if not isinstance(body, dict):
        return web.Response(status=400, text="bad json")
    mode = str(body.get("mode", "")).lower()
    if not set_flow_mode(mode):
        return web.Response(status=400, text=f"unknown mode: {mode}")
    return web.json_response(_flow_api_payload())


def _region_model_api_payload() -> dict:
    return {
        "active": get_region_model(),
        "options": [
            {"value": value, "label": label}
            for value, label in REGION_MODELS.items()
        ],
    }


async def region_model_get_handler(request: web.Request) -> web.Response:
    """Report the supervisor model used when the next scheduler turn starts."""

    return web.json_response(_region_model_api_payload())


async def region_model_set_handler(request: web.Request) -> web.Response:
    """Set the supervisor model used when the next scheduler turn starts."""

    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.Response(status=400, text="bad json")
    if not isinstance(body, dict):
        return web.Response(status=400, text="bad json")
    model = body.get("model", "")
    if not set_region_model(model):
        return web.Response(status=400, text=f"unknown model: {model}")
    return web.json_response(_region_model_api_payload())


async def preview_handler(request: web.Request) -> web.Response:
    body = await request.json()
    voice_id = body.get("voiceId", "")
    entry = voice_catalog.lookup(voice_id)
    if not entry:
        raise web.HTTPBadRequest(text="unknown voice")
    sample = _PREVIEW_SAMPLES.get(entry.get("lang", "a"), _PREVIEW_SAMPLES["a"])
    loop = asyncio.get_running_loop()
    pcm_48k = await loop.run_in_executor(None, tts_synthesize, sample, voice_id, 1.0)
    wav = pcm_to_wav(pcm_48k, 48000)
    return web.Response(body=wav, content_type="audio/wav")


def create_app(
    auth_adapter: AiohttpAuthAdapter | None = None,
) -> web.Application:
    from voice import phone

    app = web.Application(middlewares=[request_security_middleware])
    adapter = auth_adapter or AiohttpAuthAdapter.from_environment()
    app[AUTH_ADAPTER_KEY] = adapter
    app[HISTORY_RUNTIME_KEY] = _HistoryRuntime()
    app.cleanup_ctx.append(_auth_sweep_context)
    app.on_cleanup.append(close_auth_adapter)
    cost_ledger.ensure_schema(METRICS)
    app.router.add_get("/", index_handler)
    app.router.add_get("/costs", costs_page_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/voices", voices_handler)
    app.router.add_post("/api/preview", preview_handler)
    app.router.add_get("/api/models", models_handler)
    app.router.add_get("/api/metrics", metrics_handler)
    app.router.add_get("/api/costs", costs_handler)
    app.router.add_get("/api/voice/flow", flow_get_handler)
    app.router.add_post("/api/voice/flow", flow_set_handler)
    app.router.add_get("/api/voice/region-model", region_model_get_handler)
    app.router.add_post("/api/voice/region-model", region_model_set_handler)
    app.router.add_get("/api/conversations", conversations_list_handler)
    app.router.add_delete(
        "/api/conversations", conversations_delete_all_handler
    )
    app.router.add_get(
        "/api/conversations/{id}", conversation_detail_handler
    )
    app.router.add_delete(
        "/api/conversations/{id}", conversation_delete_handler
    )
    # Auth routes must precede the one-segment flat static route below or
    # aiohttp will let that catch public API names as filenames.
    adapter.register_routes(app)
    phone.register_phone_routes(app)  # no-op unless NANO_CLAW_PHONE=1
    # The gateway now lives in voice.phone (the original design predates that
    # split).  Install a runtime adapter so call-end receipts remain isolated
    # in the portable cost_ledger module and the phone hot path stays untouched.
    cost_ledger.install_phone_tracking(
        phone,
        lambda: getattr(phone, "_metrics_conn", None) or METRICS,
    )
    cost_ledger.ensure_schema(getattr(phone, "_metrics_conn", None))
    app.router.add_get("/{filename}", static_handler)
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-14s %(levelname)-5s %(message)s",
    )
    port = int(os.environ.get("VOICE_PORT", "8080"))
    app = create_app()
    log.info("Voice server starting on port %d", port)
    web.run_app(app, port=port, print=None)
    log.info("Voice server stopped")
