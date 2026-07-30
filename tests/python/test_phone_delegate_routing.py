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
from voice.phone import DelegateProfile, DelegateRoute
from voice.turn_delegate import ConversationStart

LINE = "+15123569101"
START = "http://127.0.0.1:8790/api/delegate/start"


def route(url, **profile):
    return DelegateRoute(url, DelegateProfile(start_url=START, **profile))


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
    assert phone.delegate_starts() == {LINE: DelegateProfile(start_url=START)}


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
    phone._call_routing["v3:abc"] = (route("http://127.0.0.1:8790/api/session/s1/turn"), 0.0)
    assert phone.routing_for("v3:abc") == "http://127.0.0.1:8790/api/session/s1/turn"


def test_two_calls_hold_two_conversations():
    """The collision the whole seam exists to prevent, asserted on the map that
    holds the result."""
    phone._call_routing["v3:a"] = (route("http://h/api/session/a/turn"), 0.0)
    phone._call_routing["v3:b"] = (route("http://h/api/session/b/turn"), 0.0)

    assert phone.routing_for("v3:a") != phone.routing_for("v3:b")


def test_the_map_is_keyed_on_the_raw_id_not_the_sanitized_one():
    """The media WebSocket correlates on the raw id; the sanitized one is a
    different string, so a map keyed on it would never match."""
    raw = "v3:has-a-colon"
    phone._call_routing[raw] = (route("http://h/t"), 0.0)

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
    phone._call_routing["v3:call-1"] = (route("http://h/api/session/s1/turn"), 0.0)

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


# ── the per-line profile ─────────────────────────────────────────────────────
#
# The conversation-start seam deliberately refuses a `greeting` FROM the app:
# delegate-authored text going straight to TTS is an arbitrary-speech capability
# (Codex review 2026-07-30, HIGH-3). An OPERATOR setting a greeting for a line
# they configured is a different trust level entirely, and closes the same gap —
# a business's phone should not answer in a voice that names nobody.

def test_a_bare_url_is_still_valid_configuration(monkeypatch):
    """Profiles were added without invalidating any existing config."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: START}))
    profile = phone.delegate_starts()[LINE]

    assert profile.start_url == START
    assert profile.greeting == ""
    assert profile.voice == ""


def test_a_line_can_carry_its_own_greeting_and_voice(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: {
        "start": START,
        "greeting": "Thanks for calling Rivera Plumbing.",
        "voice": "af_heart",
        "speed": 1.1,
    }}))
    profile = phone.delegate_starts()[LINE]

    assert profile.start_url == START
    assert profile.greeting == "Thanks for calling Rivera Plumbing."
    assert profile.voice == "af_heart"
    assert profile.speed == 1.1


def test_a_profile_without_a_start_url_is_refused(monkeypatch):
    """A line that names a greeting but nowhere to send turns is misconfiguration,
    not a line that answers in someone's name and then cannot talk."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        LINE: {"greeting": "Thanks for calling Rivera Plumbing."}}))

    assert phone.delegate_starts() == {}


@pytest.mark.parametrize("speed", ["fast", None, -1, 0, 99])
def test_an_unusable_speed_falls_back_to_the_node_default(monkeypatch, speed):
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        LINE: {"start": START, "speed": speed}}))

    assert phone.delegate_starts()[LINE].speed == 0.0, "0 means inherit"


def test_the_greeting_decision_lives_in_one_place(monkeypatch):
    """Precedence, most specific first: the line's own greeting, then the
    node-wide override, then the mode's. The per-DID greeting MUST beat
    NANO_CLAW_PHONE_GREETING — otherwise one node answering two businesses
    greets both as the first one."""
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.default_greeting = "Mode greeting."
    call.delegate_route = None

    monkeypatch.delenv("NANO_CLAW_PHONE_GREETING", raising=False)
    assert call.greeting_line == "Mode greeting."

    monkeypatch.setenv("NANO_CLAW_PHONE_GREETING", "Node greeting.")
    assert call.greeting_line == "Node greeting."

    call.delegate_route = route("http://h/t", greeting="Rivera Plumbing here.")
    assert call.greeting_line == "Rivera Plumbing here.", (
        "the node-wide greeting outranked the line's own — two businesses on "
        "one node would both answer as the first")


def test_a_line_without_a_greeting_does_not_override_anything(monkeypatch):
    """A delegated line with no greeting configured must behave exactly as an
    undelegated one, not fall through to empty."""
    monkeypatch.setenv("NANO_CLAW_PHONE_GREETING", "Node greeting.")
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.default_greeting = "Mode greeting."
    call.delegate_route = route("http://h/t")

    assert call.greeting_line == "Node greeting."


def _bare_call(route_=None):
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    # call_id because cost_ledger.install_phone_tracking WRAPS
    # _synthesize_sentence for billing when another test has installed it, and
    # the wrapper reads it. Its presence therefore depends on test order, which
    # is a good reason for this stand-in to look like a real call.
    call.call_id = "delegate-test-call"
    call.default_greeting = "Mode greeting."
    call.delegate_route = route_
    return call


def test_a_line_can_sound_like_itself(monkeypatch):
    """One node can answer for more than one business once lines are delegated,
    and they should not have to sound alike."""
    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "node_default")
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEED", "1.0")

    assert _bare_call().configured_voice == "node_default"
    assert _bare_call(route("http://h/t")).configured_voice == "node_default", (
        "a line with no voice set must inherit, not blank out")

    styled = _bare_call(route("http://h/t", voice="af_heart", speed=1.2))
    assert styled.configured_voice == "af_heart"
    assert styled.configured_speed == 1.2


def test_the_voice_reaches_synthesis_not_just_the_log(monkeypatch):
    """The trap this nearly shipped as.

    `_synthesize_sentence` read NANO_CLAW_PHONE_VOICE directly, every sentence,
    and ignored the value computed at construction — which turned out to be only
    a log field. A per-line voice wired there would have configured NOTHING.

    Asserted on what reaches `tts_synthesize`, not on the source: `cost_ledger.
    install_phone_tracking` legitimately wraps this method for billing, so
    inspecting it finds the wrapper.
    """
    captured = {}

    def fake_synthesize(text, voice, speed, *rest):
        captured.update(text=text, voice=voice, speed=speed)
        return b"\x00" * 96

    monkeypatch.setattr(phone, "tts_synthesize", fake_synthesize)
    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "node_default")
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEED", "1.0")

    call = _bare_call(route("http://h/t", voice="af_heart", speed=1.2))
    call.tap = None
    call._tap_sentence_index = None

    asyncio.run(call._synthesize_sentence("Hello there."))

    assert captured["voice"] == "af_heart", (
        "synthesis used the node-wide voice; the line's own voice configured "
        "nothing")
    assert captured["speed"] == 1.2


def test_an_unparseable_node_speed_still_falls_back_and_warns(monkeypatch):
    """The existing behaviour this refactor had to preserve."""
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEED", "not-a-number")
    warned = []
    monkeypatch.setattr(phone, "_warn_config_fallback",
                        lambda *a, **k: warned.append(a))

    assert _bare_call().configured_speed == 1.0
    assert warned, "a bad node speed must still warn"


def test_a_line_speed_does_not_need_the_node_value_to_parse(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEED", "not-a-number")
    styled = _bare_call(route("http://h/t", speed=1.3))

    assert styled.configured_speed == 1.3


# ── the delegate turn must clean up after itself ─────────────────────────────
#
# All three of these failed when the delegate branch sat OUTSIDE _stream_reply's
# try/finally. Each one is a different job that finally does, and skipping it
# broke the call in a different way.

def _delegate_call(monkeypatch, reply_text, ok=True):
    from voice.turn_delegate import DelegateReply

    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.session_id = "p"
    call.tap = None
    call.call_id = "c1"
    call.telnyx_call_id = "v3:c1"
    call._http = object()
    call.barge = type("B", (), {"reset": lambda s: None})()
    call.speaking = False
    call.interrupted = False
    call._playback_flush_sent = False
    call.endpointer = type("E", (), {"reset": lambda s: None})()
    call.closed = False

    state = {"cue_stopped": False, "spoke": [], "logged": []}
    call._stop_thinking_cue = lambda: state.__setitem__("cue_stopped", True)

    async def fake_speak(units):
        state["spoke"].append(units)
        call._stop_thinking_cue()

    call._speak_sentences = fake_speak
    call._speech_units = lambda t: [t]

    monkeypatch.setattr(phone.call_log, "emit",
                        lambda conn, cid, kind, payload, **k:
                        state["logged"].append((kind, payload)))

    async def fake_delegate(client, url, text, *, who):
        return DelegateReply(reply_text, ok=ok)

    monkeypatch.setattr(phone, "call_delegate", fake_delegate)
    phone._call_routing["v3:c1"] = (route("http://h/t"), 0.0)
    return call, state


def test_a_delegate_turn_releases_the_speaking_flag(monkeypatch):
    """`self.speaking` is set True before the turn and cleared only in finally.
    Skipping it left every delegated call stuck speaking after ONE turn."""
    call, _ = _delegate_call(monkeypatch, "Hello there.")

    asyncio.run(call._stream_reply("hi"))

    assert call.speaking is False


def test_an_empty_delegate_reply_still_stops_the_thinking_cue(monkeypatch):
    """The contract permits an app to have nothing to say. That is not a
    failure — but the cue must stop, or the caller hears ticking forever for a
    turn that is already over."""
    call, state = _delegate_call(monkeypatch, "")

    asyncio.run(call._stream_reply("hi"))

    assert state["spoke"] == [], "nothing should have been synthesized"
    assert state["cue_stopped"] is True
    assert call.speaking is False


def test_a_delegate_turn_reaches_the_call_review(monkeypatch):
    """Without the finally, delegated calls left no assistant_turn row at all —
    a review of the call would show the caller talking to nobody."""
    call, state = _delegate_call(monkeypatch, "Hello there.")

    asyncio.run(call._stream_reply("hi"))

    turns = [payload for kind, payload in state["logged"] if kind == "assistant_turn"]
    assert turns, "no assistant_turn was logged for a delegated turn"
    assert turns[0]["text"] == "Hello there."


def test_the_review_does_not_call_a_delegated_turn_our_persona(monkeypatch):
    """A delegated turn is not this node's persona speaking. Labelling it so
    misattributes every word to a model that never produced it."""
    call, state = _delegate_call(monkeypatch, "Hello there.")

    asyncio.run(call._stream_reply("hi"))

    turns = [payload for kind, payload in state["logged"] if kind == "assistant_turn"]
    assert turns[0]["mode"] == "delegate"
