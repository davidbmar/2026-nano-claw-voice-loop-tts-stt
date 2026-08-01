import asyncio
import concurrent.futures
import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

_spec = importlib.util.spec_from_file_location(
    "stt_stream_server",
    Path(__file__).resolve().parents[2] / "stt-service" / "server.py",
)
stt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stt
_spec.loader.exec_module(stt)


def _hypothesis(*items):
    tokens = tuple(
        stt.HypothesisToken(text, start, end)
        for text, start, end in items
    )
    return stt.DecodeHypothesis(" ".join(item[0] for item in items), tokens)


def _pcm(ms: int, rate: int = 1000) -> bytes:
    return np.ones(rate * ms // 1000, dtype=np.int16).tobytes()


def test_local_agreement_commits_trims_prompts_and_does_not_repeat():
    scripted = iter(
        [
            _hypothesis(("hello", 0.0, 0.3), ("world", 0.3, 0.6)),
            _hypothesis(
                ("hello", 0.0, 0.3),
                ("world", 0.3, 0.6),
                ("today", 0.6, 1.0),
            ),
            # Some Whisper backends echo initial_prompt.  It must not appear
            # twice in the final text.
            _hypothesis(
                ("hello", 0.0, 0.1),
                ("world", 0.1, 0.2),
                ("today", 0.2, 0.4),
                ("friend", 0.4, 0.7),
            ),
            _hypothesis(("friend", 0.0, 0.3)),
        ]
    )
    calls = []

    def decode(samples, initial_prompt):
        calls.append((len(samples), initial_prompt))
        return next(scripted)

    session = stt.LocalAgreementSession(
        "scripted", "small", decode, pass_ms=700
    )

    first = session.feed(_pcm(700), 1000)
    assert first["committed_text"] == ""
    second = session.feed(_pcm(700), 1000)
    assert second["committed_text"] == "hello world"
    # Two 700 ms feeds minus the agreed prefix ending at 600 ms.
    assert session.window_ms == pytest.approx(800.0)

    third = session.feed(_pcm(700), 1000)
    assert third["committed_text"] == "hello world today"
    assert calls[2][1] == "hello world"

    # A short post-pass tail requires a final decode; with no new samples,
    # finish reuses the newest hypothesis at zero extra model cost.
    session.feed(_pcm(100), 1000)
    result = session.finish()
    assert result["text"] == "hello world today friend"
    assert result["committed_chars"] == len("hello world today")
    assert calls[3][1] == "hello world today"
    assert result["pass_count"] == 4
    assert all(item["window_ms"] <= 1500 for response in (first, second, third)
               for item in response["passes"])


def test_registry_lifecycle_expiry_and_concurrent_guard(caplog):
    now = [10.0]

    def clock():
        return now[0]

    registry = stt.SessionRegistry(
        idle_seconds=60.0,
        clock=clock,
        decoder_factory=lambda _size: (lambda _audio, _prompt: ""),
    )
    first = registry.create("small")
    registry.create("base")
    with caplog.at_level(logging.WARNING, logger="stt-service"):
        third = registry.create("tiny")
    assert len(registry) == 3
    assert registry.get(first.session_id) is first
    assert "3 concurrent sessions" in caplog.text

    registry.pop(third.session_id)
    assert len(registry) == 2
    now[0] += 61.0
    expired = registry.expire_idle()
    assert len(expired) == 2
    assert len(registry) == 0
    with pytest.raises(KeyError):
        registry.get(first.session_id)


def test_same_session_never_decodes_concurrently():
    active = 0
    maximum = 0
    state_lock = threading.Lock()

    def decode(_audio, _prompt):
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return "hello"

    session = stt.LocalAgreementSession("serial", "small", decode)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(session.feed, _pcm(700), 1000)
            for _ in range(2)
        ]
        for future in futures:
            future.result()

    assert maximum == 1
    assert session.pass_count == 2


def test_session_api_start_feed_finish_and_removal(monkeypatch):
    calls = []

    def factory(size):
        assert size == "small"

        def decode(_audio, prompt):
            calls.append(prompt)
            return "hello there"

        return decode

    registry = stt.SessionRegistry(decoder_factory=factory)
    monkeypatch.setattr(stt, "_sessions", registry)

    async def exercise():
        transport = httpx.ASGITransport(app=stt.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://stt.test"
        ) as client:
            started = await client.post(
                "/stream/start", headers={"X-Model-Size": "small"}
            )
            assert started.status_code == 200
            session_id = started.json()["session_id"]
            fed = await client.post(
                f"/stream/{session_id}/feed",
                content=_pcm(800),
                headers={"X-Sample-Rate": "1000"},
            )
            assert fed.status_code == 200
            assert fed.json()["pass_count"] == 1
            await client.post(
                f"/stream/{session_id}/feed",
                content=_pcm(100),
                headers={"X-Sample-Rate": "1000"},
            )
            finished = await client.post(f"/stream/{session_id}/finish")
            assert finished.status_code == 200
            assert finished.json()["text"] == "hello there"
            missing = await client.post(f"/stream/{session_id}/finish")
            assert missing.status_code == 404

    asyncio.run(exercise())
    assert calls == ["", ""]


def test_kept_finish_can_resume_without_redecoding_committed_audio():
    prompts = []
    scripted = iter(["tell me about", "Mars"])

    def decode(_audio, prompt):
        prompts.append(prompt)
        return next(scripted)

    session = stt.LocalAgreementSession("continued", "small", decode)
    session.feed(_pcm(500), 1000)
    first = session.finish()
    assert first["text"] == "tell me about"
    assert session.window_ms == 0

    session.feed(_pcm(500), 1000)
    second = session.finish()
    assert second["text"] == "tell me about Mars"
    assert prompts == ["", "tell me about"]


def test_finish_reuses_hypothesis_when_last_feed_already_decoded_endpoint():
    calls = []

    def decode(_audio, prompt):
        calls.append(prompt)
        return "already complete"

    session = stt.LocalAgreementSession("covered", "small", decode)
    session.feed(_pcm(700), 1000)
    result = session.finish()

    assert result["text"] == "already complete"
    assert result["passes"] == []
    assert calls == [""]


def test_trailing_padding_estimate_is_instrumentable():
    voiced = np.full(800, 1000, dtype=np.int16)  # 100 ms @ 8 kHz
    silence = np.zeros(3200, dtype=np.int16)  # 400 ms
    audio = np.concatenate([voiced, silence]).tobytes()
    assert stt._trailing_padding_ms(audio, 8000) == 400
