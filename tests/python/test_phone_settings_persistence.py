"""Console phone settings survive restarts via a data-volume JSON file.

The web console's /api/phone/config and /api/phone/vad overrides used to be
in-memory only, so every watchdog container restart silently reverted the
line to .env defaults. Writes now persist to NANO_CLAW_PHONE_SETTINGS_PATH
and reload at boot; a corrupt or hostile file must never brick call handling.
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice import phone


@pytest.fixture(autouse=True)
def phone_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NANO_CLAW_PHONE", "1")
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    monkeypatch.setenv("NANO_CLAW_PHONE_WEBHOOK_BASE", "https://nano.example.com")
    monkeypatch.setenv("NANO_CLAW_PHONE_TOKEN", "sekrit")
    monkeypatch.setenv("NANO_CLAW_PHONE_VAD", "energy")
    settings_path = tmp_path / "phone-settings.json"
    monkeypatch.setenv("NANO_CLAW_PHONE_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(phone, "_vad_mode", None)
    phone._overrides.clear()
    yield settings_path
    phone._overrides.clear()


def run(coro):
    return asyncio.run(coro)


def _app():
    app = web.Application()
    phone.register_phone_routes(app)
    return app


def test_config_post_writes_settings_file(phone_env):
    async def exercise():
        client = TestClient(TestServer(_app()))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/phone/config",
                json={
                    "voice": "lux_george",
                    "model": "ollama/gemma4:e2b",
                    "speed": 1.2,
                    "stt_size": "medium",
                },
            )
            assert resp.status == 200
        finally:
            await client.close()

    run(exercise())
    saved = json.loads(phone_env.read_text(encoding="utf-8"))
    assert saved["NANO_CLAW_PHONE_VOICE"] == "lux_george"
    assert saved["NANO_CLAW_PHONE_MODEL"] == "ollama/gemma4:e2b"
    assert saved["NANO_CLAW_PHONE_SPEED"] == "1.2"
    assert saved["NANO_CLAW_PHONE_STT_SIZE"] == "medium"


def test_boot_reload_restores_persisted_overrides(phone_env):
    phone_env.write_text(
        json.dumps(
            {
                "NANO_CLAW_PHONE_VOICE": "lux_george",
                "NANO_CLAW_PHONE_SPEED": "1.5",
            }
        ),
        encoding="utf-8",
    )
    _app()  # register_phone_routes loads persisted settings
    assert phone._cfg("NANO_CLAW_PHONE_VOICE") == "lux_george"
    assert phone._cfg("NANO_CLAW_PHONE_SPEED") == "1.5"


def test_unknown_keys_and_invalid_values_are_dropped(phone_env):
    phone_env.write_text(
        json.dumps(
            {
                "NANO_CLAW_PHONE_VOICE": "not_a_real_voice",
                "NANO_CLAW_PHONE_SPEED": "99",
                "NANO_CLAW_PHONE_STT_SIZE": "medium",
                "TOTALLY_UNKNOWN_KEY": "x",
                "NANO_CLAW_PHONE_TOKEN": "evil-override",
            }
        ),
        encoding="utf-8",
    )
    phone._load_persisted_overrides()
    assert phone._overrides == {"NANO_CLAW_PHONE_STT_SIZE": "medium"}


def test_corrupt_settings_file_never_raises(phone_env):
    phone_env.write_text("{ definitely not json", encoding="utf-8")
    phone._load_persisted_overrides()
    assert phone._overrides == {}
    phone_env.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    phone._load_persisted_overrides()
    assert phone._overrides == {}


def test_clearing_model_persists_its_absence(phone_env):
    async def exercise():
        client = TestClient(TestServer(_app()))
        await client.start_server()
        try:
            await client.post("/api/phone/config", json={"model": "ollama/x"})
            await client.post("/api/phone/config", json={"model": ""})
        finally:
            await client.close()

    run(exercise())
    saved = json.loads(phone_env.read_text(encoding="utf-8"))
    assert "NANO_CLAW_PHONE_MODEL" not in saved
    phone._overrides.clear()
    phone._load_persisted_overrides()
    assert "NANO_CLAW_PHONE_MODEL" not in phone._overrides


def test_vad_choice_persists_and_reloads(phone_env, monkeypatch):
    async def exercise():
        client = TestClient(TestServer(_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/phone/vad", json={"mode": "energy"})
            assert resp.status == 200
        finally:
            await client.close()

    run(exercise())
    saved = json.loads(phone_env.read_text(encoding="utf-8"))
    assert saved["NANO_CLAW_PHONE_VAD"] == "energy"

    # A fresh process resolves the persisted mode through _cfg.
    phone._overrides.clear()
    monkeypatch.setattr(phone, "_vad_mode", None)
    phone._load_persisted_overrides()
    assert phone.get_vad_mode() == "energy"


def test_settings_file_only_ever_contains_known_keys(phone_env):
    phone._overrides["NANO_CLAW_PHONE_VOICE"] = "lux_george"
    phone._overrides["NOT_A_SETTING"] = "leak"
    phone._persist_overrides()
    saved = json.loads(phone_env.read_text(encoding="utf-8"))
    assert set(saved) == {"NANO_CLAW_PHONE_VOICE"}
