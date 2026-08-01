import asyncio
import json
import logging

import pytest
from aiohttp.test_utils import make_mocked_request

from voice import call_review, metrics_db, phone
from voice.phone_tap import CallTap


OPERATOR_HEADERS = {"X-NC-Operator-Read": "operator-sekrit"}


@pytest.mark.parametrize(
    "case",
    ("parent", "absolute", "encoded-parent", "nested"),
)
def test_call_tap_rejects_non_child_call_ids(
    case,
    tmp_path,
    monkeypatch,
    caplog,
):
    tap_root = tmp_path / "phone-taps"
    if case == "parent":
        call_id = "../escape"
        forbidden = (tmp_path / "escape",)
    elif case == "absolute":
        absolute_escape = tmp_path / "absolute-escape"
        call_id = str(absolute_escape)
        forbidden = (absolute_escape,)
    elif case == "encoded-parent":
        call_id = "..%2fescape"
        forbidden = (
            tap_root / "..%2fescape",
            tap_root / "2fescape",
        )
    else:
        call_id = "a/b"
        forbidden = (tap_root / "a", tap_root / "a" / "b")

    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tap_root))
    caplog.set_level(logging.ERROR, logger="nano-claw.phone_tap")

    with pytest.raises(ValueError, match="unsafe tap directory"):
        CallTap.create(call_id, "pcmu", 8_000, 8_000)

    assert not tap_root.exists()
    assert all(not path.exists() for path in forbidden)
    assert any(repr(call_id) in record.message for record in caplog.records)


def test_call_review_rejects_historical_traversal_row(
    tmp_path,
    monkeypatch,
    caplog,
):
    tap_root = tmp_path / "phone-taps"
    escaped_tap = tmp_path / "poisoned-call"
    escaped_tap.mkdir()
    secret = b"RIFF-this-must-not-be-served"
    (escaped_tap / "inbound.wav").write_bytes(secret)
    (escaped_tap / "meta.json").write_text(
        '{"wall_t0": 1000.0, "mono_t0": 50.0}',
        encoding="utf-8",
    )
    (escaped_tap / "timings.jsonl").write_text(
        '{"event": "outside", "t": 51.0}\n',
        encoding="utf-8",
    )

    conn = metrics_db.init_db(str(tmp_path / "metrics.db"))
    assert conn is not None
    poisoned_id = "../poisoned-call"
    metrics_db.record_call_start(
        conn,
        poisoned_id,
        "+15550001111",
        "+15123569101",
        "test-node",
    )
    row = conn.execute(
        "SELECT id FROM phone_calls WHERE call_id = ?",
        (poisoned_id,),
    ).fetchone()
    assert row is not None

    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tap_root))
    monkeypatch.setenv("NANO_CLAW_OPERATOR_READ_TOKEN", "operator-sekrit")
    monkeypatch.setattr(phone, "_metrics_conn", conn)
    monkeypatch.setattr(
        call_review.audio_inspect,
        "summarize",
        lambda _path: pytest.fail("unsafe inspect path reached audio reader"),
    )
    caplog.set_level(logging.ERROR, logger="nano-claw.phone_tap")

    async def exercise():
        cases = (
            (
                call_review.timeline_handler,
                f"/api/calls/{row['id']}/timeline",
                {"call_pk": str(row["id"])},
            ),
            (
                call_review.inspect_handler,
                f"/api/calls/{row['id']}/inspect",
                {"call_pk": str(row["id"])},
            ),
            (
                call_review.audio_handler,
                f"/api/calls/{row['id']}/audio/inbound",
                {"call_pk": str(row["id"]), "leg": "inbound"},
            ),
        )
        for handler, path, match_info in cases:
            request = make_mocked_request(
                "GET",
                path,
                headers=OPERATOR_HEADERS,
                match_info=match_info,
            )
            response = await handler(request)
            assert response.status == 404
            assert json.loads(response.text) == {"error": "unsafe call id"}

    asyncio.run(exercise())
    assert secret == (escaped_tap / "inbound.wav").read_bytes()
    assert sum(
        repr(poisoned_id) in record.message for record in caplog.records
    ) == 3


def test_telnyx_call_id_uses_sanitized_tap_directory(
    tmp_path,
    monkeypatch,
):
    raw_call_id = "v3:LzPQWbMd0r-Xp-CcMbKxk9CrBFyosMFapVcnD8GG"
    safe_call_id = "v3LzPQWbMd0r-Xp-CcMbKxk9CrBFyosMFapVcnD8GG"
    tap_root = tmp_path / "phone-taps"
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tap_root))
    monkeypatch.setattr(phone, "_metrics_conn", None)
    monkeypatch.setattr(phone, "get_vad_mode", lambda: "energy")

    async def exercise():
        call = phone.PhoneCall(
            object(),
            raw_call_id,
            _flow=None,
            _flow_domain_id=None,
        )
        try:
            assert call.call_id == safe_call_id
            assert call.telnyx_call_id == raw_call_id
            assert call.tap is not None
            assert call.tap.directory == tap_root / safe_call_id
        finally:
            await call.close()

    asyncio.run(exercise())
    assert (tap_root / safe_call_id / "meta.json").is_file()
    assert not (tap_root / raw_call_id).exists()


def test_incoming_call_persists_sanitized_id(
    tmp_path,
    monkeypatch,
):
    raw_call_id = "v3:abc:def"
    safe_call_id = "v3abcdef"
    conn = metrics_db.init_db(str(tmp_path / "metrics.db"))
    assert conn is not None
    commands = []

    async def fake_telnyx_cmd(_client, call_id, command, _payload):
        commands.append((call_id, command))
        return True

    monkeypatch.setenv("NANO_CLAW_PHONE_TOKEN", "sekrit")
    monkeypatch.setenv(
        "NANO_CLAW_PHONE_WEBHOOK_BASE",
        "https://nano.example.com",
    )
    monkeypatch.setenv(
        "NANO_CLAW_PHONE_TAP_DIR",
        str(tmp_path / "phone-taps"),
    )
    monkeypatch.setattr(phone, "_metrics_conn", conn)
    monkeypatch.setattr(phone, "_telnyx_cmd", fake_telnyx_cmd)
    phone._answered.clear()

    async def exercise():
        class IncomingRequest:
            headers = {}
            query = {"token": "sekrit"}

            async def json(self):
                return {
                    "data": {
                        "event_type": "call.initiated",
                        "payload": {
                            "call_control_id": raw_call_id,
                            "from": "+15550001111",
                            "to": "+15123569101",
                        },
                    }
                }

        response = await phone.incoming_handler(IncomingRequest())
        assert response.status == 200

    asyncio.run(exercise())
    stored_ids = [
        row["call_id"]
        for row in conn.execute(
            "SELECT call_id FROM phone_calls ORDER BY id"
        ).fetchall()
    ]
    assert stored_ids == [safe_call_id]
    assert commands == [(raw_call_id, "answer")]
