import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice import call_log, cost_ledger, metrics_db, phone

OPERATOR_HEADERS = {"X-NC-Operator-Read": "operator-sekrit"}


@pytest.fixture(autouse=True)
def phone_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NANO_CLAW_PHONE", "1")
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    monkeypatch.setenv("NANO_CLAW_PHONE_WEBHOOK_BASE", "https://nano.example.com")
    monkeypatch.setenv("NANO_CLAW_PHONE_TOKEN", "sekrit")
    monkeypatch.setenv("NANO_CLAW_OPERATOR_READ_TOKEN", "operator-sekrit")
    # Seed events use tiny epoch timestamps; keep the startup retention
    # sweep from deleting them (sweep behavior is covered in test_call_log).
    monkeypatch.setenv("NANO_CLAW_CALL_RETENTION_DAYS", "0")
    phone._answered.clear()
    phone._overrides.clear()
    phone._active_calls.clear()
    call_log._seq.clear()

    conn = metrics_db.init_db(str(tmp_path / "metrics.db"))
    assert conn is not None
    assert call_log.ensure_schema(conn)
    monkeypatch.setattr(phone.metrics_db, "init_db", lambda *a, **k: conn)
    yield conn


@pytest.fixture
def tap_root(tmp_path, monkeypatch):
    root = tmp_path / "phone-taps"
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(root))
    return root


def make_app():
    app = web.Application()
    phone.register_phone_routes(app)
    return app


def run(coro):
    return asyncio.run(coro)


def _seed_call(conn, call_id="v3:abc:def", caller="+15550001111"):
    metrics_db.record_call_start(conn, call_id, caller, "+15123569101", "test-node")
    call_log.emit(conn, call_id, "call_start", {"mode": "persona"}, ts=100.0)
    call_log.emit(conn, call_id, "user_turn", {"text": "hi"}, ts=101.0)
    call_log.emit(conn, call_id, "assistant_turn", {"text": "hello"}, ts=102.0)
    row = conn.execute(
        "SELECT id FROM phone_calls WHERE call_id = ?", (call_id,)
    ).fetchone()
    return row["id"]


async def _get(client, path, headers=None):
    resp = await client.get(path, headers=headers or {})
    return resp


def test_timeline_requires_token(phone_env):
    pk = _seed_call(phone_env)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(f"/api/calls/{pk}/timeline")
            assert resp.status == 403
            resp = await client.get(f"/api/calls/{pk}/timeline?token=wrong")
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_timeline_returns_call_events_cost_and_audio_flags(phone_env, tap_root):
    pk = _seed_call(phone_env)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/timeline",
                headers=OPERATOR_HEADERS,
            )
            assert resp.status == 200
            body = await resp.json()
            call = body["call"]
            assert call["id"] == pk
            assert call["callId"] == "v3:abc:def"
            assert call["caller"] == "+15550001111"
            assert call["node"] == "test-node"
            assert "answeredAt" in call
            kinds = [e["kind"] for e in body["events"]]
            assert kinds == ["call_start", "user_turn", "assistant_turn"]
            assert [e["seq"] for e in body["events"]] == [0, 1, 2]
            assert body["events"][0]["iso"].startswith("1970-01-01T00:01:40")
            assert body["audio"] == {
                "inbound": False,
                "outbound": False,
                "tts": False,
            }
            assert body["cost"] == []
            assert body["costMeta"]["tts"]["label"] == "TTS (Kokoro/Lux, local)"
            assert body["costMeta"]["stt"]["label"].startswith("STT")
        finally:
            await client.close()

    run(_run())


def test_timeline_cost_rows_carry_model(phone_env, tap_root):
    pk = _seed_call(phone_env)
    assert cost_ledger.write_call(
        phone_env,
        "v3:abc:def",
        "Acme",
        "conversation",
        [
            cost_ledger.LedgerEntry(
                "tts", 42, "characters", 1e-6, model="luxtts/lux_george"
            )
        ],
    )

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/timeline",
                headers=OPERATOR_HEADERS,
            )
            body = await resp.json()
            assert body["cost"][0]["model"] == "luxtts/lux_george"
        finally:
            await client.close()

    run(_run())


def test_timeline_query_token_is_rejected(phone_env):
    pk = _seed_call(phone_env)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/timeline?token=operator-sekrit"
            )
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_timeline_unknown_and_malformed_ids_return_404(phone_env):
    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/calls/9999/timeline", headers=OPERATOR_HEADERS
            )
            assert resp.status == 404
            resp = await client.get(
                "/api/calls/not-a-number/timeline",
                headers=OPERATOR_HEADERS,
            )
            assert resp.status == 404
        finally:
            await client.close()

    run(_run())


def test_audio_serves_wav_bytes_and_404s_missing_leg(phone_env, tap_root):
    pk = _seed_call(phone_env)
    call_dir = tap_root / "v3:abc:def"
    call_dir.mkdir(parents=True)
    (call_dir / "inbound.wav").write_bytes(b"RIFFfakewav")

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/audio/inbound",
                headers=OPERATOR_HEADERS,
            )
            assert resp.status == 200
            assert await resp.read() == b"RIFFfakewav"
            resp = await client.get(
                f"/api/calls/{pk}/audio/outbound",
                headers=OPERATOR_HEADERS,
            )
            assert resp.status == 404
            resp = await client.get(
                f"/api/calls/{pk}/audio/passwd",
                headers=OPERATOR_HEADERS,
            )
            assert resp.status == 404
            resp = await client.get(f"/api/calls/{pk}/audio/inbound")
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_audio_flags_reflect_existing_files(phone_env, tap_root):
    pk = _seed_call(phone_env)
    call_dir = tap_root / "v3:abc:def"
    call_dir.mkdir(parents=True)
    (call_dir / "inbound.wav").write_bytes(b"RIFF")
    (call_dir / "tts_48k.wav").write_bytes(b"RIFF")

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/timeline", headers=OPERATOR_HEADERS
            )
            body = await resp.json()
            assert body["audio"] == {
                "inbound": True,
                "outbound": False,
                "tts": True,
            }
        finally:
            await client.close()

    run(_run())


def test_timeline_degrades_to_error_json_without_db(phone_env, monkeypatch):
    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            monkeypatch.setattr(phone, "_metrics_conn", None)
            resp = await client.get(
                "/api/calls/1/timeline", headers=OPERATOR_HEADERS
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["error"] == "db unavailable"
        finally:
            await client.close()

    run(_run())


def test_timeline_includes_wall_projected_timings(phone_env, tap_root):
    import json as _json

    pk = _seed_call(phone_env)
    call_dir = tap_root / "v3:abc:def"
    call_dir.mkdir(parents=True)
    (call_dir / "meta.json").write_text(
        _json.dumps({"call_id": "v3:abc:def", "codec": "l16", "wall_t0": 1000.0, "mono_t0": 50.0})
    )
    (call_dir / "timings.jsonl").write_text(
        _json.dumps({"event": "stt_done", "t": 51.0, "ms": 120.0}) + "\n"
        + _json.dumps({"event": "synth_done", "t": 52.5, "ms": 300.0}) + "\n"
    )

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/timeline", headers=OPERATOR_HEADERS
            )
            body = await resp.json()
            timings = body["timings"]
            assert [t["event"] for t in timings] == ["stt_done", "synth_done"]
            assert timings[0]["wall"] == 1001.0
            assert timings[0]["iso"].startswith("1970-01-01T00:16:41")
            assert timings[0]["ms"] == 120.0
            assert timings[1]["wall"] == 1002.5
        finally:
            await client.close()

    run(_run())


def test_timeline_timings_empty_without_tap_dir(phone_env, tap_root):
    pk = _seed_call(phone_env)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/calls/{pk}/timeline", headers=OPERATOR_HEADERS
            )
            body = await resp.json()
            assert body["timings"] == []
        finally:
            await client.close()

    run(_run())
