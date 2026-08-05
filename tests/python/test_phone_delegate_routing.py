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
        # CONSUMES the source, as the real _speak_sentences does. A stub that
        # only accepted it hid the fact that what the caller heard is recorded
        # by iteration — these tests passed while the review over-reported.
        async for unit in units:
            state["spoke"].append(unit)
        call._stop_thinking_cue()

    call._speak_sentences = fake_speak
    call._speech_units = lambda t: [t]

    monkeypatch.setattr(phone.call_log, "emit",
                        lambda conn, cid, kind, payload, **k:
                        state["logged"].append((kind, payload)))

    async def fake_delegate(client, url, text, *, who, turn_id):
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


def test_the_carrier_endpoint_defaults_to_production():
    """The override exists so the gateway can be run against a local sink. It
    must never change where a real deployment sends commands."""
    import importlib

    import voice.phone as phone_module

    assert phone_module.TELNYX_API.startswith("https://api.telnyx.com"), (
        "TELNYX_API_BASE is set in this environment, or the default changed")
    assert importlib.import_module("voice.phone").TELNYX_API == phone_module.TELNYX_API


# ── barge-in must not be reported as a reply the caller heard ────────────────

def _bargeable_call(monkeypatch, reply_text, cut_after=None):
    """A delegated call whose speech stops after `cut_after` units."""
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
    call._stop_thinking_cue = lambda: None
    call._speech_units = lambda t: [p.strip() + "." for p in t.split(".") if p.strip()]

    async def speak(units):
        spoken = 0
        async for _unit in units:
            spoken += 1
            if cut_after is not None and spoken >= cut_after:
                call.interrupted = True   # the caller talked over us
                return

    call._speak_sentences = speak

    logged: list = []
    monkeypatch.setattr(phone.call_log, "emit",
                        lambda conn, cid, kind, payload, **k:
                        logged.append((kind, payload)))

    async def fake_delegate(client, url, text, *, who, turn_id):
        return DelegateReply(reply_text, ok=True)

    monkeypatch.setattr(phone, "call_delegate", fake_delegate)
    phone._call_routing["v3:c1"] = (route("http://h/t"), 0.0)
    return call, logged


def _turn(logged):
    rows = [p for k, p in logged if k == "assistant_turn"]
    assert rows, "no assistant_turn was logged"
    return rows[0]


def test_a_barged_reply_records_only_what_was_spoken(monkeypatch):
    """The review timeline must show what the caller HEARD. Appending the whole
    reply before speaking it recorded three sentences for a caller who got one."""
    call, logged = _bargeable_call(
        monkeypatch, "First sentence. Second sentence. Third sentence.",
        cut_after=1)

    asyncio.run(call._stream_reply("hi"))
    row = _turn(logged)

    assert row["text"] == "First sentence."
    assert "Third sentence" not in row["text"]


def test_a_barged_reply_is_not_reported_complete(monkeypatch):
    """complete=True beside interrupted=True is not merely wrong, it is
    self-contradictory — the same row said both."""
    call, logged = _bargeable_call(
        monkeypatch, "First sentence. Second sentence.", cut_after=1)

    asyncio.run(call._stream_reply("hi"))
    row = _turn(logged)

    assert row["complete"] is False
    assert row["interrupted"] is True


def test_an_uninterrupted_reply_is_reported_complete_and_whole(monkeypatch):
    """The fix must not make every delegate turn look truncated."""
    call, logged = _bargeable_call(
        monkeypatch, "First sentence. Second sentence.", cut_after=None)

    asyncio.run(call._stream_reply("hi"))
    row = _turn(logged)

    assert row["complete"] is True
    assert row["interrupted"] is False
    assert "First sentence." in row["text"]
    assert "Second sentence." in row["text"]


def test_an_empty_reply_is_a_complete_turn(monkeypatch):
    """Nothing to say is not a truncated turn."""
    call, logged = _bargeable_call(monkeypatch, "", cut_after=None)

    asyncio.run(call._stream_reply("hi"))

    rows = [p for k, p in logged if k == "assistant_turn"]
    if rows:  # only emitted when something was spoken
        assert rows[0]["complete"] is True
    assert call.speaking is False


def test_first_audio_latency_is_logged_for_a_delegated_turn(monkeypatch, caplog):
    """On a delegated line this is the number that matters: a slow app is
    otherwise invisible from the gateway's logs. The streaming branch marks it;
    the delegate branch did not."""
    import logging

    call, _ = _bargeable_call(monkeypatch, "First sentence. Second sentence.")

    with caplog.at_level(logging.INFO, logger="nano-claw.phone"):
        asyncio.run(call._stream_reply("hi"))

    assert any("first sentence at" in r.message for r in caplog.records), (
        "no first-audio latency was logged for a delegated turn")


def test_first_audio_is_marked_once_not_per_sentence(monkeypatch, caplog):
    import logging

    call, _ = _bargeable_call(
        monkeypatch, "One. Two. Three. Four.")

    with caplog.at_level(logging.INFO, logger="nano-claw.phone"):
        asyncio.run(call._stream_reply("hi"))

    marks = [r for r in caplog.records if "first sentence at" in r.message]
    assert len(marks) == 1, f"marked {len(marks)} times for one reply"


# ── the same defect on the NORMAL path's non-streaming fallback ──────────────
#
# Found while auditing the delegate hop: when the agent API answers with
# something other than SSE, that branch had the identical bug — whole reply
# appended and complete=True set before speaking. Both now share one helper, so
# there is no third copy to get wrong.

class _NonSseResponse:
    headers = {"content-type": "application/json"}

    def __init__(self, reply):
        self._reply = reply

    async def aread(self):
        import json as _json
        return _json.dumps({"response": self._reply}).encode()


class _NonSseStream:
    def __init__(self, reply):
        self._reply = reply

    async def __aenter__(self):
        return _NonSseResponse(self._reply)

    async def __aexit__(self, *exc):
        return False


class _NonSseHttp:
    def __init__(self, reply):
        self._reply = reply

    def stream(self, _method, _url, **_kw):
        return _NonSseStream(self._reply)


def _fallback_call(monkeypatch, reply_text, cut_after=None):
    """A call whose agent API answers non-SSE — no delegate involved."""
    call, logged = _bargeable_call(monkeypatch, "unused", cut_after=cut_after)
    phone._call_routing.clear()          # NOT delegated: the normal path
    call._http = _NonSseHttp(reply_text)
    monkeypatch.setattr(phone, "record_agent_done", lambda *a, **k: None,
                        raising=False)
    return call, logged


def test_the_non_streaming_fallback_records_only_what_was_spoken(monkeypatch):
    call, logged = _fallback_call(
        monkeypatch, "Alpha one. Beta two. Gamma three.", cut_after=1)

    asyncio.run(call._stream_reply("hi"))
    row = _turn(logged)

    assert row["text"] == "Alpha one."
    assert "Gamma three" not in row["text"], (
        "the fallback recorded a reply the caller was cut off from")
    assert row["complete"] is False


def test_the_non_streaming_fallback_is_complete_when_uninterrupted(monkeypatch):
    call, logged = _fallback_call(monkeypatch, "Alpha one. Beta two.")

    asyncio.run(call._stream_reply("hi"))
    row = _turn(logged)

    assert row["complete"] is True
    assert row["interrupted"] is False
    assert "Alpha one." in row["text"] and "Beta two." in row["text"]


def test_the_fallback_is_still_a_persona_turn_not_a_delegate_one(monkeypatch):
    """Sharing the speak helper must not blur which path answered."""
    call, logged = _fallback_call(monkeypatch, "Alpha one.")

    asyncio.run(call._stream_reply("hi"))

    assert _turn(logged)["mode"] == "persona"


# ── contract conformance, as a gateway ───────────────────────────────────────

def test_a_failed_turn_keeps_the_line_open(monkeypatch):
    """Contract: "Any non-200 → the gateway speaks a fixed apology and keeps the
    channel open." A failed turn is not a failed call — the caller may simply
    ask again, and the app may be back by then."""
    from voice.turn_delegate import DELEGATE_APOLOGY, DelegateReply

    call, logged = _bargeable_call(monkeypatch, "unused")

    async def failing(client, url, text, *, who, turn_id):
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure="status 502")

    monkeypatch.setattr(phone, "call_delegate", failing)

    asyncio.run(call._stream_reply("hi"))

    assert call.closed is False, "the call was ended by one failed turn"
    assert call.speaking is False, "the turn never finished cleanly"
    row = _turn(logged)
    assert DELEGATE_APOLOGY in row["text"], (
        "the caller heard no apology for a failed turn — silence is the failure "
        "this whole module exists to prevent")


def test_the_delegate_turn_happens_inside_the_dead_air_cue():
    """Contract: "the gateway should fill dead air past ~2s with an
    acknowledgment sound or filler line." Measured delegate turns run 1.9-6.4s,
    so this is not optional.

    Read from the FILE rather than through `inspect.getsource`. Two earlier
    attempts failed for opposite reasons: getsource returns the wrapper that
    `cost_ledger.install_phone_tracking` installs, so it passed alone and failed
    in the full suite; and driving `_run_turn` for real turned into an arms race
    of stub attributes, which is a test at the wrong level. The file on disk is
    neither wrapped nor expensive.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "phone.py").read_text()
    tree = ast.parse(source)
    run_turn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_run_turn")

    # By LINE NUMBER. `ast.walk` yields breadth-first, not in source order, so
    # comparing walk positions asserts nothing — a first version of this did
    # exactly that and passed with the cue moved after the reply.
    lines = {}
    for node in ast.walk(run_turn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            lines.setdefault(node.func.attr, node.lineno)

    assert "_start_thinking_cue" in lines, "the turn no longer starts a cue"
    assert "_stream_reply" in lines, (
        "the delegate hop is reached through _stream_reply; if _run_turn no "
        "longer calls it, this guarantee needs rechecking")
    assert lines["_start_thinking_cue"] < lines["_stream_reply"], (
        "the cue must start BEFORE the reply is fetched, or the silence it "
        "exists to fill has already happened")


# ── the design's evidence must keep pointing at something ────────────────────

def test_the_symbols_the_design_reasons_about_still_exist():
    """`docs/design/2026-07-30-conversation-start-seam.md` argues from named
    code: the placement of the start call rests on `_answered` recording before
    any await, the correlation problem rests on the stream URL being built in
    `incoming_handler`, and the hangup section rests on `PhoneCall.close` being
    local teardown.

    That doc first cited LINE NUMBERS, and four had drifted within days as this
    file grew — still read as evidence while pointing at unrelated code. Symbols
    do not drift, but they do get renamed, and a renamed symbol leaves the
    argument just as orphaned. This is the cheap half of that: the names still
    resolve. Whether they still mean what the doc says is a human's job.
    """
    import ast
    from pathlib import Path

    load_bearing = {
        "_answered": "the webhook-retry guard the start call is placed after",
        "incoming_handler": "where the stream URL is built and routing is minted",
        "close": "PhoneCall.close, cited as local teardown that cannot end a call",
        "hangup_after_playback": "what was built because close could not",
        "_call_routing": "the per-call conversation map",
        "conversation_key_for": "the digest sent instead of the carrier id",
        "delegate_starts": "the per-DID configuration",
        "route_for": "how the turn hop finds its conversation",
    }

    source = (Path(__file__).resolve().parents[2] / "voice" / "phone.py").read_text()
    tree = ast.parse(source)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    names |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    # AnnAssign too: `_answered: dict[str, float] = {}` and `_call_routing` are
    # annotated, and leaving them out made this guard fail on its own first run.
    names |= {n.target.id for n in ast.walk(tree)
              if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}

    missing = {s: why for s, why in load_bearing.items() if s not in names}
    assert not missing, (
        f"renamed or removed, leaving the design arguing from nothing: {missing}")


# ── a delegated call must survive more than one turn ─────────────────────────

def test_a_delegated_call_answers_a_second_turn(monkeypatch):
    """The `speaking` flag bug (e4815a1) left it True after turn one, because
    the delegate branch sat outside the try whose finally clears it. Every
    delegated call would have gone dead after a single exchange — and
    `feed_media` DROPS inbound audio while `speaking` is set (half-duplex), so
    the caller would have talked to a line that could no longer hear them.

    Two loopback attempts failed to prove this on the wire: the first mispaced
    the endpointer, the second produced four empty transcriptions I could not
    explain without more observability than the harness had. The property does
    not need audio — it needs two turns — so it is tested here, where the
    failure is unambiguous.
    """
    replies = ["First answer.", "Second answer."]
    call, logged = _bargeable_call(monkeypatch, "unused")
    turn_ids = []

    async def fake_delegate(client, url, text, *, who, turn_id):
        from voice.turn_delegate import DelegateReply
        turn_ids.append(turn_id)
        return DelegateReply(replies.pop(0), ok=True)

    monkeypatch.setattr(phone, "call_delegate", fake_delegate)

    asyncio.run(call._stream_reply("we do emergency repairs"))
    assert call.speaking is False, "turn one never released the speaking flag"

    asyncio.run(call._stream_reply("yes that is right"))

    turns = [p for k, p in logged if k == "assistant_turn"]
    assert len(turns) == 2, f"the call answered {len(turns)} turns, not 2"
    assert turns[0]["text"] == "First answer."
    assert turns[1]["text"] == "Second answer.", (
        "the second turn produced no reply — the call went dead after one "
        "exchange, which is what the speaking-flag bug did")
    assert len(set(turn_ids)) == 2
    assert call.speaking is False


def test_a_delegated_call_survives_a_failed_turn_and_answers_the_next(monkeypatch):
    """The recovery the contract promises: "keeps the channel open". A caller
    whose turn failed asks again, and the app may be back by then."""
    from voice.turn_delegate import DELEGATE_APOLOGY, DelegateReply

    outcomes = [DelegateReply(DELEGATE_APOLOGY, ok=False, failure="status 502"),
                DelegateReply("Back now. How can I help?", ok=True)]
    call, logged = _bargeable_call(monkeypatch, "unused")

    async def fake_delegate(client, url, text, *, who, turn_id):
        return outcomes.pop(0)

    monkeypatch.setattr(phone, "call_delegate", fake_delegate)

    asyncio.run(call._stream_reply("hello"))
    asyncio.run(call._stream_reply("hello again"))

    turns = [p for k, p in logged if k == "assistant_turn"]
    assert len(turns) == 2
    assert DELEGATE_APOLOGY in turns[0]["text"]
    # Substance, not exact text: this file's _speech_units stub splits on "."
    # and re-appends one, so a reply ending in "?" comes back "?.".
    assert "Back now" in turns[1]["text"]
    assert "How can I help" in turns[1]["text"]
    assert DELEGATE_APOLOGY not in turns[1]["text"]


# ── the caller hangs up while the app is still thinking ──────────────────────

def test_a_hangup_mid_turn_cancels_the_delegate_call(monkeypatch):
    """`PhoneCall.close` cancels the turn task. That must actually reach the
    delegate request, or a caller who hung up leaves a 30-second HTTP call in
    flight and the app processing a turn for a dead line.

    It works because `CancelledError` is a BaseException, so `call_delegate`'s
    broad `except Exception` — which exists to turn every delegate failure into
    the apology — does not swallow it. That is load-bearing and not obvious.
    """
    from voice.turn_delegate import call_delegate

    class SlowClient:
        async def post(self, url, **kw):
            await asyncio.sleep(30)

    async def exercise():
        task = asyncio.create_task(
            call_delegate(SlowClient(), "http://127.0.0.1:8790/t", "hi",
                          who="caller"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_a_cancelled_turn_still_releases_the_call(monkeypatch):
    """The finally in `_stream_reply` must run on cancellation too. If it does
    not, `speaking` stays True on a call that is ending — harmless here, but the
    same flag whose leak made every delegated call go dead after one turn."""
    call, logged = _bargeable_call(monkeypatch, "unused")

    async def slow_delegate(client, url, text, *, who, turn_id):
        await asyncio.sleep(30)

    monkeypatch.setattr(phone, "call_delegate", slow_delegate)

    async def exercise():
        task = asyncio.create_task(call._stream_reply("hi"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert call.speaking is False, "a cancelled turn left the call speaking"


def test_a_hangup_forgets_the_conversation_even_mid_turn(quiet_webhook):
    """The routing entry is a capability. A caller hanging up during a turn must
    not leave the delegate URL — and riff-builder's session id inside it —
    sitting in the map until the TTL."""
    phone._call_routing["v3:mid"] = (route("http://h/api/session/s1/turn"), 0.0)

    asyncio.run(phone.incoming_handler(hangup("v3:mid")))

    assert phone.routing_for("v3:mid") is None


def test_closing_the_call_cancels_an_in_flight_delegate_turn():
    """`close` is what a hangup ultimately calls, and it is the thing that must
    cancel the turn — the tests above cancel the task by hand, which proves
    cancellation WORKS but not that anything issues it.

    Without this, a caller who hangs up mid-reply leaves a 30-second HTTP call
    in flight and the app finishing a turn for a line nobody is on.
    """
    class Pipeline:
        async def aclose(self):
            return None

    class Stream:
        async def close(self):
            return None

    class Http:
        async def aclose(self):
            return None

    async def exercise():
        call = phone.PhoneCall.__new__(phone.PhoneCall)
        call.call_id = "c1"
        call.speaking = False
        call.closed = False
        call.interrupted = False
        call._sentence_pipelines = {Pipeline()}
        call._inbound_buffer = []
        call._stt_stream = Stream()
        call._http = Http()
        call.tap = None
        call._thinking_cue_stop = None
        call._thinking_cue_task = None
        call._idle_task = None
        call._call_end_emitted = True   # skip the metrics leg; not what this tests
        call._frame_pacer = None
        call.telnyx_call_id = "v3:c1"

        started = asyncio.Event()

        async def in_flight_turn():
            started.set()
            await asyncio.sleep(30)      # the delegate is still thinking

        call._turn_task = asyncio.create_task(in_flight_turn())
        await started.wait()

        await call.close()

        assert call._turn_task.cancelled() or call._turn_task.cancelling(), (
            "close() left the turn running — the app keeps working on a call "
            "nobody is on")

    asyncio.run(exercise())


def test_two_lines_on_one_node_resolve_to_different_identities(monkeypatch):
    """The multi-tenant case, which is the whole point of per-line profiles.
    Existing tests check ONE line against the node default; none checked two
    lines against each other, which is the arrangement that would actually
    embarrass someone — a law office answering in a plumber's voice.

    Verified beyond config against the live TTS: the same sentence in `af_heart`
    and `bm_george` produced 324000 vs 362400 bytes, different hashes, different
    RMS. Config resolution is what this pins; that the voices differ is the TTS
    service's job.
    """
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        "+15125550100": {"start": START,
                         "greeting": "Thanks for calling Rivera Plumbing.",
                         "voice": "af_heart"},
        "+15125550200": {"start": START,
                         "greeting": "Lakeside Legal, how can I help?",
                         "voice": "bm_george"},
    }))
    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "node_default")
    lines = phone.delegate_starts()

    calls = {}
    for did, profile in lines.items():
        call = phone.PhoneCall.__new__(phone.PhoneCall)
        call.call_id = did
        call.default_greeting = "MODE GREETING"
        call.delegate_route = phone.DelegateRoute("http://h/t", profile)
        calls[did] = call

    plumber, lawyer = calls["+15125550100"], calls["+15125550200"]

    assert plumber.greeting_line != lawyer.greeting_line
    assert plumber.configured_voice != lawyer.configured_voice
    assert "Rivera" in plumber.greeting_line
    assert "Lakeside" in lawyer.greeting_line
    assert "node_default" not in {plumber.configured_voice, lawyer.configured_voice}, (
        "a line with its own voice fell back to the node default")


# ── the disclosure is a legal statement made on a business's behalf ──────────

def test_a_line_can_speak_its_own_recording_disclosure(monkeypatch):
    """Node-wide until now, which is wrong once one node answers for two
    businesses: the wording is a statement made TO A CALLER ON BEHALF OF that
    business, and what it must say differs by jurisdiction."""
    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "Node-wide notice.")

    inherits = _bare_call(route("http://h/t"))
    assert inherits.recording_notice == "Node-wide notice."

    own = _bare_call(route("http://h/t",
                           record_notice="Calls to this office are recorded."))
    assert own.recording_notice == "Calls to this office are recorded."


def test_a_line_can_say_nothing(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "Node-wide notice.")
    silent = _bare_call(route("http://h/t", record_notice="off"))

    assert silent.recording_notice == ""


def test_silencing_the_disclosure_does_not_stop_the_recording(monkeypatch):
    """The property this must not be mistaken for. Calls are recorded for the
    review panel regardless — that was true of the node-wide setting too, and
    is why this returns a SENTENCE rather than a decision about recording.

    Asserted on the type: anything that reads like a switch invites someone to
    use it as one.
    """
    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "off")
    call = _bare_call(route("http://h/t"))

    assert call.recording_notice == ""
    assert isinstance(call.recording_notice, str), (
        "a boolean here would read as 'recording on/off', which it is not")


def test_the_greeting_carries_the_line_s_own_disclosure(monkeypatch):
    """Composed at the call site, so the caller hears the right one."""
    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "Node-wide notice.")
    call = _bare_call(route("http://h/t", greeting="Rivera Plumbing.",
                            record_notice="Recorded for training."))

    composed = phone._compose_greeting(call.greeting_line, call.recording_notice)

    assert composed == "Rivera Plumbing. Recorded for training."
    assert "Node-wide" not in composed


def test_an_undelegated_call_keeps_the_node_disclosure(monkeypatch):
    """Every existing deployment is unchanged: no route, no per-line wording."""
    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "Node-wide notice.")

    assert _bare_call().recording_notice == "Node-wide notice."
    assert phone._compose_greeting("Hello.") == "Hello. Node-wide notice."


def test_the_call_site_passes_the_line_s_own_disclosure():
    """A correct property the greeting path never reads is the same as none.

    The test above composes the greeting by hand; this checks the media handler
    actually hands `_compose_greeting` the CALL's notice rather than letting it
    fall back to the node-wide one. Removing that argument failed nothing until
    this existed — the same shape as an adapter written and never wired.

    Read from the file, not `inspect.getsource`: these methods get wrapped.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "phone.py").read_text()
    composes = [n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_compose_greeting"]

    assert composes, "the greeting is no longer composed — find where it moved"
    assert any(len(c.args) >= 2 for c in composes), (
        "every _compose_greeting call passes only the greeting, so the node-wide "
        "disclosure is spoken on every line regardless of its own")


# ── a typo must not look like an unset field ─────────────────────────────────

def test_an_unknown_profile_key_is_reported(monkeypatch, caplog):
    """Silently ignored operator config is a support trap this module already
    names. Here it is worse than usual: `"greetng"` leaves the line answering
    nobody's name in the node's voice — indistinguishable from a line that was
    never given a greeting."""
    import logging

    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        LINE: {"start": START, "greetng": "Rivera Plumbing.", "voice_id": "af_heart"}}))

    with caplog.at_level(logging.ERROR, logger="nano-claw.phone"):
        phone.delegate_starts()

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "greetng" in messages
    assert "greeting" in messages, "the suggestion is most of the value"
    assert "voice_id" in messages


def test_a_typo_does_not_take_the_line_off_the_air(monkeypatch):
    """Reported, not refused. Refusing would drop a working line over a spelling
    mistake in a field it does not need."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        LINE: {"start": START, "greetng": "Rivera Plumbing."}}))

    profile = phone.delegate_starts()[LINE]

    assert profile.start_url == START
    assert profile.greeting == "", "the typo'd key must not be silently honoured"


def test_every_documented_profile_key_is_recognised():
    """The runbook and the parser must not drift. A key documented but not
    accepted is the same trap with the operator following instructions."""
    from pathlib import Path

    runbook = (Path(__file__).resolve().parents[2] / "docs"
               / "delegating-a-phone-line.md").read_text()
    for key in phone._PROFILE_KEYS:
        assert f'"{key}"' in runbook or f"`{key}`" in runbook, (
            f"profile key {key!r} is accepted but appears nowhere in the runbook")


# ── a line that does not record must not say it does ─────────────────────────

def test_a_line_can_opt_out_of_recording(monkeypatch):
    """The gap the per-line disclosure exposed: the wording was configurable
    while the behaviour was not, so a business that had not agreed to be
    recorded was recorded anyway with someone else's sentence."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        LINE: {"start": START, "record": False}}))

    assert phone.delegate_starts()[LINE].record is False
    assert phone.delegate_starts()[LINE].record is not None


def test_recording_stays_on_by_default(monkeypatch):
    """Every existing line is unchanged: capture feeds the call review."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({
        LINE: {"start": START}}))

    assert phone.delegate_starts()[LINE].record is True


def test_a_non_recording_line_speaks_no_disclosure(monkeypatch):
    """Same rule as riff-builder's honest-copy gate: copy may not promise what
    the system does not do. "This call may be recorded" is a claim about
    behaviour, not a courtesy."""
    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "Node-wide notice.")
    silent = _bare_call(route("http://h/t", record=False))

    assert silent.recording_notice == ""


def test_claiming_to_record_while_not_recording_is_refused(monkeypatch, caplog):
    """The dishonest combination, reported rather than spoken."""
    import logging

    monkeypatch.setenv("NANO_CLAW_PHONE_RECORD_NOTICE", "Node-wide notice.")
    call = _bare_call(route("http://h/t", record=False,
                            record_notice="This call may be recorded."))

    with caplog.at_level(logging.ERROR, logger="nano-claw.phone"):
        notice = call.recording_notice

    assert notice == "", "the line would have claimed a recording it never makes"
    assert any("record=false" in r.getMessage() for r in caplog.records)


def test_the_tap_is_resolved_before_it_is_opened():
    """Order matters: the route must be known BEFORE `CallTap.create`, or a line
    that opted out is captured for the length of its own construction."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "phone.py").read_text()
    tree = ast.parse(source)
    init = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "__init__"
                and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "create" for c in ast.walk(n)))

    lines = {}
    for node in ast.walk(init):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in ("route_for", "create"):
                lines.setdefault(name, node.lineno)

    assert "route_for" in lines and "create" in lines
    assert lines["route_for"] < lines["create"], (
        "the tap opens before the route is known — an opted-out line is recorded")


def test_an_opted_out_line_opens_no_tap(monkeypatch):
    """The BEHAVIOUR, not the config. Every other test here checks that
    `record: false` parses and silences the disclosure; making the tap open
    regardless failed none of them. A recording opt-out that still records is
    worse than none, because the disclosure now truthfully says nothing.
    """
    created = []

    def fake_create(call_id, codec, inbound, outbound):
        created.append(call_id)
        return None

    monkeypatch.setattr(phone, "CallTap", type("T", (), {"create": staticmethod(fake_create)}))
    monkeypatch.setattr(phone, "_metrics_conn", None)
    monkeypatch.setattr(phone.call_log, "emit", lambda *a, **k: None)

    class WS:
        async def send_json(self, _m):
            return None

    def build(profile):
        phone._call_routing.clear()
        if profile is not None:
            phone._call_routing["v3:tapcheck"] = (
                phone.DelegateRoute("http://h/t", profile), 0.0)
        created.clear()
        try:
            phone.PhoneCall(WS(), "v3:tapcheck", _flow=None, _flow_domain_id=None)
        except Exception:
            pass          # __init__ touches far more than this test needs
        return list(created)

    recording = phone.DelegateProfile(start_url="http://h/s", record=True)
    silent = phone.DelegateProfile(start_url="http://h/s", record=False)

    assert build(recording), "a recording line opened no tap"
    assert build(silent) == [], (
        "an opted-out line was still captured — the disclosure now truthfully "
        "says nothing while the call is recorded anyway")


# ── two switches, deliberately not one ───────────────────────────────────────

def test_a_phone_line_ignores_the_console_mode():
    """A live phone line must not change what it does because someone touched a
    dropdown while looking at something else.

    The browser hop gates on `is_delegate_mode()`; the phone hop gates only on
    whether the call has a routing entry. Undocumented until now, and surprising
    in both directions — including the one that matters: there is NO WAY to turn
    a delegated line off from the console.
    """
    from voice.flow_session import set_flow_mode

    phone._call_routing["v3:mode-test"] = (route("http://h/api/session/s1/turn"), 0.0)

    for mode in ("spacechannel", "delegate", "lawyer", "none"):
        set_flow_mode(mode)
        assert phone.routing_for("v3:mode-test") is not None, (
            f"a configured line stopped being delegated in {mode!r} mode — a "
            f"console dropdown must not silently change a phone line")

    set_flow_mode("spacechannel")


def test_removing_the_did_is_what_turns_a_line_off(monkeypatch):
    """The documented way off, and it needs no restart: configuration is read
    per call."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps({LINE: START}))
    assert LINE in phone.delegate_starts()

    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", "{}")
    assert phone.delegate_starts() == {}, (
        "configuration must be re-read per call, or turning a line off would "
        "need a restart and drop every live call to do it")


# ── the line's identity does not wait for a round trip ───────────────────────
# The media stream connects on its own schedule and PhoneCall picks its greeting
# at construction from route_for(). On 2026-07-31 the stream started 68ms BEFORE
# the delegate start returned, found no route, and answered a Property
# Maintenance line with the node's default Space Channel greeting — while the
# turns that followed WERE delegated, because those look the route up later. The
# caller heard one business and talked to another.

def test_the_profile_is_available_before_the_delegate_url_is_known():
    """Which business a DID answers as is local configuration. It must be
    readable the instant the media stream asks, with no URL yet."""
    phone._call_routing["cid-1"] = (
        route("", greeting="Thanks for calling Property Maintenance Desk."), 0.0)

    found = phone.route_for("cid-1")

    assert found is not None
    assert found.profile.greeting == "Thanks for calling Property Maintenance Desk."


def test_no_delegate_url_yet_means_no_delegated_turn_yet():
    """The claim covers the greeting only. A turn arriving inside that window
    must not be sent to an empty URL — it falls through exactly as an unrouted
    call does."""
    phone._call_routing["cid-2"] = (route("", greeting="Property Maintenance."), 0.0)

    assert not phone.routing_for("cid-2")


def test_a_completed_start_upgrades_the_claim_to_a_real_conversation():
    phone._call_routing["cid-3"] = (route("", greeting="Property Maintenance."), 0.0)
    phone._call_routing["cid-3"] = (
        route("/api/delegate/f/s/turn", greeting="Property Maintenance."), 1.0)

    assert phone.routing_for("cid-3") == "/api/delegate/f/s/turn"
    assert phone.route_for("cid-3").profile.greeting == "Property Maintenance."


# ── an opted-in line may speak the app's opening ─────────────────────────────
# The start exchange refuses an app-authored greeting by default, because
# delegate text going straight to TTS is an arbitrary-speech capability. But a
# personalized opening cannot come from static config: "Unit B at 14723 Martell
# Ave" is known only after the caller-id lookup. So the OPERATOR opts a specific
# line in, which is the same trust level as every other setting here.

def test_a_line_does_not_speak_the_app_opening_by_default():
    """The capability stays closed unless someone opens it deliberately."""
    assert DelegateProfile(start_url=START).speak_app_opening is False


def test_an_opted_in_line_appends_the_apps_opening():
    call = object.__new__(phone.PhoneCall)
    call.delegate_route = DelegateRoute(
        "/turn",
        DelegateProfile(start_url=START,
                        greeting="Thanks for calling Property Maintenance Desk.",
                        speak_app_opening=True),
        app_opening="Unit B at 14723 Martell Ave - is that right?",
    )

    line = call.greeting_line

    assert line.startswith("Thanks for calling Property Maintenance Desk.")
    assert "Unit B at 14723 Martell Ave" in line


def test_a_line_that_did_not_opt_in_speaks_only_the_operators_words():
    call = object.__new__(phone.PhoneCall)
    call.delegate_route = DelegateRoute(
        "/turn",
        DelegateProfile(start_url=START,
                        greeting="Thanks for calling Property Maintenance Desk."),
        app_opening="Unit B at 14723 Martell Ave - is that right?",
    )

    assert call.greeting_line == "Thanks for calling Property Maintenance Desk."


def test_an_opted_in_line_with_no_opening_is_just_the_greeting():
    """An unidentified caller gets no personalized line; the greeting stands
    alone rather than trailing whitespace or an empty sentence."""
    call = object.__new__(phone.PhoneCall)
    call.delegate_route = DelegateRoute(
        "/turn",
        DelegateProfile(start_url=START, greeting="Property Maintenance Desk.",
                        speak_app_opening=True),
        app_opening="",
    )

    assert call.greeting_line == "Property Maintenance Desk."


def test_speak_app_opening_is_a_recognised_profile_key(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_DELEGATE_STARTS", json.dumps(
        {LINE: {"start": START, "greeting": "Hi.", "speak_app_opening": True}}))

    profile = phone.delegate_starts()[LINE]

    assert profile.speak_app_opening is True


def test_the_app_opening_can_stand_alone():
    """An app whose opening already names the business would be read twice if
    the operator repeated it. Empty `greeting` + `speak_app_opening` is a
    supported configuration, not a line that forgot its name."""
    call = object.__new__(phone.PhoneCall)
    call.delegate_route = DelegateRoute(
        "/turn",
        DelegateProfile(start_url=START, greeting="", speak_app_opening=True),
        app_opening="Thanks for calling Property Maintenance Desk. Unit B?",
    )

    assert call.greeting_line == (
        "Thanks for calling Property Maintenance Desk. Unit B?")


# ── the end-of-call signal, wired ────────────────────────────────────────────
#
# riff sends done=true once its flow rests terminal (the reply is the
# conversation's last). Before this wiring, "Goodbye" was only text: the
# gateway spoke it and nobody hung up, so real callers (2026-08-03) sat on a
# live leg re-hearing "your session is complete" for as long as they kept
# talking — every utterance reset the idle watchdog, which only fires on ~60s
# of total dead air.


class _Resettable:
    def reset(self):
        pass


def _delegated_call(monkeypatch, reply, interrupted=False):
    """A minimal PhoneCall whose next turn is delegated and returns `reply`.

    Complete enough that `_stream_reply` runs to the end WITHOUT raising —
    the first draft of these tests lacked `endpointer`, blew up in the
    function's `finally`, and swallowed the exception, proving nothing
    (Codex, 2026-08-03). The strict asyncio.run below is the point.
    """
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.call_id = "call-done-1"
    call.telnyx_call_id = "v3:done-1"
    call.session_id = "phone-done-1"
    call.tap = None
    call._http = object()
    call.speaking = False
    call.interrupted = interrupted
    call.closed = False
    call._playback_flush_sent = False
    call.endpointer = _Resettable()
    call.last_activity = 0.0
    call._reset_barge_in = lambda: None
    call._stop_thinking_cue = lambda: None

    async def consume(units):
        async for _ in units:
            pass

    call._speak_sentences = consume

    seen = {"hangups": 0}

    async def fake_hangup(client, **kwargs):
        seen["hangups"] += 1
        return True

    call.hangup_after_playback = fake_hangup

    monkeypatch.setattr(phone, "routing_for", lambda cid: "http://127.0.0.1:8790/t")

    async def fake_delegate(client, url, text, *, who, turn_id):
        return reply

    monkeypatch.setattr(phone, "call_delegate", fake_delegate)
    return call, seen


def test_a_terminal_delegate_reply_hangs_up_after_speaking(monkeypatch):
    from voice.turn_delegate import DelegateReply

    call, seen = _delegated_call(
        monkeypatch, DelegateReply("Done — Goodbye.", ok=True, terminal=True))
    asyncio.run(call._stream_reply("thanks, that's all"))

    assert seen["hangups"] == 1
    assert call.closed is True
    assert call.speaking is False, "the finally must still run after a hangup"


def test_a_mid_conversation_reply_never_hangs_up(monkeypatch):
    from voice.turn_delegate import DelegateReply

    call, seen = _delegated_call(
        monkeypatch, DelegateReply("What unit are you in?", ok=True))
    asyncio.run(call._stream_reply("the door sticks"))

    assert seen["hangups"] == 0
    assert call.closed is False


def test_an_interrupted_goodbye_defers_the_hangup_to_the_next_turn(monkeypatch):
    """Barging into the goodbye means the caller wants something. The delegate
    repeats done on every post-terminal turn, so deferring costs one turn,
    never the call."""
    from voice.turn_delegate import DelegateReply

    call, seen = _delegated_call(
        monkeypatch, DelegateReply("Goodbye.", ok=True, terminal=True),
        interrupted=True)
    asyncio.run(call._stream_reply("wait actually"))

    assert seen["hangups"] == 0
    assert call.closed is False


def test_a_failed_delegate_turn_never_hangs_up(monkeypatch):
    """The apology path must keep the caller, whatever a broken delegate sent."""
    from voice.turn_delegate import DELEGATE_APOLOGY, DelegateReply

    call, seen = _delegated_call(
        monkeypatch, DelegateReply(DELEGATE_APOLOGY, ok=False, failure="status 502"))
    asyncio.run(call._stream_reply("hello?"))

    assert seen["hangups"] == 0
    assert call.closed is False


def test_a_failed_carrier_hangup_leaves_the_call_open(monkeypatch):
    """`closed` only on a confirmed send: marking a failed hangup closed would
    silence media and the idle watchdog on a leg the carrier still holds."""
    from voice.turn_delegate import DelegateReply

    call, _ = _delegated_call(
        monkeypatch, DelegateReply("Goodbye.", ok=True, terminal=True))

    async def failing_hangup(client, **kwargs):
        return False

    call.hangup_after_playback = failing_hangup
    asyncio.run(call._stream_reply("bye"))

    assert call.closed is False


# ── hangup_after_playback itself (the drain wait, the abort, the surplus) ────


def _drain_call():
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.call_id = "call-drain-1"
    call.telnyx_call_id = "v3:drain-1"
    call.interrupted = False
    return call


def test_hangup_waits_out_the_stashed_playback_surplus(monkeypatch):
    """_speak_sentences restores _frame_pacer before any caller can hang up,
    so the live pacer is ALWAYS gone here. Without the stashed surplus every
    real hangup waited only the 350ms margin and clipped the goodbye."""
    import time as _time

    call = _drain_call()
    call._frame_pacer = None
    call._playback_surplus = (1.0, _time.monotonic() - 0.4)

    waits = []

    async def fake_sleep(s):
        waits.append(s)

    async def fake_cmd(client, cid, command, payload):
        return True

    monkeypatch.setattr(phone.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)
    assert asyncio.run(call.hangup_after_playback(object(), margin_s=0.1)) is True
    assert len(waits) == 1
    # ~0.6s of surplus remains after the 0.4s that already elapsed, + margin.
    assert 0.5 < waits[0] < 0.8, waits


def test_a_barge_in_during_the_drain_wait_aborts_the_hangup(monkeypatch):
    """The drain wait is exactly the window a caller barges into the goodbye;
    deciding before the wait and never re-checking would drop them mid-word."""
    call = _drain_call()
    call._frame_pacer = None
    call._playback_surplus = (0.0, 0.0)

    sent = []

    async def fake_sleep(s):
        call.interrupted = True  # the barge-in lands during the wait

    async def fake_cmd(client, cid, command, payload):
        sent.append(command)
        return True

    monkeypatch.setattr(phone.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)
    result = asyncio.run(call.hangup_after_playback(
        object(), abort=lambda: call.interrupted))

    assert result is False
    assert sent == [], "the carrier command must not go out after an abort"


def test_a_failed_telnyx_send_reports_false(monkeypatch):
    call = _drain_call()
    call._frame_pacer = None

    async def fake_sleep(s):
        pass

    async def fake_cmd(client, cid, command, payload):
        return False

    monkeypatch.setattr(phone.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)
    assert asyncio.run(call.hangup_after_playback(object())) is False
