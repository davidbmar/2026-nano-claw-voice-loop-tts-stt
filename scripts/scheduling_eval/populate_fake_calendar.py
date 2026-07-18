#!/usr/bin/env python3
"""Populate the eval week with FAKE plumber appointments on Google Calendar.

The scheduling eval (hybrid goal-region test) needs a calendar whose free/busy
structure is KNOWN ground truth: each job duration (30m / 1h / 2h / 4h) must
have days where it fits easily, days where it barely fits, and days where it
cannot fit. This script builds that week and writes the expected availability
to ground_truth.json so eval runs can be scored mechanically.

Credentials come from riff's .env (GOOGLE_SERVICE_ACCOUNT_JSON path +
DEFAULT_CALENDAR_ID) — the same service account riff's cal-provider uses.
Run with riff's venv, which has the Google client installed:

    ~/src/riff/.venv/bin/python scripts/scheduling_eval/populate_fake_calendar.py            # dry-run
    ~/src/riff/.venv/bin/python scripts/scheduling_eval/populate_fake_calendar.py --apply    # create events
    ~/src/riff/.venv/bin/python scripts/scheduling_eval/populate_fake_calendar.py --cleanup  # delete them (needs --apply)

Every event: summary prefixed "FAKE — ", description marks it as eval residue,
and a private extended property nanoclaw_eval=1 (cleanup matches the marker,
never the title, so a real event named FAKE-anything is untouchable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta

TZ = "America/Chicago"
MARKER = {"nanoclaw_eval": "1"}
DAY_START = time(8, 0)
DAY_END = time(18, 0)

# (day_offset_from_tomorrow, label, [(start_hhmm, end_hhmm, job_title)])
# Gap design (8:00–18:00 frame):
#   day 0 fragmented  → only 30 min gaps        (30m ok; 1h/2h/4h impossible)
#   day 1+2 weekend   → fully blocked            (nothing fits)
#   day 3 medium      → 1h, 1h, 2h gaps          (2h barely; 4h impossible)
#   day 4 open pm     → 30m gap + 7.5h afternoon (everything fits)
#   day 5 exact 4h    → one 8:00–12:00 window    (4h exact-fit boundary)
#   day 6 traps       → 30m, 30m, 15m gaps       (15m gap must never be offered)
WEEK_PLAN = [
    (0, "fragmented", [
        ("08:00", "09:30", "water heater flush (Alvarez)"),
        ("10:00", "11:30", "garbage disposal swap (Chen)"),
        ("12:00", "13:30", "kitchen faucet leak (Okafor)"),
        ("14:00", "15:30", "toilet reseat (Braun)"),
        ("16:00", "17:30", "shower valve repair (Dietz)"),
    ]),
    (1, "blocked", [("08:00", "18:00", "out of service area — Waco job")]),
    (2, "blocked", [("08:00", "18:00", "off — family day")]),
    (3, "medium", [
        ("08:00", "10:00", "sump pump install (Reyes)"),
        ("11:00", "12:00", "inspection walkthrough (HOA)"),
        ("13:00", "16:00", "repipe estimate + start (Nguyen)"),
    ]),
    (4, "open-pm", [
        ("08:00", "09:00", "supply pickup (Ferguson)"),
        ("09:30", "10:30", "drain snake (Patel)"),
    ]),
    (5, "exact-4h", [("12:00", "18:00", "tankless conversion (Marsh)")]),
    (6, "traps", [
        ("08:00", "11:45", "slab leak detection (Irwin)"),
        ("12:15", "15:00", "bathroom rough-in (Solis)"),
        ("15:30", "17:45", "water softener install (Grant)"),
    ]),
]


def load_riff_env() -> tuple[str, str]:
    env_path = os.path.expanduser("~/src/riff/.env")
    sa_path = cal_id = ""
    for line in open(env_path):
        line = line.strip()
        if line.startswith("GOOGLE_SERVICE_ACCOUNT_JSON="):
            sa_path = os.path.expanduser(line.split("=", 1)[1].strip())
        elif line.startswith("DEFAULT_CALENDAR_ID="):
            cal_id = line.split("=", 1)[1].strip()
    if not sa_path or not cal_id:
        sys.exit("riff/.env missing GOOGLE_SERVICE_ACCOUNT_JSON or DEFAULT_CALENDAR_ID")
    return sa_path, cal_id


def service(sa_path: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def week_events(base: date) -> list[dict]:
    events = []
    for offset, label, blocks in WEEK_PLAN:
        day = base + timedelta(days=offset)
        for start_s, end_s, title in blocks:
            events.append({
                "summary": f"FAKE — {title}",
                "description": (
                    "nano-claw hybrid scheduling eval fixture — safe to delete. "
                    f"day-profile: {label}"
                ),
                "start": {"dateTime": f"{day}T{start_s}:00", "timeZone": TZ},
                "end": {"dateTime": f"{day}T{end_s}:00", "timeZone": TZ},
                "extendedProperties": {"private": dict(MARKER)},
            })
    return events


def expected_gaps(base: date) -> dict:
    """Ground truth: the free windows the busy blocks leave in the day frame."""
    out = {}
    for offset, label, blocks in WEEK_PLAN:
        day = base + timedelta(days=offset)
        cursor = datetime.combine(day, DAY_START)
        gaps = []
        for start_s, end_s, _ in blocks:
            b_start = datetime.combine(day, time.fromisoformat(start_s))
            if b_start > cursor:
                gaps.append((cursor, b_start))
            cursor = max(cursor, datetime.combine(day, time.fromisoformat(end_s)))
        day_end = datetime.combine(day, DAY_END)
        if cursor < day_end:
            gaps.append((cursor, day_end))
        out[str(day)] = {
            "profile": label,
            "free_windows": [
                {"start": g0.isoformat(), "end": g1.isoformat(),
                 "minutes": int((g1 - g0).total_seconds() // 60)}
                for g0, g1 in gaps
            ],
        }
    return out


def cleanup(svc, cal_id: str, apply: bool) -> None:
    """Delete every event carrying the eval marker (past 30d → future 30d)."""
    now = datetime.now()
    resp = svc.events().list(
        calendarId=cal_id,
        privateExtendedProperty="nanoclaw_eval=1",
        timeMin=(now - timedelta(days=30)).isoformat() + "Z",
        timeMax=(now + timedelta(days=30)).isoformat() + "Z",
        singleEvents=True, maxResults=250,
    ).execute()
    items = resp.get("items", [])
    print(f"{len(items)} marked eval events found")
    for ev in items:
        line = f"  {ev['start'].get('dateTime', '?')}  {ev.get('summary', '?')}"
        if apply:
            svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
            print(line, "— deleted")
        else:
            print(line, "— would delete (dry-run; pass --apply)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="actually write to the calendar")
    p.add_argument("--cleanup", action="store_true", help="delete marked eval events instead")
    p.add_argument("--start", help="week start date YYYY-MM-DD (default: tomorrow)")
    args = p.parse_args()

    sa_path, cal_id = load_riff_env()
    svc = service(sa_path)
    print(f"calendar: {cal_id}")

    if args.cleanup:
        cleanup(svc, cal_id, args.apply)
        return

    base = date.fromisoformat(args.start) if args.start else date.today() + timedelta(days=1)
    events = week_events(base)
    truth = expected_gaps(base)

    truth_path = os.path.join(os.path.dirname(__file__), "ground_truth.json")
    with open(truth_path, "w") as f:
        json.dump({"timezone": TZ, "week_start": str(base), "days": truth}, f, indent=2)
    print(f"ground truth → {truth_path}")

    for ev in events:
        line = f"  {ev['start']['dateTime']} → {ev['end']['dateTime']}  {ev['summary']}"
        if args.apply:
            svc.events().insert(calendarId=cal_id, body=ev).execute()
            print(line, "— created")
        else:
            print(line, "— would create (dry-run; pass --apply)")


if __name__ == "__main__":
    main()
