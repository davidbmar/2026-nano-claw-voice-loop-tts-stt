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
    assert "agent_reply" in types_sent
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
    assert "unreachable" in ws.sent[-1]["text"].lower()


def test_other_modes_are_left_alone(monkeypatch):
    """The handler must decline every mode but its own, or it eats the product."""
    monkeypatch.setattr(server, "is_transcribe_mode", lambda *a, **k: False)
    handled = asyncio.run(
        server._handle_transcribe_request(FakeWS(), _session(), "book me a plumber")
    )
    assert handled is False


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
