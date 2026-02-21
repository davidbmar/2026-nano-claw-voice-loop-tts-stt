"""Voice server — aiohttp + WebSocket bridge between browser and nano-claw API."""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from aiohttp import web

from voice.webrtc import Session

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
    """POST to nano-claw /api/chat and handle the response."""
    try:
        resp = await client.post(
            f"{NANO_CLAW_URL}/api/chat",
            json={"message": text, "sessionId": SESSION_ID},
        )
        await _process_api_response(ws, session, resp.json())
    except Exception:
        log.exception("nano-claw API call failed")
        error_text = "Sorry, I couldn't reach the agent."
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await session.speak_text(error_text)


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
        await session.speak_text(error_text)


async def _process_api_response(
    ws: web.WebSocketResponse,
    session: Session,
    data: dict,
) -> None:
    """Route an API response to the browser and optionally TTS."""
    if data.get("type") == "final":
        reply = data.get("response", "")
        await ws.send_json({"type": "agent_reply", "text": reply})
        if reply:
            await session.speak_text(reply)
    elif data.get("type") == "tool_pending":
        await ws.send_json({
            "type": "tool_pending",
            "requestId": data["requestId"],
            "tools": data["tools"],
        })
    elif data.get("error"):
        error_text = f"Error: {data['error']}"
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await session.speak_text(error_text)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)
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
