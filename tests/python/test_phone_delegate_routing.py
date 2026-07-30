"""Per-DID delegation on the phone, and proof it is inert until configured.

The live line answers as a persona today. Every test here that matters most is
the negative one: with no `NANO_CLAW_DELEGATE_STARTS` entry, nothing about an
incoming call changes — no start request, no routing entry, no different turn
path.

Design: docs/design/2026-07-30-conversation-start-seam.md (items 2, 4, 5).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from voice import phone
from voice.turn_delegate import ConversationStart

LINE = "+15123569101"
START = "http://127.0.0.1:8790/api/delegate/start"


@pytest.fixture(autouse=True)
def clean_maps(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_DELEGATE_STARTS", raising=False)
    phone._call_routing.clear()
    phone._answered.clear()
    yield
    phone._call_routing.clear()
    phone._answered.clear()


# ── configuration ────────────────────────────────────────────────────────────

def test_no_configuration_means_no_line_is_delegated():
    assert phone.delegate_starts() == {}


def test_malformed_configuration_delegates_nothing_rather_than_crashing(monkeypatch):
    """A broken env var must not take the phone down, and must not read as
    'delegate everything'."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", "{not json")
    assert phone.delegate_starts() == {}


def test_a_configured_line_is_read_per_call(monkeypatch):
    """Read per call, not cached at import, so a line can be added without a
    restart — restarting drops live calls."""
    assert phone.delegate_starts() == {}
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: START}))
    assert phone.delegate_starts() == {LINE: START}


# ── the conversation key ─────────────────────────────────────────────────────

def test_the_conversation_key_is_stable_per_call():
    assert phone.conversation_key_for("v3:abc") == phone.conversation_key_for("v3:abc")
    assert phone.conversation_key_for("v3:abc") != phone.conversation_key_for("v3:xyz")


def test_the_conversation_key_does_not_leak_the_carrier_id():
    """The raw call_control_id is the string every Call Control command is
    addressed to, including hangup. An app needs something stable to deduplicate
    on; it does not need that."""
    key = phone.conversation_key_for("v3:secret-carrier-id")

    assert "secret-carrier-id" not in key
    assert "v3" not in key
    assert len(key) == 32


# ── the routing map ──────────────────────────────────────────────────────────

def test_an_unrouted_call_has_no_delegate():
    assert phone.routing_for("v3:not-delegated") is None


def test_a_routed_call_finds_its_conversation():
    phone._call_routing["v3:abc"] = ("http://127.0.0.1:8790/api/session/s1/turn", 0.0)
    assert phone.routing_for("v3:abc") == "http://127.0.0.1:8790/api/session/s1/turn"


def test_two_calls_hold_two_conversations():
    """The collision the whole seam exists to prevent, asserted on the map that
    holds the result."""
    phone._call_routing["v3:a"] = ("http://h/api/session/a/turn", 0.0)
    phone._call_routing["v3:b"] = ("http://h/api/session/b/turn", 0.0)

    assert phone.routing_for("v3:a") != phone.routing_for("v3:b")


def test_the_map_is_keyed_on_the_raw_id_not_the_sanitized_one():
    """The media WebSocket correlates on the raw id; the sanitized one is a
    different string, so a map keyed on it would never match."""
    raw = "v3:has-a-colon"
    phone._call_routing[raw] = ("http://h/t", 0.0)

    assert phone.routing_for(raw) is not None
    assert phone.routing_for(phone._contained_call_id(raw)) is None


# ── the webhook ──────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def initiated(cid="v3:call-1", to=LINE, frm="+15125550111"):
    return FakeRequest({"data": {
        "event_type": "call.initiated",
        "payload": {"call_control_id": cid, "to": to, "from": frm},
    }})


def hangup(cid="v3:call-1"):
    return FakeRequest({"data": {
        "event_type": "call.hangup",
        "payload": {"call_control_id": cid},
    }})


@pytest.fixture
def quiet_webhook(monkeypatch, tmp_path):
    """Stub the carrier and the metrics DB; record what would have been sent."""
    sent = []

    async def fake_cmd(client, cid, command, payload):
        sent.append({"cid": cid, "command": command})
        return True

    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)
    monkeypatch.setattr(phone, "_token_ok", lambda _r: True)
    monkeypatch.setattr(phone.metrics_db, "record_call_start",
                        lambda *a, **k: None)
    monkeypatch.setattr(phone.metrics_db, "record_call_end", lambda *a, **k: None)
    monkeypatch.setattr(phone, "_cfg", lambda name, default="": {
        "NANO_CLAW_PHONE_WEBHOOK_BASE": "https://example.invalid",
        "NANO_CLAW_PHONE_TOKEN": "t",
    }.get(name, __import__("os").environ.get(name, default)))
    return sent


def test_an_unconfigured_call_never_asks_anyone_for_a_conversation(
        quiet_webhook, monkeypatch):
    """The inertness guarantee. This is what protects the live line while the
    feature is unfinished."""
    asked = []

    async def fake_start(*a, **k):
        asked.append(k)
        return ConversationStart("http://h/t", ok=True)

    monkeypatch.setattr(phone, "start_conversation", fake_start)

    asyncio.run(phone.incoming_handler(initiated()))

    assert asked == [], "a call on an undelegated line asked for a conversation"
    assert phone._call_routing == {}
    assert [s["command"] for s in quiet_webhook] == ["answer"], (
        "the call must still be answered exactly as before")


def test_a_configured_call_mints_a_conversation_and_still_answers(
        quiet_webhook, monkeypatch):
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: START}))
    seen = {}

    async def fake_start(client, url, **k):
        seen.update(k, url=url)
        return ConversationStart("http://127.0.0.1:8790/api/session/s9/turn", ok=True)

    monkeypatch.setattr(phone, "start_conversation", fake_start)

    asyncio.run(phone.incoming_handler(initiated()))

    assert seen["url"] == START
    assert seen["to"] == LINE
    assert seen["from_"] == "+15125550111"
    assert seen["conversation_key"] == phone.conversation_key_for("v3:call-1")
    assert phone.routing_for("v3:call-1") == (
        "http://127.0.0.1:8790/api/session/s9/turn")
    assert [s["command"] for s in quiet_webhook] == ["answer"], (
        "delegation must not stop the call being answered")


def test_a_failed_start_leaves_the_call_undelegated_rather_than_refused(
        quiet_webhook, monkeypatch):
    """Fail OPEN here, unlike everywhere else in this module. The alternative is
    refusing a real phone call because another service is down."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: START}))

    async def fake_start(*a, **k):
        return ConversationStart("", ok=False, failure="ConnectionRefused")

    monkeypatch.setattr(phone, "start_conversation", fake_start)

    asyncio.run(phone.incoming_handler(initiated()))

    assert phone.routing_for("v3:call-1") is None
    assert [s["command"] for s in quiet_webhook] == ["answer"]


def test_hangup_forgets_the_conversation(quiet_webhook):
    """The delegate URL is a capability — riff-builder's carries its session id —
    so it must not outlive the call that earned it."""
    phone._call_routing["v3:call-1"] = ("http://h/api/session/s1/turn", 0.0)

    asyncio.run(phone.incoming_handler(hangup()))

    assert phone.routing_for("v3:call-1") is None


def test_a_redelivered_webhook_does_not_mint_twice(quiet_webhook, monkeypatch):
    """`_answered` records the id before any await, so ordinary redelivery is
    already guarded — which is why the start call sits after it rather than in
    the media WebSocket, where there is no guard at all."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: START}))
    calls = []

    async def fake_start(*a, **k):
        calls.append(k["conversation_key"])
        return ConversationStart("http://127.0.0.1:8790/api/session/s9/turn", ok=True)

    monkeypatch.setattr(phone, "start_conversation", fake_start)

    asyncio.run(phone.incoming_handler(initiated()))
    asyncio.run(phone.incoming_handler(initiated()))   # the retry

    assert len(calls) == 1, f"minted {len(calls)} conversations for one call"
