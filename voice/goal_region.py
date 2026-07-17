"""Transport-agnostic goal-region runner with deterministic exit validation."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

import anthropic

BUSINESS_TIMEZONE = ZoneInfo("America/Chicago")
BUSINESS_START = wall_time(8, 0)
BUSINESS_END = wall_time(18, 0)
DEFAULT_DURATIONS = (30, 60, 120, 240)
_UNPARSEABLE_REPLY = "Sorry — could you say that again?"
_UNPARSEABLE_REJECTION = "supervisor: unparseable output (after retry)"

_SUPERVISOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "slot_candidates": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job": {"type": ["string", "null"]},
                "slot_start": {"type": ["string", "null"]},
                "duration_minutes": {"type": ["integer", "null"]},
            },
            "required": ["job", "slot_start", "duration_minutes"],
        },
        "exit_candidate": {
            # The structured-outputs validator rejects enum paired with a
            # union type; anyOf is the supported spelling of "booked or null".
            "anyOf": [
                {"type": "string", "enum": ["booked"]},
                {"type": "null"},
            ],
        },
        "evidence": {"type": "string"},
    },
    "required": ["reply", "slot_candidates", "exit_candidate", "evidence"],
}


@dataclass
class FreeWindow:
    """One bookable gap in the local business calendar."""

    start: datetime
    end: datetime


@dataclass
class RegionConfig:
    goal: str
    persona: str
    digest: str
    slots: dict
    escape_phrases: tuple[str, ...]
    max_turns: int
    deadline_s: float


@dataclass
class RegionTurn:
    reply: str
    exit: str | None
    slots: dict
    supervisor_ms: float | None
    rejected: list[str]


class GoalRegionRunner:
    """Run one bounded free-form region behind deterministic validators."""

    def __init__(
        self,
        config: RegionConfig,
        free_windows: list[FreeWindow],
        clock: Callable[[], float] = time.monotonic,
        client=None,
    ) -> None:
        self.config = config
        self.free_windows = sorted(
            (
                FreeWindow(_local_wall_time(window.start), _local_wall_time(window.end))
                for window in free_windows
            ),
            key=lambda window: window.start,
        )
        self.clock = clock
        self.client = client if client is not None else anthropic.Anthropic()
        self._entered_at = clock()
        self._completed_turns = 0
        self._slots: dict = {}
        self._transcript: list[dict[str, str]] = []

    @property
    def slots(self) -> dict:
        return dict(self._slots)

    @property
    def transcript(self) -> list[dict[str, str]]:
        return [dict(message) for message in self._transcript]

    @property
    def turns_used(self) -> int:
        return self._completed_turns

    @property
    def max_turns(self) -> int:
        return self.config.max_turns

    def turn(self, caller_text: str) -> RegionTurn:
        if self._matches_escape(caller_text):
            return self._short_circuit("escape")
        if (
            self.clock() - self._entered_at >= self.config.deadline_s
            or self._completed_turns >= self.config.max_turns
        ):
            return self._short_circuit("budget")

        messages = [*self._transcript, {"role": "user", "content": caller_text}]
        started = self.clock()
        request: dict = {
            "model": os.environ.get("SCHED_EVAL_MODEL", "claude-haiku-4-5"),
            "max_tokens": 4096,
            "system": [{
                "type": "text",
                "text": self._system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": messages,
            "output_config": {
                "format": {"type": "json_schema", "schema": _SUPERVISOR_SCHEMA}
            },
        }
        # Latency knob: Sonnet 5 runs adaptive thinking when the field is
        # omitted; SCHED_EVAL_THINKING=disabled turns it off for a fair
        # per-turn latency comparison. (Omitted = off on Opus 4.8/Haiku 4.5.)
        if os.environ.get("SCHED_EVAL_THINKING", "").strip() == "disabled":
            request["thinking"] = {"type": "disabled"}

        payload = None
        for _attempt in range(2):
            response = self.client.messages.create(**request)
            if _response_stop_reason(response) == "max_tokens":
                continue
            try:
                payload = _structured_payload(response)
            except ValueError:
                continue
            break
        supervisor_ms = max(0.0, (self.clock() - started) * 1000)
        if payload is None:
            self._completed_turns += 1
            self._transcript.extend([
                {"role": "user", "content": caller_text},
                {"role": "assistant", "content": _UNPARSEABLE_REPLY},
            ])
            return RegionTurn(
                reply=_UNPARSEABLE_REPLY,
                exit=None,
                slots=dict(self._slots),
                supervisor_ms=supervisor_ms,
                rejected=[_UNPARSEABLE_REJECTION],
            )

        reply = payload.get("reply") if isinstance(payload.get("reply"), str) else ""
        candidates = payload.get("slot_candidates")
        if not isinstance(candidates, dict):
            candidates = {}

        updates, rejected = self._validate_candidates(candidates)
        self._slots.update(updates)
        exit_name = self._validated_exit(payload.get("exit_candidate"), rejected)

        self._completed_turns += 1
        self._transcript.extend([
            {"role": "user", "content": caller_text},
            {"role": "assistant", "content": reply},
        ])
        return RegionTurn(
            reply="" if exit_name == "booked" else reply,
            exit=exit_name,
            slots=dict(self._slots),
            supervisor_ms=supervisor_ms,
            rejected=rejected,
        )

    def _short_circuit(self, exit_name: str) -> RegionTurn:
        return RegionTurn(
            reply="",
            exit=exit_name,
            slots=dict(self._slots),
            supervisor_ms=None,
            rejected=[],
        )

    def _matches_escape(self, caller_text: str) -> bool:
        for phrase in self.config.escape_phrases:
            if phrase and re.search(
                rf"(?<!\w){re.escape(phrase)}(?!\w)", caller_text, re.IGNORECASE
            ):
                return True
        return False

    def _system_prompt(self) -> str:
        return (
            f"{self.config.persona.strip()}\n\n"
            f"Goal:\n{self.config.goal.strip()}\n\n"
            f"Grounding availability:\n{self.config.digest.strip()}\n\n"
            "For each caller turn, respond naturally and nominate only facts supported "
            "by the conversation. Keep negotiating when a requested time does not fit "
            "the grounding availability. Set exit_candidate to booked only after the "
            "caller has accepted a specific start and duration. Deterministic code will "
            "validate every nomination and makes the final transition decision."
        )

    def _validate_candidates(self, candidates: dict) -> tuple[dict, list[str]]:
        updates: dict = {}
        rejected: list[str] = []

        job = candidates.get("job")
        if job is not None and "job" in self.config.slots:
            if isinstance(job, str) and job.strip():
                updates["job"] = job.strip()
            else:
                rejected.append("job: expected non-empty text")

        start_given = candidates.get("slot_start") is not None
        duration_given = candidates.get("duration_minutes") is not None
        parsed_start: datetime | None = None
        duration: int | None = None

        if start_given:
            parsed_start = _parse_slot_start(candidates.get("slot_start"))
            if parsed_start is None:
                rejected.append("slot_start: malformed ISO datetime")

        if duration_given:
            raw_duration = candidates.get("duration_minutes")
            allowed = self._allowed_durations()
            if (
                isinstance(raw_duration, int)
                and not isinstance(raw_duration, bool)
                and raw_duration in allowed
            ):
                duration = raw_duration
            else:
                rejected.append(
                    "duration_minutes: expected one of "
                    + ", ".join(str(value) for value in sorted(allowed))
                )

        # A nominated appointment is atomic: a bad start must not leave a new
        # duration paired with an older time (or vice versa) on the next turn.
        if start_given:
            if parsed_start is None or (duration_given and duration is None):
                return updates, rejected
            effective_duration = duration
            if effective_duration is None:
                effective_duration = self._slots.get("duration_minutes")
            if effective_duration is None:
                rejected.append("slot_start: duration_minutes is required")
                return updates, rejected
            interval_error = self._interval_error(parsed_start, effective_duration)
            if interval_error:
                rejected.append(f"slot_start: {interval_error}")
                return updates, rejected
            updates["slot_start"] = parsed_start.isoformat()
            if duration_given:
                updates["duration_minutes"] = duration
            return updates, rejected

        if duration_given and duration is not None:
            existing_start = _parse_slot_start(self._slots.get("slot_start"))
            if existing_start is not None:
                interval_error = self._interval_error(existing_start, duration)
                if interval_error:
                    rejected.append(f"duration_minutes: {interval_error}")
                    return updates, rejected
            updates["duration_minutes"] = duration

        return updates, rejected

    def _allowed_durations(self) -> set[int]:
        spec = self.config.slots.get("duration_minutes", {})
        values = spec.get("values", spec.get("allowed", DEFAULT_DURATIONS))
        return {
            value
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }

    def _interval_error(self, start: datetime, duration_minutes: int) -> str | None:
        end = start + timedelta(minutes=duration_minutes)
        if (
            start.date() != end.date()
            or start.time() < BUSINESS_START
            or end.time() > BUSINESS_END
        ):
            return "interval is outside the 08:00–18:00 business frame"
        if not any(
            start >= window.start and end <= window.end
            for window in self.free_windows
        ):
            return "interval does not fit entirely inside one free window"
        return None

    def _validated_exit(self, candidate, rejected: list[str]) -> str | None:
        if candidate is None:
            return None
        if candidate != "booked":
            rejected.append(f"exit_candidate: unsupported value {candidate!r}")
            return None
        if rejected:
            return None
        required = {
            name
            for name, spec in self.config.slots.items()
            if spec.get("required", True)
        }
        missing = sorted(required - self._slots.keys())
        if missing:
            rejected.append("exit_candidate: missing required slots: " + ", ".join(missing))
            return None
        return "booked"


def _local_wall_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)


def _parse_slot_start(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local_wall_time(parsed)


def _structured_payload(response) -> dict:
    content = response.get("content", []) if isinstance(response, dict) else response.content
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
        else:
            text = getattr(block, "text", None)
        if isinstance(text, str):
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
    raise ValueError("supervisor response did not contain a JSON object")


def _response_stop_reason(response) -> str | None:
    if isinstance(response, dict):
        value = response.get("stop_reason")
    else:
        value = getattr(response, "stop_reason", None)
    return value if isinstance(value, str) else None
