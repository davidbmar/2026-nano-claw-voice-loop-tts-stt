"""Voice server — aiohttp + WebSocket bridge between browser and nano-claw API."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from aiohttp import web

from voice.webrtc import Session
from voice import metrics_db
from voice import voice_catalog
from voice.text_chunker import TextChunker
from voice.tts import synthesize as tts_synthesize
from voice.wav import pcm_to_wav
from voice import kokoro_client
from voice.backoff import Backoff

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
SESSION_ID = "voice-default"
STATIC_DIR = Path(__file__).resolve().parent / "web"
BARGE_IN_ENABLED = os.environ.get("NANO_CLAW_BARGE_IN", "0") not in ("0", "false", "")
METRICS = metrics_db.init_db()


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def static_handler(request: web.Request) -> web.FileResponse:
    filename = request.match_info["filename"]
    path = (STATIC_DIR / filename).resolve()
    if not path.is_relative_to(STATIC_DIR.resolve()) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("WebSocket connected")

    session: Session | None = None
    http_client = httpx.AsyncClient(timeout=120.0)

    def _spawn_agent(coro):
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
                session = Session()
                session._backoff = Backoff()          # per-session backoff
                session._resume_task = None            # pending false-alarm resume timer
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
                session._turn = {"t0": t0, "asked": text, "stt_ms": stt_ms,
                                 "stt_size": session.stt_size, "voice_id": session.voice_id,
                                 "model": session.model}
                await ws.send_json({"type": "transcription", "text": text})
                _spawn_agent(_handle_agent_request(ws, session, http_client, text))

            elif msg_type == "mic_cancel":
                if session:
                    session.cancel_recording()

            elif msg_type == "text_message":
                text = msg.get("text", "").strip()
                if not text or not session:
                    continue
                await ws.send_json({"type": "transcription", "text": text})
                session._turn = {"t0": time.monotonic(), "asked": text, "stt_ms": None,
                                 "stt_size": session.stt_size, "voice_id": session.voice_id,
                                 "model": session.model}
                _spawn_agent(_handle_agent_request(ws, session, http_client, text))

            elif msg_type == "set_model":
                if session:
                    session.model = msg.get("modelId", "") or ""
                    log.info("Model set: %s", session.model or "(default)")

            elif msg_type == "set_stt":
                if session:
                    size = msg.get("size", "base")
                    session.stt_size = size if size in ("tiny", "base", "small", "medium") else "base"
                    log.info("STT size set: %s", session.stt_size)

            elif msg_type == "set_voice":
                if not session:
                    continue
                voice_id = msg.get("voiceId", "")
                session.set_voice(voice_id, msg.get("speed", 1.0))
                # Proactively warn if a Kokoro voice was picked but the service
                # is down — the reply will still work (Piper fallback).
                entry = voice_catalog.lookup(voice_id)
                if entry and entry["engine"] == "kokoro":
                    loop = asyncio.get_running_loop()
                    healthy = await loop.run_in_executor(None, kokoro_client.is_healthy)
                    if not healthy:
                        await ws.send_json({
                            "type": "voice_notice",
                            "text": "Kokoro voice unavailable — using the fast voice.",
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
        if session and session._stream_task and not session._stream_task.done():
            session._stream_task.cancel()
            try:
                await session._stream_task
            except BaseException:
                pass  # CancelledError (expected) or the task's own error — we're tearing down
        await http_client.aclose()
        if session:
            await session.close()
        log.info("WebSocket disconnected")

    return ws


async def _handle_agent_request(
    ws: web.WebSocketResponse,
    session: Session,
    client: httpx.AsyncClient,
    text: str,
) -> None:
    """Stream nano-claw's reply as SSE; synthesize + forward chunks as they arrive."""
    try:
        async with client.stream(
            "POST",
            f"{NANO_CLAW_URL}/api/chat",
            json={"message": text, "sessionId": SESSION_ID, **({"model": session.model} if session.model else {})},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                data = json.loads(await resp.aread())
                await _process_api_response(ws, session, data)
                return
            await _consume_sse(ws, session, resp)
    except Exception:
        log.exception("nano-claw streaming call failed")
        error_text = "Sorry, I couldn't reach the agent."
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


async def _consume_sse(
    ws: web.WebSocketResponse,
    session: Session,
    resp: httpx.Response,
) -> None:
    """Parse SSE frames, speaking each chunk and forwarding text to the browser."""
    # Redundant with the spawn-time set_stream_task() in websocket_handler:
    # this coroutine now always runs inside that same spawned task, so
    # current_task() here IS the task already registered on the session.
    # Left in place as a harmless no-op / safety net.
    session.set_stream_task(asyncio.current_task())
    req_start = time.monotonic()
    first_delta = None
    first_audio = None
    said_parts = []
    chunker = TextChunker()
    loop = asyncio.get_running_loop()
    total_bytes = 0
    event = ""
    data_lines: list[str] = []

    async def speak_chunk(chunk: str):
        nonlocal total_bytes, first_audio
        if first_audio is None:
            first_audio = time.monotonic()
        said_parts.append(chunk)
        await ws.send_json({"type": "agent_reply_delta", "text": chunk})
        total_bytes += await loop.run_in_executor(
            None, session.enqueue_chunk, chunk, session.voice_id, session.speed
        )

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
                    if first_delta is None:
                        first_delta = time.monotonic()
                    for chunk in chunker.push(obj.get("text", "")):
                        await speak_chunk(chunk)
                elif ev == "tool_pending":
                    tail = chunker.flush()
                    if tail:
                        await speak_chunk(tail)
                    await ws.send_json({"type": "tool_pending", "requestId": obj["requestId"], "tools": obj["tools"]})
                    await ws.send_json({"type": "agent_audio_end"})
                    return
                elif ev == "final":
                    tail = chunker.flush()
                    if tail:
                        said_parts.append(tail)
                        await speak_chunk(tail)
                    debug = obj.get("debug") or {}
                    if debug:
                        await ws.send_json({"type": "debug", **debug})
                    _write_turn_metrics(session, req_start, first_delta, first_audio, said_parts, debug)
                    await ws.send_json({"type": "agent_reply_done"})
                elif ev == "error":
                    await ws.send_json({"type": "agent_reply", "text": f"Error: {obj.get('error', 'agent error')}"})
                continue
            if raw.startswith("event:"):
                event = raw[6:].strip()
            elif raw.startswith("data:"):
                data_lines.append(raw[5:].strip())

        await session.end_stream(total_bytes)
        session._backoff.reset()   # clean drain — clear consecutive-false count
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})
    except Exception:
        session.stop_speaking()
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})
        raise


def _ms(a, b):
    return int((b - a) * 1000) if (a is not None and b is not None) else None


def _write_turn_metrics(session, req_start, first_delta, first_audio, said_parts, debug):
    try:
        turn = getattr(session, "_turn", {}) or {}
        t0 = turn.get("t0", req_start)
        tokens_in = (debug.get("tokenUsage") or {}).get("prompt")
        tokens_out = (debug.get("tokenUsage") or {}).get("completion")
        total_ms = debug.get("durationMs")
        gen_ms = None
        if total_ms is not None and debug.get("firstTokenMs") is not None:
            gen_ms = max(1, total_ms - debug["firstTokenMs"])
        tok_per_sec = round(tokens_out / (gen_ms / 1000), 2) if (tokens_out and gen_ms) else None
        model = turn.get("model") or debug.get("model") or ""
        provider = model.split("/")[0] if "/" in model else None
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_id": SESSION_ID, "provider": provider, "model": model,
            "model_version": debug.get("model"),
            "stt_size": turn.get("stt_size"), "voice_id": turn.get("voice_id"),
            "asked_text": turn.get("asked"), "said_text": " ".join(said_parts).strip() or None,
            "stt_ms": turn.get("stt_ms"),
            "llm_ttft_ms": _ms(req_start, first_delta),
            "llm_total_ms": total_ms,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "tok_per_sec": tok_per_sec,
            "tts_ms": _ms(first_delta, first_audio),
            "e2e_ms": _ms(t0, first_audio),
            "est_cost_usd": metrics_db.estimate_cost(METRICS, model, tokens_in, tokens_out) if METRICS else None,
        }
        metrics_db.record_turn(METRICS, rec)
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
    try:
        endpoint = f"{NANO_CLAW_URL}/api/chat/{action}"
        async with client.stream(
            "POST",
            endpoint,
            json={"requestId": request_id, "sessionId": SESSION_ID},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                data = json.loads(await resp.aread())
                await _process_api_response(ws, session, data)
                return
            await _consume_sse(ws, session, resp)
    except Exception:
        log.exception("nano-claw API %s call failed", action)
        error_text = "Sorry, tool execution failed."
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


async def _speak_with_events(
    ws: web.WebSocketResponse,
    session: Session,
    text: str,
) -> None:
    """Keep browser VAD muted until synthesized audio actually finishes."""
    await ws.send_json({"type": "agent_audio_start"})
    try:
        await session.speak_text(text, session.voice_id, session.speed)
    finally:
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})


async def _process_api_response(
    ws: web.WebSocketResponse,
    session: Session,
    data: dict,
) -> None:
    """Route an API response to the browser and optionally TTS."""
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
        if reply:
            await _speak_with_events(ws, session, reply)
        else:
            await ws.send_json({"type": "agent_audio_end"})
    elif data.get("type") == "tool_pending":
        await ws.send_json({
            "type": "tool_pending",
            "requestId": data["requestId"],
            "tools": data["tools"],
        })
    elif data.get("error"):
        error_text = f"Error: {data['error']}"
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


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
    return web.json_response({
        "recent": metrics_db.recent(METRICS, 50),
        "byModel": metrics_db.aggregates(METRICS),
    })


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


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/voices", voices_handler)
    app.router.add_post("/api/preview", preview_handler)
    app.router.add_get("/api/models", models_handler)
    app.router.add_get("/api/metrics", metrics_handler)
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
