"""Transport-agnostic goal-region runner with deterministic exit validation.

``RegionConfig.slots`` is a small declarative language.  Every entry has a
``type`` and may set ``required`` (default true):

* ``text`` is a non-empty string.
* ``datetime`` is an ISO datetime; the appointment start slot is conventionally
  named ``slot_start``.
* ``minutes`` is a positive integer selected from ``values`` (``allowed`` is
  retained as a legacy alias).
* ``enum`` is a string selected from ``values``.
* ``derived_minutes`` is never nominated by the model.  Its ``from`` key names
  an enum slot and its ``map`` maps each enum value to a duration in minutes.

Derived slots are omitted from the supervisor schema and populated
deterministically when their source enum validates.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from voice.region_providers import AnthropicProvider, resolve_supervisor

log = logging.getLogger("nano-claw.region")

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


def _slot_type(name: str, spec: dict) -> str:
    """Return a declared slot type, with compatibility for legacy configs."""

    declared = spec.get("type")
    if isinstance(declared, str):
        return declared
    if name == "slot_start":
        return "datetime"
    if name == "duration_minutes":
        return "minutes"
    return "text"


def build_supervisor_schema(slots: dict) -> dict:
    """Build the strict structured-output contract for a slot configuration."""

    candidate_properties: dict[str, dict] = {}
    for name, spec in slots.items():
        slot_type = _slot_type(name, spec)
        if slot_type == "derived_minutes":
            continue
        if slot_type in {"text", "datetime"}:
            candidate_properties[name] = {"type": ["string", "null"]}
        elif slot_type == "minutes":
            candidate_properties[name] = {"type": ["integer", "null"]}
        elif slot_type == "enum":
            candidate_properties[name] = {
                # Structured outputs reject enum paired with a union type.
                "anyOf": [
                    {"type": "string", "enum": list(spec.get("values", ()))},
                    {"type": "null"},
                ]
            }
        else:
            raise ValueError(f"unsupported slot type for {name}: {slot_type}")

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {"type": "string"},
            "slot_candidates": {
                "type": "object",
                "additionalProperties": False,
                "properties": candidate_properties,
                "required": list(candidate_properties),
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


_SUPERVISOR_SCHEMA = build_supervisor_schema(
    {
        "job": {"type": "text", "required": True},
        "slot_start": {"type": "datetime", "required": True},
        "duration_minutes": {
            "type": "minutes",
            "values": [30, 60, 120, 240],
            "required": True,
        },
    }
)


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
        self._schema = build_supervisor_schema(config.slots)
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
        self._grace_turns = 0

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

    def remove_free_window_overlap(self, start: datetime, end: datetime) -> None:
        """Remove the half-open interval from free windows, clipping as needed."""

        start = _local_wall_time(start)
        end = _local_wall_time(end)
        if end <= start:
            return

        remaining: list[FreeWindow] = []
        for window in self.free_windows:
            if end <= window.start or start >= window.end:
                remaining.append(window)
                continue
            if window.start < start:
                remaining.append(FreeWindow(window.start, start))
            if end < window.end:
                remaining.append(FreeWindow(end, window.end))
        self.free_windows = remaining

    def clear_slot(self, name: str) -> None:
        """Drop a validated slot, if present."""

        self._slots.pop(name, None)

    def grant_grace_turn(self) -> None:
        """Exempt the next turn from budget/deadline enforcement.

        A caller answering the scripted booking confirmation must never be
        preempted by the session budget: the budget bounds open-ended
        negotiation, not an imminent commit.
        """

        self._grace_turns += 1

    def turn(self, caller_text: str) -> RegionTurn:
        if self._matches_escape(caller_text):
            return self._short_circuit("escape")
        if (
            self.clock() - self._entered_at >= self.config.deadline_s
            or self._completed_turns >= self.config.max_turns
        ):
            if self._grace_turns > 0:
                self._grace_turns -= 1
            else:
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
                    schema=self._schema,
                    max_tokens=4096,
                )
            except Exception as exc:
                # Kept for outcome handling below, but log per attempt too:
                # which provider call failed and why is exactly what booking
                # post-mortems need and never had.
                provider_failure = exc
                log.warning(
                    "Supervisor attempt %d/2 provider failure: %s: %s",
                    _attempt + 1, type(exc).__name__, exc,
                )
                continue
            if stop_reason == "max_tokens":
                log.warning(
                    "Supervisor attempt %d/2 hit max_tokens; retrying", _attempt + 1
                )
                continue
            try:
                payload = _structured_payload(raw_text)
            except ValueError:
                log.warning(
                    "Supervisor attempt %d/2 returned unparseable payload: %.120s",
                    _attempt + 1, raw_text,
                )
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

        slot_specs = self.config.slots
        derived_items = [
            (name, spec)
            for name, spec in slot_specs.items()
            if _slot_type(name, spec) == "derived_minutes"
        ]
        derived_name, derived_spec = derived_items[0] if derived_items else (None, None)
        derived_source = (
            derived_spec.get("from") if derived_spec is not None else None
        )

        datetime_names = [
            name
            for name, spec in slot_specs.items()
            if _slot_type(name, spec) == "datetime"
        ]
        start_name = (
            "slot_start"
            if "slot_start" in datetime_names
            else (datetime_names[0] if datetime_names else None)
        )
        minutes_names = [
            name
            for name, spec in slot_specs.items()
            if _slot_type(name, spec) == "minutes"
        ]
        direct_duration_name = (
            "duration_minutes"
            if "duration_minutes" in minutes_names
            else (minutes_names[0] if minutes_names else None)
        )
        duration_name = derived_name or direct_duration_name

        given: set[str] = set()
        invalid: set[str] = set()
        validated: dict = {}
        for name, spec in slot_specs.items():
            slot_type = _slot_type(name, spec)
            if slot_type == "derived_minutes":
                continue
            raw_value = candidates.get(name)
            if raw_value is None:
                continue
            given.add(name)

            if slot_type == "text":
                if isinstance(raw_value, str) and raw_value.strip():
                    validated[name] = raw_value.strip()
                else:
                    invalid.add(name)
                    rejected.append(f"{name} {raw_value}: expected non-empty text")
            elif slot_type == "enum":
                values = list(spec.get("values", ()))
                if isinstance(raw_value, str) and raw_value in values:
                    validated[name] = raw_value
                else:
                    invalid.add(name)
                    rejected.append(
                        f"{name} {raw_value}: expected one of "
                        + ", ".join(str(value) for value in values)
                    )
            elif slot_type == "datetime":
                parsed = _parse_slot_start(raw_value)
                if parsed is None:
                    invalid.add(name)
                    rejected.append(
                        f"{name} {raw_value}: malformed ISO datetime"
                    )
                else:
                    validated[name] = parsed
            elif slot_type == "minutes":
                values = spec.get(
                    "values", spec.get("allowed", DEFAULT_DURATIONS)
                )
                allowed = {
                    value
                    for value in values
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                }
                if (
                    isinstance(raw_value, int)
                    and not isinstance(raw_value, bool)
                    and raw_value in allowed
                ):
                    validated[name] = raw_value
                else:
                    invalid.add(name)
                    rejected.append(
                        f"{name} {raw_value}: expected one of "
                        + ", ".join(str(value) for value in sorted(allowed))
                    )

        derived_duration: int | None = None
        if (
            isinstance(derived_source, str)
            and derived_source in validated
            and derived_spec is not None
        ):
            duration_map = derived_spec.get("map", {})
            derived_duration = duration_map.get(validated[derived_source])
            if (
                not isinstance(derived_duration, int)
                or isinstance(derived_duration, bool)
                or derived_duration <= 0
            ):
                invalid.add(derived_source)
                rejected.append(
                    f"{derived_source} {candidates.get(derived_source)}: "
                    "no derived duration configured"
                )
                derived_duration = None

        appointment_names = {
            name
            for name in (
                start_name,
                duration_name,
                direct_duration_name,
                derived_source,
            )
            if isinstance(name, str)
        }
        for name, value in validated.items():
            if name in appointment_names:
                continue
            updates[name] = value.isoformat() if isinstance(value, datetime) else value

        start_given = start_name in given if start_name is not None else False
        duration_given = (
            direct_duration_name in given
            if direct_duration_name is not None
            else False
        )
        source_given = (
            derived_source in given if isinstance(derived_source, str) else False
        )

        # A nominated appointment is atomic: a bad start must not leave a new
        # duration or derived service paired with an older time (or vice versa).
        if start_given:
            if (
                start_name in invalid
                or (
                    derived_name is not None
                    and source_given
                    and derived_source in invalid
                )
                or (
                    derived_name is None
                    and duration_given
                    and direct_duration_name in invalid
                )
            ):
                return updates, rejected

            parsed_start = validated[start_name]
            if derived_name is not None:
                # A fresh deterministic derivation wins.  Otherwise retain the
                # held derived value before considering a legacy minutes slot.
                effective_duration = derived_duration
                if effective_duration is None:
                    effective_duration = self._slots.get(derived_name)
                if (
                    effective_duration is None
                    and direct_duration_name is not None
                ):
                    effective_duration = validated.get(direct_duration_name)
            else:
                # Preserve the plumber behavior: a nominated duration replaces
                # a held duration when the start and duration arrive together.
                effective_duration = validated.get(direct_duration_name)
                if effective_duration is None and direct_duration_name is not None:
                    effective_duration = self._slots.get(direct_duration_name)

            if effective_duration is None:
                required_name = (
                    derived_source
                    if derived_name is not None
                    and isinstance(derived_source, str)
                    else (duration_name or "duration_minutes")
                )
                rejected.append(
                    f"{start_name} {candidates.get(start_name)}: "
                    f"{required_name} is required"
                )
                return updates, rejected
            interval_error = self._interval_error(parsed_start, effective_duration)
            if interval_error:
                rejected.append(
                    f"{start_name} {candidates.get(start_name)}: {interval_error}"
                )
                return updates, rejected

            updates[start_name] = parsed_start.isoformat()
            if (
                derived_duration is not None
                and isinstance(derived_source, str)
                and derived_name is not None
            ):
                updates[derived_source] = validated[derived_source]
                updates[derived_name] = derived_duration
            elif (
                derived_name is None
                and duration_given
                and direct_duration_name in validated
            ):
                updates[direct_duration_name] = validated[direct_duration_name]
            return updates, rejected

        if derived_name is not None and source_given:
            if derived_source in invalid or derived_duration is None:
                return updates, rejected
            existing_start = (
                _parse_slot_start(self._slots.get(start_name))
                if start_name is not None
                else None
            )
            if existing_start is not None:
                interval_error = self._interval_error(
                    existing_start, derived_duration
                )
                if interval_error:
                    rejected.append(
                        f"{derived_source} {candidates.get(derived_source)}: "
                        f"{interval_error}; propose a new time for this service"
                    )
                    return updates, rejected
            updates[derived_source] = validated[derived_source]
            updates[derived_name] = derived_duration
            return updates, rejected

        if (
            direct_duration_name is not None
            and duration_given
            and direct_duration_name in validated
        ):
            duration = validated[direct_duration_name]
            existing_start = (
                _parse_slot_start(self._slots.get(start_name))
                if start_name is not None
                else None
            )
            if existing_start is not None:
                interval_error = self._interval_error(existing_start, duration)
                if interval_error:
                    rejected.append(
                        f"{direct_duration_name} "
                        f"{candidates.get(direct_duration_name)}: {interval_error}"
                    )
                    return updates, rejected
            updates[direct_duration_name] = duration

        return updates, rejected

    def _allowed_durations(self) -> set[int]:
        spec = next(
            (
                candidate
                for name, candidate in self.config.slots.items()
                if _slot_type(name, candidate) == "derived_minutes"
            ),
            None,
        )
        if spec is not None:
            duration_map = spec.get("map", {})
            values = duration_map.values() if isinstance(duration_map, dict) else ()
        else:
            spec = next(
                (
                    candidate
                    for name, candidate in self.config.slots.items()
                    if _slot_type(name, candidate) == "minutes"
                ),
                {},
            )
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
        return None  # health-ok: pure parse guard; callers validate the None
    return _local_wall_time(parsed)


def _structured_payload(raw_text: str) -> dict:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = json.loads(_strip_json_fences(raw_text))
    if isinstance(payload, dict):
        return payload
    raise ValueError("supervisor response did not contain a JSON object")


def _strip_json_fences(text: str) -> str:
    """Unwrap markdown-fenced JSON (```json ... ```).

    Small local models habitually fence structured output even under
    response_format json_object; the payload inside is typically valid
    (measured on riff's eval: gemma4:e2b fenced ~93% of turns, every one
    containing parseable JSON). Only consulted after a strict parse fails,
    so well-behaved output never takes this path. Mirrors riff's
    goal_region fix — keep the two in sync."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    stripped = stripped[3:]
    if stripped[:4].lower() == "json":
        stripped = stripped[4:]
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()[:-3]
    return stripped.strip()
