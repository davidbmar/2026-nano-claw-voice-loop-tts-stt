import asyncio
import json
import logging
from types import SimpleNamespace

from scripts.scheduling_eval import run_eval
from voice import phone, server
from voice.flow_session import (
    FlowReply,
    FlowSession,
    SCHEDULER_GREETING,
    scheduler_region_config,
)
from voice.goal_region import RegionTurn


def run(coro):
    return asyncio.run(coro)


class StaticRunner:
    def __init__(self, turn):
        self.result = turn
        self.caller_texts = []

    def turn(self, caller_text):
        self.caller_texts.append(caller_text)
        return self.result


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class FakeBrowserSession:
    def __init__(self):
        self.model = ""
        self.voice_id = "test-voice"
        self.speed = 1.0
        self.spoken = []
        self._turn = {}

    async def speak_text(self, text, voice_id, speed):
        self.spoken.append(text)
        return None


class FakeResponse:
    headers = {"content-type": "application/json"}

    async def aread(self):
        return json.dumps({"type": "final", "response": "normal API reply"}).encode()


class FakeStreamContext:
    async def __aenter__(self):
        return FakeResponse()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeStreamContext()


def test_booked_terminal_is_speech_friendly():
    slots = {
        "job": "water heater repair",
        "slot_start": "2026-07-20T10:00:00",
        "duration_minutes": 60,
    }
    session = FlowSession(
        StaticRunner(
            RegionTurn(
                reply="",
                exit="booked",
                slots=slots,
                supervisor_ms=5.0,
                rejected=[],
            )
        )
    )

    reply = run(session.reply("Monday at ten works"))

    assert reply == FlowReply(
        text=(
            "You're booked: water heater repair on Monday July twentieth at "
            "10 AM for 60 minutes. See you then. Goodbye!"
        ),
        done=True,
        outcome="booked",
        slots=slots,
    )
    assert "2026-07-20" not in reply.text


def test_run_eval_uses_shared_scheduler_config_without_network():
    config = scheduler_region_config("availability digest")
    assert config.max_turns == 12
    assert config.deadline_s == 600
    assert run_eval.scheduler_region_config is scheduler_region_config
    assert run_eval.GREETING == SCHEDULER_GREETING

    class FakeMessages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text="human, please")])

    availability = {
        "timezone": "America/Chicago",
        "days": {
            "2026-07-20": [
                {"start": "2026-07-20T10:00:00", "end": "2026-07-20T11:00:00"}
            ]
        },
    }
    scenario = {
        "id": "offline-smoke",
        "name": "Offline smoke",
        "brief": "Ask for a human",
        "duration_minutes": 30,
        "expected_outcome": "escape",
        "expected_exit": "escape",
    }

    result = run_eval.run_scenario(
        SimpleNamespace(messages=FakeMessages()), availability, scenario
    )

    assert result["passed"] is True
    assert result["exit"] == "escape"


def test_phone_flag_off_keeps_normal_defaults(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_VOICE_FLOW", "other-flow")
    monkeypatch.setattr(phone, "get_vad_mode", lambda: "energy")

    class UnexpectedFlowSession:
        @classmethod
        def create(cls):
            raise AssertionError("flow must not be created when the flag is off")

    monkeypatch.setattr(phone, "FlowSession", UnexpectedFlowSession)

    async def exercise():
        call = phone.PhoneCall(object(), "flag-off")
        try:
            assert call.flow is None
            assert call.default_greeting == phone.DEFAULT_GREETING
        finally:
            await call.close()

    run(exercise())


def test_phone_missing_availability_falls_back_and_logs(
    monkeypatch, tmp_path, caplog
):
    missing = tmp_path / "missing-availability.json"
    monkeypatch.setenv("NANO_CLAW_VOICE_FLOW", "scheduler")
    monkeypatch.setenv("NANO_CLAW_FLOW_AVAILABILITY", str(missing))
    monkeypatch.setattr(phone, "get_vad_mode", lambda: "energy")
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)
    caplog.set_level(logging.ERROR, logger="nano-claw.flow")

    async def exercise():
        call = phone.PhoneCall(object(), "missing-file")
        streamed = []

        async def transcribe(_pcm):
            return "what launches are next"

        async def stream_reply(text):
            streamed.append(text)

        call._transcribe = transcribe
        call._stream_reply = stream_reply
        try:
            assert call.flow is None
            await call._run_turn(b"audio")
            assert streamed == ["what launches are next"]
        finally:
            await call.close()

    run(exercise())
    assert "Scheduler flow unavailable; cannot load" in caplog.text
    assert str(missing) in caplog.text


def test_phone_booked_reply_is_spoken_then_hung_up(monkeypatch):
    terminal = FlowReply(
        text="You're booked. Goodbye!",
        done=True,
        outcome="booked",
        slots={"job": "leak repair"},
    )
    events = []

    class FakeFlow:
        greeting = SCHEDULER_GREETING

        async def reply(self, text):
            events.append(("flow", text))
            return terminal

    flow = FakeFlow()

    class FakeFlowSession:
        @classmethod
        def create(cls):
            return flow

    async def fake_telnyx_cmd(client, call_id, command, payload):
        events.append(("telnyx", call_id, command, payload))
        return True

    monkeypatch.setenv("NANO_CLAW_VOICE_FLOW", "scheduler")
    monkeypatch.setattr(phone, "FlowSession", FakeFlowSession)
    monkeypatch.setattr(phone, "get_vad_mode", lambda: "energy")
    monkeypatch.setattr(phone, "_telnyx_cmd", fake_telnyx_cmd)
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)

    async def exercise():
        call = phone.PhoneCall(object(), "booked-call")

        async def transcribe(_pcm):
            return "yes, book it"

        async def speak(text):
            events.append(("speak", text))

        async def unexpected_stream(_text):
            raise AssertionError("booked flow must not route to /api/chat")

        call._transcribe = transcribe
        call.speak = speak
        call._stream_reply = unexpected_stream
        try:
            assert call.default_greeting == SCHEDULER_GREETING
            await call._run_turn(b"audio")
            assert call.closed is True
        finally:
            await call.close()

    run(exercise())
    assert events == [
        ("flow", "yes, book it"),
        ("speak", terminal.text),
        ("telnyx", "booked-call", "hangup", {}),
    ]


def test_browser_flag_off_routes_to_normal_api(monkeypatch):
    monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)

    class UnexpectedFlowSession:
        @classmethod
        def create(cls):
            raise AssertionError("flow must not be created when the flag is off")

    monkeypatch.setattr(server, "FlowSession", UnexpectedFlowSession)
    ws = FakeWebSocket()
    session = FakeBrowserSession()
    client = FakeHttpClient()

    run(server._handle_agent_request(ws, session, client, "hello"))

    assert len(client.calls) == 1
    assert session.spoken == ["normal API reply"]
    assert {"type": "agent_reply", "text": "normal API reply"} in ws.messages


def test_browser_missing_availability_falls_back_and_logs(
    monkeypatch, tmp_path, caplog
):
    missing = tmp_path / "missing-availability.json"
    monkeypatch.setenv("NANO_CLAW_VOICE_FLOW", "scheduler")
    monkeypatch.setenv("NANO_CLAW_FLOW_AVAILABILITY", str(missing))
    monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
    caplog.set_level(logging.ERROR, logger="nano-claw.flow")
    ws = FakeWebSocket()
    session = FakeBrowserSession()
    session._scheduler_flow_enabled = server.scheduler_flow_enabled()
    session._scheduler_flow_attempted = False
    session._scheduler_flow = None
    client = FakeHttpClient()

    run(server._handle_agent_request(ws, session, client, "hello"))

    assert len(client.calls) == 1
    assert session.spoken == ["normal API reply"]
    assert session._scheduler_flow_enabled is False
    assert "Scheduler flow unavailable; cannot load" in caplog.text


def test_browser_flow_greets_speaks_and_reverts_to_normal_api(monkeypatch):
    terminal = FlowReply(
        text="You're booked. Goodbye!",
        done=True,
        outcome="booked",
        slots={"job": "drain repair"},
    )

    class FakeFlow:
        greeting = SCHEDULER_GREETING

        async def reply(self, text):
            assert text == "book Tuesday"
            return terminal

    flow = FakeFlow()

    class FakeFlowSession:
        @classmethod
        def create(cls):
            return flow

    monkeypatch.setattr(server, "FlowSession", FakeFlowSession)
    monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
    ws = FakeWebSocket()
    session = FakeBrowserSession()
    session._scheduler_flow_enabled = True
    session._scheduler_flow_attempted = False
    session._scheduler_flow = None
    client = FakeHttpClient()

    run(server._handle_agent_request(ws, session, client, "book Tuesday"))

    assert client.calls == []
    assert session.spoken == [SCHEDULER_GREETING, terminal.text]
    assert session._scheduler_flow is None
    assert session._scheduler_flow_enabled is False

    run(server._handle_agent_request(ws, session, client, "what launches are next"))

    assert len(client.calls) == 1
    assert session.spoken[-1] == "normal API reply"
