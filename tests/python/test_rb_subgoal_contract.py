"""Can nano-claw run a subgoal riff-builder actually authors? Measured, not argued.

The plan for moving conversation into nano-claw asserted three structural
blockers. Two are now gone (the exit enum, `2cfa076`; the dishonest terminal
copy, `25c7938`) and the third turned out to be theoretical. So this file stops
reasoning about the gap and runs a REAL artifact through the real machinery.

What it establishes, in order:

  1. The region layer accepts a real subgoal today — config, schema, runner and
     exit validation all build.
  2. The TAIL does not. A `classified` exit reaches `BookingFlow`, matches no
     branch, and comes back `done=False, outcome=None` — the conversation never
     ends. That is the honest remaining blocker, and it is here as an assertion
     rather than a note so it cannot quietly stop being true.

The fixture is a real `rb_subgoal_v0` produced by riff-builder's own
`suggest_subgoal` for the plumbing seed's first goal. `test_the_fixture_still
_matches_riff_builder` regenerates it when that repo is present, so this cannot
drift silently into testing a shape nobody produces.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice.goal_region import (
    GoalRegionRunner,
    RegionConfig,
    RegionTurn,
    build_supervisor_schema,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rb_subgoal_v0_emergency.json"


@pytest.fixture
def subgoal():
    return json.loads(FIXTURE.read_text())


def region_config_from(blob) -> RegionConfig:
    """The translation a mode emitter would perform. Deliberately minimal — if
    this needs to grow, that growth IS the remaining incompatibility."""
    return RegionConfig(
        goal=blob["goal"],
        persona=blob["persona"],
        digest="",
        slots=blob["slots"],
        escape_phrases=tuple(blob.get("escape_phrases", [])),
        max_turns=int(blob.get("max_turns", 8)),
        deadline_s=float(blob.get("deadline_s", 60)),
        # riff-builder keeps exit names under `riff`, not at the top level.
        exit_name=blob["riff"]["exit_names"][0],
    )


# ── what works ───────────────────────────────────────────────────────────────

def test_a_real_subgoal_declares_the_exit_nano_claw_could_not_name(subgoal):
    """The blocker that made every other question moot. The supervisor enum was
    the literal ["booked"], so this exit could not be proposed at all."""
    assert subgoal["riff"]["exit_names"] == ["classified"]


def test_its_slot_types_are_all_ones_nano_claw_understands(subgoal):
    """riff-builder's SLOT_TYPES_ALLOWED is nano-claw's five plus `choice`, and
    no shipped subgoal uses `choice` — riff-builder's own flow compiler refuses
    `choices:` grounding as deferred. So the slot-type gap is theoretical."""
    understood = {"text", "enum", "minutes", "datetime", "derived_minutes"}

    for name, spec in subgoal["slots"].items():
        assert spec.get("type", "text") in understood, (name, spec)


def test_the_region_layer_runs_it(subgoal):
    """Config, schema, runner. This is what the last two iterations bought."""
    config = region_config_from(subgoal)
    schema = build_supervisor_schema(config.slots, exit_name=config.exit_name)

    assert schema["properties"]["exit_candidate"]["anyOf"][0]["enum"] == [
        "classified"]
    assert set(schema["properties"]["slot_candidates"]["properties"]) == set(
        subgoal["slots"])

    runner = GoalRegionRunner(config, ())
    runner._slots = {name: "something the caller said" for name in subgoal["slots"]}
    rejected: list[str] = []
    assert runner._validated_exit("classified", rejected) == "classified"
    assert rejected == []


def test_the_wrong_exit_is_still_refused(subgoal):
    """Configurable must not mean unchecked."""
    runner = GoalRegionRunner(region_config_from(subgoal), ())
    runner._slots = {name: "x" for name in subgoal["slots"]}

    rejected: list[str] = []
    assert runner._validated_exit("booked", rejected) is None
    assert rejected


# ── what does not, stated as an assertion so it cannot silently change ───────

def test_the_tail_is_the_remaining_blocker(subgoal):
    """A classified exit is not terminal to `BookingFlow`.

    `booking.py` reads `if exit_name != "booked": return ...region reply` — so
    the flow comes back `done=False, outcome=None` and the conversation never
    ends. nano-claw books against a calendar or records nothing; it has no
    equivalent of this subgoal's `tail.submit`.

    When that changes, this test fails, and that failure is the signal to delete
    it rather than a regression.
    """
    from types import SimpleNamespace

    from voice.booking import BookingFlow
    from voice.scheduling_domains import DOMAINS

    class Runner:
        config = SimpleNamespace(goal="g")
        slots: dict = {}
        turns_used = 1
        max_turns = 12

        def turn(self, _text):
            return RegionTurn(reply="Noted.", exit="classified", rejected=[],
                              supervisor_ms=1.0, slots={"emergency_details": "x"})

    turn = BookingFlow(Runner(), DOMAINS["plumber"], None).turn("burst pipe")

    assert turn.done is False, (
        "a classified exit became terminal — the tail gap may be closed; if so "
        "this test has done its job and should go")
    assert turn.outcome is None


def test_the_subgoal_declares_a_submit_nano_claw_cannot_perform(subgoal):
    """What the tail actually asks for, named so the next step is concrete: a
    tool call that RECORDS the request. nano-claw's only terminal write is a
    calendar insert."""
    submit = subgoal["tail"]["submit"]

    assert submit["tool"] == "record_issue"
    assert submit["success_line"]


def test_the_subgoals_success_line_is_already_honest(subgoal):
    """Worth pinning: riff-builder's default promises a callback, not a booking —
    the same claim nano-claw's `recorded_template` now makes for a flow that
    writes nothing. The two sides agree about what may be said."""
    line = subgoal["tail"]["submit"]["success_line"].lower()

    assert "call you back" in line
    assert "you're booked" not in line


# ── the fixture must keep describing something real ──────────────────────────

def test_the_fixture_still_matches_riff_builder():
    """Regenerate from riff-builder when it is present; skip when it is not.

    A vendored fixture that drifts is worse than none — it turns into a test of
    a shape nobody produces, which is how the compile-path duplication bugs in
    this project started.
    """
    import subprocess
    import sys

    repo = Path.home() / "src" / "riff-builder-goal-driven"
    if not (repo / "rb" / "subgoal.py").exists():
        pytest.skip("riff-builder-goal-driven not present")
    python = repo / ".venv" / "bin" / "python"
    if not python.exists():
        pytest.skip("riff-builder venv not present")

    script = (
        "import json, yaml, pathlib\n"
        "from rb.subgoal import suggest_subgoal\n"
        "seed = yaml.safe_load(pathlib.Path('seeds/plumbing.seed.yaml').read_text())\n"
        "exp = pathlib.Path('seeds/plumbing.expansion.yaml')\n"
        "expansion = yaml.safe_load(exp.read_text()) if exp.exists() else None\n"
        "print(json.dumps(suggest_subgoal(seed['goals'][0], expansion, "
        "{'name': 'Rivera Plumbing', 'industry': 'plumbing'}), indent=2))\n"
    )
    result = subprocess.run([str(python), "-c", script], cwd=repo,
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        pytest.skip(f"could not regenerate: {result.stderr[-200:]}")

    assert json.loads(result.stdout) == json.loads(FIXTURE.read_text()), (
        "riff-builder now produces a different subgoal than this fixture — "
        f"regenerate {FIXTURE.name} and re-read the assertions above")
