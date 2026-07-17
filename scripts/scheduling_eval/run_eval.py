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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.goal_region import FreeWindow, GoalRegionRunner, RegionConfig

HERE = Path(__file__).resolve().parent
AVAILABILITY_PATH = HERE / "availability.json"
GROUND_TRUTH_PATH = HERE / "ground_truth.json"
SCENARIOS_PATH = HERE / "scenarios.json"
RESULTS_PATH = HERE / "results.json"

GREETING = "Thanks for calling Lakeside Plumbing. What can I help you schedule?"
CONFIRMATION = "You're all set. We'll send the appointment details shortly."
DEFAULT_MODEL = "claude-opus-4-8"


def _response_text(response) -> str:
    for block in response.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text.strip().strip('"')
    raise ValueError("caller simulator returned no text")


def _caller_turn(client, scenario: dict, messages: list[dict], agent_text: str) -> str:
    prompt = (
        "You are simulating a caller in a phone scheduling test. Speak only as "
        "the caller: short, casual, and occasionally fragmented. Do not narrate "
        "or mention this test. State constraints naturally, correct mistakes when "
        "needed, and accept a proposed time only when it satisfies the brief.\n\n"
        f"Scenario brief: {scenario['brief']}"
    )
    next_messages = [*messages, {"role": "user", "content": agent_text}]
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
    messages.extend([
        {"role": "user", "content": agent_text},
        {"role": "assistant", "content": caller_text},
    ])
    return caller_text


def _load_windows(availability: dict) -> list[FreeWindow]:
    return [
        FreeWindow(
            start=datetime.fromisoformat(window["start"]),
            end=datetime.fromisoformat(window["end"]),
        )
        for windows in availability["days"].values()
        for window in windows
    ]


def _availability_digest(availability: dict) -> str:
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
    exit_match = (
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
        RegionConfig(
            goal=(
                "Book one plumbing appointment that satisfies the caller and fits "
                "the grounded availability. Never shorten the requested duration."
            ),
            persona=(
                "You are a concise, warm plumbing scheduler. Offer concrete available "
                "times, clarify constraints, and never claim a time outside the digest."
            ),
            digest=_availability_digest(availability),
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
            max_turns=scenario.get("max_turns", 9),
            deadline_s=180,
        ),
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

    for caller_turns in range(1, 11):
        caller_text = _caller_turn(client, scenario, caller_messages, agent_text)
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
    scenarios = json.loads(SCENARIOS_PATH.read_text())
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
