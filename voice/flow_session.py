"""Live-session adapter for bounded goal-region voice flows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

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


class FlowModeConfig(TypedDict):
    """One labeled assistant mode exposed by the voice configuration API."""

    label: str
    profile: str
    scheduler: bool


FLOW_MODES: dict[str, FlowModeConfig] = {
    "none": {"label": "None", "profile": "none", "scheduler": False},
    "spacechannel": {
        "label": "Space Channel",
        "profile": "spacechannel",
        "scheduler": False,
    },
    "replicantpm": {
        "label": "Replicant PM",
        "profile": "replicantpm",
        "scheduler": False,
    },
    "scheduler": {
        "label": "Plumber Scheduler",
        # If the scheduler is unavailable or ends, retain today's Space Channel
        # fallback behavior for subsequent normal agent turns.
        "profile": "spacechannel",
        "scheduler": True,
    },
}
DEFAULT_FLOW_MODE = "spacechannel"
_flow_mode: str | None = None
# Keep the provider-verified dropdown registry centralized here. These exact
# IDs were checked against the live provider /v1/models endpoints on 2026-07-17.
REGION_MODELS = {
    "claude-haiku-4-5": "Claude Haiku 4.5 — proven",
    "deepseek/deepseek-v4-flash": "DeepSeek V4 Flash — cheapest",
    "xai/grok-4.20-0309-non-reasoning": "Grok 4.20 fast",
    "xai/grok-4.3": "Grok 4.3",
    "gemini/gemini-2.5-flash-lite": "Gemini Flash-Lite — fastest TTFT",
    "groq/openai/gpt-oss-20b": "GPT-OSS 20B (Groq) — fastest",
    "openrouter/meta-llama/llama-4-scout": "Llama 4 Scout — 11/11",
    "openrouter/openai/gpt-oss-20b:nitro": "GPT-OSS 20B (fastest route)",
    "local/qwen3:14b": "Local Ollama qwen3:14b",
}
DEFAULT_REGION_MODEL = "claude-haiku-4-5"
_region_model: str | None = None
_AVAILABILITY_ERRORS = (
    OSError,
    json.JSONDecodeError,
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    IndexError,
)


@dataclass
class FlowReply:
    """One flow response, including any deterministic terminal outcome."""

    text: str
    done: bool
    outcome: FlowOutcome | None
    slots: dict
    rejected: list[str] = field(default_factory=list)
    turns_used: int | None = None
    max_turns: int | None = None
    supervisor_ms: float | None = None


def _normalize_flow_mode(mode: str) -> str | None:
    """Return a registered mode, including the legacy ``off`` alias."""

    if mode == "off":
        return DEFAULT_FLOW_MODE
    return mode if mode in FLOW_MODES else None


def get_flow_mode() -> str:
    """Return the runtime flow selection, initialized from the environment."""

    global _flow_mode
    if _flow_mode is None:
        configured = os.environ.get("NANO_CLAW_VOICE_FLOW", "").strip()
        _flow_mode = _normalize_flow_mode(configured) or DEFAULT_FLOW_MODE
    return _flow_mode


def set_flow_mode(mode: str) -> bool:
    """Select a flow for new calls and browser sessions."""

    global _flow_mode
    normalized = _normalize_flow_mode(mode) if isinstance(mode, str) else None
    if normalized is None:
        return False
    _flow_mode = normalized
    log.info("Voice flow switched to %s (applies to new sessions/calls)", normalized)
    return True


def get_flow_profile(mode: str | None = None) -> str:
    """Return the agent profile paired with a runtime assistant mode."""

    active = get_flow_mode() if mode is None else _normalize_flow_mode(mode)
    if active is None:
        active = DEFAULT_FLOW_MODE
    return FLOW_MODES[active]["profile"]


def get_region_model() -> str:
    """Return the runtime scheduler model or its environment/default fallback."""

    if _region_model is not None:
        return _region_model
    configured = os.environ.get("SCHED_EVAL_MODEL", "").strip()
    return configured or DEFAULT_REGION_MODEL


def set_region_model(name: str) -> bool:
    """Select the supervisor model used when the next scheduler turn starts."""

    global _region_model
    if not isinstance(name, str) or name not in REGION_MODELS:
        return False
    _region_model = name
    log.info("Scheduler model switched to %s (applies to new turns)", name)
    return True


def scheduler_flow_enabled() -> bool:
    """Compatibility helper for callers that only need a boolean check."""

    return FLOW_MODES[get_flow_mode()]["scheduler"]


def scheduler_region_config(digest: str) -> RegionConfig:
    """Return the scheduler configuration shared by live voice and evals."""

    return RegionConfig(
        goal=(
            "Book one plumbing appointment that satisfies the caller and fits "
            "the grounded availability. Never shorten the requested duration."
        ),
        persona=(
            "You are a concise, warm plumbing scheduler. Offer concrete available "
            "times, clarify constraints, and never claim a time outside the digest. "
            "Keep every reply to one or two short spoken sentences; offer at most "
            "two candidate times per turn. When a requested duration does not fit "
            "any window on a day, say so plainly and offer the nearest other day "
            "whose window fits it; never keep proposing a day that cannot fit the "
            "duration."
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


def _render_availability_window(window: dict) -> str:
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    capacity_minutes = int((end - start).total_seconds() // 60)
    return f"{start:%H:%M}–{end:%H:%M} (fits ≤{capacity_minutes}m)"


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
            _render_availability_window(window)
            for window in windows
        )
        lines.append(f"- {label} ({day}): {rendered or 'no availability'}")
    return "\n".join(lines)


class FlowSession:
    """Async voice-facing wrapper around the synchronous goal-region runner."""

    greeting = SCHEDULER_GREETING

    def __init__(self, runner: GoalRegionRunner) -> None:
        self._runner = runner
        self._turn_lock = asyncio.Lock()
        self._inflight_turn: asyncio.Future | None = None

    @property
    def goal(self) -> str:
        return str(getattr(getattr(self._runner, "config", None), "goal", ""))

    @property
    def slots(self) -> dict:
        return dict(getattr(self._runner, "slots", {}) or {})

    @property
    def turns_used(self) -> int:
        return int(getattr(self._runner, "turns_used", 0))

    @property
    def max_turns(self) -> int:
        return int(getattr(self._runner, "max_turns", 0))

    @classmethod
    def availability_ok(cls) -> bool:
        """Check scheduler availability without constructing or caching a runner."""

        try:
            _load_scheduler_inputs()
        except _AVAILABILITY_ERRORS:
            return False
        return True

    @classmethod
    def create(cls, *, client=None) -> FlowSession | None:
        """Build a scheduler session, or return None when it cannot be loaded."""

        try:
            path, config, windows = _load_scheduler_inputs()
        except _AVAILABILITY_ERRORS as exc:
            path = _availability_path()
            log.error("Scheduler flow unavailable; cannot load %s: %s", path, exc)
            return None

        try:
            return cls(GoalRegionRunner(config, windows, client=client))
        except Exception as exc:
            log.error("Scheduler flow unavailable; cannot initialize runner: %s", exc)
            return None

    async def reply(self, caller_text: str) -> FlowReply:
        """Run one blocking supervisor turn without blocking the event loop."""

        async with self._turn_lock:
            await self._discard_orphaned_turn()
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, self._runner.turn, caller_text)
            self._inflight_turn = future
            try:
                # Shield keeps the executor future awaitable after browser
                # barge-in cancels this request task; the worker keeps mutating
                # the runner until the next serialized caller drains it.
                turn = await asyncio.shield(future)
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._inflight_turn = None
                raise
            else:
                self._inflight_turn = None

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
        return FlowReply(
            text=text,
            done=outcome is not None,
            outcome=outcome,
            slots=slots,
            rejected=list(turn.rejected),
            turns_used=getattr(self._runner, "turns_used", None),
            max_turns=getattr(self._runner, "max_turns", None),
            supervisor_ms=turn.supervisor_ms,
        )

    async def _discard_orphaned_turn(self) -> None:
        future = self._inflight_turn
        if future is None:
            return
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            log.error("Discarded scheduler flow turn was cancelled")
        except Exception:
            log.exception("Discarded scheduler flow turn failed")
        finally:
            if future.done() and self._inflight_turn is future:
                self._inflight_turn = None


def _availability_path() -> Path:
    configured = os.environ.get("NANO_CLAW_FLOW_AVAILABILITY", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_AVAILABILITY_PATH
    return path if path.is_absolute() else REPO_ROOT / path


def _load_scheduler_inputs() -> tuple[Path, RegionConfig, list[FreeWindow]]:
    path = _availability_path()
    availability = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(availability, dict):
        raise ValueError("availability must be a JSON object")
    windows = load_free_windows(availability)
    config = scheduler_region_config(availability_digest(availability))
    return path, config, windows


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
