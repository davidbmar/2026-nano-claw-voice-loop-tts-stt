import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from voice import flow_session
from voice.goal_region import (
    FreeWindow,
    GoalRegionRunner,
    RegionConfig,
    build_supervisor_schema,
)


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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def config(
    *,
    max_turns=6,
    deadline_s=60,
    escape_on_provider_failure=True,
    suppress_premature_confirmation=True,
):
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
        escape_on_provider_failure=escape_on_provider_failure,
        suppress_premature_confirmation=suppress_premature_confirmation,
    )


def lawyer_config():
    return RegionConfig(
        goal="Book a legal appointment.",
        persona="You are a helpful legal scheduler.",
        digest="Monday has the listed free windows.",
        slots={
            "service_type": {
                "type": "enum",
                "values": [
                    "follow_up_call",
                    "initial_consultation",
                    "contract_dispute_consult",
                ],
                "required": True,
            },
            "slot_start": {"type": "datetime", "required": True},
            "duration_minutes": {
                "type": "derived_minutes",
                "from": "service_type",
                "map": {
                    "follow_up_call": 30,
                    "initial_consultation": 60,
                    "contract_dispute_consult": 120,
                },
                "required": True,
            },
        },
        escape_phrases=("operator", "human", "goodbye"),
        max_turns=6,
        deadline_s=60,
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


def test_provider_failure_retries_once_and_returns_successful_turn():
    recovered = response(reply="What time works for you?")
    messages = RawMessages([
        TimeoutError("first request timed out"),
        raw_response(json.dumps(recovered)),
    ])
    runner = GoalRegionRunner(
        config(), [], client=SimpleNamespace(messages=messages)
    )

    turn = runner.turn("I need an appointment.")

    assert len(messages.calls) == 2
    assert messages.calls[0] == messages.calls[1]
    assert turn.reply == "What time works for you?"
    assert turn.exit is None
    assert turn.rejected == []


def test_two_provider_failures_degrade_without_raising():
    messages = RawMessages([
        TimeoutError("first request timed out"),
        TimeoutError("retry timed out"),
    ])
    runner = GoalRegionRunner(
        config(), [], client=SimpleNamespace(messages=messages)
    )

    turn = runner.turn("I need an appointment.")

    assert len(messages.calls) == 2
    assert turn.reply == "Sorry — could you say that again?"
    assert turn.exit is None
    assert turn.rejected == [
        "supervisor: provider failure TimeoutError (after retry)"
    ]


@pytest.mark.parametrize(
    ("escape_on_provider_failure", "expected_second_exit"),
    [(True, "escape"), (False, None)],
)
def test_consecutive_degraded_provider_turns_follow_escape_policy(
    escape_on_provider_failure,
    expected_second_exit,
):
    messages = RawMessages([
        ConnectionError("turn one request failed"),
        ConnectionError("turn one retry failed"),
        ConnectionError("turn two request failed"),
        ConnectionError("turn two retry failed"),
    ])
    runner = GoalRegionRunner(
        config(escape_on_provider_failure=escape_on_provider_failure),
        [],
        client=SimpleNamespace(messages=messages),
    )

    first = runner.turn("I need an appointment.")
    second = runner.turn("Monday might work.")

    assert first.exit is None
    assert second.exit == expected_second_exit
    assert second.reply == "Sorry — could you say that again?"
    assert second.rejected == [
        "supervisor: provider failure ConnectionError (after retry)"
    ]
    assert len(messages.calls) == 4


@pytest.mark.parametrize("reply", ("", "   \t"))
def test_empty_reply_without_exit_returns_reprompt_and_records_rejection(reply):
    client = FakeClient([response(reply=reply)])
    runner = GoalRegionRunner(config(), [], client=client)

    turn = runner.turn("Can you help me find a time?")

    assert turn.reply == "Sorry — could you say that again?"
    assert turn.exit is None
    assert turn.rejected == [
        "supervisor: empty reply — substituted reprompt"
    ]
    assert runner.transcript[-1] == {
        "role": "assistant",
        "content": "Sorry — could you say that again?",
    }


def test_rejection_names_invalid_interval_and_compares_window_capacity():
    runner = GoalRegionRunner(
        config(),
        [window("2026-07-20T09:00:00", "2026-07-20T12:00:00")],
        client=FakeClient(),
    )

    updates, rejected = runner._validate_candidates({
        "slot_start": "2026-07-20T11:30:00",
        "duration_minutes": 60,
    })

    assert updates == {}
    assert rejected == [
        "slot_start 2026-07-20T11:30:00: "
        "interval does not fit entirely inside one free window "
        "(that window fits at most 180m; this visit needs 60m — choose a window "
        "marked as fitting at least 60m)"
    ]


@pytest.mark.parametrize(
    "reply",
    (
        "You are BOOKED.",
        "You're all set.",
        "Your appointment is confirmed.",
        "I've scheduled you for Monday.",
    ),
)
def test_premature_confirmation_language_is_suppressed_without_exit(reply):
    client = FakeClient([response(reply=reply, job="leaking sink")])
    runner = GoalRegionRunner(config(), [], client=client)

    turn = runner.turn("Can you book it?")

    assert turn.exit is None
    assert turn.reply == "Sorry — could you say that again?"
    assert turn.rejected == [
        "reply: premature confirmation language suppressed"
    ]
    assert runner.transcript[-1] == {
        "role": "assistant",
        "content": "Sorry — could you say that again?",
    }


def test_disabling_premature_confirmation_suppression_restores_old_behavior():
    client = FakeClient([
        response(reply="You are booked.", job="leaking sink")
    ])
    runner = GoalRegionRunner(
        config(suppress_premature_confirmation=False),
        [],
        client=client,
    )

    turn = runner.turn("Book that.")

    assert turn.exit is None
    assert turn.reply == "You are booked."
    assert turn.rejected == []


def test_empty_reply_on_legitimate_booked_exit_is_unaffected():
    client = FakeClient([
        response(
            reply="",
            job="leaking sink",
            start="2026-07-20T09:00:00",
            duration=60,
            exit_candidate="booked",
        )
    ])
    runner = GoalRegionRunner(
        config(),
        [window("2026-07-20T09:00:00", "2026-07-20T12:00:00")],
        client=client,
    )

    turn = runner.turn("Monday at nine works.")

    assert turn.exit == "booked"
    assert turn.reply == ""
    assert turn.rejected == []


def test_schema_builder_uses_enum_any_of_and_omits_derived_slots():
    schema = build_supervisor_schema(lawyer_config().slots)

    candidates = schema["properties"]["slot_candidates"]
    assert candidates["additionalProperties"] is False
    assert candidates["required"] == ["service_type", "slot_start"]
    assert candidates["properties"] == {
        "service_type": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": [
                        "follow_up_call",
                        "initial_consultation",
                        "contract_dispute_consult",
                    ],
                },
                {"type": "null"},
            ]
        },
        "slot_start": {"type": ["string", "null"]},
    }


def test_enum_candidate_accepts_and_derives_duration_minutes():
    runner = GoalRegionRunner(lawyer_config(), [], client=FakeClient())

    updates, rejected = runner._validate_candidates({
        "service_type": "initial_consultation",
    })

    assert updates == {
        "service_type": "initial_consultation",
        "duration_minutes": 60,
    }
    assert rejected == []
    assert runner._allowed_durations() == {30, 60, 120}


def test_enum_candidate_rejects_value_outside_configured_values():
    runner = GoalRegionRunner(lawyer_config(), [], client=FakeClient())

    updates, rejected = runner._validate_candidates({
        "service_type": "estate_planning",
    })

    assert updates == {}
    assert rejected == [
        "service_type estate_planning: expected one of "
        "follow_up_call, initial_consultation, contract_dispute_consult"
    ]


def test_derived_120_minute_service_must_fit_inside_one_60_minute_window():
    runner = GoalRegionRunner(
        lawyer_config(),
        [window("2026-07-20T09:00:00", "2026-07-20T10:00:00")],
        client=FakeClient(),
    )

    updates, rejected = runner._validate_candidates({
        "service_type": "contract_dispute_consult",
        "slot_start": "2026-07-20T09:00:00",
    })

    assert updates == {}
    assert rejected == [
        "slot_start 2026-07-20T09:00:00: "
        "interval does not fit entirely inside one free window "
        "(that window fits at most 60m; this visit needs 120m — choose a window "
        "marked as fitting at least 120m)"
    ]


def test_enum_change_rejects_new_duration_that_does_not_fit_held_start():
    runner = GoalRegionRunner(
        lawyer_config(),
        [window("2026-07-20T09:00:00", "2026-07-20T10:00:00")],
        client=FakeClient(),
    )
    initial, rejected = runner._validate_candidates({
        "service_type": "initial_consultation",
        "slot_start": "2026-07-20T09:00:00",
    })
    assert rejected == []
    runner._slots.update(initial)

    updates, rejected = runner._validate_candidates({
        "service_type": "contract_dispute_consult",
    })

    assert updates == {}
    assert rejected == [
        "service_type contract_dispute_consult: "
        "interval does not fit entirely inside one free window "
        "(that window fits at most 60m; this visit needs 120m — choose a window "
        "marked as fitting at least 120m); propose a new time for this service"
    ]
    assert runner.slots == initial


@pytest.mark.parametrize(
    ("remove_start", "remove_end", "expected"),
    [
        (
            "2026-07-20T08:00:00",
            "2026-07-20T10:00:00",
            [("2026-07-20T10:00:00", "2026-07-20T12:00:00")],
        ),
        (
            "2026-07-20T11:00:00",
            "2026-07-20T13:00:00",
            [("2026-07-20T09:00:00", "2026-07-20T11:00:00")],
        ),
        (
            "2026-07-20T10:00:00",
            "2026-07-20T11:00:00",
            [
                ("2026-07-20T09:00:00", "2026-07-20T10:00:00"),
                ("2026-07-20T11:00:00", "2026-07-20T12:00:00"),
            ],
        ),
        ("2026-07-20T08:00:00", "2026-07-20T13:00:00", []),
        (
            "2026-07-20T12:00:00",
            "2026-07-20T13:00:00",
            [("2026-07-20T09:00:00", "2026-07-20T12:00:00")],
        ),
    ],
    ids=["clip-left", "clip-right", "split", "delete", "no-op"],
)
def test_remove_free_window_overlap_matrix(remove_start, remove_end, expected):
    runner = GoalRegionRunner(
        config(),
        [window("2026-07-20T09:00:00", "2026-07-20T12:00:00")],
        client=FakeClient(),
    )

    runner.remove_free_window_overlap(
        datetime.fromisoformat(remove_start),
        datetime.fromisoformat(remove_end),
    )

    assert [
        (free_window.start.isoformat(), free_window.end.isoformat())
        for free_window in runner.free_windows
    ] == expected


def test_clear_slot_drops_existing_slot_and_ignores_missing_slot():
    runner = GoalRegionRunner(config(), [], client=FakeClient())
    runner._slots.update({"job": "leak repair", "duration_minutes": 60})

    runner.clear_slot("job")
    runner.clear_slot("not_present")

    assert runner.slots == {"duration_minutes": 60}


def test_grace_turn_exempts_exhausted_turn_budget_once():
    client = FakeClient([
        response(reply="What day do you prefer?"),
        response(reply="How about Monday at ten?"),
    ])
    runner = GoalRegionRunner(config(max_turns=1), [], client=client)

    first = runner.turn("I need a plumber.")
    runner.grant_grace_turn()
    graced = runner.turn("Wait — not that time after all.")
    exhausted = runner.turn("Hmm, let me think.")

    assert first.exit is None
    assert graced.exit is None
    assert exhausted.exit == "budget"
    assert len(client.messages.calls) == 2


def test_grace_turn_exempts_expired_deadline():
    clock = FrozenClock(100)
    client = FakeClient([response(reply="Sure — what works instead?")])
    runner = GoalRegionRunner(config(deadline_s=5), [], clock=clock, client=client)
    clock.value = 105

    runner.grant_grace_turn()
    turn = runner.turn("Can we look at another day?")

    assert turn.exit is None
    assert len(client.messages.calls) == 1


def test_structured_payload_tolerates_markdown_fences():
    """Small local models fence structured output (~93% of gemma4:e2b eval
    turns); the JSON inside is valid and must parse. Strict-first: garbage
    still raises, and fenced garbage still raises."""
    from voice.goal_region import _structured_payload

    fenced = '```json\n{"reply": "Monday at 9 works", "exit": null}\n```'
    assert _structured_payload(fenced) == {"reply": "Monday at 9 works", "exit": None}
    bare_fence = '```\n{"reply": "ok"}\n```'
    assert _structured_payload(bare_fence) == {"reply": "ok"}
    assert _structured_payload('{"reply": "plain"}') == {"reply": "plain"}
    import pytest

    prose_wrapped = 'Sure thing! {"reply": "Thursday at 9 works", "exit": null} Anything else?'
    assert _structured_payload(prose_wrapped) == {
        "reply": "Thursday at 9 works",
        "exit": None,
    }
    with pytest.raises(ValueError):
        _structured_payload("no json here")
    with pytest.raises(ValueError):
        _structured_payload("{unterminated")


# ── the exit name is configuration, not a scheduling word ────────────────────

def _classify_config(**over):
    """A region shaped like a riff-builder intake subgoal, not a booking."""
    base = dict(
        goal="Classify what the caller needs.",
        persona="A calm dispatcher.",
        digest="",
        slots={"request_details": {"type": "text", "required": True}},
        escape_phrases=("operator",),
        max_turns=8,
        deadline_s=60.0,
        exit_name="classified",
    )
    base.update(over)
    return RegionConfig(**base)


def test_the_default_exit_is_unchanged():
    """Every existing domain and call site depends on this. The parameter was
    added with a default precisely so none of them had to change."""
    from voice.goal_region import RegionConfig as RC

    assert RC(goal="g", persona="p", digest="", slots={}, escape_phrases=(),
              max_turns=1, deadline_s=1.0).exit_name == "booked"
    assert build_supervisor_schema({})["properties"]["exit_candidate"]["anyOf"][0][
        "enum"] == ["booked"]


def test_a_classified_region_offers_only_its_own_exit():
    """riff-builder authors `exit_names: ["classified"]` for every intake
    subgoal (rb/subgoal.py EXIT_NAMES_ALLOWED). With the enum hardcoded to
    "booked", the supervisor could not name that exit at all — so nano-claw
    could not run a single subgoal riff-builder is able to author."""
    schema = build_supervisor_schema(
        _classify_config().slots, exit_name="classified")

    assert schema["properties"]["exit_candidate"]["anyOf"][0]["enum"] == [
        "classified"]


def test_the_runner_accepts_its_configured_exit_and_rejects_others():
    config = _classify_config()
    runner = GoalRegionRunner(config, ())
    runner._slots = {"request_details": "burst pipe under the sink"}

    rejected: list[str] = []
    assert runner._validated_exit("classified", rejected) == "classified"
    assert rejected == []

    # "booked" is now the WRONG word for this region, and must be refused by the
    # same mechanism that used to refuse everything except it.
    rejected = []
    assert runner._validated_exit("booked", rejected) is None
    assert rejected and "unsupported value" in rejected[0]


def test_a_successful_exit_still_suppresses_the_models_prose():
    """The reply is blanked on a successful exit so the DETERMINISTIC tail
    speaks. That reasoning is about exiting, not about scheduling — a second
    hardcoded "booked" here would have let a classified region speak both its
    own prose and its tail."""
    import inspect

    from voice import goal_region

    source = inspect.getsource(goal_region.GoalRegionRunner.turn)
    assert 'exit_name == self.config.exit_name' in source, (
        "the reply-suppression check went back to comparing against a literal")
    assert 'exit_name == "booked"' not in source
