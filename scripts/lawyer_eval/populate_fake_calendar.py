#!/usr/bin/env python3
"""Populate a known lawyer-eval week on the nano-claw test calendar.

The default dry run writes ``ground_truth.json`` and prints the events without
contacting Google.  ``--apply`` creates them using nano-claw's
``voice.calendar_client`` configuration:

* ``NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON``
* ``NANO_CLAW_GCAL_CALENDAR_ID``

Every fixture event carries the private ``nanoclaw_lawyer_eval=1`` marker.
Cleanup matches private markers rather than titles, and ``--cleanup --apply``
also removes events carrying ``nanoclaw_booking=1`` so bookings made by live
eval and integration runs are purged mechanically.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.calendar_client import (
    CalendarClient,
    CalendarError,
    load_calendar_settings,
)


HERE = Path(__file__).resolve().parent
GROUND_TRUTH_PATH = HERE / "ground_truth.json"
TZ = "America/Chicago"
MARKER = {"nanoclaw_lawyer_eval": "1"}
BOOKING_MARKER = {"nanoclaw_booking": "1"}
DAY_START = time(8, 0)
DAY_END = time(18, 0)

# (day_offset_from_tomorrow, profile, [(start, end, fixture title)])
#
# Free-window design inside the 08:00-18:00 frame:
#   day 0: one exact 45m trap (only the 30m follow-up fits)
#   day 1: fully blocked
#   day 2: 90m / 60m / 90m fragments (120m cannot fit)
#   day 3: one exact 120m boundary window
#   day 4: open afternoon, including 14:00
#   day 5: one 60m window plus an open afternoon
#   day 6: fully open
WEEK_PLAN = [
    (
        0,
        "forty-five-minute-trap",
        [
            ("08:00", "09:00", "morning case review"),
            ("09:45", "18:00", "depositions and client meetings"),
        ],
    ),
    (
        1,
        "fully-blocked",
        [("08:00", "18:00", "all-day trial")],
    ),
    (
        2,
        "no-120-minute-fit",
        [
            ("08:00", "09:30", "court appearance"),
            ("11:00", "12:00", "estate planning meeting"),
            ("13:00", "14:30", "mediation preparation"),
            ("16:00", "18:00", "mediation"),
        ],
    ),
    (
        3,
        "exact-120-minute-window",
        [
            ("08:00", "10:00", "motion hearing"),
            ("12:00", "18:00", "arbitration"),
        ],
    ),
    (
        4,
        "open-afternoon",
        [("08:00", "13:00", "morning docket and lunch")],
    ),
    (
        5,
        "mixed-openings",
        [
            ("08:00", "10:00", "document review"),
            ("11:00", "13:00", "client conference"),
        ],
    ),
    (6, "fully-open", []),
]


def week_events(base: date) -> list[dict[str, Any]]:
    """Build Google-shaped fixture events for the seven-day eval week."""

    events: list[dict[str, Any]] = []
    for offset, profile, blocks in WEEK_PLAN:
        day = base + timedelta(days=offset)
        for start_text, end_text, title in blocks:
            events.append(
                {
                    "summary": f"FAKE — {title}",
                    "description": (
                        "nano-claw lawyer scheduling eval fixture — safe to delete. "
                        f"day-profile: {profile}"
                    ),
                    "start": {
                        "dateTime": f"{day}T{start_text}:00",
                        "timeZone": TZ,
                    },
                    "end": {
                        "dateTime": f"{day}T{end_text}:00",
                        "timeZone": TZ,
                    },
                    "extendedProperties": {"private": dict(MARKER)},
                }
            )
    return events


def expected_gaps(base: date) -> dict[str, dict[str, Any]]:
    """Return the authoritative free windows left by ``WEEK_PLAN``."""

    days: dict[str, dict[str, Any]] = {}
    for offset, profile, blocks in WEEK_PLAN:
        day = base + timedelta(days=offset)
        cursor = datetime.combine(day, DAY_START)
        gaps: list[tuple[datetime, datetime]] = []
        for start_text, end_text, _title in blocks:
            block_start = datetime.combine(day, time.fromisoformat(start_text))
            block_end = datetime.combine(day, time.fromisoformat(end_text))
            if block_start > cursor:
                gaps.append((cursor, block_start))
            cursor = max(cursor, block_end)
        frame_end = datetime.combine(day, DAY_END)
        if cursor < frame_end:
            gaps.append((cursor, frame_end))
        days[day.isoformat()] = {
            "profile": profile,
            "free_windows": [
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "minutes": int((end - start).total_seconds() // 60),
                }
                for start, end in gaps
            ],
        }
    return days


def ground_truth(base: date) -> dict[str, Any]:
    """Build the complete persisted truth document for one fixture week."""

    return {
        "timezone": TZ,
        "week_start": base.isoformat(),
        "business_hours": {
            "start": DAY_START.isoformat(timespec="minutes"),
            "end": DAY_END.isoformat(timespec="minutes"),
        },
        "days": expected_gaps(base),
    }


def _calendar_from_env() -> CalendarClient:
    settings = load_calendar_settings()
    if settings is None:
        raise SystemExit(
            "NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON and "
            "NANO_CLAW_GCAL_CALENDAR_ID are required for calendar changes"
        )
    return CalendarClient(settings)


def _marker_events(
    client: CalendarClient,
    marker_name: str,
    marker_value: str,
) -> list[dict[str, Any]]:
    """List all events carrying one private marker, following pagination."""

    # CalendarClient intentionally exposes only runtime booking operations.
    # The eval's maintenance-only list query still reuses its sole authorized
    # transport boundary instead of importing Google auth here.
    url = client._events_url()
    page_token: str | None = None
    events: list[dict[str, Any]] = []
    while True:
        params = {
            "privateExtendedProperty": f"{marker_name}={marker_value}",
            "showDeleted": "false",
            "singleEvents": "true",
            "maxResults": "2500",
        }
        if page_token is not None:
            params["pageToken"] = page_token
        payload = client._request_json("get", url, params=params)
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise CalendarError("Google events list response has no items list")
        for event in items:
            if not isinstance(event, dict):
                raise CalendarError("Google events list contains a malformed event")
            events.append(event)
        raw_token = payload.get("nextPageToken")
        if raw_token is None:
            break
        if not isinstance(raw_token, str) or not raw_token:
            raise CalendarError(
                "Google events list returned a malformed nextPageToken"
            )
        page_token = raw_token
    return events


def cleanup(client: CalendarClient, apply: bool) -> None:
    """Delete marked fixtures and nano-claw bookings, or print a dry run."""

    by_id: dict[str, dict[str, Any]] = {}
    for marker in (MARKER, BOOKING_MARKER):
        marker_name, marker_value = next(iter(marker.items()))
        for event in _marker_events(client, marker_name, marker_value):
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                raise CalendarError("marked Google event has no id")
            by_id[event_id] = event

    events = sorted(
        by_id.values(),
        key=lambda event: (
            event.get("start", {}).get("dateTime")
            or event.get("start", {}).get("date")
            or "",
            event.get("id", ""),
        ),
    )
    print(f"{len(events)} marked lawyer-eval/booking events found")
    for event in events:
        start = event.get("start", {})
        when = start.get("dateTime") or start.get("date") or "?"
        summary = event.get("summary", "?")
        line = f"  {when}  {summary}"
        if apply:
            client.delete_event(event["id"])
            print(f"{line} — deleted")
        else:
            print(f"{line} — would delete (dry-run; pass --apply)")


def _insert_fixture(client: CalendarClient, event: dict[str, Any]) -> str:
    return client.insert_event(
        summary=event["summary"],
        description=event["description"],
        start=datetime.fromisoformat(event["start"]["dateTime"]),
        end=datetime.fromisoformat(event["end"]["dateTime"]),
        private_props=dict(MARKER),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually create or delete events on the configured test calendar",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="find marked eval fixtures and nano-claw bookings instead of populating",
    )
    parser.add_argument(
        "--start",
        help="fixture week start date, YYYY-MM-DD (default: tomorrow in Chicago)",
    )
    args = parser.parse_args()

    if args.cleanup:
        cleanup(_calendar_from_env(), args.apply)
        return 0

    base = (
        date.fromisoformat(args.start)
        if args.start
        else datetime.now(ZoneInfo(TZ)).date() + timedelta(days=1)
    )
    events = week_events(base)
    GROUND_TRUTH_PATH.write_text(
        json.dumps(ground_truth(base), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"ground truth → {GROUND_TRUTH_PATH}")

    calendar = _calendar_from_env() if args.apply else None
    if calendar is not None:
        print(f"calendar: {calendar.settings.calendar_id}")
    for event in events:
        line = (
            f"  {event['start']['dateTime']} → {event['end']['dateTime']}  "
            f"{event['summary']}"
        )
        if calendar is None:
            print(f"{line} — would create (dry-run; pass --apply)")
        else:
            event_id = _insert_fixture(calendar, event)
            print(f"{line} — created ({event_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
