from __future__ import annotations

import asyncio
import logging

import pytest
from aiohttp.test_utils import make_mocked_request

from voice import call_log, call_review, cost_ledger, metrics_db, phone, server


OPERATOR_TOKEN = "operator-read-secret"
PHONE_TOKEN = "telnyx-phone-secret"
OPERATOR_HEADER = {phone.OPERATOR_READ_HEADER: OPERATOR_TOKEN}


@pytest.fixture
def operator_data(monkeypatch, tmp_path):
    monkeypatch.setenv("NANO_CLAW_PHONE_TOKEN", PHONE_TOKEN)
    monkeypatch.setenv("NANO_CLAW_OPERATOR_READ_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv(
        "NANO_CLAW_PHONE_WEBHOOK_BASE", "https://nano.example.test"
    )
    monkeypatch.setenv("NANO_CLAW_PHONE_VAD", "energy")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tmp_path / "phone-taps"))
    monkeypatch.setattr(metrics_db, "DB_PATH", str(tmp_path / "metrics.db"))

    phone._answered.clear()
    phone._overrides.clear()
    call_log._seq.clear()

    conn = metrics_db.init_db()
    assert conn is not None
    assert call_log.ensure_schema(conn)
    assert cost_ledger.ensure_schema(conn)
    monkeypatch.setattr(phone, "_metrics_conn", conn)
    monkeypatch.setattr(server, "METRICS", conn)

    call_id = "operator-token-test-call"
    metrics_db.record_call_start(
        conn, call_id, "+15550001111", "+15125550100", "test-node"
    )
    row = conn.execute(
        "SELECT id FROM phone_calls WHERE call_id = ?", (call_id,)
    ).fetchone()
    call_pk = row["id"]

    call_dir = tmp_path / "phone-taps" / call_id
    call_dir.mkdir(parents=True)
    (call_dir / "inbound.wav").write_bytes(b"RIFFoperator-token-test")

    yield call_pk
    conn.close()


def run(coro):
    return asyncio.run(coro)


def _operator_requests(call_pk, *, headers=None, query_token=None):
    suffix = f"?token={query_token}" if query_token is not None else ""
    headers = headers or {}
    return (
        (
            "/api/metrics",
            server.metrics_handler,
            make_mocked_request("GET", f"/api/metrics{suffix}", headers=headers),
        ),
        (
            "/api/costs",
            server.costs_handler,
            make_mocked_request("GET", f"/api/costs{suffix}", headers=headers),
        ),
        (
            "/api/calls",
            phone.calls_handler,
            make_mocked_request("GET", f"/api/calls{suffix}", headers=headers),
        ),
        (
            f"/api/calls/{call_pk}/audio/inbound",
            call_review.audio_handler,
            make_mocked_request(
                "GET",
                f"/api/calls/{call_pk}/audio/inbound{suffix}",
                headers=headers,
                match_info={"call_pk": str(call_pk), "leg": "inbound"},
            ),
        ),
    )


async def _statuses(requests):
    return [
        (path, (await handler(request)).status)
        for path, handler, request in requests
    ]


def test_operator_reads_never_accept_query_token(operator_data):
    statuses = run(
        _statuses(
            _operator_requests(operator_data, query_token=OPERATOR_TOKEN)
        )
    )
    assert statuses == [
        ("/api/metrics", 403),
        ("/api/costs", 403),
        ("/api/calls", 403),
        (f"/api/calls/{operator_data}/audio/inbound", 403),
    ]


def test_operator_reads_accept_correct_header(operator_data):
    statuses = run(
        _statuses(_operator_requests(operator_data, headers=OPERATOR_HEADER))
    )
    assert statuses == [
        ("/api/metrics", 200),
        ("/api/costs", 200),
        ("/api/calls", 200),
        (f"/api/calls/{operator_data}/audio/inbound", 200),
    ]


def test_operator_reads_fail_closed_when_both_tokens_are_unset(
    operator_data, monkeypatch, caplog
):
    monkeypatch.delenv("NANO_CLAW_OPERATOR_READ_TOKEN")
    monkeypatch.delenv("NANO_CLAW_PHONE_TOKEN")

    with caplog.at_level(logging.ERROR, logger="nano-claw.phone"):
        statuses = run(
            _statuses(
                _operator_requests(operator_data, headers=OPERATOR_HEADER)
            )
        )
    assert {status for _path, status in statuses} == {403}
    assert "NANO_CLAW_OPERATOR_READ_TOKEN is unset" in caplog.text


def test_telnyx_routes_still_accept_phone_token_query(
    operator_data, monkeypatch
):
    commands = []

    async def fake_telnyx_cmd(_client, cid, command, payload):
        commands.append((cid, command, payload))
        return True

    monkeypatch.setattr(phone, "_telnyx_cmd", fake_telnyx_cmd)

    class IncomingRequest:
        path = "/api/phone/incoming"
        headers = {}
        query = {"token": PHONE_TOKEN}

        async def json(self):
            return {
                "data": {
                    "event_type": "call.initiated",
                    "payload": {
                        "call_control_id": "operator-token-webhook",
                        "from": "+15550002222",
                        "to": "+15125550100",
                    },
                }
            }

    class StubWebSocket:
        def __init__(self):
            self.prepared = False

        async def prepare(self, _request):
            self.prepared = True

        def __aiter__(self):
            async def empty_messages():
                if False:
                    yield None

            return empty_messages()

    websocket = StubWebSocket()
    monkeypatch.setattr(phone.web, "WebSocketResponse", lambda: websocket)

    async def exercise():
        incoming = await phone.incoming_handler(IncomingRequest())
        media_request = make_mocked_request(
            "GET", f"/ws/phone-media?token={PHONE_TOKEN}"
        )
        media = await phone.media_ws_handler(media_request)
        return incoming, media

    incoming, media = run(exercise())
    assert incoming.status == 200
    assert media is websocket
    assert websocket.prepared
    assert [(cid, command) for cid, command, _payload in commands] == [
        ("operator-token-webhook", "answer")
    ]


def test_phone_token_migration_is_header_only(
    operator_data, monkeypatch, caplog
):
    monkeypatch.delenv("NANO_CLAW_OPERATOR_READ_TOKEN")

    async def exercise():
        header_request = make_mocked_request(
            "GET",
            "/api/calls",
            headers={phone.OPERATOR_READ_HEADER: PHONE_TOKEN},
        )
        query_request = make_mocked_request(
            "GET", f"/api/calls?token={PHONE_TOKEN}"
        )
        return (
            await phone.calls_handler(header_request),
            await phone.calls_handler(query_request),
        )

    with caplog.at_level(logging.WARNING, logger="nano-claw.phone"):
        header_response, query_response = run(exercise())
    assert header_response.status == 200
    assert query_response.status == 403
    assert "NANO_CLAW_OPERATOR_READ_TOKEN" in caplog.text
