"""Static availability is anchored to the moment the calendar launches.

2026-07-27 bug: the plumber scheduler offered July 17-23 — days 4-10 in the
PAST — because it reads a frozen fixture
(scripts/scheduling_eval/availability.json) whose dates were written for the
week of the 17th. The supervisor was obeying its prompt exactly; the data was
stale. (The lawyer domain was unaffected: uses_live_calendar=True starts its
snapshot at tomorrow.)

Design (David, 2026-07-27): when the calendar component launches it must know
the current day and time, then prefer upcoming days, so any slot a caller can
request is in the future. Rolling by whole WEEKS keeps each fixture day on its
original weekday, so weekend/weekday shape — which the scheduling evals depend
on — survives the shift.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from voice.flow_session import (
    anchor_availability,
    availability_digest,
    load_free_windows,
)

CHICAGO = ZoneInfo("America/Chicago")


def _fixture(first_day: str = "2026-07-17") -> dict:
    """Two days of windows starting on a Friday, mirroring the real file."""
    start = datetime.fromisoformat(first_day)
    days = {}
    for offset, windows in enumerate(
        [
            [("09:30", "10:00"), ("13:30", "14:00")],
            [],  # the fixture's weekend day: no availability
        ]
    ):
        day = (start + timedelta(days=offset)).date().isoformat()
        days[day] = [
            {
                "start": f"{day}T{begin}:00-05:00",
                "end": f"{day}T{finish}:00-05:00",
            }
            for begin, finish in windows
        ]
    return {"timezone": "America/Chicago", "days": days}


def test_stale_fixture_rolls_forward_to_upcoming_days():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)  # Monday
    anchored = anchor_availability(_fixture(), now=now)

    days = sorted(anchored["days"])
    assert days, "anchoring must not empty the calendar"
    # Every offered day is in the future — the bug in one assertion.
    assert all(day > now.date().isoformat() for day in days)


def test_rolling_preserves_weekday_shape_for_evals():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)
    original = _fixture()
    anchored = anchor_availability(original, now=now)

    def weekdays(availability):
        return [
            datetime.fromisoformat(day).weekday()
            for day in sorted(availability["days"])
        ]

    # Friday stays a Friday, Saturday stays a Saturday: a whole-week shift.
    assert weekdays(anchored) == weekdays(original)
    # The no-availability day stays empty, so eval expectations still hold.
    empty_before = [d for d, w in sorted(original["days"].items()) if not w]
    empty_after = [d for d, w in sorted(anchored["days"].items()) if not w]
    assert len(empty_before) == len(empty_after) == 1


def test_window_times_and_timezone_survive_the_shift():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)
    anchored = anchor_availability(_fixture(), now=now)

    windows = load_free_windows(anchored)
    assert windows
    first = min(windows, key=lambda w: w.start)
    assert (first.start.hour, first.start.minute) == (9, 30)
    assert first.start.utcoffset() == timedelta(hours=-5)
    assert anchored["timezone"] == "America/Chicago"


def test_already_future_availability_is_left_alone():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)
    future = _fixture("2026-07-31")  # already ahead of now
    assert anchor_availability(future, now=now)["days"].keys() == future["days"].keys()


def test_digest_states_todays_date_for_the_supervisor():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)
    digest = availability_digest(anchor_availability(_fixture(), now=now), now=now)

    # The model must be able to resolve "Monday"/"tomorrow" against a known
    # today, and notice if it is ever handed contradictory days.
    assert "Monday July 27" in digest
    assert "2026-07-27" in digest
    assert digest.splitlines()[0].lower().startswith("today is")


def test_live_calendar_availability_is_never_rolled():
    # Live snapshots already start at tomorrow; anchoring must be a no-op
    # rather than shifting real calendar data away from the real calendar.
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)
    live = _fixture("2026-07-28")
    assert anchor_availability(live, now=now) == live


@pytest.mark.parametrize("bad", [{}, {"days": {}}, {"timezone": "America/Chicago"}])
def test_degenerate_availability_never_raises(bad):
    now = datetime(2026, 7, 27, 10, 0, tzinfo=CHICAGO)
    anchor_availability(bad, now=now)  # must not raise; scheduling is best-effort


def test_booking_refuses_a_slot_in_the_past():
    """Last-line defense: even if availability data goes stale again, a past
    slot must never reach the calendar write. Turns a silent wrong booking
    into a caught error."""
    from voice.booking import BookingFlow
    from voice.calendar_client import CalendarError

    past = (datetime.now(CHICAGO) - timedelta(days=1)).isoformat()
    with pytest.raises(CalendarError):
        BookingFlow._slot_start({"slot_start": past})

    # A naive (offset-less) past timestamp is caught too — models emit both.
    naive_past = (datetime.now() - timedelta(days=1)).replace(tzinfo=None).isoformat()
    with pytest.raises(CalendarError):
        BookingFlow._slot_start({"slot_start": naive_past})


def test_booking_still_accepts_a_future_slot():
    from voice.booking import BookingFlow

    future = (datetime.now(CHICAGO) + timedelta(days=2)).isoformat()
    assert BookingFlow._slot_start({"slot_start": future})
