"""Transcribe mode: everything heard goes to gemma4, and nothing is ever spoken.

The mode exists to feed real speech to the local model on the M5 so its
internals can be inspected there (j-space probing). That purpose puts an unusual
weight on one property: **silence**. A probe that occasionally answers out loud
is not a quieter assistant, it is a contaminated experiment — and on a phone
line it is an assistant talking to someone who was told nothing would.

So the load-bearing test here is not "does it forward the text". It is
`test_transcribe_mode_never_speaks`, plus the ordering test that keeps the
handler ahead of the two dispatch branches that do speak.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice import gemma_probe, server
from voice.flow_session import FLOW_MODES, is_transcribe_mode

ROOT = Path(__file__).resolve().parents[2]


class FakeWS:
    """Records what was sent to the browser; never closed."""

    closed = False

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _session() -> SimpleNamespace:
    """A session with history disabled, so `_complete_agent_turn` short-circuits.

    `_history_store=None` is what makes the completion path a no-op; the tenant
    fields stay unset so `_history_owner` returns None as well. Both are checked
    before any write, so nothing here touches a store.
    """
    return SimpleNamespace(
        _history_agent_active=True,
        _history_agent_failed=False,
        _history_agent_parts=[],
        _history_store=None,
        _history_started=False,
        _deep_projection_pending=False,
    )


@pytest.fixture
def in_transcribe_mode(monkeypatch):
    monkeypatch.setattr(server, "is_transcribe_mode", lambda *a, **k: True)


@pytest.fixture
def capture_to_tmp(monkeypatch, tmp_path):
    """Point the JSONL capture at a temp file and hand back its path."""
    target = tmp_path / "jspace.jsonl"
    monkeypatch.setenv("NANO_CLAW_TRANSCRIBE_LOG", str(target))
    return target


# ---------------------------------------------------------------- the contract


def test_transcribe_mode_never_speaks(monkeypatch, in_transcribe_mode, capture_to_tmp):
    """The mode. Any synthesis call at all is a failure.

    Both speak helpers are replaced with a detonator rather than a spy: a spy
    records the violation and lets the turn finish looking healthy, which is how
    a muted-but-still-synthesizing path would slip through.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "transcribe mode reached a speech-synthesis path — it must never "
            "produce audio"
        )

    monkeypatch.setattr(server, "_speak_with_events", forbidden)
    monkeypatch.setattr(server, "_speak_text_for_generation", forbidden)

    async def fake_send(text, **kwargs):
        return {"response": "gemma heard you", "eval_count": 7}

    monkeypatch.setattr(server, "send_to_gemma", fake_send)

    ws, session = FakeWS(), _session()
    handled = asyncio.run(
        server._handle_transcribe_request(ws, session, "the tap is dripping")
    )

    assert handled is True, "the turn must be consumed, not passed on"
    types_sent = [m["type"] for m in ws.sent]
    assert not any(t.startswith("agent_audio") for t in types_sent), (
        f"audio events were emitted in transcribe mode: {types_sent}"
    )


def test_transcribe_runs_before_the_branches_that_speak():
    """Ordering is the structural half of the guarantee.

    Delegate and scheduler both synthesize. If the transcribe check ever moves
    below either, a transcribe-mode turn could be consumed by a speaking branch
    and the mode would break with no test failing — every assertion above would
    still pass, because they call the handler directly.
    """
    tree = ast.parse((ROOT / "voice" / "server.py").read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_handle_agent_request"
    )
    called: list[str] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in {
                "_handle_transcribe_request",
                "_handle_delegate_request",
                "_handle_scheduler_request",
            }:
                called.append(name)

    assert called, "no dispatch branches found — the handler was restructured"
    assert called[0] == "_handle_transcribe_request", (
        "the transcribe check must come first in _handle_agent_request; "
        f"dispatch order is {called}"
    )


def test_a_dead_probe_still_stays_silent(monkeypatch, in_transcribe_mode, capture_to_tmp):
    """The M5 being unreachable must not fall through to the speaking agent.

    Returning False on error would be the natural way to write this and is
    exactly wrong: the mode would start talking the moment the probe host went
    down, which is both the least expected time and the hardest to notice.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("a failed probe fell through to a speaking path")

    monkeypatch.setattr(server, "_speak_with_events", forbidden)
    monkeypatch.setattr(server, "_speak_text_for_generation", forbidden)

    async def dead(text, **kwargs):
        raise ConnectionError("M5 unreachable")

    monkeypatch.setattr(server, "send_to_gemma", dead)

    ws, session = FakeWS(), _session()
    handled = asyncio.run(server._handle_transcribe_request(ws, session, "hello?"))

    assert handled is True, "a failed probe must still consume the turn"
    assert not any(m["type"] == "agent_reply" for m in ws.sent), (
        "a probe failure must not surface as a reply — the screen shows only "
        "what was heard; failures belong in the log and the capture file"
    )


def test_the_mic_rearms_before_the_model_is_called(
    monkeypatch, in_transcribe_mode, capture_to_tmp
):
    """Continuous listening: re-arm first, ask gemma second.

    The browser arms its next turn only when playback ends, and this mode never
    plays anything — so without an explicit signal `autoTurnPending` latches
    true and the page goes deaf after one automatic turn while still looking
    live. That is the 2026-08-06 bug where a count to twenty was never heard.

    Sending it AFTER the model call would fix that bug and leave a quieter one:
    deafness for the whole generation, ~3s at the observed rate and unbounded
    for a slow model. So the ordering is the requirement, not the message.
    """
    seen_when_model_ran: list[list[str]] = []

    async def fake_send(text, **kwargs):
        seen_when_model_ran.append([m["type"] for m in ws.sent])
        return {"response": "ok"}

    monkeypatch.setattr(server, "send_to_gemma", fake_send)

    ws, session = FakeWS(), _session()
    asyncio.run(server._handle_transcribe_request(ws, session, "one two three"))

    assert seen_when_model_ran, "the model was never called"
    assert "transcribe_listening" in seen_when_model_ran[0], (
        "the mic must be re-armed BEFORE waiting on the model, or the page is "
        f"deaf for the whole generation; sent so far was {seen_when_model_ran[0]}"
    )


def test_a_dead_probe_still_rearms_the_mic(
    monkeypatch, in_transcribe_mode, capture_to_tmp
):
    """An unreachable M5 must not leave the session permanently deaf.

    The re-arm precedes the call, so this holds structurally — but it is the
    consequence worth naming: a probe failure that also stopped the listening
    would end the session rather than degrade it, and would look like the user
    having stopped talking.
    """

    async def dead(text, **kwargs):
        raise ConnectionError("M5 unreachable")

    monkeypatch.setattr(server, "send_to_gemma", dead)
    ws = FakeWS()
    asyncio.run(server._handle_transcribe_request(ws, _session(), "still talking"))

    assert "transcribe_listening" in [m["type"] for m in ws.sent]


def test_the_browser_handles_the_rearm_message():
    """The server's signal is useless if the page ignores it.

    Two spellings of one fact — this file's recurring bug shape. A `case` that
    never landed in app.js would leave the mode deaf with every python test
    green, because none of them run the browser.
    """
    app_js = (ROOT / "voice" / "web" / "app.js").read_text()
    assert "case 'transcribe_listening':" in app_js, (
        "app.js has no handler for transcribe_listening — the mic will never "
        "re-arm in the browser"
    )
    handler = app_js.split("case 'transcribe_listening':", 1)[1].split("break;", 1)[0]
    assert "rearmPhoneMode" in handler, (
        "the transcribe_listening handler must call rearmPhoneMode; clearing "
        "the thinking indicator alone leaves autoTurnPending latched"
    )


def test_utterances_are_numbered_in_speech_order(
    monkeypatch, in_transcribe_mode, capture_to_tmp
):
    """Probes run concurrently, so file order is arrival order — `seq` is truth.

    Without it a capture cannot be read as a sequence, and a lost utterance is
    undetectable: line count alone can never show a gap.
    """

    async def fake_send(text, **kwargs):
        return {"response": f"re: {text}"}

    monkeypatch.setattr(server, "send_to_gemma", fake_send)

    session = _session()
    for utterance in ("first", "second", "third"):
        asyncio.run(server._handle_transcribe_request(FakeWS(), session, utterance))

    rows = [json.loads(line) for line in capture_to_tmp.read_text().splitlines() if line]
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["transcript"] for r in rows] == ["first", "second", "third"]


def test_other_modes_are_left_alone(monkeypatch):
    """The handler must decline every mode but its own, or it eats the product."""
    monkeypatch.setattr(server, "is_transcribe_mode", lambda *a, **k: False)
    handled = asyncio.run(
        server._handle_transcribe_request(FakeWS(), _session(), "book me a plumber")
    )
    assert handled is False


def test_every_other_mode_still_takes_the_normal_turn_path():
    """Transcribe mode must not have changed how anything else behaves.

    Two shared things were touched to build it — the turn dispatcher and the
    browser's mic re-arm — and both are used by every other mode. This asserts
    the transcribe-specific behaviour is reached only behind the mode check,
    rather than trusting that it is.
    """
    source = (ROOT / "voice" / "server.py").read_text()

    # The concurrent spawn is gated on the mode, not applied to every turn.
    # Ungated, it would remove the one-reply-at-a-time protection that keeps two
    # speaking turns from racing on the audio queue — for every mode at once.
    spawn_site = source.split("_spawn_probe(_handle_transcribe_request", 1)
    assert len(spawn_site) == 2, "the probe spawn site moved or was renamed"
    preceding = spawn_site[0][-400:]
    assert "if is_transcribe_mode():" in preceding, (
        "_spawn_probe must be reached only inside a transcribe-mode branch; "
        "ungated it would drop turn serialization for every mode"
    )
    assert "_spawn_agent(" in source, (
        "the normal serialized turn path must still exist for other modes"
    )


def test_the_browser_rearm_delay_is_unchanged_for_other_modes():
    """`rearmPhoneMode` gained an `immediate` flag; the default must not shift.

    Every speaking mode calls it with one argument. If the delay became
    unconditional, they would all re-arm into the decaying tail of their own
    audio and re-trigger the VAD on the assistant's own voice.
    """
    app_js = (ROOT / "voice" / "web" / "app.js").read_text()
    body = app_js.split("function rearmPhoneMode(", 1)[1].split("\n}", 1)[0]
    assert "immediate ? 0 : PHONE_REARM_MS" in body, (
        "the re-arm delay must still default to PHONE_REARM_MS when `immediate` "
        f"is not passed; body was:\n{body}"
    )
    # Every existing caller passes one argument, so `immediate` is undefined for
    # them — this checks the speaking path was not switched over wholesale.
    speaking_rearm = app_js.count("rearmPhoneMode('Waiting for the phone side...')")
    assert speaking_rearm >= 1, (
        "the end-of-playback re-arm for speaking modes is gone or changed shape"
    )


# ------------------------------------------------------------------ the capture


def test_the_exchange_is_recorded(monkeypatch, in_transcribe_mode, capture_to_tmp):
    """Transcript and reply both land in the JSONL capture."""

    async def fake_send(text, **kwargs):
        return {"response": "acknowledged", "prompt_eval_count": 11, "eval_count": 3}

    monkeypatch.setattr(server, "send_to_gemma", fake_send)
    asyncio.run(server._handle_transcribe_request(FakeWS(), _session(), "kitchen sink"))

    entry = json.loads(capture_to_tmp.read_text().strip())
    assert entry["transcript"] == "kitchen sink"
    assert entry["response"] == "acknowledged"
    assert entry["prompt_eval_count"] == 11
    assert entry["error"] is None


def test_failures_are_recorded_too(monkeypatch, in_transcribe_mode, capture_to_tmp):
    """A capture of successes only would misrepresent the run.

    For a research record the gaps are data: an utterance the model never saw is
    a different fact from one it saw and answered emptily.
    """

    async def dead(text, **kwargs):
        raise ConnectionError("M5 unreachable")

    monkeypatch.setattr(server, "send_to_gemma", dead)
    asyncio.run(server._handle_transcribe_request(FakeWS(), _session(), "still heard"))

    entry = json.loads(capture_to_tmp.read_text().strip())
    assert entry["transcript"] == "still heard", "the transcript survives a dead probe"
    assert "ConnectionError" in entry["error"]


def test_a_broken_capture_path_does_not_drop_the_turn(
    monkeypatch, in_transcribe_mode, tmp_path
):
    """Recording is best-effort. A live turn must not depend on the disk."""
    unwritable = tmp_path / "file-not-a-dir"
    unwritable.write_text("blocking the path")
    monkeypatch.setenv("NANO_CLAW_TRANSCRIBE_LOG", str(unwritable / "nested.jsonl"))

    async def fake_send(text, **kwargs):
        return {"response": "fine"}

    monkeypatch.setattr(server, "send_to_gemma", fake_send)
    handled = asyncio.run(server._handle_transcribe_request(FakeWS(), _session(), "hi"))
    assert handled is True


# ------------------------------------------------------------------ the targeting


def test_base_url_strips_the_openai_compat_suffix(monkeypatch):
    """NANO_CLAW_OLLAMA_BASE carries `/v1`; the native endpoints sit at the root.

    Left on, every request would go to `/v1/api/generate` and 404 — and because
    the probe swallows nothing, that would surface as every single utterance
    recorded with an error.
    """
    monkeypatch.setenv("NANO_CLAW_OLLAMA_BASE", "http://192.168.86.29:11435/v1")
    assert gemma_probe.probe_base_url() == "http://192.168.86.29:11435"


def test_an_empty_env_var_falls_through_to_the_default(monkeypatch):
    """run.sh sends `-e VAR="$VAR"`, so an unset host var arrives as "".

    Treating "" as configured is the 2026-07-29 outage in miniature: the probe
    would target the empty string and fail on every turn.
    """
    monkeypatch.setenv("NANO_CLAW_OLLAMA_BASE", "")
    monkeypatch.setenv("NANO_CLAW_TRANSCRIBE_BASE", "   ")
    assert gemma_probe.probe_base_url() == gemma_probe.DEFAULT_BASE

    monkeypatch.setenv("NANO_CLAW_TRANSCRIBE_MODEL", "")
    assert gemma_probe.probe_model() == "gemma4:26b"


def test_the_prompt_is_the_bare_transcript():
    """No wrapper, or the wrapper becomes part of what is measured.

    A prefix would also sit at the front of the context, where its influence on
    the representation is strongest — precisely the region under study.
    """
    assert gemma_probe.build_prompt("just these words") == "just these words"


def test_the_probe_is_stateless(monkeypatch):
    """Each utterance is an independent stimulus — no history, ever.

    Chosen deliberately on 2026-08-06 so that turns stay comparable to each
    other and no representation is conditioned on what came before.

    This is the decision most likely to be undone by accident, because adding
    history looks like a straight improvement — the dialogue reads better and
    nothing errors. What breaks is silent and total: every captured turn becomes
    conditioned on its predecessors, so the file can no longer be compared
    across turns, and the context grows until it truncates without a signal.

    Asserted at the payload, not the prompt, because history could arrive either
    as concatenated text OR as a `messages` list — this catches both.
    """
    sent: dict = {}

    class FakeResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"response": "ok"}

    class FakeClient:
        async def post(self, url, json=None, timeout=None):
            sent["url"] = url
            sent["payload"] = json
            return FakeResponse()

    for utterance in ("first thing said", "second thing said"):
        asyncio.run(gemma_probe.send_to_gemma(utterance, client=FakeClient()))

    payload = sent["payload"]
    assert payload["prompt"] == "second thing said", (
        "the prompt must be this utterance alone; prior turns leaked in"
    )
    assert "messages" not in payload, (
        "a messages list means conversation history — the probe must be stateless"
    )
    assert "context" not in payload, (
        "ollama's `context` field replays prior state and would carry history "
        "invisibly, without changing the prompt"
    )
    assert sent["url"].endswith("/api/generate"), (
        "/api/chat is the conversational endpoint; the stateless probe uses "
        "/api/generate"
    )


def test_thinking_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_TRANSCRIBE_THINK", raising=False)
    assert gemma_probe.probe_thinking_enabled() is False
    monkeypatch.setenv("NANO_CLAW_TRANSCRIBE_THINK", "1")
    assert gemma_probe.probe_thinking_enabled() is True


# ------------------------------------------------------------------ registration


def test_the_mode_is_selectable_and_personaless():
    """It must appear in the dropdown, and carry no persona.

    `profile: none` is not cosmetic here — a persona would shape the prompt, and
    the prompt is the stimulus.
    """
    assert "transcribe" in FLOW_MODES
    assert FLOW_MODES["transcribe"]["profile"] == "none"
    assert FLOW_MODES["transcribe"]["scheduler"] is False


def test_the_mode_predicate_matches_only_itself():
    assert is_transcribe_mode("transcribe") is True
    for other in ("spacechannel", "delegate", "base", "lawyer"):
        assert is_transcribe_mode(other) is False, f"{other} misread as transcribe"
