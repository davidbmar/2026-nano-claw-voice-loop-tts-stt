from __future__ import annotations

from collections import deque
from datetime import datetime
from types import SimpleNamespace

import pytest

from voice.booking import BookingFlow
from voice.calendar_client import CalendarError
from voice.flow_session import digest_from_windows
from voice.goal_region import FreeWindow, GoalRegionRunner, RegionTurn
from voice.scheduling_domains import DOMAINS


START = "2026-07-27T10:00:00"
SECOND_START = "2026-07-27T11:00:00"
LAWYER_SLOTS = {
    "service_type": "initial_consultation",
    "slot_start": START,
    "duration_minutes": 60,
}


def region_turn(
    *,
    exit: str | None,
    slots: dict | None = None,
    reply: str = "",
    rejected: list[str] | None = None,
    supervisor_ms: float | None = 8.5,
) -> RegionTurn:
    return RegionTurn(
        reply=reply,
        exit=exit,
        slots=dict(slots or {}),
        supervisor_ms=supervisor_ms,
        rejected=list(rejected or ()),
    )


class ScriptedRunner:
    """Small runner double that retains the real escape/window mutators."""

    _matches_escape = GoalRegionRunner._matches_escape
    remove_free_window_overlap = GoalRegionRunner.remove_free_window_overlap
    clear_slot = GoalRegionRunner.clear_slot
    grant_grace_turn = GoalRegionRunner.grant_grace_turn

    def __init__(
        self,
        turns: list[RegionTurn],
        *,
        windows: list[FreeWindow] | None = None,
    ) -> None:
        self._turns = deque(turns)
        self._slots: dict = {}
        self._grace_turns = 0
        self.inputs: list[str] = []
        self.free_windows = list(
            windows
            or [
                FreeWindow(
                    datetime.fromisoformat("2026-07-27T09:00:00"),
                    datetime.fromisoformat("2026-07-27T12:00:00"),
                )
            ]
        )
        self.config = SimpleNamespace(
            escape_phrases=DOMAINS["lawyer"].escape_phrases,
            digest="old digest",
            max_turns=DOMAINS["lawyer"].max_turns,
        )

    @property
    def slots(self) -> dict:
        return dict(self._slots)

    @property
    def turns_used(self) -> int:
        return len(self.inputs)

    @property
    def max_turns(self) -> int:
        return self.config.max_turns

    def turn(self, caller_text: str) -> RegionTurn:
        self.inputs.append(caller_text)
        turn = self._turns.popleft()
        self._slots = dict(turn.slots)
        return turn


class FakeCalendar:
    def __init__(
        self,
        *,
        free_results: list[bool] | None = None,
        is_free_error: bool = False,
        insert_error: bool = False,
    ) -> None:
        self.settings = SimpleNamespace(timezone="America/Chicago")
        self._free_results = deque(free_results or [True])
        self._is_free_error = is_free_error
        self._insert_error = insert_error
        self.checked: list[tuple[datetime, datetime]] = []
        self.inserted: list[dict] = []

    def is_free(self, start: datetime, end: datetime) -> bool:
        self.checked.append((start, end))
        if self._is_free_error:
            raise CalendarError("freebusy failed")
        return self._free_results.popleft()

    def insert_event(self, **event) -> str:
        if self._insert_error:
            raise CalendarError("insert failed")
        self.inserted.append(event)
        return "event-123"


def test_confirm_yes_commits_and_carries_event_id_with_policy_duration():
    # Deliberately make the runner's duration stale: commit-time policy wins.
    slots = {**LAWYER_SLOTS, "duration_minutes": 30}
    runner = ScriptedRunner([region_turn(exit="booked", slots=slots)])
    calendar = FakeCalendar()
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)

    confirmation = flow.turn("Monday at ten works")

    assert confirmation.done is False
    assert confirmation.outcome is None
    assert confirmation.event_id is None
    assert confirmation.reply == (
        "To confirm: a 60-minute initial consultation by video, "
        "Monday July twenty seventh at 10 AM — shall I book it?"
    )
    assert calendar.inserted == []

    booked = flow.turn("yes, please do")

    assert booked.done is True
    assert booked.outcome == "booked"
    assert booked.event_id == "event-123"
    assert booked.reply == (
        "You're booked: a 60-minute initial consultation by video, "
        "Monday July twenty seventh at 10 AM. See you then. Goodbye!"
    )
    event = calendar.inserted[0]
    assert (event["end"] - event["start"]).total_seconds() == 60 * 60
    assert event["summary"] == "initial consultation — phone booking"
    assert event["description"] == (
        "service_type: initial_consultation\n"
        "duration_minutes: 60\n"
        "booked via nano-claw voice"
    )
    assert event["private_props"] == {"nanoclaw_domain": "lawyer"}


@pytest.mark.parametrize("caller_text", ["hmm maybe", "no, try Friday instead"])
def test_ambiguous_or_negative_confirmation_returns_same_utterance_to_region(
    caller_text,
):
    runner = ScriptedRunner(
        [
            region_turn(exit="booked", slots=LAWYER_SLOTS),
            region_turn(
                exit=None,
                slots={"service_type": "initial_consultation"},
                reply="Let's find another time.",
            ),
        ]
    )
    calendar = FakeCalendar()
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)
    flow.turn("Monday works")

    reconsidered = flow.turn(caller_text)

    assert reconsidered.done is False
    assert reconsidered.reply == "Let's find another time."
    assert runner.inputs == ["Monday works", caller_text]
    assert calendar.checked == []
    assert calendar.inserted == []


def test_early_yes_stays_in_negotiation_and_cannot_commit():
    runner = ScriptedRunner(
        [
            region_turn(
                exit=None,
                slots={},
                reply="What kind of appointment do you need?",
            )
        ]
    )
    calendar = FakeCalendar()
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)

    turn = flow.turn("yes")

    assert turn.done is False
    assert turn.reply == "What kind of appointment do you need?"
    assert runner.inputs == ["yes"]
    assert calendar.checked == []
    assert calendar.inserted == []


def test_commit_conflict_clips_windows_refreshes_digest_then_books_elsewhere():
    second_slots = {**LAWYER_SLOTS, "slot_start": SECOND_START}
    runner = ScriptedRunner(
        [
            region_turn(exit="booked", slots=LAWYER_SLOTS),
            region_turn(exit="booked", slots=second_slots),
        ]
    )
    calendar = FakeCalendar(free_results=[False, True])
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)
    flow.turn("Monday at ten")

    conflict = flow.turn("yes")

    assert conflict.done is False
    assert conflict.outcome is None
    assert conflict.reply == DOMAINS["lawyer"].slot_taken_text
    assert "slot_start" not in conflict.slots
    assert [
        (window.start.isoformat(), window.end.isoformat())
        for window in runner.free_windows
    ] == [
        ("2026-07-27T09:00:00", "2026-07-27T10:00:00"),
        ("2026-07-27T11:00:00", "2026-07-27T12:00:00"),
    ]
    assert runner.config.digest == digest_from_windows(
        runner.free_windows,
        "America/Chicago",
    )
    assert runner.config.digest == (
        "All times are America/Chicago; business hours are 08:00–18:00.\n"
        "A visit must fit inside one listed half-open free window:\n"
        "- Monday July 27 (2026-07-27): 09:00–10:00 (fits ≤60m), "
        "11:00–12:00 (fits ≤60m)"
    )

    second_confirmation = flow.turn("Eleven instead")
    assert second_confirmation.done is False
    assert "at 11 AM" in second_confirmation.reply

    booked = flow.turn("that works")

    assert booked.done is True
    assert booked.outcome == "booked"
    assert booked.event_id == "event-123"
    assert calendar.inserted[0]["start"] == datetime.fromisoformat(SECOND_START)


@pytest.mark.parametrize("failure_point", ["is_free", "insert"])
def test_calendar_errors_return_not_booked_apology_without_raising(failure_point):
    runner = ScriptedRunner([region_turn(exit="booked", slots=LAWYER_SLOTS)])
    calendar = FakeCalendar(
        is_free_error=failure_point == "is_free",
        insert_error=failure_point == "insert",
    )
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)
    flow.turn("Monday works")

    turn = flow.turn("confirm")

    assert turn.done is True
    assert turn.outcome == "not_booked"
    assert turn.event_id is None
    assert turn.reply == DOMAINS["lawyer"].apology_unavailable
    assert calendar.inserted == []


@pytest.mark.parametrize(
    ("exit_name", "slots", "expected"),
    [
        (
            "booked",
            {
                "job": "clogged drain",
                "slot_start": START,
                "duration_minutes": 60,
            },
            "You're booked: clogged drain on Monday July twenty seventh at "
            "10 AM for 60 minutes. See you then. Goodbye!",
        ),
        (
            "escape",
            {},
            "Of course — I'm transferring you now. Goodbye!",
        ),
        (
            "budget",
            {},
            "Our scheduler will call you back to finish this up. Goodbye!",
        ),
    ],
)
def test_plumber_without_calendar_preserves_terminal_speech(
    exit_name,
    slots,
    expected,
):
    runner = ScriptedRunner([region_turn(exit=exit_name, slots=slots)])
    flow = BookingFlow(runner, DOMAINS["plumber"], None)

    turn = flow.turn("caller utterance")

    assert turn.done is True
    assert turn.outcome == exit_name
    assert turn.reply == expected
    assert turn.event_id is None


def test_third_confirmation_cycle_ends_as_budget():
    runner = ScriptedRunner(
        [
            region_turn(exit="booked", slots=LAWYER_SLOTS),
            region_turn(exit="booked", slots=LAWYER_SLOTS),
            region_turn(exit="booked", slots=LAWYER_SLOTS),
        ]
    )
    calendar = FakeCalendar()
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)

    first = flow.turn("Monday works")
    second = flow.turn("hmm maybe")
    capped = flow.turn("I'm still not sure")

    assert first.done is False
    assert second.done is False
    assert capped.done is True
    assert capped.outcome == "budget"
    assert capped.reply == DOMAINS["lawyer"].budget_text
    assert runner.inputs == ["Monday works", "hmm maybe", "I'm still not sure"]
    assert calendar.checked == []
    assert calendar.inserted == []


def test_escape_phrase_wins_over_affirmation_while_confirming():
    runner = ScriptedRunner([region_turn(exit="booked", slots=LAWYER_SLOTS)])
    calendar = FakeCalendar()
    flow = BookingFlow(runner, DOMAINS["lawyer"], calendar)
    flow.turn("Monday works")

    turn = flow.turn("yes, get me a human")

    assert turn.done is True
    assert turn.outcome == "escape"
    assert turn.reply == DOMAINS["lawyer"].escape_text
    assert runner.inputs == ["Monday works"]
    assert calendar.checked == []
    assert calendar.inserted == []


def test_ambiguous_confirmation_answer_grants_budget_grace():
    runner = ScriptedRunner([
        region_turn(exit="booked", slots=LAWYER_SLOTS),
        region_turn(exit=None, reply="Would another day work?", slots=LAWYER_SLOTS),
    ])
    flow = BookingFlow(runner, DOMAINS["lawyer"], FakeCalendar())

    flow.turn("An initial consultation Monday at ten.")
    reply = flow.turn("hmm, let me think about that")

    # The answer went back into the region with one budget-exempt turn granted,
    # so a caller mid-confirmation can never be swallowed by the session cap.
    assert runner._grace_turns == 1
    assert reply.done is False
    assert runner.inputs[-1] == "hmm, let me think about that"
