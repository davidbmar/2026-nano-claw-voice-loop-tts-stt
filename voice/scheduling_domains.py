"""Declarative policy for each scheduling domain.

``SchedulingDomain`` deliberately contains data only: its fields map directly
to a future YAML or tenant configuration, while :func:`region_config_for`
adapts that data to the goal-region runner.  To add a scheduling domain, add
one entry to ``DOMAINS`` here and one matching entry to ``FLOW_MODES`` in
``voice.flow_session`` (task 076); no scheduler control-flow changes are
required.

Spoken templates use named fields from the validated slots and appointment
policy.  In particular, ``slot_start`` is the speech-friendly datetime,
``label`` and ``location`` come from ``AppointmentType``, and
``duration_minutes`` always comes from validated policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from voice.goal_region import RegionConfig

AppointmentLocation = Literal["phone", "video"]

SCHEDULER_GREETING = (
    "Thanks for calling Lakeside Plumbing. What can I help you schedule?"
)


@dataclass(frozen=True)
class AppointmentType:
    """One policy-owned appointment type offered by a scheduling domain."""

    service_type: str
    label: str
    duration_minutes: int
    location: AppointmentLocation


@dataclass(frozen=True)
class SchedulingDomain:
    """Pure-data policy consumed by scheduling and booking orchestration."""

    id: str
    greeting: str
    goal: str
    persona: str
    slots: dict
    appointment_types: dict[str, AppointmentType] | None
    escape_phrases: tuple[str, ...]
    max_turns: int
    deadline_s: float
    uses_live_calendar: bool
    booked_template: str
    # What to say when the flow completes but NOTHING was written — the case
    # where `uses_live_calendar` is false, so `BookingFlow` holds no calendar.
    # Kept separate from `booked_template` rather than reused, because the two
    # describe different events and one of them must not claim the other:
    # speaking "You're booked" with no calendar is a promise no part of this
    # system keeps. Enforced by
    # test_scheduling_domains.py::test_a_domain_that_writes_nothing_promises_nothing.
    recorded_template: str
    escape_text: str
    budget_text: str
    confirm_template: str
    apology_unavailable: str
    slot_taken_text: str


_PLUMBER_SLOTS = {
    "job": {"type": "text", "required": True},
    "slot_start": {"type": "datetime", "required": True},
    "duration_minutes": {
        "type": "minutes",
        "values": [30, 60, 120, 240],
        "required": True,
    },
}

_LAWYER_APPOINTMENT_TYPES = {
    "follow_up_call": AppointmentType(
        service_type="follow_up_call",
        label="follow-up call",
        duration_minutes=30,
        location="phone",
    ),
    "initial_consultation": AppointmentType(
        service_type="initial_consultation",
        label="initial consultation",
        duration_minutes=60,
        location="video",
    ),
    "contract_dispute_consult": AppointmentType(
        service_type="contract_dispute_consult",
        label="contract dispute consultation",
        duration_minutes=120,
        location="video",
    ),
}

_LAWYER_DURATION_MAP = {
    service_type: appointment_type.duration_minutes
    for service_type, appointment_type in _LAWYER_APPOINTMENT_TYPES.items()
}

_LAWYER_SLOTS = {
    "service_type": {
        "type": "enum",
        "values": list(_LAWYER_APPOINTMENT_TYPES),
        "required": True,
    },
    "slot_start": {"type": "datetime", "required": True},
    "duration_minutes": {
        "type": "derived_minutes",
        "from": "service_type",
        "map": _LAWYER_DURATION_MAP,
        "required": True,
    },
}

DOMAINS: dict[str, SchedulingDomain] = {
    "plumber": SchedulingDomain(
        id="plumber",
        greeting=SCHEDULER_GREETING,
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
        slots=_PLUMBER_SLOTS,
        appointment_types=None,
        escape_phrases=("operator", "human", "goodbye"),
        max_turns=12,
        deadline_s=600,
        uses_live_calendar=False,
        booked_template=(
            "You're booked: {job} on {slot_start} for {duration_minutes} minutes. "
            "See you then. Goodbye!"
        ),
        recorded_template=(
            "I've got that down: {job} on {slot_start} for {duration_minutes} "
            "minutes. Someone will call you back to confirm it. Goodbye!"
        ),
        escape_text="Of course — I'm transferring you now. Goodbye!",
        budget_text="Our scheduler will call you back to finish this up. Goodbye!",
        confirm_template=(
            "To confirm: {job} on {slot_start} for {duration_minutes} minutes — "
            "shall I book it?"
        ),
        apology_unavailable=(
            "I'm sorry — scheduling is unavailable right now. "
            "Please call us back shortly."
        ),
        slot_taken_text=(
            "I'm sorry — that time was just taken. Can we look at another?"
        ),
    ),
    "lawyer": SchedulingDomain(
        id="lawyer",
        greeting=(
            "Thanks for calling our law office. How can I help you schedule?"
        ),
        goal=(
            "Book one legal appointment of the appropriate service type that "
            "satisfies the caller and fits the grounded availability. Use only "
            "the policy-derived duration and format."
        ),
        persona=(
            "You are a concise, warm legal scheduler. Figure out which service "
            "fits the caller's plain-language description: follow_up_call is a "
            "30-minute follow-up call by phone; initial_consultation is a "
            "60-minute initial consultation by video; contract_dispute_consult "
            "is a 120-minute contract dispute consultation by video. Describe "
            "the duration and format from this policy and never ask the caller "
            "how long they need. Then negotiate a time strictly inside the "
            "grounded availability digest. Keep every reply to one or two short "
            "spoken sentences; offer at most two candidate times per turn. When "
            "the selected service's duration does not fit any window on a day, "
            "say so plainly and offer the nearest other day whose window fits it; "
            "never keep proposing a day that cannot fit the duration."
        ),
        slots=_LAWYER_SLOTS,
        appointment_types=_LAWYER_APPOINTMENT_TYPES,
        escape_phrases=("operator", "human", "goodbye"),
        max_turns=15,
        deadline_s=600,
        uses_live_calendar=True,
        # Unreachable for this domain — it books against a live calendar, so
        # `BookingFlow` always holds one. Present because the field is required,
        # and honest in case that ever changes.
        recorded_template=(
            "I've taken your details and the office will call you back to "
            "confirm. Goodbye!"
        ),
        booked_template=(
            "You're booked: a {duration_minutes}-minute {label} by {location}, "
            "{slot_start}. See you then. Goodbye!"
        ),
        escape_text="Of course — I'm transferring you now. Goodbye!",
        budget_text="Our office will call you back to finish scheduling. Goodbye!",
        confirm_template=(
            "To confirm: a {duration_minutes}-minute {label} by {location}, "
            "{slot_start} — shall I book it?"
        ),
        apology_unavailable=(
            "I'm sorry — our calendar is unavailable right now, so I couldn't "
            "book that appointment. Please call the office to schedule."
        ),
        slot_taken_text=(
            "I'm sorry — that time was just taken. Can we look at another?"
        ),
    ),
}


def region_config_for(domain: SchedulingDomain, digest: str) -> RegionConfig:
    """Adapt a declarative scheduling-domain policy to the region runner."""

    return RegionConfig(
        goal=domain.goal,
        persona=domain.persona,
        digest=digest,
        slots=domain.slots,
        escape_phrases=domain.escape_phrases,
        max_turns=domain.max_turns,
        deadline_s=domain.deadline_s,
    )
