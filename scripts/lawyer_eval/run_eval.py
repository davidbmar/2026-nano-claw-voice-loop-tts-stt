#!/usr/bin/env python3
"""Run the lawyer BookingFlow evaluation offline, or opt in to live writes.

Offline mode uses ``ground_truth.json`` for both the negotiation digest and a
``FakeCommitCalendar``.  It never loads Google Calendar settings or performs
calendar I/O.  Supervisor and caller-simulator model calls still require the
provider keys selected by ``SCHED_EVAL_MODEL`` and
``SCHED_EVAL_CALLER_MODEL``.

``--live`` is the only path that constructs ``CalendarClient``.  It writes to
the configured test calendar and verifies every returned event id with
``events.get``; created bookings remain marked for the populate script's
cleanup command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.booking import BookingFlow
from voice.calendar_client import (
    CalendarClient,
    CalendarError,
    availability_snapshot,
    load_calendar_settings,
)
from voice.flow_session import availability_digest, load_free_windows
from voice.goal_region import GoalRegionRunner
from voice.region_providers import AnthropicProvider, resolve_supervisor
from voice.scheduling_domains import DOMAINS, region_config_for


HERE = Path(__file__).resolve().parent
GROUND_TRUTH_PATH = HERE / "ground_truth.json"
AVAILABILITY_PATH = HERE / "availability.json"
SCENARIOS_PATH = HERE / "scenarios.json"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_CALLER_TURNS = 14

_AFFIRMATION_RE = re.compile(
    r"(?<!\w)(?:yes|yeah|yep|correct|right|book\ it|sounds\ good|"
    r"please\ do|that\ works|confirm)(?!\w)",
    re.IGNORECASE,
)
_CONFIRM_PREFIX = "To confirm:"
_CONFIRM_SUFFIX = "— shall I book it?"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"{path.name} is missing; run populate_fake_calendar.py first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc


def _truth_to_availability(truth: dict[str, Any]) -> dict[str, Any]:
    """Convert fixture truth to the exact digest/window input shape."""

    timezone_name = truth.get("timezone")
    raw_days = truth.get("days")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("ground_truth.json has no timezone")
    if not isinstance(raw_days, dict) or not raw_days:
        raise ValueError("ground_truth.json has no days object")

    days: dict[str, list[dict[str, str]]] = {}
    for day, day_truth in raw_days.items():
        if not isinstance(day, str) or not isinstance(day_truth, dict):
            raise ValueError("ground_truth.json contains a malformed day")
        raw_windows = day_truth.get("free_windows")
        if not isinstance(raw_windows, list):
            raise ValueError(f"ground_truth.json day {day} has no free_windows list")
        windows: list[dict[str, str]] = []
        for raw_window in raw_windows:
            if not isinstance(raw_window, dict):
                raise ValueError(f"ground_truth.json day {day} has a bad window")
            start = raw_window.get("start")
            end = raw_window.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                raise ValueError(
                    f"ground_truth.json day {day} has non-string window bounds"
                )
            try:
                parsed_start = datetime.fromisoformat(start)
                parsed_end = datetime.fromisoformat(end)
            except ValueError as exc:
                raise ValueError(
                    f"ground_truth.json day {day} has invalid ISO bounds"
                ) from exc
            if parsed_end <= parsed_start:
                raise ValueError(
                    f"ground_truth.json day {day} has a non-positive window"
                )
            windows.append({"start": start, "end": end})
        days[day] = windows
    return {"timezone": timezone_name, "days": days}


def _truth_windows(truth: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    availability = _truth_to_availability(truth)
    return [
        (datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"]))
        for windows in availability["days"].values()
        for window in windows
    ]


def _inside_windows(
    start: datetime,
    end: datetime,
    windows: list[tuple[datetime, datetime]],
) -> bool:
    return end > start and any(
        start >= window_start and end <= window_end
        for window_start, window_end in windows
    )


def _subtract_interval(
    windows: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    remaining: list[tuple[datetime, datetime]] = []
    for window_start, window_end in windows:
        if end <= window_start or start >= window_end:
            remaining.append((window_start, window_end))
            continue
        if window_start < start:
            remaining.append((window_start, start))
        if end < window_end:
            remaining.append((end, window_end))
    return remaining


class _CommitRecorder:
    """Shared call recording and post-conflict scoring truth."""

    def __init__(
        self,
        truth: dict[str, Any],
        *,
        conflict_on_first_commit: bool,
    ) -> None:
        self.settings = SimpleNamespace(timezone=truth["timezone"])
        self.score_windows = _truth_windows(truth)
        self.conflict_on_first_commit = conflict_on_first_commit
        self.conflict_interval: tuple[datetime, datetime] | None = None
        self.is_free_calls: list[dict[str, Any]] = []
        self.insert_calls: list[dict[str, Any]] = []

    def _inject_first_conflict(
        self,
        start: datetime,
        end: datetime,
    ) -> bool:
        if not self.conflict_on_first_commit or self.is_free_calls:
            return False
        self.conflict_interval = (start, end)
        self.score_windows = _subtract_interval(self.score_windows, start, end)
        return True

    def _record_is_free(
        self,
        start: datetime,
        end: datetime,
        *,
        result: bool,
        forced_conflict: bool,
    ) -> bool:
        self.is_free_calls.append(
            {
                "start": start,
                "end": end,
                "result": result,
                "forced_conflict": forced_conflict,
            }
        )
        return result


class FakeCommitCalendar(_CommitRecorder):
    """Ground-truth calendar double used by every offline scenario.

    When requested, the first ``is_free`` call is flipped to false exactly
    once and its interval is removed from scoring truth.  Later checks use the
    resulting post-conflict free windows.
    """

    def is_free(self, start: datetime, end: datetime) -> bool:
        forced = self._inject_first_conflict(start, end)
        result = False if forced else _inside_windows(start, end, self.score_windows)
        return self._record_is_free(
            start,
            end,
            result=result,
            forced_conflict=forced,
        )

    def insert_event(self, **event: Any) -> str:
        event_id = f"fake-lawyer-event-{len(self.insert_calls) + 1}"
        self.insert_calls.append({**event, "event_id": event_id})
        return event_id


class LiveCommitCalendar(_CommitRecorder):
    """Recording wrapper that delegates real writes and reads to CalendarClient."""

    def __init__(
        self,
        client: CalendarClient,
        truth: dict[str, Any],
        *,
        conflict_on_first_commit: bool,
    ) -> None:
        super().__init__(
            truth,
            conflict_on_first_commit=conflict_on_first_commit,
        )
        self._client = client
        self.settings = client.settings

    def is_free(self, start: datetime, end: datetime) -> bool:
        forced = self._inject_first_conflict(start, end)
        result = False if forced else self._client.is_free(start, end)
        return self._record_is_free(
            start,
            end,
            result=result,
            forced_conflict=forced,
        )

    def insert_event(self, **event: Any) -> str:
        record = {**event, "event_id": None}
        self.insert_calls.append(record)
        event_id = self._client.insert_event(**event)
        record["event_id"] = event_id
        return event_id

    def get_event(self, event_id: str) -> dict[str, Any]:
        return self._client.get_event(event_id)


def _template_values(week_start: str) -> dict[str, str]:
    base = date.fromisoformat(week_start)
    values: dict[str, str] = {}
    for offset in range(7):
        fixture_day = base + timedelta(days=offset)
        values[f"day_{offset}_name"] = fixture_day.strftime("%A")
        values[f"day_{offset}_label"] = (
            f"{fixture_day:%A %B} {fixture_day.day}"
        )
    return values


def _resolve_scenarios(
    raw_scenarios: list[dict[str, Any]],
    week_start: str,
) -> list[dict[str, Any]]:
    base = date.fromisoformat(week_start)
    values = _template_values(week_start)
    scenarios: list[dict[str, Any]] = []
    for raw in raw_scenarios:
        scenario = dict(raw)
        brief_template = scenario.pop("brief_template", None)
        if brief_template is not None:
            scenario["brief"] = brief_template.format_map(values)

        scripted_templates = scenario.pop("scripted_utterance_templates", [])
        scenario["scripted_utterances"] = [
            template.format_map(values) for template in scripted_templates
        ]
        confirmation_templates = scenario.pop(
            "confirmation_response_templates",
            [],
        )
        scenario["confirmation_responses"] = [
            template.format_map(values) for template in confirmation_templates
        ]

        required_offset = scenario.pop("required_day_offset", None)
        if required_offset is not None:
            if (
                not isinstance(required_offset, int)
                or isinstance(required_offset, bool)
                or not 0 <= required_offset <= 6
            ):
                raise ValueError(
                    f"{scenario.get('id')}: required_day_offset must be 0..6"
                )
            scenario["required_date"] = (
                base + timedelta(days=required_offset)
            ).isoformat()

        forbidden_offset = scenario.pop("forbidden_day_offset", None)
        if forbidden_offset is not None:
            if (
                not isinstance(forbidden_offset, int)
                or isinstance(forbidden_offset, bool)
                or not 0 <= forbidden_offset <= 6
            ):
                raise ValueError(
                    f"{scenario.get('id')}: forbidden_day_offset must be 0..6"
                )
            scenario["forbidden_date"] = (
                base + timedelta(days=forbidden_offset)
            ).isoformat()
        scenarios.append(scenario)
    return scenarios


def _load_scenarios(truth: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _read_json(SCENARIOS_PATH)
    if not isinstance(raw, list) or len(raw) < 8:
        raise ValueError("scenarios.json must contain at least eight scenarios")
    if not all(isinstance(scenario, dict) for scenario in raw):
        raise ValueError("scenarios.json must contain only objects")
    week_start = truth.get("week_start")
    if not isinstance(week_start, str):
        raise ValueError("ground_truth.json has no week_start")
    resolved = _resolve_scenarios(raw, week_start)
    ids = [scenario.get("id") for scenario in resolved]
    if any(not isinstance(scenario_id, str) or not scenario_id for scenario_id in ids):
        raise ValueError("every scenario needs a non-empty id")
    if len(set(ids)) != len(ids):
        raise ValueError("scenario ids must be unique")
    return resolved


def _is_scripted_confirmation(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(_CONFIRM_PREFIX) and stripped.endswith(_CONFIRM_SUFFIX)


def _is_affirmative(text: str) -> bool:
    return _AFFIRMATION_RE.search(text) is not None


def _caller_completion(
    client: Any,
    *,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    """Call the selected caller model without the supervisor JSON schema."""

    provider = resolve_supervisor(model)
    if isinstance(provider, AnthropicProvider) and client is not None:
        provider.client = client
    raw_text, _stop_reason = provider.complete_text(
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )
    return raw_text.strip().strip('"')


def _record_caller_exchange(
    messages: list[dict[str, str]],
    agent_text: str,
    caller_text: str,
) -> None:
    messages.extend(
        [
            {"role": "user", "content": agent_text},
            {"role": "assistant", "content": caller_text},
        ]
    )


def _caller_turn(
    client: Any,
    scenario: dict[str, Any],
    messages: list[dict[str, str]],
    agent_text: str,
    caller_turn: int,
    state: dict[str, int],
) -> tuple[str | None, str]:
    scripted = scenario["scripted_utterances"]
    if caller_turn <= len(scripted):
        caller_text = scripted[caller_turn - 1]
        _record_caller_exchange(messages, agent_text, caller_text)
        return caller_text, "scripted_prefix"

    confirmation_responses = scenario["confirmation_responses"]
    confirmation_index = state["confirmation_index"]
    if (
        _is_scripted_confirmation(agent_text)
        and confirmation_index < len(confirmation_responses)
    ):
        caller_text = confirmation_responses[confirmation_index]
        state["confirmation_index"] += 1
        _record_caller_exchange(messages, agent_text, caller_text)
        return caller_text, "scripted_confirmation"

    prompt = (
        "You are simulating a caller in a phone scheduling acceptance test. "
        "Speak only as the caller: short, casual, and occasionally fragmented. "
        "Do not narrate, mention the test, invent calendar facts, or claim an "
        "appointment was booked. Follow the scenario constraints exactly. Accept "
        "a proposed time only when it satisfies the brief. A proposal is not a "
        "booking: give an explicit affirmative answer only after the scheduler "
        "asks the exact confirmation question ending in 'shall I book it?'. If "
        "you are declining, avoid ambiguous affirmative words.\n\n"
        f"Scenario brief: {scenario['brief']}"
    )
    next_messages = [*messages, {"role": "user", "content": agent_text}]
    caller_text = ""
    for _attempt in range(2):
        caller_text = _caller_completion(
            client,
            model=os.environ.get("SCHED_EVAL_CALLER_MODEL", DEFAULT_MODEL),
            system=prompt,
            messages=next_messages,
            max_tokens=180,
        )
        if caller_text:
            break
    if not caller_text:
        return None, "model_empty"
    _record_caller_exchange(messages, agent_text, caller_text)
    return caller_text, "model"


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _api_datetime_to_wall(value: Any, timezone_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("event datetime is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed
    return parsed.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)


def _verify_live_event(
    calendar: LiveCommitCalendar,
    event_id: str,
    insert_call: dict[str, Any],
) -> dict[str, Any]:
    """Fetch and independently verify the event returned by live insertion."""

    checks: dict[str, bool] = {}
    event = calendar.get_event(event_id)
    checks["events_get_id_matches"] = event.get("id") == event_id
    try:
        event_start = _api_datetime_to_wall(
            event.get("start", {}).get("dateTime"),
            calendar.settings.timezone,
        )
        event_end = _api_datetime_to_wall(
            event.get("end", {}).get("dateTime"),
            calendar.settings.timezone,
        )
        checks["events_get_bounds_match_insert"] = (
            event_start == insert_call["start"] and event_end == insert_call["end"]
        )
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        event_start = event_end = None
        checks["events_get_bounds_match_insert"] = False
    private = event.get("extendedProperties", {}).get("private", {})
    checks["events_get_booking_marker"] = (
        isinstance(private, dict)
        and private.get("nanoclaw_booking") == "1"
        and private.get("nanoclaw_domain") == "lawyer"
    )
    return {
        "checks": checks,
        "start": event_start,
        "end": event_end,
        "event": event,
    }


def _expected_outcomes(scenario: dict[str, Any]) -> list[str]:
    allowed = scenario.get("expected_outcomes")
    if allowed is None:
        allowed = [scenario.get("expected_outcome")]
    if (
        not isinstance(allowed, list)
        or not allowed
        or not all(isinstance(value, str) for value in allowed)
    ):
        raise ValueError(f"{scenario['id']}: expected outcome is malformed")
    return allowed


def _score_scenario(
    scenario: dict[str, Any],
    *,
    outcome: str,
    event_id: str | None,
    calendar: _CommitRecorder,
    trace: list[dict[str, Any]],
    live_verification: dict[str, Any] | None,
) -> tuple[dict[str, bool], bool]:
    """Score consequential facts from calendar calls and fixture truth."""

    checks: dict[str, bool] = {
        "expected_outcome": outcome in _expected_outcomes(scenario),
    }
    commit_turns = [turn for turn in trace if turn["commit_attempt_count"]]
    checks["confirm_gate"] = all(
        turn["agent_was_scripted_confirmation"]
        and turn["caller_was_affirmative"]
        for turn in commit_turns
    )

    insert_call = calendar.insert_calls[0] if calendar.insert_calls else None
    event_start: datetime | None = None
    event_end: datetime | None = None
    if outcome == "booked":
        checks["commit_attempted"] = bool(commit_turns)
        checks["insert_called_exactly_once"] = len(calendar.insert_calls) == 1
        checks["event_id_non_empty"] = (
            isinstance(event_id, str) and bool(event_id.strip())
        )
        checks["event_id_matches_insert"] = bool(
            insert_call is not None
            and event_id is not None
            and event_id == insert_call.get("event_id")
        )

        expected_service = scenario.get("expected_service_type")
        appointment = DOMAINS["lawyer"].appointment_types.get(expected_service)
        checks["expected_service_type_known"] = appointment is not None
        if insert_call is not None and appointment is not None:
            event_start = insert_call.get("start")
            event_end = insert_call.get("end")
            checks["event_bounds_are_datetimes"] = isinstance(
                event_start,
                datetime,
            ) and isinstance(event_end, datetime)
            if isinstance(event_start, datetime) and isinstance(event_end, datetime):
                duration = int((event_end - event_start).total_seconds() // 60)
                checks["policy_duration"] = (
                    duration == appointment.duration_minutes
                )
                checks["inside_post_conflict_ground_truth"] = _inside_windows(
                    event_start,
                    event_end,
                    calendar.score_windows,
                )
            else:
                checks["policy_duration"] = False
                checks["inside_post_conflict_ground_truth"] = False
            description = insert_call.get("description", "")
            checks["service_policy_metadata"] = (
                insert_call.get("summary")
                == f"{appointment.label} — phone booking"
                and f"service_type: {expected_service}" in description
                and f"duration_minutes: {appointment.duration_minutes}" in description
                and insert_call.get("private_props")
                == {"nanoclaw_domain": "lawyer"}
            )
        else:
            checks["event_bounds_are_datetimes"] = False
            checks["policy_duration"] = False
            checks["inside_post_conflict_ground_truth"] = False
            checks["service_policy_metadata"] = False
    else:
        checks["no_insert_when_not_booked"] = len(calendar.insert_calls) == 0

    if live_verification is not None:
        checks.update(live_verification["checks"])
        verified_start = live_verification.get("start")
        verified_end = live_verification.get("end")
        if isinstance(verified_start, datetime) and isinstance(verified_end, datetime):
            event_start, event_end = verified_start, verified_end

    if scenario.get("required_date") is not None:
        checks["required_booking_date"] = (
            outcome == "booked"
            and isinstance(event_start, datetime)
            and event_start.date().isoformat() == scenario["required_date"]
        )
    if scenario.get("required_time") is not None:
        checks["required_booking_time"] = (
            outcome == "booked"
            and isinstance(event_start, datetime)
            and event_start.strftime("%H:%M") == scenario["required_time"]
        )
    if scenario.get("forbidden_date") is not None:
        checks["avoided_impossible_date"] = (
            outcome != "booked"
            or (
                isinstance(event_start, datetime)
                and event_start.date().isoformat() != scenario["forbidden_date"]
            )
        )

    if scenario.get("required_clarification"):
        first_reply = trace[0]["agent_reply"] if trace else ""
        lowered = first_reply.lower()
        checks["service_clarification_observed"] = (
            "?" in first_reply
            and not _is_scripted_confirmation(first_reply)
            and ("initial" in lowered or "consult" in lowered)
            and ("dispute" in lowered or "conflict" in lowered)
        )

    if scenario.get("required_decline_then_rebook"):
        checks["decline_did_not_commit"] = any(
            turn["response_source"] == "scripted_confirmation"
            and turn["agent_was_scripted_confirmation"]
            and not turn["caller_was_affirmative"]
            and turn["commit_attempt_count"] == 0
            for turn in trace
        )

    if scenario.get("required_conflict_reoffer"):
        conflict = calendar.conflict_interval
        checks["first_commit_conflict_injected"] = bool(
            calendar.is_free_calls
            and calendar.is_free_calls[0]["forced_conflict"]
            and calendar.is_free_calls[0]["result"] is False
        )
        checks["revalidated_after_conflict"] = len(calendar.is_free_calls) >= 2
        checks["booked_elsewhere_after_conflict"] = (
            outcome == "booked"
            and conflict is not None
            and isinstance(event_start, datetime)
            and isinstance(event_end, datetime)
            and (event_end <= conflict[0] or event_start >= conflict[1])
        )

    early_turn = scenario.get("early_affirmation_turn")
    if early_turn is not None:
        early = next(
            (turn for turn in trace if turn["caller_turn"] == early_turn),
            None,
        )
        checks["early_yes_stayed_in_negotiation"] = bool(
            early is not None
            and early["caller_was_affirmative"]
            and not early["agent_was_scripted_confirmation"]
            and early["commit_attempt_count"] == 0
            and early["insert_count_after"] == 0
        )

    passed = all(checks.values())
    return checks, passed


def run_scenario(
    llm_client: Any,
    availability: dict[str, Any],
    truth: dict[str, Any],
    scenario: dict[str, Any],
    *,
    live_client: CalendarClient | None = None,
) -> dict[str, Any]:
    """Drive one synchronous BookingFlow conversation and score its writes."""

    domain = DOMAINS["lawyer"]
    config = region_config_for(domain, availability_digest(availability))
    runner = GoalRegionRunner(
        config,
        load_free_windows(availability),
        client=llm_client,
    )
    if live_client is None:
        calendar: _CommitRecorder = FakeCommitCalendar(
            truth,
            conflict_on_first_commit=bool(
                scenario.get("conflict_on_first_commit")
            ),
        )
    else:
        calendar = LiveCommitCalendar(
            live_client,
            truth,
            conflict_on_first_commit=bool(
                scenario.get("conflict_on_first_commit")
            ),
        )
    flow = BookingFlow(runner, domain, calendar)

    caller_messages: list[dict[str, str]] = []
    caller_state = {"confirmation_index": 0}
    agent_text = domain.greeting
    trace: list[dict[str, Any]] = []
    latencies: list[float] = []
    rejected: list[str] = []
    final_turn = None
    raw_outcome = "caller_cap"
    max_caller_turns = scenario.get("max_caller_turns", DEFAULT_CALLER_TURNS)
    if (
        not isinstance(max_caller_turns, int)
        or isinstance(max_caller_turns, bool)
        or max_caller_turns <= 0
    ):
        raise ValueError(f"{scenario['id']}: max_caller_turns must be positive")

    for caller_turn_number in range(1, max_caller_turns + 1):
        caller_text, response_source = _caller_turn(
            llm_client,
            scenario,
            caller_messages,
            agent_text,
            caller_turn_number,
            caller_state,
        )
        if caller_text is None:
            raw_outcome = "caller_gave_up"
            break

        agent_was_confirmation = _is_scripted_confirmation(agent_text)
        caller_was_affirmative = _is_affirmative(caller_text)
        checks_before = len(calendar.is_free_calls)
        inserts_before = len(calendar.insert_calls)
        turn = flow.turn(caller_text)
        final_turn = turn
        rejected.extend(turn.rejected)
        if turn.supervisor_ms is not None:
            latencies.append(turn.supervisor_ms)
        trace.append(
            {
                "caller_turn": caller_turn_number,
                "agent_prompt": agent_text,
                "caller_text": caller_text,
                "response_source": response_source,
                "agent_was_scripted_confirmation": agent_was_confirmation,
                "caller_was_affirmative": caller_was_affirmative,
                "commit_attempt_count": len(calendar.is_free_calls) - checks_before,
                "insert_attempt_count": len(calendar.insert_calls) - inserts_before,
                "insert_count_after": len(calendar.insert_calls),
                "agent_reply": turn.reply,
                "done": turn.done,
                "outcome": turn.outcome,
            }
        )
        if turn.done:
            raw_outcome = turn.outcome or "unknown_terminal"
            break
        agent_text = turn.reply

    outcome = {
        "booked": "booked",
        "escape": "escape",
        "budget": "not_booked",
        "not_booked": "not_booked",
        "caller_cap": "not_booked",
        "caller_gave_up": "not_booked",
        "unknown_terminal": "not_booked",
    }.get(raw_outcome, "not_booked")
    event_id = final_turn.event_id if final_turn is not None else None

    live_verification = None
    if (
        live_client is not None
        and outcome == "booked"
        and isinstance(event_id, str)
        and calendar.insert_calls
    ):
        live_verification = _verify_live_event(
            calendar,
            event_id,
            calendar.insert_calls[0],
        )

    checks, passed = _score_scenario(
        scenario,
        outcome=outcome,
        event_id=event_id,
        calendar=calendar,
        trace=trace,
        live_verification=live_verification,
    )
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "expected": _expected_outcomes(scenario),
        "outcome": outcome,
        "raw_outcome": raw_outcome,
        "passed": passed,
        "checks": checks,
        "failed_checks": [name for name, passed_check in checks.items() if not passed_check],
        "event_id": event_id,
        "insert_calls": len(calendar.insert_calls),
        "is_free_calls": len(calendar.is_free_calls),
        "caller_turns": len(trace),
        "slots": final_turn.slots if final_turn is not None else {},
        "rejected": rejected,
        "supervisor_samples_ms": latencies,
        "supervisor_latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else None,
            "p95": (
                round(_percentile(latencies, 0.95), 1)
                if latencies
                else None
            ),
        },
        "trace": trace,
    }


def _availability_week_matches(
    availability: dict[str, Any],
    truth: dict[str, Any],
) -> bool:
    return (
        availability.get("timezone") == truth.get("timezone")
        and list(availability.get("days", {})) == list(truth.get("days", {}))
    )


def _provider_setup() -> tuple[Any, Any, Any | None]:
    supervisor_model = os.environ.get("SCHED_EVAL_MODEL", DEFAULT_MODEL)
    caller_model = os.environ.get("SCHED_EVAL_CALLER_MODEL", DEFAULT_MODEL)
    supervisor = resolve_supervisor(supervisor_model)
    caller = resolve_supervisor(caller_model)
    if (
        supervisor.provider == "anthropic" or caller.provider == "anthropic"
    ) and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise ValueError(
            "ANTHROPIC_API_KEY is required by the selected supervisor/caller model"
        )

    llm_client = None
    if supervisor.provider == "anthropic" or caller.provider == "anthropic":
        try:
            import anthropic
        except ImportError as exc:
            raise ValueError(
                "the anthropic package is required by the selected model"
            ) from exc
        llm_client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"].strip()
        )
    return supervisor, caller, llm_client


def _print_table(results: list[dict[str, Any]]) -> None:
    headers = (
        "scenario",
        "expected",
        "actual",
        "turns",
        "inserts",
        "p50 ms",
        "pass",
    )
    rows = []
    for result in results:
        rows.append(
            (
                result.get("name", result["id"]),
                "/".join(result.get("expected", [])),
                result.get("outcome", "error"),
                str(result.get("caller_turns", "-")),
                str(result.get("insert_calls", "-")),
                str(result.get("supervisor_latency_ms", {}).get("p50", "-")),
                "PASS" if result.get("passed") else "FAIL",
            )
        )
    widths = [
        max(len(str(row[index])) for row in [headers, *rows])
        for index in range(len(headers))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(
            "  ".join(
                str(value).ljust(widths[index])
                for index, value in enumerate(row)
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="book and verify events on the configured dedicated test calendar",
    )
    args = parser.parse_args()

    try:
        truth = _read_json(GROUND_TRUTH_PATH)
        if not isinstance(truth, dict):
            raise ValueError("ground_truth.json must contain an object")
        offline_availability = _truth_to_availability(truth)
        scenarios = _load_scenarios(truth)
        supervisor, caller, llm_client = _provider_setup()
    except (KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    live_client = None
    if args.live:
        settings = load_calendar_settings()
        if settings is None:
            print(
                "NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON and "
                "NANO_CLAW_GCAL_CALENDAR_ID are required for --live",
                file=sys.stderr,
            )
            return 2
        try:
            persisted_availability = _read_json(AVAILABILITY_PATH)
            if not isinstance(persisted_availability, dict):
                raise ValueError("availability.json must contain an object")
            if not _availability_week_matches(persisted_availability, truth):
                raise ValueError(
                    "availability.json and ground_truth.json cover different weeks; "
                    "rerun populate and fetch"
                )
            live_client = CalendarClient(settings)
        except (CalendarError, TypeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    print(
        f"supervisor: {supervisor.provider}/{supervisor.model} · "
        f"caller: {caller.provider}/{caller.model} · "
        f"calendar: {'LIVE' if args.live else 'offline fake'}"
    )
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        try:
            scenario_availability = offline_availability
            if live_client is not None:
                # Include bookings created by earlier live scenarios while
                # preserving fixture-relative scenario dates.
                scenario_availability = availability_snapshot(live_client)
                if not _availability_week_matches(scenario_availability, truth):
                    raise ValueError(
                        "live availability no longer covers the fixture week"
                    )
            result = run_scenario(
                llm_client,
                scenario_availability,
                truth,
                scenario,
                live_client=live_client,
            )
        except Exception as exc:
            result = {
                "id": scenario["id"],
                "name": scenario["name"],
                "expected": _expected_outcomes(scenario),
                "outcome": "error",
                "passed": False,
                "failed_checks": ["scenario_exception"],
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)

    _print_table(results)
    passed = sum(bool(result.get("passed")) for result in results)
    print(f"\nOverall: {passed}/{len(results)} passed")
    for result in results:
        if result.get("passed"):
            continue
        details = ", ".join(result.get("failed_checks", [])) or "unknown failure"
        error = f" · {result['error']}" if result.get("error") else ""
        print(f"FAIL {result['id']}: {details}{error}")
    if args.live:
        event_ids = [
            result["event_id"]
            for result in results
            if isinstance(result.get("event_id"), str)
        ]
        print(f"live event ids: {', '.join(event_ids) if event_ids else 'none'}")
        print(
            "cleanup: python3 scripts/lawyer_eval/populate_fake_calendar.py "
            "--cleanup --apply"
        )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
