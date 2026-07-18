import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from voice import flow_session
from voice.goal_region import FreeWindow, GoalRegionRunner, RegionConfig


class FrozenClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeMessages:
    def __init__(self, outputs=()):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("supervisor should not have been called")
        text = json.dumps(self.outputs.pop(0))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)]
        )


class FakeClient:
    def __init__(self, outputs=()):
        self.messages = FakeMessages(outputs)


class RawMessages:
    def __init__(self, responses, clock=None):
        self.responses = list(responses)
        self.calls = []
        self.clock = clock

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("supervisor should not have been called")
        if self.clock is not None:
            self.clock.value += 0.25
        return self.responses.pop(0)


def raw_response(text, *, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
    )


def response(
    *,
    reply="What time works for you?",
    job=None,
    start=None,
    duration=None,
    exit_candidate=None,
):
    return {
        "reply": reply,
        "slot_candidates": {
            "job": job,
            "slot_start": start,
            "duration_minutes": duration,
        },
        "exit_candidate": exit_candidate,
        "evidence": "fake evidence",
    }


def config(*, max_turns=6, deadline_s=60):
    return RegionConfig(
        goal="Book a plumbing appointment.",
        persona="You are a helpful plumbing scheduler.",
        digest="Monday has the listed free windows.",
        slots={
            "job": {"type": "text", "required": True},
            "slot_start": {"type": "datetime", "required": True},
            "duration_minutes": {
                "type": "minutes",
                "values": [30, 60, 120, 240],
                "required": True,
            },
        },
        escape_phrases=("operator", "human", "goodbye"),
        max_turns=max_turns,
        deadline_s=deadline_s,
    )


def window(start, end):
    return FreeWindow(datetime.fromisoformat(start), datetime.fromisoformat(end))


def test_exact_free_window_books_and_drops_supervisor_reply():
    client = FakeClient([
        response(
            reply="Draft confirmation that the FSM must not speak.",
            job="water heater install",
            start="2026-07-22T08:00:00",
            duration=240,
            exit_candidate="booked",
        )
    ])
    runner = GoalRegionRunner(
        config(),
        [window("2026-07-22T08:00:00", "2026-07-22T12:00:00")],
        client=client,
    )
    assert runner.turns_used == 0
    assert runner.max_turns == 6

    turn = runner.turn("Wednesday morning works. Book it.")

    assert runner.turns_used == 1
    assert turn.exit == "booked"
    assert turn.reply == ""
    assert turn.rejected == []
    assert turn.slots == {
        "job": "water heater install",
        "slot_start": "2026-07-22T08:00:00",
        "duration_minutes": 240,
    }


@pytest.mark.parametrize(
    ("windows", "start", "duration", "rejection"),
    [
        (
            [
                window("2026-07-20T08:00:00", "2026-07-20T10:00:00"),
                window("2026-07-20T10:00:00", "2026-07-20T12:00:00"),
            ],
            "2026-07-20T09:00:00",
            120,
            "one free window",
        ),
        (
            [
                window("2026-07-20T08:00:00", "2026-07-20T10:00:00"),
                window("2026-07-20T11:00:00", "2026-07-20T18:00:00"),
            ],
            "2026-07-20T09:30:00",
            60,
            "one free window",
        ),
        (
            [window("2026-07-20T07:00:00", "2026-07-20T09:00:00")],
            "2026-07-20T07:00:00",
            60,
            "business frame",
        ),
        (
            [window("2026-07-20T08:00:00", "2026-07-20T18:00:00")],
            "2026-07-20T09:00:00",
            45,
            "duration_minutes",
        ),
        (
            [window("2026-07-20T08:00:00", "2026-07-20T18:00:00")],
            "Monday after breakfast",
            60,
            "malformed ISO",
        ),
    ],
    ids=["spans-windows", "crosses-busy-time", "outside-hours", "unknown-duration", "bad-iso"],
)
def test_validator_rejects_invalid_appointment_atomically(
    windows, start, duration, rejection
):
    client = FakeClient([
        response(
            reply="Let's find another option.",
            start=start,
            duration=duration,
            exit_candidate="booked",
        )
    ])
    runner = GoalRegionRunner(config(), windows, client=client)

    turn = runner.turn("That works, book it.")

    assert turn.exit is None
    assert turn.reply == "Let's find another option."
    assert turn.slots == {}
    assert any(rejection in item for item in turn.rejected)


def test_rejected_candidate_does_not_block_later_valid_booking():
    client = FakeClient([
        response(
            reply="That time is unavailable; how about 10?",
            start="2026-07-20T09:30:00",
            duration=60,
            exit_candidate="booked",
        ),
        response(
            reply="Internal booking draft.",
            job="toilet repair",
            start="2026-07-20T10:00:00",
            duration=60,
            exit_candidate="booked",
        ),
    ])
    runner = GoalRegionRunner(
        config(),
        [window("2026-07-20T10:00:00", "2026-07-20T11:00:00")],
        client=client,
    )

    first = runner.turn("Can you do 9:30?")
    second = runner.turn("Okay, ten works.")

    assert first.exit is None
    assert first.slots == {}
    assert first.rejected
    assert second.exit == "booked"
    assert second.slots["slot_start"] == "2026-07-20T10:00:00"


def test_escape_precedes_deadline_and_never_calls_supervisor():
    clock = FrozenClock()
    client = FakeClient()
    runner = GoalRegionRunner(config(deadline_s=1), [], clock=clock, client=client)
    clock.value = 10

    turn = runner.turn("Could I speak to a HUMAN operator?")

    assert turn.exit == "escape"
    assert turn.supervisor_ms is None
    assert client.messages.calls == []


def test_deadline_exceeded_exits_budget_without_supervisor():
    clock = FrozenClock(100)
    client = FakeClient()
    runner = GoalRegionRunner(config(deadline_s=5), [], clock=clock, client=client)
    clock.value = 105

    turn = runner.turn("Are there any openings?")

    assert turn.exit == "budget"
    assert turn.supervisor_ms is None
    assert client.messages.calls == []


def test_completed_turn_budget_short_circuits_next_turn():
    client = FakeClient([response(reply="What day do you prefer?")])
    runner = GoalRegionRunner(config(max_turns=1), [], client=client)

    first = runner.turn("I need a plumber.")
    second = runner.turn("Maybe Monday.")

    assert first.exit is None
    assert second.exit == "budget"
    assert second.supervisor_ms is None
    assert len(client.messages.calls) == 1


def test_slots_accumulate_across_turns():
    client = FakeClient([
        response(job="drain repair", duration=120),
        response(
            reply="Draft confirmation.",
            start="2026-07-20T16:00:00",
            exit_candidate="booked",
        ),
    ])
    runner = GoalRegionRunner(
        config(),
        [window("2026-07-20T16:00:00", "2026-07-20T18:00:00")],
        client=client,
    )

    first = runner.turn("It's a two-hour drain repair.")
    second = runner.turn("Monday at four is good.")

    assert first.slots == {"job": "drain repair", "duration_minutes": 120}
    assert second.exit == "booked"
    assert second.slots["slot_start"] == "2026-07-20T16:00:00"


def test_transcript_and_structured_api_shape_grow_across_turns(monkeypatch):
    monkeypatch.setenv("SCHED_EVAL_MODEL", "test-supervisor-model")
    client = FakeClient([
        response(reply="How long will the job take?", job="leak repair"),
        response(reply="Which day works?", duration=60),
    ])
    runner = GoalRegionRunner(config(), [], client=client)

    runner.turn("I have a leaking pipe.")
    runner.turn("It should take an hour.")

    first_call, second_call = client.messages.calls
    assert first_call["model"] == "test-supervisor-model"
    assert first_call["max_tokens"] == 4096
    assert first_call["system"][0]["cache_control"] == {"type": "ephemeral"}
    schema = first_call["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["slot_candidates"]["additionalProperties"] is False
    assert first_call["messages"] == [
        {"role": "user", "content": "I have a leaking pipe."}
    ]
    assert second_call["messages"] == [
        {"role": "user", "content": "I have a leaking pipe."},
        {"role": "assistant", "content": "How long will the job take?"},
        {"role": "user", "content": "It should take an hour."},
    ]
    assert runner.transcript == [
        {"role": "user", "content": "I have a leaking pipe."},
        {"role": "assistant", "content": "How long will the job take?"},
        {"role": "user", "content": "It should take an hour."},
        {"role": "assistant", "content": "Which day works?"},
    ]


def test_runtime_region_model_switch_applies_to_existing_runner(monkeypatch):
    monkeypatch.setattr(flow_session, "_region_model", None)
    monkeypatch.setenv("SCHED_EVAL_MODEL", "environment-supervisor")
    client = FakeClient([
        response(reply="What day works?"),
        response(reply="What time works?"),
    ])
    runner = GoalRegionRunner(config(), [], client=client)

    runner.turn("I need a plumber.")
    assert flow_session.set_region_model("claude-haiku-4-5") is True
    runner.turn("Monday works.")

    assert [call["model"] for call in client.messages.calls] == [
        "environment-supervisor",
        "claude-haiku-4-5",
    ]


def test_truncated_supervisor_output_retries_once_then_succeeds():
    recovered = response(reply="Which day works?", job="leak repair")
    messages = RawMessages([
        raw_response('{"reply": "cut off'),
        raw_response(json.dumps(recovered)),
    ])
    runner = GoalRegionRunner(
        config(), [], client=SimpleNamespace(messages=messages)
    )

    turn = runner.turn("I have a leaking pipe.")

    assert len(messages.calls) == 2
    assert messages.calls[0] == messages.calls[1]
    assert turn.reply == "Which day works?"
    assert turn.slots == {"job": "leak repair"}
    assert turn.rejected == []
    assert runner.turns_used == 1


def test_two_truncated_supervisor_outputs_return_safe_generic_turn():
    clock = FrozenClock()
    messages = RawMessages(
        [
            raw_response('{"reply": "cut off'),
            raw_response('{"reply": "still cut off'),
        ],
        clock=clock,
    )
    runner = GoalRegionRunner(
        config(), [], clock=clock, client=SimpleNamespace(messages=messages)
    )
    slots_before = runner.slots

    turn = runner.turn("How about Tuesday?")

    assert len(messages.calls) == 2
    assert messages.calls[0] == messages.calls[1]
    assert turn.reply == "Sorry — could you say that again?"
    assert turn.exit is None
    assert turn.slots == slots_before
    assert runner.slots == slots_before
    assert turn.rejected == ["supervisor: unparseable output (after retry)"]
    assert turn.supervisor_ms == 500.0
    assert runner.turns_used == 1
    assert runner.transcript == [
        {"role": "user", "content": "How about Tuesday?"},
        {"role": "assistant", "content": "Sorry — could you say that again?"},
    ]


def test_max_tokens_stop_reason_retries_even_with_valid_json():
    ignored = response(reply="Ignore this truncated response.")
    recovered = response(reply="What time works for you?")
    messages = RawMessages([
        raw_response(json.dumps(ignored), stop_reason="max_tokens"),
        raw_response(json.dumps(recovered)),
    ])
    runner = GoalRegionRunner(
        config(), [], client=SimpleNamespace(messages=messages)
    )

    turn = runner.turn("I need a plumber.")

    assert len(messages.calls) == 2
    assert messages.calls[0] == messages.calls[1]
    assert turn.reply == "What time works for you?"
    assert turn.rejected == []
