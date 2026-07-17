#!/usr/bin/env python3
"""Run the goal-region scheduling evaluation (API calls, no calendar writes).

Generate availability first, then load the Anthropic key before running:

    ~/src/riff/.venv/bin/python scripts/scheduling_eval/fetch_availability.py
    set -a; source .env; set +a
    /path/to/testenv/bin/python scripts/scheduling_eval/run_eval.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.flow_session import (
    SCHEDULER_GREETING as GREETING,
    availability_digest as _availability_digest,
    load_free_windows as _load_windows,
    scheduler_region_config,
)
from voice.goal_region import GoalRegionRunner

HERE = Path(__file__).resolve().parent
AVAILABILITY_PATH = HERE / "availability.json"
GROUND_TRUTH_PATH = HERE / "ground_truth.json"
SCENARIOS_PATH = HERE / "scenarios.json"
RESULTS_PATH = HERE / "results.json"

CONFIRMATION = "You're all set. We'll send the appointment details shortly."
DEFAULT_MODEL = "claude-opus-4-8"


def _response_text(response) -> str:
    for block in response.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            cleaned = text.strip().strip('"')
            if cleaned:
                return cleaned
    return ""


def _caller_turn(
    client, scenario: dict, messages: list[dict], agent_text: str
) -> str | None:
    prompt = (
        "You are simulating a caller in a phone scheduling test. Speak only as "
        "the caller: short, casual, and occasionally fragmented. Do not narrate "
        "or mention this test. State constraints naturally, correct mistakes when "
        "needed, and accept a proposed time only when it satisfies the brief.\n\n"
        f"Scenario brief: {scenario['brief']}"
    )
    next_messages = [*messages, {"role": "user", "content": agent_text}]
    caller_text = ""
    for _attempt in range(2):
        response = client.messages.create(
            model=os.environ.get("SCHED_EVAL_CALLER_MODEL", DEFAULT_MODEL),
            max_tokens=160,
            system=[{
                "type": "text",
                "text": prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=next_messages,
        )
        caller_text = _response_text(response)
        if caller_text:
            break
    if not caller_text:
        return None

    messages.extend([
        {"role": "user", "content": agent_text},
        {"role": "assistant", "content": caller_text},
    ])
    return caller_text


def _resolve_scenarios(scenarios: list[dict], week_start: str) -> list[dict]:
    """Resolve fixture-relative scenario text and dates for one eval week."""

    base = date.fromisoformat(week_start)
    template_values = {}
    for offset in range(7):
        fixture_day = base + timedelta(days=offset)
        template_values[f"day_{offset}_name"] = fixture_day.strftime("%A")
        template_values[f"day_{offset}_label"] = (
            f"{fixture_day:%A %B} {fixture_day.day}"
        )

    resolved = []
    for raw_scenario in scenarios:
        scenario = dict(raw_scenario)
        name_template = scenario.pop("name_template", None)
        if name_template is not None:
            scenario["name"] = name_template.format_map(template_values)

        brief_template = scenario.pop("brief_template", None)
        if brief_template is not None:
            scenario["brief"] = brief_template.format_map(template_values)

        required_day_offset = scenario.pop("required_day_offset", None)
        if required_day_offset is not None:
            if (
                not isinstance(required_day_offset, int)
                or not 0 <= required_day_offset < 7
            ):
                raise ValueError("required_day_offset must be an integer from 0 to 6")
            scenario["required_date"] = str(
                base + timedelta(days=required_day_offset)
            )
        resolved.append(scenario)
    return resolved


def _load_scenarios() -> list[dict]:
    truth = json.loads(GROUND_TRUTH_PATH.read_text())
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    return _resolve_scenarios(scenarios, truth["week_start"])


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _truth_slot_valid(slots: dict, truth: dict) -> bool:
    try:
        start = datetime.fromisoformat(slots["slot_start"])
        duration = int(slots["duration_minutes"])
    except (KeyError, TypeError, ValueError):
        return False
    end = start + timedelta(minutes=duration)
    for day in truth["days"].values():
        for window in day["free_windows"]:
            free_start = datetime.fromisoformat(window["start"])
            free_end = datetime.fromisoformat(window["end"])
            if start >= free_start and end <= free_end:
                return True
    return False


def _score(scenario: dict, outcome: str, raw_exit: str, slots: dict) -> dict:
    truth = json.loads(GROUND_TRUTH_PATH.read_text())
    allowed = scenario.get("expected_outcomes", [scenario.get("expected_outcome")])
    expected_match = outcome in allowed
    capped_no_booking = (
        outcome == "no_booking"
        and "no_booking" in allowed
        and raw_exit in ("caller_cap", "caller_gave_up")
    )
    exit_match = capped_no_booking or (
        "expected_exit" not in scenario or raw_exit == scenario["expected_exit"]
    )
    valid_slot = _truth_slot_valid(slots, truth) if outcome == "booked" else None
    duration_honored = (
        slots.get("duration_minutes") == scenario["duration_minutes"]
        if outcome == "booked"
        else None
    )
    preference_honored = None
    if outcome == "booked" and scenario.get("required_date"):
        try:
            preference_honored = (
                datetime.fromisoformat(slots["slot_start"]).date().isoformat()
                == scenario["required_date"]
            )
        except (KeyError, ValueError):
            preference_honored = False

    booked_checks = outcome != "booked" or (
        valid_slot
        and duration_honored
        and preference_honored is not False
    )
    return {
        "expected_match": expected_match,
        "exit_match": exit_match,
        "valid_against_ground_truth": valid_slot,
        "duration_honored": duration_honored,
        "preference_honored": preference_honored,
        "passed": bool(expected_match and exit_match and booked_checks),
    }


def run_scenario(client, availability: dict, scenario: dict) -> dict:
    runner = GoalRegionRunner(
        scheduler_region_config(_availability_digest(availability)),
        _load_windows(availability),
        client=client,
    )
    caller_messages: list[dict] = []
    agent_text = GREETING
    latencies = []
    rejections = []
    slots = {}
    raw_exit = "caller_cap"
    confirmation = ""
    caller_turns = 0
    caller_gave_up = False

    for caller_turns in range(1, 11):
        caller_text = _caller_turn(client, scenario, caller_messages, agent_text)
        if caller_text is None:
            caller_gave_up = True
            if raw_exit != "booked":
                raw_exit = "caller_gave_up"
            break
        turn = runner.turn(caller_text)
        slots = turn.slots
        rejections.extend(turn.rejected)
        if turn.supervisor_ms is not None:
            latencies.append(turn.supervisor_ms)
        if turn.exit:
            raw_exit = turn.exit
            if turn.exit == "booked":
                confirmation = CONFIRMATION
            break
        agent_text = turn.reply

    outcome = {
        "booked": "booked",
        "escape": "escape",
        "budget": "no_booking",
        "caller_cap": "no_booking",
        "caller_gave_up": "no_booking",
    }[raw_exit]
    score = _score(scenario, outcome, raw_exit, slots)
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "expected": scenario.get(
            "expected_outcomes", scenario.get("expected_outcome")
        ),
        "outcome": outcome,
        "exit": raw_exit,
        "slots": slots,
        "scripted_confirmation": confirmation,
        "caller_turns": caller_turns,
        "caller_gave_up": caller_gave_up,
        "supervisor_latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else None,
            "p95": round(_percentile(latencies, 0.95), 1) if latencies else None,
        },
        "supervisor_samples_ms": [round(value, 1) for value in latencies],
        "rejected": rejections,
        **score,
    }


def _print_table(results: list[dict]) -> None:
    headers = ("scenario", "expected", "actual", "turns", "p50 ms", "p95 ms", "pass")
    rows = []
    for result in results:
        latency = result.get("supervisor_latency_ms", {})
        rows.append((
            result.get("name", result["id"]),
            str(result.get("expected")),
            result.get("outcome", "error"),
            str(result.get("caller_turns", "-")),
            str(latency.get("p50", "-")),
            str(latency.get("p95", "-")),
            "PASS" if result.get("passed") else "FAIL",
        ))
    widths = [max(len(str(row[i])) for row in [headers, *rows]) for i in range(len(headers))]
    print("  ".join(value.ljust(widths[i]) for i, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)))


def main() -> None:
    if not AVAILABILITY_PATH.exists():
        raise SystemExit(
            "availability.json is missing; run fetch_availability.py with riff's venv first"
        )
    availability = json.loads(AVAILABILITY_PATH.read_text())
    scenarios = _load_scenarios()
    client = anthropic.Anthropic()
    results = []
    for scenario in scenarios:
        try:
            results.append(run_scenario(client, availability, scenario))
        except Exception as exc:
            results.append({
                "id": scenario["id"],
                "name": scenario["name"],
                "expected": scenario.get(
                    "expected_outcomes", scenario.get("expected_outcome")
                ),
                "outcome": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "passed": False,
            })

    latencies = [
        value
        for result in results
        for value in result.get("supervisor_samples_ms", [])
    ]
    passed = sum(bool(result.get("passed")) for result in results)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "supervisor_model": os.environ.get("SCHED_EVAL_MODEL", DEFAULT_MODEL),
        "caller_model": os.environ.get("SCHED_EVAL_CALLER_MODEL", DEFAULT_MODEL),
        "overall": {
            "passed": passed,
            "failed": len(results) - passed,
            "total": len(results),
            "supervisor_latency_ms": {
                "p50": round(statistics.median(latencies), 1) if latencies else None,
                "p95": round(_percentile(latencies, 0.95), 1) if latencies else None,
            },
        },
        "scenarios": results,
    }
    RESULTS_PATH.write_text(json.dumps(output, indent=2) + "\n")
    _print_table(results)
    print(f"\nOverall: {passed}/{len(results)} passed")
    print(f"results → {RESULTS_PATH}")


if __name__ == "__main__":
    main()
