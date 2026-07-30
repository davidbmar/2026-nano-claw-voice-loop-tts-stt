from dataclasses import fields
import re

import pytest

from voice.flow_session import SCHEDULER_GREETING, scheduler_region_config
from voice.goal_region import RegionConfig, build_supervisor_schema
from voice.scheduling_domains import DOMAINS, region_config_for


@pytest.mark.parametrize(
    ("service_type", "label", "duration_minutes", "location"),
    [
        ("follow_up_call", "follow-up call", 30, "phone"),
        ("initial_consultation", "initial consultation", 60, "video"),
        (
            "contract_dispute_consult",
            "contract dispute consultation",
            120,
            "video",
        ),
    ],
)
def test_lawyer_appointment_policy_derives_duration_and_location(
    service_type,
    label,
    duration_minutes,
    location,
):
    domain = DOMAINS["lawyer"]
    appointment_type = domain.appointment_types[service_type]
    duration_spec = domain.slots["duration_minutes"]

    assert appointment_type.service_type == service_type
    assert appointment_type.label == label
    assert appointment_type.duration_minutes == duration_minutes
    assert appointment_type.location == location
    assert duration_spec["from"] == "service_type"
    assert duration_spec["map"][service_type] == duration_minutes


def test_plumber_region_config_matches_existing_values_field_for_field():
    digest = "availability digest"
    domain = DOMAINS["plumber"]
    expected = RegionConfig(
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

    built = region_config_for(domain, digest)
    delegated = scheduler_region_config(digest)

    for field in fields(RegionConfig):
        assert getattr(built, field.name) == getattr(expected, field.name)
        assert getattr(delegated, field.name) == getattr(expected, field.name)
    assert domain.greeting == SCHEDULER_GREETING
    assert domain.appointment_types is None
    assert domain.uses_live_calendar is False
    assert domain.booked_template.format(
        job="water heater repair",
        slot_start="Monday July twentieth at 10 AM",
        duration_minutes=60,
    ) == (
        "You're booked: water heater repair on Monday July twentieth at "
        "10 AM for 60 minutes. See you then. Goodbye!"
    )
    assert domain.escape_text == "Of course — I'm transferring you now. Goodbye!"
    assert (
        domain.budget_text
        == "Our scheduler will call you back to finish this up. Goodbye!"
    )


def test_lawyer_confirmation_template_composes_from_policy():
    domain = DOMAINS["lawyer"]
    appointment_type = domain.appointment_types["initial_consultation"]

    confirmation = domain.confirm_template.format(
        duration_minutes=appointment_type.duration_minutes,
        label=appointment_type.label,
        location=appointment_type.location,
        slot_start="Thursday March fifth at 2 PM",
    )

    assert confirmation == (
        "To confirm: a 60-minute initial consultation by video, "
        "Thursday March fifth at 2 PM — shall I book it?"
    )


def test_lawyer_supervisor_schema_enums_service_and_omits_duration():
    schema = build_supervisor_schema(DOMAINS["lawyer"].slots)
    candidates = schema["properties"]["slot_candidates"]

    assert list(candidates["properties"]) == ["service_type", "slot_start"]
    assert candidates["required"] == ["service_type", "slot_start"]
    assert candidates["properties"]["service_type"] == {
        "anyOf": [
            {
                "type": "string",
                "enum": [
                    "follow_up_call",
                    "initial_consultation",
                    "contract_dispute_consult",
                ],
            },
            {"type": "null"},
        ]
    }
    assert "duration_minutes" not in candidates["properties"]


# ── copy may not outrun capability ───────────────────────────────────────────

# The same rule riff-builder enforces in `rb/honest_copy.py`, restated here
# because these domains are the shape business profiles will be built from, and
# a profile inherits whatever dishonesty its template had.
_CLAIMS_A_BOOKING = re.compile(
    r"\b(?:you'?re|you are)\s+booked\b"
    r"|\bi'?ve\s+booked\b"
    r"|\bbooked\s+(?:you|it)\b"
    r"|\bconfirmed\s+(?:your|the)\s+appointment\b",
    re.IGNORECASE,
)


def test_a_domain_that_writes_nothing_promises_nothing():
    """The bug this guards, verified before it was fixed:

    `plumber` has `uses_live_calendar=False`, so `FlowSession` passes
    `calendar=None` and `BookingFlow` holds none. The completion branch spoke
    `booked_template`, so a caller heard "You're booked: burst pipe on Monday
    August third at 9 AM for 60 minutes" — for an appointment written to no
    calendar, no database, and no file. `event_id` was None.

    This runs over every domain, not just plumber, because the next domain is
    the one nobody re-reads.
    """
    for domain_id, domain in DOMAINS.items():
        if domain.uses_live_calendar:
            continue
        assert not _CLAIMS_A_BOOKING.search(domain.recorded_template), (
            f"domain {domain_id!r} writes nothing — no calendar, no store — but "
            f"its completion line claims a booking: "
            f"{domain.recorded_template!r}")


def test_the_no_calendar_path_speaks_the_recorded_line_not_the_booked_one():
    """Asserted at the call site too, not just on the data. A correct template
    the code never reaches would leave the caller hearing the same lie."""
    from types import SimpleNamespace

    from voice.booking import BookingFlow
    from voice.goal_region import RegionTurn

    class Runner:
        config = SimpleNamespace(goal="g")
        slots: dict = {}
        turns_used = 1
        max_turns = 12

        def turn(self, _text):
            return RegionTurn(
                reply="", exit="booked", rejected=[], supervisor_ms=1.0,
                slots={"job": "burst pipe",
                       "slot_start": "2026-08-03T09:00:00",
                       "duration_minutes": 60})

    turn = BookingFlow(Runner(), DOMAINS["plumber"], None).turn("monday at nine")

    assert not _CLAIMS_A_BOOKING.search(turn.reply), turn.reply
    assert turn.event_id is None, (
        "nothing was written, which is the whole point of the line above")


def test_every_domain_carries_both_terminal_lines():
    """`recorded_template` is required rather than optional, so adding a domain
    forces the author to answer "what happens if nothing writes?" instead of
    inheriting a booking claim by default."""
    for domain_id, domain in DOMAINS.items():
        assert domain.booked_template, domain_id
        assert domain.recorded_template, domain_id
        assert domain.recorded_template != domain.booked_template, (
            f"domain {domain_id!r} reuses one line for two different events")
