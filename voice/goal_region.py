"""Transport-agnostic goal-region runner with deterministic exit validation."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from voice.region_providers import AnthropicProvider, resolve_supervisor

BUSINESS_TIMEZONE = ZoneInfo("America/Chicago")
BUSINESS_START = wall_time(8, 0)
BUSINESS_END = wall_time(18, 0)
DEFAULT_DURATIONS = (30, 60, 120, 240)
_UNPARSEABLE_REPLY = "Sorry — could you say that again?"
_UNPARSEABLE_REJECTION = "supervisor: unparseable output (after retry)"
_EMPTY_REPLY_REJECTION = "supervisor: empty reply — substituted reprompt"
_PREMATURE_CONFIRMATION_REJECTION = (
    "reply: premature confirmation language suppressed"
)
_PREMATURE_CONFIRMATION_RE = re.compile(
    r"\b(?:booked|you['’]re\s+all\s+set|confirmed|scheduled\s+you)\b",
    re.IGNORECASE,
)

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
    escape_on_provider_failure: bool = True
    suppress_premature_confirmation: bool = True


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
        self._model = _runtime_region_model()
        self._supervisor = resolve_supervisor(self._model)
        # Preserve eager Anthropic client construction and the public-ish
        # ``client`` attribute used by the existing fake-client tests.
        if isinstance(self._supervisor, AnthropicProvider):
            if client is not None:
                self._supervisor.client = client
            self.client = self._supervisor.ensure_client()
        else:
            self.client = client
        self._entered_at = clock()
        self._completed_turns = 0
        self._slots: dict = {}
        self._transcript: list[dict[str, str]] = []
        self._consecutive_provider_failures = 0

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
        system = self._system_prompt()
        self._sync_supervisor_model()

        payload = None
        provider_failure: Exception | None = None
        for _attempt in range(2):
            try:
                raw_text, stop_reason = self._supervisor.complete(
                    system=system,
                    messages=messages,
                    schema=_SUPERVISOR_SCHEMA,
                    max_tokens=4096,
                )
            except Exception as exc:
                provider_failure = exc
                continue
            if stop_reason == "max_tokens":
                continue
            try:
                payload = _structured_payload(raw_text)
            except ValueError:
                continue
            break
        supervisor_ms = max(0.0, (self.clock() - started) * 1000)
        if payload is None:
            if provider_failure is None:
                self._consecutive_provider_failures = 0
                rejection = _UNPARSEABLE_REJECTION
            else:
                self._consecutive_provider_failures += 1
                rejection = (
                    "supervisor: provider failure "
                    f"{type(provider_failure).__name__} (after retry)"
                )
            exit_name = None
            if (
                provider_failure is not None
                and self.config.escape_on_provider_failure
                and self._consecutive_provider_failures >= 2
            ):
                exit_name = "escape"
            self._completed_turns += 1
            self._transcript.extend([
                {"role": "user", "content": caller_text},
                {"role": "assistant", "content": _UNPARSEABLE_REPLY},
            ])
            return RegionTurn(
                reply=_UNPARSEABLE_REPLY,
                exit=exit_name,
                slots=dict(self._slots),
                supervisor_ms=supervisor_ms,
                rejected=[rejection],
            )

        self._consecutive_provider_failures = 0
        reply = payload.get("reply") if isinstance(payload.get("reply"), str) else ""
        candidates = payload.get("slot_candidates")
        if not isinstance(candidates, dict):
            candidates = {}

        updates, rejected = self._validate_candidates(candidates)
        self._slots.update(updates)
        exit_name = self._validated_exit(payload.get("exit_candidate"), rejected)
        if (
            exit_name is None
            and self.config.suppress_premature_confirmation
            and _PREMATURE_CONFIRMATION_RE.search(reply)
        ):
            reply = _UNPARSEABLE_REPLY
            rejected.append(_PREMATURE_CONFIRMATION_REJECTION)
        if not reply.strip() and exit_name is None:
            reply = _UNPARSEABLE_REPLY
            rejected.append(_EMPTY_REPLY_REJECTION)

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

    def _sync_supervisor_model(self) -> None:
        """Resolve a changed runtime selection at the start of each LLM turn."""

        model = _runtime_region_model()
        if model == self._model:
            return
        supervisor = resolve_supervisor(model)
        if isinstance(supervisor, AnthropicProvider):
            # Reuse the eagerly-created or injected Anthropic client when a
            # live runner switches back to an Anthropic model.
            if self.client is not None:
                supervisor.client = self.client
            self.client = supervisor.ensure_client()
        self._model = model
        self._supervisor = supervisor

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
                rejected.append(f"job {job}: expected non-empty text")

        start_given = candidates.get("slot_start") is not None
        duration_given = candidates.get("duration_minutes") is not None
        parsed_start: datetime | None = None
        duration: int | None = None

        if start_given:
            parsed_start = _parse_slot_start(candidates.get("slot_start"))
            if parsed_start is None:
                rejected.append(
                    f"slot_start {candidates.get('slot_start')}: "
                    "malformed ISO datetime"
                )

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
                    f"duration_minutes {raw_duration}: expected one of "
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
                rejected.append(
                    f"slot_start {candidates.get('slot_start')}: "
                    "duration_minutes is required"
                )
                return updates, rejected
            interval_error = self._interval_error(parsed_start, effective_duration)
            if interval_error:
                rejected.append(
                    f"slot_start {candidates.get('slot_start')}: {interval_error}"
                )
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
                    rejected.append(
                        f"duration_minutes {candidates.get('duration_minutes')}: "
                        f"{interval_error}"
                    )
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
            containing = next(
                (
                    window
                    for window in self.free_windows
                    if window.start <= start < window.end
                ),
                None,
            )
            if containing is not None:
                fits = int(
                    (containing.end - containing.start).total_seconds() // 60
                )
                return (
                    "interval does not fit entirely inside one free window "
                    f"(that window fits at most {fits}m; this visit needs "
                    f"{duration_minutes}m — choose a window marked as fitting "
                    f"at least {duration_minutes}m)"
                )
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


def _runtime_region_model() -> str:
    # Import lazily because flow_session owns the registry and imports this
    # runner. At turn time both modules are fully initialized.
    from voice.flow_session import get_region_model

    return get_region_model()


def _parse_slot_start(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local_wall_time(parsed)


def _structured_payload(raw_text: str) -> dict:
    payload = json.loads(raw_text)
    if isinstance(payload, dict):
        return payload
    raise ValueError("supervisor response did not contain a JSON object")
