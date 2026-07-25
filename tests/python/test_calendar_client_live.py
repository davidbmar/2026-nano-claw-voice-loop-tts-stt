"""Opt-in smoke test against the dedicated Google Calendar test calendar."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from voice.calendar_client import (
    CalendarClient,
    availability_snapshot,
    load_calendar_settings,
)


LIVE_ENABLED = os.getenv("NANO_CLAW_GCAL_LIVE_TEST", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="set NANO_CLAW_GCAL_LIVE_TEST=1 to use the test calendar",
)


def test_live_insert_blocks_slot_and_delete_cleans_up():
    settings = load_calendar_settings()
    assert settings is not None, "live test requires both NANO_CLAW_GCAL settings"
    client = CalendarClient(settings)

    snapshot = availability_snapshot(client)
    zone = ZoneInfo(settings.timezone)
    earliest = datetime.now(zone).replace(tzinfo=None) + timedelta(minutes=5)
    start = end = None
    for windows in snapshot["days"].values():
        for window in windows:
            window_start = datetime.fromisoformat(window["start"])
            window_end = datetime.fromisoformat(window["end"])
            candidate = max(window_start, earliest)
            if candidate.second or candidate.microsecond:
                candidate = candidate.replace(second=0, microsecond=0) + timedelta(
                    minutes=1
                )
            if candidate + timedelta(minutes=5) <= window_end:
                start = candidate
                end = candidate + timedelta(minutes=5)
                break
        if start is not None:
            break
    if start is None or end is None:
        pytest.skip("the next seven business days contain no five-minute free slot")

    assert client.is_free(start, end) is True
    event_id = client.insert_event(
        summary=f"nano-claw live test {uuid4()}",
        description="Automated nano-claw Calendar REST client smoke test.",
        start=start,
        end=end,
        private_props={"nanoclaw_live_test": "1"},
    )
    try:
        assert client.is_free(start, end) is False
        event = client.get_event(event_id)
        assert event["extendedProperties"]["private"]["nanoclaw_booking"] == "1"
        assert event["extendedProperties"]["private"]["nanoclaw_live_test"] == "1"
    finally:
        client.delete_event(event_id)
