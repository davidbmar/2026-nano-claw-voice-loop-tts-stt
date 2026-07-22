import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from scripts.scheduling_eval import run_eval
from voice import flow_session, phone, server
from voice.flow_session import (
    DEFAULT_FLOW_MODE,
    DEFAULT_REGION_MODEL,
    FLOW_MODES,
    REGION_MODELS,
    FlowReply,
    FlowSession,
    SCHEDULER_GREETING,
    get_flow_mode,
    get_flow_profile,
    get_region_model,
    scheduler_region_config,
    set_flow_mode,
    set_region_model,
)
from voice.goal_region import RegionTurn


@pytest.fixture(autouse=True)
def reset_flow_mode(monkeypatch):
    monkeypatch.setattr(flow_session, "_flow_mode", None)
    monkeypatch.setattr(flow_session, "_region_model", None)


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
        supervisor_ms=5.0,
    )
    assert "2026-07-20" not in reply.text


def test_run_eval_uses_shared_scheduler_config_without_network():
    config = scheduler_region_config("availability digest")
    assert config.max_turns == 12
    assert config.deadline_s == 600
    assert (
        "Keep every reply to one or two short spoken sentences; offer at most "
        "two candidate times per turn."
    ) in config.persona
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


def test_eval_score_accepts_no_booking_caps_but_keeps_escape_strict():
    no_booking = {
        "duration_minutes": 240,
        "expected_outcome": "no_booking",
        "expected_exit": "budget",
    }
    for raw_exit in ("caller_cap", "caller_gave_up"):
        score = run_eval._score(no_booking, "no_booking", raw_exit, {})
        assert score["expected_match"] is True
        assert score["exit_match"] is True
        assert score["passed"] is True

    expected_escape = {
        "duration_minutes": 60,
        "expected_outcome": "escape",
        "expected_exit": "escape",
    }
    score = run_eval._score(
        expected_escape, "no_booking", "caller_cap", {}
    )
    assert score["expected_match"] is False
    assert score["exit_match"] is False
    assert score["passed"] is False


def test_run_eval_empty_caller_retries_then_scores_give_up():
    class EmptyMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(content=[SimpleNamespace(text="")])

    messages = EmptyMessages()
    availability = {
        "timezone": "America/Chicago",
        "days": {},
    }
    scenario = {
        "id": "empty-caller",
        "name": "Empty caller",
        "brief": "End the call",
        "duration_minutes": 30,
        "expected_outcome": "no_booking",
    }

    result = run_eval.run_scenario(
        SimpleNamespace(messages=messages), availability, scenario
    )

    assert messages.calls == 2
    assert result["outcome"] == "no_booking"
    assert result["exit"] == "caller_gave_up"
    assert result["caller_gave_up"] is True
    assert result["passed"] is True


def test_eval_scenarios_resolve_against_shifted_fixture_week(
    monkeypatch, tmp_path
):
    raw_scenarios = json.loads(run_eval.SCENARIOS_PATH.read_text())
    scenarios = run_eval._resolve_scenarios(raw_scenarios, "2030-02-04")
    by_id = {scenario["id"]: scenario for scenario in scenarios}

    friday_only = by_id["four-hour-friday-impossible"]
    assert friday_only["name"] == "4h, Monday only"
    assert friday_only["required_date"] == "2030-02-04"
    assert "Monday February 4" in friday_only["brief"]

    changed = by_id["change-of-mind"]
    assert changed["required_date"] == "2030-02-09"
    assert "Friday" in changed["brief"]
    assert "Saturday February 9" in changed["brief"]

    truth_path = tmp_path / "ground_truth.json"
    truth_path.write_text(json.dumps({
        "week_start": "2030-02-04",
        "days": {
            "2030-02-09": {
                "free_windows": [{
                    "start": "2030-02-09T10:00:00",
                    "end": "2030-02-09T11:00:00",
                }],
            },
        },
    }))
    monkeypatch.setattr(run_eval, "GROUND_TRUTH_PATH", truth_path)

    score = run_eval._score(
        changed,
        "booked",
        "booked",
        {
            "slot_start": "2030-02-09T10:00:00",
            "duration_minutes": 60,
        },
    )

    assert score["valid_against_ground_truth"] is True
    assert score["preference_honored"] is True
    assert score["passed"] is True


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
    assert client.calls[0][2]["json"]["profile"] == "spacechannel"
    assert client.calls[0][2]["json"]["analysisStyle"] == "topic_map"
    assert session.spoken == ["normal API reply"]
    assert {"type": "agent_reply", "text": "normal API reply"} in ws.messages


def test_browser_passes_experimental_analysis_style(monkeypatch):
    monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
    ws = FakeWebSocket()
    session = FakeBrowserSession()
    session.analysis_style = "principle_graph"
    client = FakeHttpClient()

    run(server._handle_agent_request(ws, session, client, "review this strategy deeply"))

    assert client.calls[0][2]["json"]["analysisStyle"] == "principle_graph"


def test_browser_passes_current_mode_profile_on_every_agent_request(monkeypatch):
    monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
    ws = FakeWebSocket()
    session = FakeBrowserSession()
    client = FakeHttpClient()

    assert set_flow_mode("replicantpm") is True
    run(server._handle_agent_request(ws, session, client, "tell me about rentals"))
    assert client.calls[-1][2]["json"]["profile"] == "replicantpm"

    assert set_flow_mode("none") is True
    run(server._handle_agent_request(ws, session, client, "hello again"))
    assert client.calls[-1][2]["json"]["profile"] == "none"


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
    session._scheduler_flow_enabled = server.get_flow_mode() == "scheduler"
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


def test_browser_first_flow_reply_keeps_audio_gate_closed(monkeypatch):
    async def exercise():
        reply_started = asyncio.Event()
        release_reply = asyncio.Event()

        class PendingFlow:
            greeting = SCHEDULER_GREETING

            async def reply(self, text):
                assert text == "I need a plumber"
                reply_started.set()
                await release_reply.wait()
                return FlowReply(
                    text="What day works for you?",
                    done=False,
                    outcome=None,
                    slots={"job": "plumbing"},
                )

        flow = PendingFlow()

        class FakeFlowSession:
            @classmethod
            def create(cls):
                return flow

        monkeypatch.setattr(server, "FlowSession", FakeFlowSession)
        ws = FakeWebSocket()
        session = FakeBrowserSession()
        session._scheduler_flow_enabled = True
        session._scheduler_flow_attempted = False
        session._scheduler_flow = None
        client = FakeHttpClient()

        task = asyncio.create_task(
            server._handle_agent_request(
                ws, session, client, "I need a plumber"
            )
        )
        await asyncio.wait_for(reply_started.wait(), timeout=1)
        try:
            event_types = [message["type"] for message in ws.messages]
            assert event_types.count("agent_audio_start") == 1
            assert "agent_audio_end" not in event_types
            assert session.spoken == [SCHEDULER_GREETING]
        finally:
            release_reply.set()
            await task

        event_types = [message["type"] for message in ws.messages]
        assert event_types.count("agent_audio_start") == 1
        assert event_types.count("agent_audio_end") == 1
        assert session.spoken == [
            SCHEDULER_GREETING,
            "What day works for you?",
        ]
        assert client.calls == []

    run(exercise())


def test_browser_terminal_playback_cancel_still_reverts_flow(monkeypatch):
    terminal = FlowReply(
        text="You're booked. Goodbye!",
        done=True,
        outcome="booked",
        slots={"job": "drain repair"},
    )

    async def exercise():
        playback_started = asyncio.Event()
        flow_calls = []

        class TerminalFlow:
            async def reply(self, text):
                flow_calls.append(text)
                return terminal

        class BlockingSession(FakeBrowserSession):
            async def speak_text(self, text, voice_id, speed):
                self.spoken.append(text)
                if text == terminal.text:
                    playback_started.set()
                    await asyncio.Event().wait()
                return None

        monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
        ws = FakeWebSocket()
        session = BlockingSession()
        session._scheduler_flow_enabled = True
        session._scheduler_flow_attempted = True
        session._scheduler_flow = TerminalFlow()
        client = FakeHttpClient()

        task = asyncio.create_task(
            server._handle_agent_request(ws, session, client, "book it")
        )
        await asyncio.wait_for(playback_started.wait(), timeout=1)
        assert session._scheduler_flow is None
        assert session._scheduler_flow_enabled is False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        await server._handle_agent_request(
            ws, session, client, "what launches are next"
        )

        assert flow_calls == ["book it"]
        assert len(client.calls) == 1
        assert session.spoken[-1] == "normal API reply"

    run(exercise())


def test_flow_mode_registry_defaults_and_maps_legacy_off(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_VOICE_FLOW", raising=False)

    assert list(FLOW_MODES) == [
        "none",
        "spacechannel",
        "intelligence",
        "replicantpm",
        "scheduler",
    ]
    assert DEFAULT_FLOW_MODE == "spacechannel"
    assert get_flow_mode() == "spacechannel"
    assert get_flow_profile() == "spacechannel"
    assert get_flow_profile("scheduler") == "spacechannel"

    assert set_flow_mode("off") is True
    assert get_flow_mode() == "spacechannel"
    assert set_flow_mode("not-a-flow") is False
    assert get_flow_mode() == "spacechannel"

    monkeypatch.setenv("NANO_CLAW_VOICE_FLOW", "off")
    monkeypatch.setattr(flow_session, "_flow_mode", None)
    assert get_flow_mode() == "spacechannel"


def test_flow_toggle_endpoints_use_env_then_runtime_override(monkeypatch, tmp_path):
    availability = tmp_path / "availability.json"
    availability.write_text(json.dumps({
        "timezone": "America/Chicago",
        "days": {},
    }))
    monkeypatch.setenv("NANO_CLAW_VOICE_FLOW", "scheduler")
    monkeypatch.setenv("NANO_CLAW_FLOW_AVAILABILITY", str(availability))
    monkeypatch.setenv("NANO_CLAW_PHONE", "0")
    monkeypatch.delenv("SCHED_EVAL_MODEL", raising=False)

    async def exercise():
        client = TestClient(TestServer(server.create_app()))
        await client.start_server()
        try:
            response = await client.get("/api/voice/flow")
            assert response.status == 200
            assert await response.json() == {
                "active": "scheduler",
                "options": [
                    {"id": "none", "label": "None"},
                    {"id": "spacechannel", "label": "HYPERRIFF"},
                    {"id": "intelligence", "label": "Document Intelligence"},
                    {"id": "replicantpm", "label": "Replicant PM"},
                    {"id": "scheduler", "label": "Plumber Scheduler"},
                ],
                "availability_ok": True,
            }

            response = await client.post(
                "/api/voice/flow", json={"mode": "off"}
            )
            assert response.status == 200
            assert (await response.json())["active"] == "spacechannel"

            response = await client.post(
                "/api/voice/flow", json={"mode": "replicantpm"}
            )
            assert response.status == 200
            assert (await response.json())["active"] == "replicantpm"

            response = await client.post(
                "/api/voice/flow", json={"mode": "not-a-flow"}
            )
            assert response.status == 400

            monkeypatch.setenv(
                "NANO_CLAW_FLOW_AVAILABILITY", str(tmp_path / "missing.json")
            )
            response = await client.get("/api/voice/flow")
            assert (await response.json())["availability_ok"] is False

            response = await client.get("/api/voice/region-model")
            assert response.status == 200
            assert await response.json() == {
                "active": DEFAULT_REGION_MODEL,
                "options": [
                    {"value": value, "label": label}
                    for value, label in REGION_MODELS.items()
                ],
            }

            response = await client.post(
                "/api/voice/region-model", json={"model": "xai/grok-4.3"}
            )
            assert response.status == 200
            assert (await response.json())["active"] == "xai/grok-4.3"

            response = await client.post(
                "/api/voice/region-model", json={"model": "grok-4-1-fast"}
            )
            assert response.status == 400
        finally:
            await client.close()

    run(exercise())


def test_region_model_registry_uses_env_until_valid_runtime_override(monkeypatch):
    monkeypatch.setenv("SCHED_EVAL_MODEL", "environment-supervisor")

    assert get_region_model() == "environment-supervisor"
    assert set_region_model("deepseek/deepseek-v4-flash") is True
    assert get_region_model() == "deepseek/deepseek-v4-flash"
    assert set_region_model("grok-4-1-fast") is False
    assert get_region_model() == "deepseek/deepseek-v4-flash"


def test_region_model_default_is_haiku(monkeypatch):
    monkeypatch.delenv("SCHED_EVAL_MODEL", raising=False)

    assert get_region_model() == DEFAULT_REGION_MODEL


def test_region_model_handlers_get_post_and_reject_invalid_without_socket(monkeypatch):
    monkeypatch.delenv("SCHED_EVAL_MODEL", raising=False)

    class Request:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    response = run(server.region_model_get_handler(Request(None)))
    assert response.status == 200
    assert json.loads(response.body) == {
        "active": DEFAULT_REGION_MODEL,
        "options": [
            {"value": value, "label": label}
            for value, label in REGION_MODELS.items()
        ],
    }

    response = run(
        server.region_model_set_handler(Request({"model": "xai/grok-4.3"}))
    )
    assert response.status == 200
    assert json.loads(response.body)["active"] == "xai/grok-4.3"

    response = run(
        server.region_model_set_handler(Request({"model": "grok-4-1-fast"}))
    )
    assert response.status == 400
    assert get_region_model() == "xai/grok-4.3"


def test_browser_flow_turn_emits_live_flow_state(monkeypatch):
    expected_slots = {
        "job": "drain repair",
        "slot_start": "2026-07-21T10:30:00",
        "duration_minutes": 60,
    }

    class FakeFlow:
        goal = "Book one grounded plumbing appointment."
        slots = {"job": "drain repair"}
        turns_used = 2
        max_turns = 12

        async def reply(self, text):
            assert text == "Tuesday morning"
            return FlowReply(
                text="That time crosses a busy window. How about 10:30?",
                done=False,
                outcome=None,
                slots=expected_slots,
                rejected=["slot_start: interval does not fit one free window"],
                turns_used=3,
                max_turns=12,
                supervisor_ms=42.5,
            )

    monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
    ws = FakeWebSocket()
    session = FakeBrowserSession()
    session._scheduler_flow_enabled = True
    session._scheduler_flow_attempted = True
    session._scheduler_flow = FakeFlow()
    client = FakeHttpClient()

    run(server._handle_agent_request(ws, session, client, "Tuesday morning"))

    states = [message for message in ws.messages if message["type"] == "flow_state"]
    assert states == [{
        "type": "flow_state",
        "goal": "Book one grounded plumbing appointment.",
        "outcome": None,
        "slots": expected_slots,
        "rejected": ["slot_start: interval does not fit one free window"],
        "turns_used": 3,
        "max_turns": 12,
        "supervisor_ms": 42.5,
    }]
    assert client.calls == []


def test_cancelled_flow_reply_serializes_next_runner_turn(monkeypatch):
    async def exercise():
        loop = asyncio.get_running_loop()
        first_started = asyncio.Event()
        release_first = threading.Event()
        calls = []

        class SlowRunner:
            config = SimpleNamespace(goal="Book the appointment.")
            slots = {}
            max_turns = 12

            @property
            def turns_used(self):
                return len(calls)

            def turn(self, text):
                calls.append(text)
                if text == "first turn":
                    loop.call_soon_threadsafe(first_started.set)
                    if not release_first.wait(timeout=2):
                        raise AssertionError("test did not release first turn")
                    return RegionTurn(
                        reply="orphaned answer",
                        exit=None,
                        slots={},
                        supervisor_ms=10.0,
                        rejected=[],
                    )
                return RegionTurn(
                    reply="second answer",
                    exit=None,
                    slots={"job": "drain repair"},
                    supervisor_ms=12.0,
                    rejected=[],
                )

        monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
        ws = FakeWebSocket()
        browser_session = FakeBrowserSession()
        browser_session._scheduler_flow_enabled = True
        browser_session._scheduler_flow_attempted = True
        browser_session._scheduler_flow = FlowSession(SlowRunner())
        client = FakeHttpClient()

        first_task = asyncio.create_task(
            server._handle_agent_request(
                ws, browser_session, client, "first turn"
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

        second_task = asyncio.create_task(
            server._handle_agent_request(
                ws, browser_session, client, "second turn"
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert calls == ["first turn"]
            assert all(
                message.get("text") != "orphaned answer"
                for message in ws.messages
            )

            release_first.set()
            await asyncio.wait_for(second_task, timeout=1)
        finally:
            release_first.set()
            if not second_task.done():
                second_task.cancel()

        assert calls == ["first turn", "second turn"]
        spoken_replies = [
            message["text"]
            for message in ws.messages
            if message["type"] == "agent_reply"
        ]
        assert spoken_replies == ["second answer"]
        assert browser_session.spoken == ["second answer"]
        assert client.calls == []

    run(exercise())


def test_orphaned_flow_exception_does_not_break_next_turn(caplog):
    caplog.set_level(logging.ERROR, logger="nano-claw.flow")

    async def exercise():
        loop = asyncio.get_running_loop()
        first_started = asyncio.Event()
        release_first = threading.Event()
        calls = []

        class RaisingRunner:
            config = SimpleNamespace(goal="Book the appointment.")
            slots = {}
            max_turns = 12

            @property
            def turns_used(self):
                return len(calls)

            def turn(self, text):
                calls.append(text)
                if text == "first turn":
                    loop.call_soon_threadsafe(first_started.set)
                    if not release_first.wait(timeout=2):
                        raise AssertionError("test did not release first turn")
                    raise RuntimeError("orphan failed")
                return RegionTurn(
                    reply="recovered answer",
                    exit=None,
                    slots={},
                    supervisor_ms=8.0,
                    rejected=[],
                )

        flow = FlowSession(RaisingRunner())
        first_task = asyncio.create_task(flow.reply("first turn"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

        second_task = asyncio.create_task(flow.reply("second turn"))
        try:
            await asyncio.sleep(0.05)
            assert calls == ["first turn"]
            release_first.set()
            reply = await asyncio.wait_for(second_task, timeout=1)
        finally:
            release_first.set()
            if not second_task.done():
                second_task.cancel()

        assert calls == ["first turn", "second turn"]
        assert reply.text == "recovered answer"

    run(exercise())
    assert "Discarded scheduler flow turn failed" in caplog.text
    assert "orphan failed" in caplog.text
