import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice import phone


@pytest.fixture(autouse=True)
def phone_env(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE", "1")
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    monkeypatch.setenv("NANO_CLAW_PHONE_WEBHOOK_BASE", "https://nano.example.com")
    monkeypatch.setenv("NANO_CLAW_PHONE_TOKEN", "sekrit")
    phone._answered.clear()


def make_app():
    app = web.Application()
    phone.register_phone_routes(app)
    return app


def run(coro):
    return asyncio.run(coro)


def initiated_event(cid="cc-123"):
    return {
        "data": {
            "event_type": "call.initiated",
            "payload": {"call_control_id": cid, "from": "+15550001111", "to": "+15123569101"},
        }
    }


def test_webhook_rejects_bad_token(monkeypatch):
    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/phone/incoming?token=wrong", json=initiated_event())
            assert resp.status == 403
            resp = await client.post("/api/phone/incoming", json=initiated_event())
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_call_initiated_answers_with_streaming(monkeypatch):
    commands = []

    async def fake_cmd(client, cid, command, payload):
        commands.append((cid, command, payload))
        return True

    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/phone/incoming?token=sekrit", json=initiated_event())
            assert resp.status == 200
            # Carrier retry of the same call must not answer twice.
            resp = await client.post("/api/phone/incoming?token=sekrit", json=initiated_event())
            assert (await resp.json()).get("dedup") is True
        finally:
            await client.close()

    run(_run())
    assert len(commands) == 1
    cid, command, payload = commands[0]
    assert (cid, command) == ("cc-123", "answer")
    assert payload["stream_url"] == "wss://nano.example.com/ws/phone-media?token=sekrit"
    assert payload["stream_bidirectional_codec"] == "PCMU"


def test_media_ws_rejects_bad_token():
    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get("/ws/phone-media?token=wrong")
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_routes_not_registered_when_env_incomplete(monkeypatch):
    monkeypatch.delenv("TELNYX_API_KEY")
    app = make_app()
    paths = [r.resource.canonical for r in app.router.routes()]
    assert "/api/phone/incoming" not in paths
