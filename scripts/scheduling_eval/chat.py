#!/usr/bin/env python3
"""Talk to the scheduling goal region yourself — you are the caller.

Same engine, digest, validators, and budgets as the automated eval; the only
difference is that your keyboard replaces the simulated caller.

    cd ~/src/nano-claw
    set -a; source .env; set +a
    <eval-venv>/bin/python scripts/scheduling_eval/chat.py

Say "operator", "human", or "goodbye" to trigger the deterministic escape.
Booked appointments are printed, not written to the calendar.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # run_eval helpers
sys.path.insert(0, str(HERE.parent.parent))  # repo root for voice.goal_region

from run_eval import (  # noqa: E402
    AVAILABILITY_PATH,
    GREETING,
    _availability_digest,
    _load_windows,
    _truth_slot_valid,
    GROUND_TRUTH_PATH,
)
from voice.flow_session import scheduler_region_config  # noqa: E402
from voice.goal_region import GoalRegionRunner  # noqa: E402


def main() -> None:
    availability = json.loads(AVAILABILITY_PATH.read_text())
    runner = GoalRegionRunner(
        scheduler_region_config(_availability_digest(availability)),
        _load_windows(availability),
    )

    print(f"\nagent: {GREETING}\n(you are the caller — Ctrl-C to quit)\n")
    while True:
        try:
            caller = input("you: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        if not caller:
            continue
        turn = runner.turn(caller)
        if turn.rejected:
            print(f"  [validator rejected: {'; '.join(turn.rejected)}]")
        if turn.supervisor_ms is not None:
            print(f"  [supervisor {turn.supervisor_ms:.0f} ms | slots {turn.slots}]")
        if turn.exit == "booked":
            truth = json.loads(GROUND_TRUTH_PATH.read_text())
            valid = _truth_slot_valid(turn.slots, truth)
            print(
                f"\nagent: You're booked: {turn.slots.get('job')} on "
                f"{turn.slots.get('slot_start')} for "
                f"{turn.slots.get('duration_minutes')} minutes. See you then!"
            )
            print(f"[BOOKED — ground-truth valid: {valid}]")
            return
        if turn.exit == "escape":
            print("\nagent: Of course — transferring you to a person now.")
            print("[ESCAPE — deterministic, no LLM call]")
            return
        if turn.exit == "budget":
            print("\nagent: Let me have our scheduler call you back to finish this up.")
            print("[BUDGET — turn/deadline limit hit]")
            return
        print(f"agent: {turn.reply}\n")


if __name__ == "__main__":
    main()
