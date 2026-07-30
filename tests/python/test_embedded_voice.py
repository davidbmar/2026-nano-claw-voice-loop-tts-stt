"""nano-claw as another product's audio engine.

riff-builder's page wants to talk on its OWN page and let this gateway do the
audio. Two things stood in the way: its origin got a flat 403 from `/ws`, and
there was no way to say "speak exactly this" — `text_message` means "a human
said X" and runs a turn, so a page could only speak by pretending someone talked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voice.webauth.aiohttp_adapter import _origin_matches_request


def origin_ok(origin, host="localhost:8080"):
    return _origin_matches_request(
        SimpleNamespace(headers={"Origin": origin, "Host": host}))


# ── who may embed ────────────────────────────────────────────────────────────

def test_a_product_origin_is_locked_out_by_default(monkeypatch):
    """Empty by default. Declaring who may speak through this node is the
    operator's act, never a default."""
    monkeypatch.delenv("NANO_CLAW_EMBED_ORIGINS", raising=False)

    assert origin_ok("http://127.0.0.1:8790") is False


def test_a_declared_origin_is_admitted(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_EMBED_ORIGINS", "http://127.0.0.1:8790")

    assert origin_ok("http://127.0.0.1:8790") is True


def test_declaring_one_origin_does_not_admit_others(monkeypatch):
    """Exact origins, not a wildcard or a suffix match — the mistake that turns
    an allowlist into an open door."""
    monkeypatch.setenv("NANO_CLAW_EMBED_ORIGINS", "http://127.0.0.1:8790")

    for other in ("http://evil.example.com",
                  "http://127.0.0.1:8790.evil.com",
                  "https://127.0.0.1:8790",
                  "http://127.0.0.1:8791"):
        assert origin_ok(other) is False, other


def test_the_console_still_works(monkeypatch):
    """The relaxation must not disturb the gateway's own page."""
    monkeypatch.setenv("NANO_CLAW_EMBED_ORIGINS", "http://127.0.0.1:8790")

    assert origin_ok("http://localhost:8080", "localhost:8080") is True


# ── speaking on demand ───────────────────────────────────────────────────────

class FakeWS:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def test_speak_is_bounded_like_a_delegate_reply():
    """Text from another origin reaching TTS, so it takes the same limits as a
    delegate's reply: control characters stripped, length capped."""
    from voice.turn_delegate import _CONTROL_CHARS, _MAX_REPLY_CHARS

    raw = "Marked\x00 as a keeper\x07."
    cleaned = raw.translate(_CONTROL_CHARS)[:_MAX_REPLY_CHARS].strip()

    assert cleaned == "Marked as a keeper."
    assert "\x00" not in cleaned

    huge = ("x" * (_MAX_REPLY_CHARS + 500)).translate(_CONTROL_CHARS)[:_MAX_REPLY_CHARS]
    assert len(huge) == _MAX_REPLY_CHARS


def test_speak_is_a_distinct_message_from_text_message():
    """They mean different things. `text_message` is "a human said X" and runs a
    turn; `speak` is "say this" and runs none. Conflating them would make every
    announcement look like a caller utterance in the transcript."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "server.py").read_text()
    handled = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "msg_type"
                and node.comparators
                and isinstance(node.comparators[0], ast.Constant)):
            handled.add(node.comparators[0].value)

    assert "speak" in handled, "the gateway cannot be told to say something"
    assert "text_message" in handled
