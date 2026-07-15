"""Voice server — aiohttp + WebSocket bridge between browser and nano-claw API."""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from aiohttp import web

from voice.webrtc import Session
from voice import voice_catalog
from voice.text_chunker import TextChunker
from voice.tts import synthesize as tts_synthesize
from voice.wav import pcm_to_wav
from voice import kokoro_client

log = logging.getLogger("voice-server")

NANO_CLAW_URL = os.environ.get("NANO_CLAW_URL", "http://localhost:3001")
SESSION_ID = "voice-default"
STATIC_DIR = Path(__file__).resolve().parent / "web"


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
                await ws.send_json({"type": "hello_ack"})

            elif msg_type == "webrtc_offer":
                session = Session()
                answer_sdp = await session.handle_offer(msg["sdp"])
                await ws.send_json({"type": "webrtc_answer", "sdp": answer_sdp})

            elif msg_type == "mic_start":
                if session:
                    session.start_recording()

            elif msg_type == "mic_stop":
                if not session:
                    continue

                # Transcribe
                text, duration = await session.stop_recording()
                if not text:
                    await ws.send_json({"type": "transcription", "text": ""})
                    continue

                # Show user's speech
                await ws.send_json({"type": "transcription", "text": text})

                # Send to nano-claw API
                await _handle_agent_request(ws, session, http_client, text)

            elif msg_type == "mic_cancel":
                if session:
                    session.cancel_recording()

            elif msg_type == "text_message":
                text = msg.get("text", "").strip()
                if not text or not session:
                    continue
                await ws.send_json({"type": "transcription", "text": text})
                await _handle_agent_request(ws, session, http_client, text)

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
                await _handle_tool_decision(ws, session, http_client, "approve", request_id)

            elif msg_type == "tool_reject":
                request_id = msg.get("requestId", "")
                if not request_id or not session:
                    continue
                await _handle_tool_decision(ws, session, http_client, "reject", request_id)

            elif msg_type == "stop_speaking":
                if session:
                    session.stop_speaking()

            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

    except Exception:
        log.exception("WebSocket error")
    finally:
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
            json={"message": text, "sessionId": SESSION_ID},
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
    chunker = TextChunker()
    loop = asyncio.get_running_loop()
    total_bytes = 0
    event = ""
    data_lines: list[str] = []

    async def speak_chunk(chunk: str):
        nonlocal total_bytes
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
                    for chunk in chunker.push(obj.get("text", "")):
                        await speak_chunk(chunk)
                elif ev == "tool_pending":
                    await ws.send_json({"type": "tool_pending", "requestId": obj["requestId"], "tools": obj["tools"]})
                    await ws.send_json({"type": "agent_audio_end"})
                    return
                elif ev == "final":
                    tail = chunker.flush()
                    if tail:
                        await speak_chunk(tail)
                    if obj.get("debug"):
                        await ws.send_json({"type": "debug", **obj["debug"]})
                    await ws.send_json({"type": "agent_reply_done"})
                elif ev == "error":
                    await ws.send_json({"type": "agent_reply", "text": f"Error: {obj.get('error', 'agent error')}"})
                continue
            if raw.startswith("event:"):
                event = raw[6:].strip()
            elif raw.startswith("data:"):
                data_lines.append(raw[5:].strip())

        await session.end_stream(total_bytes)
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})
    except Exception:
        session.stop_speaking()
        if not ws.closed:
            await ws.send_json({"type": "agent_audio_end"})
        raise


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
        resp = await client.post(
            endpoint,
            json={"requestId": request_id, "sessionId": SESSION_ID},
        )
        await _process_api_response(ws, session, resp.json())
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
