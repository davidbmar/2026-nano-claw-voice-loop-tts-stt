"""Greeting follows the active mode, and the mode survives restarts.

07-27 bug: every container restart re-read NANO_CLAW_VOICE_FLOW from .env
(=intelligence), so callers got the Space Channel intro over Document
Intelligence answers. Now the intro comes from the active mode and the
console MODE choice persists in the same settings file as the phone knobs.
"""

import asyncio
import json

import pytest

from voice import flow_session, phone
from voice.flow_session import flow_mode_greeting, set_flow_mode


@pytest.fixture(autouse=True)
def clean_mode(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "NANO_CLAW_PHONE_SETTINGS_PATH", str(tmp_path / "phone-settings.json")
    )
    monkeypatch.delenv("NANO_CLAW_VOICE_FLOW", raising=False)
    monkeypatch.setattr(flow_session, "_flow_mode", None)
    phone._overrides.clear()
    yield tmp_path / "phone-settings.json"
    phone._overrides.clear()


def run(coro):
    return asyncio.run(coro)


def test_greetings_match_their_modes():
    assert "document" in flow_mode_greeting("intelligence").lower()
    assert flow_mode_greeting("spacechannel") == phone.DEFAULT_GREETING
    assert "Space Channel" in flow_mode_greeting("spacechannel")
    # Modes without bespoke copy get the generic assistant greeting.
    assert "voice assistant" in flow_mode_greeting("base")
    # No mode's goodbye/greeting hardcodes a persona it isn't running.
    assert "Space Channel" not in phone.IDLE_GOODBYE_TEXT


def test_phone_call_greeting_follows_active_mode():
    async def exercise():
        set_flow_mode("intelligence")
        call = phone.PhoneCall(object(), "cc-mode-greet")
        greeting = call.default_greeting
        await call.close()
        return greeting

    greeting = run(exercise())
    assert greeting == flow_mode_greeting("intelligence")


def test_mode_persists_and_reapplies_at_boot(clean_mode):
    phone.persist_runtime_setting("NANO_CLAW_VOICE_FLOW", "intelligence")
    saved = json.loads(clean_mode.read_text(encoding="utf-8"))
    assert saved["NANO_CLAW_VOICE_FLOW"] == "intelligence"

    # Fresh process: env says spacechannel, but the persisted choice wins.
    flow_session._flow_mode = None
    phone._overrides.clear()
    phone._load_persisted_overrides()
    assert flow_session.get_flow_mode() == "intelligence"


def test_invalid_mode_is_never_persisted(clean_mode):
    phone.persist_runtime_setting("NANO_CLAW_VOICE_FLOW", "bogus")
    assert not clean_mode.exists()
    assert phone._valid_setting("NANO_CLAW_VOICE_FLOW", "intelligence")
    assert not phone._valid_setting("NANO_CLAW_VOICE_FLOW", "bogus")
