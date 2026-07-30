"""Wiring tests: the browser hop actually routes through the delegate.

`test_turn_delegate.py` proves the adapter is safe in isolation. These prove it is
reached — that selecting the mode diverts the turn away from nano-claw's own
model, that the URL comes from the conversation rather than a global, and that a
mode with nowhere to send says so instead of quietly answering in our own voice.

The distinction matters: a correct adapter nobody calls is the same outage as no
adapter at all.
"""
from __future__ import annotations

import asyncio

import pytest

import voice.server as server
from voice.flow_session import (
    FLOW_MODES,
    default_delegate_url,
    delegate_allowed_hosts,
    is_delegate_mode,
    set_flow_mode,
)
from voice.turn_delegate import DELEGATE_APOLOGY


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakeSession:
    """Only what the hop touches."""

    def __init__(self, delegate_url=""):
        self.delegate_url = delegate_url


class RecordingClient:
    def __init__(self, body=None, status=200):
        self.body = body if body is not None else {"reply": "From the app."}
        self.status = status
        self.calls = []

    async def post(self, url, *, json, timeout, follow_redirects=None):
        self.calls.append({"url": url, "json": json})

        class R:
            status_code = self.status
            def json(_self):
                return self.body
        return R()


@pytest.fixture
def spoken(monkeypatch):
    """Capture what _handle_delegate_request hands to the shared reply path."""
    seen = []

    async def fake_process(ws, session, data, req_start=None):
        seen.append(data)

    monkeypatch.setattr(server, "_process_api_response", fake_process)
    return seen


@pytest.fixture(autouse=True)
def restore_mode():
    yield
    set_flow_mode("spacechannel")


# ── the diversion itself ─────────────────────────────────────────────────────

def test_a_normal_mode_is_not_intercepted(spoken):
    """The hop must be inert unless delegate mode is selected, or every existing
    mode would start POSTing the caller's words somewhere."""
    async def exercise():
        set_flow_mode("spacechannel")
        client = RecordingClient()
        handled = await server._handle_delegate_request(
            FakeWS(), FakeSession("http://127.0.0.1:8790/t"), client, "hello")

        assert handled is False, "the turn must fall through to the model"
        assert client.calls == [], "and nothing may be sent to the delegate"

    asyncio.run(exercise())


def test_delegate_mode_diverts_the_turn_and_speaks_the_apps_words(spoken, monkeypatch):
    async def exercise():
        monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "")
        set_flow_mode("delegate")
        client = RecordingClient(body={"reply": "Rivera Plumbing, how can I help?"})
        handled = await server._handle_delegate_request(
            FakeWS(), FakeSession("http://127.0.0.1:8790/api/session/s1/turn"),
            client, "do you do water heaters")

        assert handled is True, "the turn must NOT also reach nano-claw's model"
        assert client.calls[0]["url"] == "http://127.0.0.1:8790/api/session/s1/turn"
        assert client.calls[0]["json"] == {
            "text": "do you do water heaters", "who": "caller", "speak": False}
        assert spoken == [
            {"type": "final", "response": "Rivera Plumbing, how can I help?"}]

    asyncio.run(exercise())


def test_a_failing_delegate_speaks_the_apology_not_silence(spoken, monkeypatch):
    """The failure that motivated the adapter, asserted at the wiring level."""
    async def exercise():
        monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "")
        set_flow_mode("delegate")
        client = RecordingClient(status=502, body={"detail": "interview agent failed"})
        handled = await server._handle_delegate_request(
            FakeWS(), FakeSession("http://127.0.0.1:8790/t"), client, "hi")

        assert handled is True
        assert spoken == [{"type": "final", "response": DELEGATE_APOLOGY}]

    asyncio.run(exercise())


def test_delegate_mode_with_no_url_apologizes_rather_than_answering_as_itself(
        spoken, monkeypatch):
    """Falling through to our own model here would be the subtle failure: the
    caller gets a fluent answer from the wrong assistant and nobody notices."""
    async def exercise():
        monkeypatch.delenv("NANO_CLAW_DELEGATE_URL", raising=False)
        set_flow_mode("delegate")
        client = RecordingClient()
        handled = await server._handle_delegate_request(
            FakeWS(), FakeSession(""), client, "hi")

        assert handled is True, "must not fall through to nano-claw's model"
        assert client.calls == []
        assert spoken == [{"type": "final", "response": DELEGATE_APOLOGY}]

    asyncio.run(exercise())


# ── where the URL comes from ─────────────────────────────────────────────────

def test_the_session_url_wins_over_the_environment_default(monkeypatch):
    """One delegate URL == one conversation. A second browser tab pointed at a
    different app must not be dragged to the first one's default."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "http://127.0.0.1:9999/default")

    assert server.resolve_delegate_url(
        FakeSession("http://127.0.0.1:8790/mine")) == "http://127.0.0.1:8790/mine"
    assert server.resolve_delegate_url(
        FakeSession("")) == "http://127.0.0.1:9999/default"


def test_a_refused_environment_default_leaves_the_mode_inert(monkeypatch):
    """Fail closed. A bad default must not become a live destination for
    everything the caller says."""
    monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "https://evil.example.com/collect")
    monkeypatch.delenv("NANO_CLAW_DELEGATE_HOSTS", raising=False)

    assert default_delegate_url() == ""


def test_the_allowlist_admits_a_named_host(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "https://builder.internal/t")
    monkeypatch.setenv("NANO_CLAW_DELEGATE_HOSTS", "builder.internal, other.internal")

    assert delegate_allowed_hosts() == {"builder.internal", "other.internal"}
    assert default_delegate_url() == "https://builder.internal/t"


def test_an_unset_default_is_empty_not_an_error(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_DELEGATE_URL", raising=False)
    assert default_delegate_url() == ""


# ── the mode itself ──────────────────────────────────────────────────────────

def test_is_delegate_mode_tracks_the_selection():
    set_flow_mode("delegate")
    assert is_delegate_mode() is True
    set_flow_mode("spacechannel")
    assert is_delegate_mode() is False


def test_the_delegate_greeting_claims_no_identity():
    """The gateway does not know whose line this is — v0 has no greeting
    exchange — so it must not answer as anyone in particular."""
    from voice.flow_session import flow_mode_greeting

    greeting = flow_mode_greeting("delegate")
    assert greeting
    for name in ("nano-claw", "Space Channel", "Riff", "Replicant"):
        assert name.lower() not in greeting.lower(), (
            f"the delegate greeting names {name}, which may not be whose line "
            "this is")


def test_delegate_mode_carries_no_persona():
    """`profile: "none"` is load-bearing: any other profile would layer our
    persona's system prompt under words the delegate authored."""
    assert FLOW_MODES["delegate"]["profile"] == "none"
    assert FLOW_MODES["delegate"]["scheduler"] is False


# ── the operator endpoint ────────────────────────────────────────────────────
#
# Auth for /api/voice/delegate is covered automatically: test_operator_auth.py is
# parametrized over OPERATOR_PATHS, so adding the path there earned it six tests.
# These cover what it DOES.

def test_setting_a_url_changes_where_new_conversations_go(monkeypatch):
    from voice.flow_session import set_default_delegate_url

    monkeypatch.delenv("NANO_CLAW_DELEGATE_URL", raising=False)
    assert default_delegate_url() == ""

    assert set_default_delegate_url("http://127.0.0.1:8790/api/session/x/turn") is True
    assert default_delegate_url() == "http://127.0.0.1:8790/api/session/x/turn"


def test_the_operator_choice_beats_the_environment(monkeypatch):
    """Same precedence as the flow mode and the region model: a runtime choice
    overrides the env it was seeded from."""
    from voice.flow_session import set_default_delegate_url

    monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "http://127.0.0.1:1111/env")
    assert default_delegate_url() == "http://127.0.0.1:1111/env"

    set_default_delegate_url("http://127.0.0.1:2222/operator")
    assert default_delegate_url() == "http://127.0.0.1:2222/operator"


def test_clearing_actually_clears_rather_than_revealing_the_env(monkeypatch):
    """The one that would be a real incident: an operator clears the field, the
    console shows empty, and the env value quietly rearms the delegate."""
    from voice.flow_session import set_default_delegate_url

    monkeypatch.setenv("NANO_CLAW_DELEGATE_URL", "http://127.0.0.1:1111/env")
    set_default_delegate_url("")

    assert default_delegate_url() == "", (
        "clearing fell back to the environment — the console would show no "
        "delegate while turns still went to one")


def test_a_refused_url_is_rejected_without_changing_anything(monkeypatch):
    from voice.flow_session import set_default_delegate_url

    monkeypatch.delenv("NANO_CLAW_DELEGATE_HOSTS", raising=False)
    set_default_delegate_url("http://127.0.0.1:8790/good")

    assert set_default_delegate_url("https://evil.example.com/t") is False
    assert set_default_delegate_url("http://user:pass@127.0.0.1:2375/") is False
    assert default_delegate_url() == "http://127.0.0.1:8790/good", (
        "a refused URL must not clobber the working one")


@pytest.fixture(autouse=True)
def _reset_runtime_delegate_url():
    import voice.flow_session as fs
    yield
    fs._delegate_url = None


# ── the browser's half of the start seam ─────────────────────────────────────

def test_the_console_can_mint_a_fresh_conversation(monkeypatch):
    """A phone call gets a new conversation per call. The browser had none: an
    operator created one by hand, pasted its URL, and to start a second one did
    it again — which is exactly what you want when testing a flow from the top."""
    from voice.turn_delegate import ConversationStart

    seen = {}

    async def fake_start(client, url, **kwargs):
        seen.update(kwargs, url=url)
        return ConversationStart("http://127.0.0.1:8790/api/session/fresh/turn",
                                 ok=True)

    monkeypatch.setattr(server, "start_conversation", fake_start)

    async def exercise():
        return await server.delegate_set_handler(
            _JsonRequest({"start": "http://127.0.0.1:8790/api/delegate/start",
                          "did": "+15125550100"}))

    response = asyncio.run(exercise())

    assert response.status == 200
    assert seen["url"] == "http://127.0.0.1:8790/api/delegate/start"
    assert seen["channel"] == "browser"
    assert default_delegate_url() == "http://127.0.0.1:8790/api/session/fresh/turn"


def test_clicking_twice_mints_two_conversations(monkeypatch):
    """The OPPOSITE of the phone's need. A redelivered webhook is one call and
    must deduplicate; an operator clicking twice wants two conversations, so the
    key must differ each time."""
    from voice.turn_delegate import ConversationStart

    keys = []

    async def fake_start(client, url, **kwargs):
        keys.append(kwargs["conversation_key"])
        return ConversationStart(
            f"http://127.0.0.1:8790/api/session/s{len(keys)}/turn", ok=True)

    monkeypatch.setattr(server, "start_conversation", fake_start)

    async def exercise():
        for _ in range(2):
            await server.delegate_set_handler(
                _JsonRequest({"start": "http://127.0.0.1:8790/api/delegate/start"}))

    asyncio.run(exercise())

    assert len(set(keys)) == 2, "the same key twice would reuse one conversation"


def test_a_refused_start_url_is_rejected_before_dialling(monkeypatch):
    called = []

    async def fake_start(*a, **k):
        called.append(True)

    monkeypatch.setattr(server, "start_conversation", fake_start)
    monkeypatch.delenv("NANO_CLAW_DELEGATE_HOSTS", raising=False)

    async def exercise():
        return await server.delegate_set_handler(
            _JsonRequest({"start": "https://evil.example.com/start"}))

    response = asyncio.run(exercise())

    assert response.status == 400
    assert called == [], "an unvetted host was dialled before being refused"


def test_a_failed_start_reports_the_apps_reason(monkeypatch):
    """502 with the app's own explanation, not a bare failure — a ceiling, a
    crash and an unconfigured line are otherwise indistinguishable."""
    from voice.turn_delegate import ConversationStart

    async def fake_start(*a, **k):
        return ConversationStart("", ok=False, failure="status 503: at the ceiling")

    monkeypatch.setattr(server, "start_conversation", fake_start)

    async def exercise():
        return await server.delegate_set_handler(
            _JsonRequest({"start": "http://127.0.0.1:8790/api/delegate/start"}))

    response = asyncio.run(exercise())

    assert response.status == 502
    assert "ceiling" in response.text


class _JsonRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body
