#!/usr/bin/env python3
"""Fetch the eval week's Google Calendar free windows.

Run with riff's environment, which owns the Google client and credentials:

    ~/src/riff/.venv/bin/python scripts/scheduling_eval/fetch_availability.py

This adapter is intentionally the only eval component that reads Google.
The goal-region engine and evaluator consume availability.json as plain data.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .populate_fake_calendar import TZ, load_riff_env, service
except ImportError:  # direct script execution adds this directory to sys.path
    from populate_fake_calendar import TZ, load_riff_env, service

HERE = Path(__file__).resolve().parent
GROUND_TRUTH_PATH = HERE / "ground_truth.json"
OUTPUT_PATH = HERE / "availability.json"
DAY_START = time(8, 0)
DAY_END = time(18, 0)


def _parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _free_windows(busy: list[dict], week_start: date, timezone: str) -> dict:
    zone = ZoneInfo(timezone)
    intervals = [
        (
            _parse_rfc3339(block["start"]).astimezone(zone),
            _parse_rfc3339(block["end"]).astimezone(zone),
        )
        for block in busy
    ]
    days = {}
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        frame_start = datetime.combine(day, DAY_START, tzinfo=zone)
        frame_end = datetime.combine(day, DAY_END, tzinfo=zone)
        clipped = sorted(
            (
                (max(start, frame_start), min(end, frame_end))
                for start, end in intervals
                if start < frame_end and end > frame_start
            ),
            key=lambda pair: pair[0],
        )

        merged: list[tuple[datetime, datetime]] = []
        for start, end in clipped:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            elif end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)

        cursor = frame_start
        windows = []
        for start, end in merged:
            if start > cursor:
                windows.append({
                    "start": cursor.replace(tzinfo=None).isoformat(),
                    "end": start.replace(tzinfo=None).isoformat(),
                })
            cursor = max(cursor, end)
        if cursor < frame_end:
            windows.append({
                "start": cursor.replace(tzinfo=None).isoformat(),
                "end": frame_end.replace(tzinfo=None).isoformat(),
            })
        days[str(day)] = windows
    return days


def main() -> None:
    truth = json.loads(GROUND_TRUTH_PATH.read_text())
    timezone = truth.get("timezone", TZ)
    week_start = date.fromisoformat(truth["week_start"])
    zone = ZoneInfo(timezone)
    range_start = datetime.combine(week_start, time.min, tzinfo=zone)
    range_end = range_start + timedelta(days=7)

    sa_path, calendar_id = load_riff_env()
    calendar = service(sa_path)
    response = calendar.freebusy().query(body={
        "timeMin": range_start.isoformat(),
        "timeMax": range_end.isoformat(),
        "timeZone": timezone,
        "items": [{"id": calendar_id}],
    }).execute()
    calendar_result = response.get("calendars", {}).get(calendar_id, {})
    if calendar_result.get("errors"):
        raise RuntimeError(f"Google freebusy failed: {calendar_result['errors']}")

    output = {
        "timezone": timezone,
        "days": _free_windows(calendar_result.get("busy", []), week_start, timezone),
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2) + "\n")
    print(f"availability → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
