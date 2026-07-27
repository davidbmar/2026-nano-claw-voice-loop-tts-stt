"""TTS synth cache: fixed lines (greetings, notices) synthesize once.

The 2026-07-26 incident class: every pickup re-synthesized the same fixed
greeting live, so a cold/degraded TTS became caller-audible dead air. The
cache stores ENGINE PCM keyed (normalized text, voice, speed) — pause
jitter is applied after the cache, so jittered pauses still hit. Fallback
renders (engine down → piper) are never cached, so a boot-time warm-up
while a service is still starting cannot poison a real voice's cache.
"""

import numpy as np
import pytest

from voice import tts


@pytest.fixture(autouse=True)
def clean_cache():
    tts.clear_synth_cache()
    yield
    tts.clear_synth_cache()


def _pcm(ms=200):
    return np.zeros(48_000 * ms // 1000, dtype=np.int16).tobytes()


def test_repeat_synthesis_hits_cache(monkeypatch):
    calls = []

    def fake_lux(text, voice_id, speed):
        calls.append(text)
        return _pcm()

    monkeypatch.setattr(tts, "_synthesize_lux_engine", fake_lux)
    first = tts.synthesize("Hello there.", "lux_george", 1.0, pause_after_ms=600)
    second = tts.synthesize("Hello there.", "lux_george", 1.0, pause_after_ms=140)
    assert calls == ["Hello there."]  # engine ran once
    assert tts.is_cached("Hello there.", "lux_george", 1.0)
    # Different pause targets still produce different gap lengths.
    assert len(first) != len(second)


def test_distinct_voice_or_speed_misses(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tts, "_synthesize_lux_engine", lambda t, v, s: calls.append((v, s)) or _pcm()
    )
    tts.synthesize("Hi.", "lux_george", 1.0)
    tts.synthesize("Hi.", "lux_heart", 1.0)
    tts.synthesize("Hi.", "lux_george", 1.2)
    assert len(calls) == 3


def test_fallback_render_is_never_cached(monkeypatch):
    from voice import lux_client

    attempts = []

    def failing_lux(text, voice_id, speed):
        attempts.append(text)
        raise lux_client.LuxUnavailable("service starting")

    monkeypatch.setattr(tts, "_synthesize_lux_engine", failing_lux)
    monkeypatch.setattr(tts, "_synthesize_piper", lambda t, v: _pcm(50))
    out = tts.synthesize("Hello there.", "lux_george", 1.0)
    assert out  # degraded but audible
    assert not tts.is_cached("Hello there.", "lux_george", 1.0)
    # Engine recovered: the next call tries the real engine again and caches.
    monkeypatch.setattr(tts, "_synthesize_lux_engine", lambda t, v, s: _pcm())
    tts.synthesize("Hello there.", "lux_george", 1.0)
    assert tts.is_cached("Hello there.", "lux_george", 1.0)
    assert attempts == ["Hello there."]


def test_cache_is_bounded_lru(monkeypatch):
    monkeypatch.setattr(tts, "_SYNTH_CACHE_CAP", 3)
    monkeypatch.setattr(tts, "_synthesize_lux_engine", lambda t, v, s: _pcm(20))
    for i in range(5):
        tts.synthesize(f"Line {i}.", "lux_george", 1.0)
    assert not tts.is_cached("Line 0.", "lux_george", 1.0)
    assert tts.is_cached("Line 4.", "lux_george", 1.0)
    # Touching an entry refreshes recency.
    tts.synthesize("Line 2.", "lux_george", 1.0)
    tts.synthesize("Line 5.", "lux_george", 1.0)
    assert tts.is_cached("Line 2.", "lux_george", 1.0)


def test_boot_warm_task_fills_greeting_cache(monkeypatch):
    import asyncio

    from voice import phone

    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "lux_george")
    warmed = []
    monkeypatch.setattr(
        phone, "tts_synthesize", lambda text, voice, speed: warmed.append(text)
    )
    monkeypatch.setattr(
        phone, "tts_is_cached", lambda text, voice, speed: text in warmed
    )

    async def exercise():
        app = {}
        await phone._warm_greeting_cache(app)
        await app["phone_greeting_warm_task"]

    asyncio.run(exercise())
    assert warmed, "no greeting units were synthesized"
    joined = " ".join(warmed)
    assert "Space Channel" in joined or "recorded" in joined
