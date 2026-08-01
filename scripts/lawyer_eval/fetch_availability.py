#!/usr/bin/env python3
"""Write the live lawyer-eval calendar snapshot to ``availability.json``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.calendar_client import (
    CalendarClient,
    availability_snapshot,
    load_calendar_settings,
)


HERE = Path(__file__).resolve().parent
OUTPUT_PATH = HERE / "availability.json"


def main() -> int:
    settings = load_calendar_settings()
    if settings is None:
        raise SystemExit(
            "NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON and "
            "NANO_CLAW_GCAL_CALENDAR_ID are required"
        )

    snapshot = availability_snapshot(CalendarClient(settings))
    OUTPUT_PATH.write_text(
        json.dumps(snapshot, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"availability → {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
