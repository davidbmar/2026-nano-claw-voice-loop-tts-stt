"""Live-session adapter for bounded goal-region voice flows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from voice.goal_region import (
    BUSINESS_TIMEZONE,
    FreeWindow,
    GoalRegionRunner,
    RegionConfig,
)

log = logging.getLogger("nano-claw.flow")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AVAILABILITY_PATH = REPO_ROOT / "scripts/scheduling_eval/availability.json"
SCHEDULER_GREETING = (
    "Thanks for calling Lakeside Plumbing. What can I help you schedule?"
)

FlowOutcome = Literal["booked", "escape", "budget"]


@dataclass
class FlowReply:
    """One flow response, including any deterministic terminal outcome."""

    text: str
    done: bool
    outcome: FlowOutcome | None
    slots: dict


def scheduler_flow_enabled() -> bool:
    """Read the scheduler feature flag at the start of a call/session."""

    return os.environ.get("NANO_CLAW_VOICE_FLOW", "").strip() == "scheduler"


def scheduler_region_config(digest: str) -> RegionConfig:
    """Return the scheduler configuration shared by live voice and evals."""

    return RegionConfig(
        goal=(
            "Book one plumbing appointment that satisfies the caller and fits "
            "the grounded availability. Never shorten the requested duration."
        ),
        persona=(
            "You are a concise, warm plumbing scheduler. Offer concrete available "
            "times, clarify constraints, and never claim a time outside the digest."
        ),
        digest=digest,
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
        max_turns=12,
        deadline_s=600,
    )


def load_free_windows(availability: dict) -> list[FreeWindow]:
    """Convert the persisted availability digest to validator windows."""

    return [
        FreeWindow(
            start=datetime.fromisoformat(window["start"]),
            end=datetime.fromisoformat(window["end"]),
        )
        for windows in availability["days"].values()
        for window in windows
    ]


def availability_digest(availability: dict) -> str:
    """Render availability for the goal-region supervisor prompt."""

    lines = [
        f"All times are {availability['timezone']}; business hours are 08:00–18:00.",
        "A visit must fit inside one listed half-open free window:",
    ]
    for day, windows in availability["days"].items():
        parsed_day = datetime.fromisoformat(day)
        label = f"{parsed_day:%A %B} {parsed_day.day}"
        rendered = ", ".join(
            f"{window['start'][11:16]}–{window['end'][11:16]}" for window in windows
        )
        lines.append(f"- {label} ({day}): {rendered or 'no availability'}")
    return "\n".join(lines)


class FlowSession:
    """Async voice-facing wrapper around the synchronous goal-region runner."""

    greeting = SCHEDULER_GREETING

    def __init__(self, runner: GoalRegionRunner) -> None:
        self._runner = runner

    @classmethod
    def create(cls, *, client=None) -> FlowSession | None:
        """Build a scheduler session, or return None when it cannot be loaded."""

        configured = os.environ.get("NANO_CLAW_FLOW_AVAILABILITY", "").strip()
        path = Path(configured).expanduser() if configured else DEFAULT_AVAILABILITY_PATH
        if not path.is_absolute():
            path = REPO_ROOT / path

        try:
            availability = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(availability, dict):
                raise ValueError("availability must be a JSON object")
            windows = load_free_windows(availability)
            config = scheduler_region_config(availability_digest(availability))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.error("Scheduler flow unavailable; cannot load %s: %s", path, exc)
            return None

        try:
            return cls(GoalRegionRunner(config, windows, client=client))
        except Exception as exc:
            log.error("Scheduler flow unavailable; cannot initialize runner: %s", exc)
            return None

    async def reply(self, caller_text: str) -> FlowReply:
        """Run one blocking supervisor turn without blocking the event loop."""

        loop = asyncio.get_running_loop()
        turn = await loop.run_in_executor(None, self._runner.turn, caller_text)
        outcome: FlowOutcome | None = (
            turn.exit if turn.exit in ("booked", "escape", "budget") else None
        )
        slots = dict(turn.slots)
        if outcome == "booked":
            text = (
                f"You're booked: {slots.get('job')} on "
                f"{_spoken_datetime(slots.get('slot_start'))} for "
                f"{slots.get('duration_minutes')} minutes. See you then. Goodbye!"
            )
        elif outcome == "escape":
            text = "Of course — I'm transferring you now. Goodbye!"
        elif outcome == "budget":
            text = "Our scheduler will call you back to finish this up. Goodbye!"
        else:
            text = turn.reply
        return FlowReply(text=text, done=outcome is not None, outcome=outcome, slots=slots)


_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
    21: "twenty first",
    22: "twenty second",
    23: "twenty third",
    24: "twenty fourth",
    25: "twenty fifth",
    26: "twenty sixth",
    27: "twenty seventh",
    28: "twenty eighth",
    29: "twenty ninth",
    30: "thirtieth",
    31: "thirty first",
}


def _spoken_datetime(value) -> str:
    """Render a validated slot time in a form TTS reads naturally."""

    if not isinstance(value, str):
        return "the scheduled date and time"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return "the scheduled date and time"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BUSINESS_TIMEZONE)

    hour = parsed.strftime("%I").lstrip("0")
    minute = f":{parsed.minute:02d}" if parsed.minute else ""
    return (
        f"{parsed:%A %B} {_ORDINALS[parsed.day]} at "
        f"{hour}{minute} {parsed:%p}"
    )
