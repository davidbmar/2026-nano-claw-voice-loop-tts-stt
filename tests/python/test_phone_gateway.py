import asyncio
import base64
import json
import logging
from unittest.mock import Mock

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice import phone
from voice.phone_audio import FRAME_SAMPLES, ulaw_decode, ulaw_encode


@pytest.fixture(autouse=True)
def phone_env(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE", "1")
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    monkeypatch.setenv("NANO_CLAW_PHONE_WEBHOOK_BASE", "https://nano.example.com")
    monkeypatch.setenv("NANO_CLAW_PHONE_TOKEN", "sekrit")
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_IN", "0")
    monkeypatch.setenv("NANO_CLAW_PHONE_DYNAMIC_ENDPOINT", "0")
    monkeypatch.setenv("NANO_CLAW_PHONE_VAD", "energy")
    monkeypatch.delenv("NANO_CLAW_PHONE_CODEC", raising=False)
    monkeypatch.setattr(phone, "_vad_mode", None)
    phone._answered.clear()
    phone._overrides.clear()
    phone._active_calls.clear()


def make_app():
    app = web.Application()
    phone.register_phone_routes(app)
    return app


def run(coro):
    return asyncio.run(coro)


def initiated_event(cid="cc-123"):
    return {
        "data": {
            "event_type": "call.initiated",
            "payload": {"call_control_id": cid, "from": "+15550001111", "to": "+15123569101"},
        }
    }


def tone(freq_hz: float, ms: int, amp: int = 8000) -> np.ndarray:
    t = np.arange(8000 * ms // 1000) / 8000
    return (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)


def silence(ms: int) -> np.ndarray:
    return np.zeros(8000 * ms // 1000, dtype=np.int16)


def feed_pcm(call: phone.PhoneCall, pcm: np.ndarray) -> list[np.ndarray]:
    decoded = []
    for i in range(0, len(pcm), FRAME_SAMPLES):
        encoded = ulaw_encode(pcm[i : i + FRAME_SAMPLES])
        decoded_frame = ulaw_decode(encoded)
        decoded.append(decoded_frame)
        call.feed_media(base64.b64encode(encoded).decode())
    return decoded


class RecordingWebSocket:
    def __init__(self, *, closed=False, send_error=None):
        self.closed = closed
        self.send_error = send_error
        self.messages = []
        self.send_attempts = 0
        self.media_sent = asyncio.Event()
        self.clear_sent = asyncio.Event()

    async def send_json(self, message):
        self.send_attempts += 1
        if self.send_error:
            raise self.send_error
        self.messages.append(message)
        if message.get("event") == "media":
            self.media_sent.set()
        elif message == {"event": "clear"}:
            self.clear_sent.set()


def test_webhook_rejects_bad_token(monkeypatch):
    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/phone/incoming?token=wrong", json=initiated_event())
            assert resp.status == 403
            resp = await client.post("/api/phone/incoming", json=initiated_event())
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_call_initiated_answers_with_streaming(monkeypatch):
    commands = []

    async def fake_cmd(client, cid, command, payload):
        commands.append((cid, command, payload))
        return True

    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/phone/incoming?token=sekrit", json=initiated_event())
            assert resp.status == 200
            # Carrier retry of the same call must not answer twice.
            resp = await client.post("/api/phone/incoming?token=sekrit", json=initiated_event())
            assert (await resp.json()).get("dedup") is True
        finally:
            await client.close()

    run(_run())
    assert len(commands) == 1
    cid, command, payload = commands[0]
    assert (cid, command) == ("cc-123", "answer")
    assert payload["stream_url"] == "wss://nano.example.com/ws/phone-media?token=sekrit"
    assert payload["stream_bidirectional_codec"] == "PCMU"


def test_l16_call_answers_with_wideband_streaming(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_CODEC", "l16")
    commands = []

    async def fake_cmd(client, cid, command, payload):
        commands.append((cid, command, payload))
        return True

    monkeypatch.setattr(phone, "_telnyx_cmd", fake_cmd)

    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/phone/incoming?token=sekrit",
                json=initiated_event("cc-l16"),
            )
            assert resp.status == 200
        finally:
            await client.close()

    run(_run())
    assert len(commands) == 1
    cid, command, payload = commands[0]
    assert (cid, command) == ("cc-l16", "answer")
    assert payload["stream_codec"] == "L16"
    assert payload["stream_bidirectional_codec"] == "L16"
    assert payload["stream_bidirectional_sampling_rate"] == 16000


def test_l16_media_is_decoded_as_raw_pcm16(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_CODEC", "l16")

    async def _run():
        call = phone.PhoneCall(object(), "cc-l16-media")
        captured = []

        def capture_frame(frame, is_speech=None):
            captured.append(frame.copy())
            return None

        call.endpointer.feed = capture_frame
        source = np.arange(-160, 160, dtype=np.int16)
        try:
            call.feed_media(base64.b64encode(source.tobytes()).decode())
            assert call.endpointer.rate_hz == 16000
            assert len(captured) == 1
            assert len(captured[0]) == len(source)
            assert np.array_equal(captured[0], source)
        finally:
            await call.close()

    run(_run())


def test_media_ws_rejects_bad_token():
    async def _run():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        try:
            resp = await client.get("/ws/phone-media?token=wrong")
            assert resp.status == 403
        finally:
            await client.close()

    run(_run())


def test_idle_action_policy():
    # Under threshold: nothing, prompted or not
    assert phone.idle_action(10, False, 30) == ""
    assert phone.idle_action(29.9, True, 30) == ""
    # First stretch of silence: prompt once
    assert phone.idle_action(31, False, 30) == "prompt"
    # Prompted and the caller stayed silent another stretch: hang up
    assert phone.idle_action(31, True, 30) == "hangup"


def test_routes_not_registered_when_env_incomplete(monkeypatch):
    monkeypatch.delenv("TELNYX_API_KEY")
    app = make_app()
    paths = [r.resource.canonical for r in app.router.routes()]
    assert "/api/phone/incoming" not in paths


def test_audio_during_running_turn_replays_as_next_turn():
    async def _run():
        call = phone.PhoneCall(object(), "cc-buffered")
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        turns = []

        async def fake_turn(pcm):
            turns.append(pcm)
            if len(turns) == 1:
                await release_first.wait()
            else:
                second_started.set()
                await asyncio.Event().wait()

        call._run_turn = fake_turn
        try:
            call._start_turn(b"first turn")
            await asyncio.sleep(0)
            frames = feed_pcm(
                call, np.concatenate([tone(300, 300), silence(700)])
            )

            assert len(turns) == 1
            assert len(call._inbound_buffer) == len(frames)
            assert call.endpointer._frames == []

            release_first.set()
            await asyncio.wait_for(second_started.wait(), timeout=1)

            assert turns[0] == b"first turn"
            assert turns[1] == b"".join(frame.tobytes() for frame in frames)
            assert not call._inbound_buffer
        finally:
            await call.close()
            await asyncio.sleep(0)

    run(_run())


def test_tail_prime_merges_buffered_continuation(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_DYNAMIC_ENDPOINT", "1")
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)

    async def _run():
        call = phone.PhoneCall(object(), "cc-tail")
        first_transcribing = asyncio.Event()
        release_first = asyncio.Event()
        second_transcribing = asyncio.Event()
        transcribed = []

        async def fake_transcribe(pcm):
            transcribed.append(pcm)
            if len(transcribed) == 1:
                first_transcribing.set()
                await release_first.wait()
                return "tell me about"
            second_transcribing.set()
            return "Mars"

        async def fake_stream_reply(text):
            return None

        call._transcribe = fake_transcribe
        call._stream_reply = fake_stream_reply
        initial = np.concatenate([tone(300, 300), silence(450)]).tobytes()
        try:
            call._start_turn(initial)
            await asyncio.wait_for(first_transcribing.wait(), timeout=1)
            continuation = feed_pcm(
                call, np.concatenate([tone(500, 300), silence(450)])
            )

            release_first.set()
            await asyncio.wait_for(second_transcribing.wait(), timeout=1)

            expected = initial + b"".join(frame.tobytes() for frame in continuation)
            assert transcribed == [initial, expected]
        finally:
            await call.close()
            await asyncio.sleep(0)

    run(_run())


def test_audio_while_speaking_without_barge_in_is_dropped():
    async def _run():
        call = phone.PhoneCall(object(), "cc-speaking")
        try:
            call.speaking = True
            feed_pcm(call, np.concatenate([tone(300, 300), silence(700)]))

            assert not call._inbound_buffer
            assert call.endpointer._frames == []
            assert call.endpointer._preroll == []
        finally:
            call.speaking = False
            await call.close()

    run(_run())


def test_barge_in_clears_buffer_once_and_stops_playback(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_IN", "1")
    pcm48k = np.full(48_000 * 2, 2_000, dtype=np.int16).tobytes()
    monkeypatch.setattr(phone, "tts_synthesize", lambda *args: pcm48k)

    async def _run():
        ws = RecordingWebSocket()
        call = phone.PhoneCall(ws, "cc-barge-clear")
        tap = Mock()
        call.tap = tap
        try:
            call.speaking = True
            playback = asyncio.create_task(call._speak_chunk("long answer"))
            await asyncio.wait_for(ws.media_sent.wait(), timeout=1)

            feed_pcm(call, tone(300, 240))
            await asyncio.wait_for(ws.clear_sent.wait(), timeout=1)
            await playback
            await call._flush_playback()  # one-shot even if requested again

            media = [message for message in ws.messages if message["event"] == "media"]
            clears = [message for message in ws.messages if message == {"event": "clear"}]
            assert 0 < len(media) < 100
            assert clears == [{"event": "clear"}]
            assert call.speaking is False
            assert sum(
                event.args == ("clear_sent",) for event in tap.event.call_args_list
            ) == 1
        finally:
            call.speaking = False
            await call.close()

        closed_ws = RecordingWebSocket(closed=True)
        closed_call = phone.PhoneCall(closed_ws, "cc-barge-closed")
        try:
            closed_call.speaking = True
            closed_call._interrupt()
            await asyncio.sleep(0)
        finally:
            closed_call.speaking = False
            await closed_call.close()
        assert closed_ws.send_attempts == 0

    run(_run())


def test_close_while_speaking_clears_buffer_once():
    async def _run():
        ws = RecordingWebSocket()
        call = phone.PhoneCall(ws, "cc-hangup-clear")
        call.speaking = True

        await call.close()
        await call.close()

        assert ws.messages == [{"event": "clear"}]
        assert call.speaking is False

    run(_run())


def test_clear_send_failure_does_not_escape_hangup():
    async def _run():
        ws = RecordingWebSocket(send_error=RuntimeError("media socket failed"))
        call = phone.PhoneCall(ws, "cc-clear-error")
        call.speaking = True

        await call.close()

        assert ws.send_attempts == 1
        assert call.closed is True

    run(_run())


def test_inbound_buffer_cap_trims_oldest(monkeypatch, caplog):
    monkeypatch.setattr(phone, "MAX_BUFFERED_INBOUND_FRAMES", 3)
    caplog.set_level(logging.WARNING, logger="nano-claw.phone")

    async def _run():
        call = phone.PhoneCall(object(), "cc-cap")
        keep_running = asyncio.Event()

        async def fake_turn(pcm):
            await keep_running.wait()

        call._run_turn = fake_turn
        try:
            call._start_turn(b"first turn")
            await asyncio.sleep(0)
            decoded = []
            for amplitude in (1000, 2000, 3000, 4000):
                decoded.extend(
                    feed_pcm(
                        call,
                        np.full(FRAME_SAMPLES, amplitude, dtype=np.int16),
                    )
                )

            assert len(call._inbound_buffer) == 3
            assert np.array_equal(call._inbound_buffer[0][0], decoded[1])
            assert np.array_equal(call._inbound_buffer[-1][0], decoded[-1])
            assert "inbound buffer capped at 3 frames" in caplog.text
        finally:
            await call.close()
            await asyncio.sleep(0)

    run(_run())


# ── /api/phone/config — live overrides from the web UI ───────────────────


def _config_roundtrip(method, path="/api/phone/config", payload=None):
    async def go():
        client = TestClient(TestServer(make_app()))
        await client.start_server()
        if method == "get":
            resp = await client.get(path)
        else:
            resp = await client.post(path, json=payload)
        body = await resp.json() if resp.status == 200 else None
        await client.close()
        return resp.status, body

    return run(go())


def test_phone_config_get_reflects_env(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "bm_george")
    status, body = _config_roundtrip("get")
    assert status == 200
    assert body["voice"] == "bm_george"
    assert body["model"] == ""  # server default
    assert body["speed"] == 1.0
    assert body["active_calls"] == 0
    assert body["speech_mode"] == "prepared"
    assert body["speech_version"] == "nanoclaw-speech-v1"


def test_phone_config_set_overrides_env_live(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "bm_george")
    status, body = _config_roundtrip(
        "post",
        payload={"voice": "lux_george", "model": "gemini/gemini-flash-latest", "speed": 1.3},
    )
    assert status == 200
    assert body["voice"] == "lux_george"
    assert body["model"] == "gemini/gemini-flash-latest"
    assert body["speed"] == 1.3
    # The override wins over the environment — this is what makes changes
    # apply to a call already in progress.
    assert phone._cfg("NANO_CLAW_PHONE_VOICE") == "lux_george"


def test_phone_config_rejects_unknown_voice_and_bad_speed():
    s1, _ = _config_roundtrip("post", payload={"voice": "not-a-voice"})
    s2, _ = _config_roundtrip("post", payload={"speed": 9})
    assert (s1, s2) == (400, 400)
    assert "NANO_CLAW_PHONE_VOICE" not in phone._overrides


def test_phone_config_clearing_model_returns_to_server_default():
    phone._overrides["NANO_CLAW_PHONE_MODEL"] = "some/model"
    status, body = _config_roundtrip("post", payload={"model": ""})
    assert status == 200
    assert body["model"] == ""
    assert "NANO_CLAW_PHONE_MODEL" not in phone._overrides


def test_phone_config_stt_size_validated_and_live(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_STT_SIZE", "base")
    status, body = _config_roundtrip("post", payload={"stt_size": "small"})
    assert status == 200
    assert body["stt_size"] == "small"
    assert phone._cfg("NANO_CLAW_PHONE_STT_SIZE") == "small"
    s_bad, _ = _config_roundtrip("post", payload={"stt_size": "gigantic"})
    assert s_bad == 400


def test_flow_switches_mid_call(monkeypatch):
    class FakeFlow:
        greeting = "hi"

    async def _run():
        call = phone.PhoneCall(object(), "cc-flow")
        try:
            assert call.flow is None  # started with flow off

            # UI flips to scheduler mid-call → next turn joins the flow
            monkeypatch.setattr(phone, "get_flow_mode", lambda: "scheduler")
            monkeypatch.setattr(phone.FlowSession, "create", classmethod(lambda cls, **kw: FakeFlow()))
            call._sync_flow_mode()
            assert isinstance(call.flow, FakeFlow)

            # UI flips back to off → next turn returns to persona chat
            monkeypatch.setattr(phone, "get_flow_mode", lambda: "off")
            call._sync_flow_mode()
            assert call.flow is None
        finally:
            await call.close()
            await asyncio.sleep(0)

    run(_run())


def test_flow_create_failure_falls_back_and_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def _create(cls, **kw):
        calls["n"] += 1
        return None

    async def _run():
        call = phone.PhoneCall(object(), "cc-flow-fail")
        try:
            monkeypatch.setattr(phone, "get_flow_mode", lambda: "scheduler")
            monkeypatch.setattr(phone.FlowSession, "create", classmethod(_create))
            call._sync_flow_mode()
            call._sync_flow_mode()
            assert call.flow is None
            assert calls["n"] == 1  # no retry spam after a failed create
        finally:
            await call.close()
            await asyncio.sleep(0)

    run(_run())


def test_phone_session_id_is_valid_for_the_agent_api():
    # Telnyx call ids carry a "v3:" prefix; the colon must not leak into the
    # session id or the agent API rejects it with 400 and the caller hears only
    # the fallback line. The id must match ^[A-Za-z0-9_-]{1,64}$.
    import re as _re

    async def _run():
        call = phone.PhoneCall(object(), "v3:LzPQWbMd0r-Xp-CcMbKxk9CrBFyosMFapVcnD8GG")
        try:
            assert _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", call.session_id), call.session_id
            assert ":" not in call.session_id
        finally:
            await call.close()

    run(_run())


def test_phone_chat_payload_carries_selected_mode_profile(monkeypatch):
    # The console MODE selector sets the shared flow mode. The phone must send
    # the matching profile per turn, or a switch to riff/nano-claw/intelligence
    # is ignored and the agent answers with the default Space Channel persona.
    from contextlib import asynccontextmanager
    from voice.flow_session import set_flow_mode, get_flow_profile

    captured = {}

    class FakeResp:
        headers = {"content-type": "application/json"}
        async def aread(self):
            return b'{"response": "ok"}'

    class FakeHttp:
        @asynccontextmanager
        async def stream(self, method, url, json, headers):
            captured["payload"] = json
            yield FakeResp()

    async def _run(mode, expected_profile):
        call = phone.PhoneCall.__new__(phone.PhoneCall)
        call.session_id = "phone-test"
        call.tap = None
        call._http = FakeHttp()
        # Minimal attributes _stream_reply touches before the reply returns.
        call.barge = type("B", (), {"reset": lambda self: None})()
        call.speaking = False
        call.interrupted = False
        call._playback_flush_sent = False
        call.endpointer = type("E", (), {"reset": lambda self: None})()
        assert set_flow_mode(mode) is True

        async def fake_speak_sentences(units):
            captured["spoke"] = True

        call._speak_sentences = fake_speak_sentences
        # We only need the outbound payload; the rest of _stream_reply touches
        # far more call state than this unit builds, so ignore any later error.
        try:
            await call._stream_reply("hello")
        except Exception:
            pass
        assert captured["payload"]["profile"] == expected_profile

    run(_run("riff", get_flow_profile("riff")))
    run(_run("intelligence", get_flow_profile("intelligence")))
    set_flow_mode("spacechannel")


def test_phone_config_toggles_speech_mode_live(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_PHONE_SPEECH_PREPARATION", raising=False)
    monkeypatch.delenv("NANO_CLAW_SPEECH_PREPARATION", raising=False)
    # Default is prepared.
    _, body = _config_roundtrip("get")
    assert body["speech_mode"] == "prepared"
    # Flip to raw (whole sentences) live.
    status, body = _config_roundtrip("post", payload={"speech_mode": "raw"})
    assert status == 200
    assert body["speech_mode"] == "raw"
    # And back.
    _, body = _config_roundtrip("post", payload={"speech_mode": "prepared"})
    assert body["speech_mode"] == "prepared"
    # Reject nonsense.
    status, _ = _config_roundtrip("post", payload={"speech_mode": "bogus"})
    assert status == 400
