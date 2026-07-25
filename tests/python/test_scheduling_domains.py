from dataclasses import fields

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
