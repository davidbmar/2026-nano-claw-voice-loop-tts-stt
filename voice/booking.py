"""Confirm-then-commit orchestration for synchronous scheduling flows."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from voice.calendar_client import CalendarClient, CalendarError
from voice.flow_session import _spoken_datetime, digest_from_windows
from voice.goal_region import BUSINESS_TIMEZONE, GoalRegionRunner, RegionTurn
from voice.scheduling_domains import AppointmentType, SchedulingDomain

log = logging.getLogger("nano-claw.booking")

_NEGOTIATING = "NEGOTIATING"
_CONFIRMING = "CONFIRMING"
_MAX_CONFIRM_CYCLES = 2
_AFFIRMATION_RE = re.compile(
    r"(?<!\w)(?:yes|yeah|yep|correct|right|book\ it|sounds\ good|"
    r"please\ do|that\ works|confirm)(?!\w)",
    re.IGNORECASE,
)


@dataclass
class BookingTurn:
    """One caller-visible turn from the booking orchestrator."""

    reply: str
    done: bool
    outcome: str | None
    slots: dict
    event_id: str | None
    rejected: list[str]
    supervisor_ms: float | None
    turns_used: int
    max_turns: int


class BookingFlow:
    """Synchronously negotiate, confirm, and commit one appointment.

    The state machine is ``NEGOTIATING -> CONFIRMING -> COMMIT``. A rejected
    confirmation returns to ``NEGOTIATING``; a commit conflict does the same
    after removing the stale window. The confirmation gate is deterministic
    and LLM-free because caller assent is the trust boundary between a
    proposed time and a consequential calendar write.

    ``turn()`` never raises: calendar and unexpected integration failures
    terminate with the domain's unavailable apology and no booked outcome.
    """

    # NEGOTIATING --validated proposal--> CONFIRMING --affirmation--> COMMIT
    #      ^                                |                 |
    #      +---- ambiguity / conflict ------+                 +--> BOOKED

    def __init__(
        self,
        runner: GoalRegionRunner,
        domain: SchedulingDomain,
        calendar: CalendarClient | None,
    ) -> None:
        self._runner = runner
        self._domain = domain
        self._calendar = calendar
        self._state = _NEGOTIATING
        self._confirm_cycles = 0

    def turn(self, caller_text: str) -> BookingTurn:
        """Advance the booking state machine by one caller utterance."""

        try:
            if self._state == _CONFIRMING:
                return self._confirming_turn(caller_text)
            return self._negotiating_turn(caller_text)
        except CalendarError as exc:
            log.warning("Calendar booking failed: %s", exc)
            return self._unavailable_turn()
        except Exception:
            log.exception("Booking flow failed")
            return self._unavailable_turn()

    def _negotiating_turn(self, caller_text: str) -> BookingTurn:
        region_turn = self._runner.turn(caller_text)
        exit_name = region_turn.exit

        if exit_name == "escape":
            return self._from_region(
                region_turn,
                reply=self._domain.escape_text,
                done=True,
                outcome="escape",
            )
        if exit_name == "budget":
            return self._from_region(
                region_turn,
                reply=self._domain.budget_text,
                done=True,
                outcome="budget",
            )
        if exit_name != "booked":
            return self._from_region(region_turn, reply=region_turn.reply)

        slots = dict(region_turn.slots)
        if self._calendar is None:
            # No calendar means no write — not to a calendar, not anywhere. This
            # branch used to speak `booked_template`, so a caller completing the
            # plumber flow heard "You're booked: burst pipe on Monday August
            # third at 9 AM" for an appointment that existed in no system.
            # `recorded_template` says what actually happened.
            return self._from_region(
                region_turn,
                reply=self._render_template(self._domain.recorded_template, slots),
                done=True,
                outcome="booked",
            )

        self._confirm_cycles += 1
        if self._confirm_cycles > _MAX_CONFIRM_CYCLES:
            self._state = _NEGOTIATING
            return self._from_region(
                region_turn,
                reply=self._domain.budget_text,
                done=True,
                outcome="budget",
            )

        self._state = _CONFIRMING
        return self._from_region(
            region_turn,
            reply=self._render_template(self._domain.confirm_template, slots),
        )

    def _confirming_turn(self, caller_text: str) -> BookingTurn:
        # Reuse the runner's matcher so escape phrase boundaries cannot drift.
        if self._runner._matches_escape(caller_text):
            return self._turn(
                reply=self._domain.escape_text,
                done=True,
                outcome="escape",
            )
        if _AFFIRMATION_RE.search(caller_text):
            return self._commit()

        self._state = _NEGOTIATING
        # The utterance was an answer to the scripted confirmation; budget
        # expiry must not swallow it on the way back into negotiation.
        self._runner.grant_grace_turn()
        return self._negotiating_turn(caller_text)

    def _commit(self) -> BookingTurn:
        calendar = self._calendar
        if calendar is None:
            raise CalendarError("calendar disappeared before commit")

        slots = self._runner.slots
        appointment = self._appointment_type(slots)
        start = self._slot_start(slots)
        end = start + timedelta(minutes=appointment.duration_minutes)
        booked_reply = self._render_template(self._domain.booked_template, slots)

        if not calendar.is_free(start, end):
            self._runner.remove_free_window_overlap(start, end)
            self._runner.clear_slot("slot_start")
            timezone = getattr(
                getattr(calendar, "settings", None),
                "timezone",
                str(BUSINESS_TIMEZONE),
            )
            self._runner.config.digest = digest_from_windows(
                self._runner.free_windows,
                timezone,
            )
            self._state = _NEGOTIATING
            return self._turn(reply=self._domain.slot_taken_text)

        service_type = appointment.service_type
        event_id = calendar.insert_event(
            summary=f"{appointment.label} — phone booking",
            description=(
                f"service_type: {service_type}\n"
                f"duration_minutes: {appointment.duration_minutes}\n"
                "booked via nano-claw voice"
            ),
            start=start,
            end=end,
            private_props={"nanoclaw_domain": self._domain.id},
        )
        if not isinstance(event_id, str) or not event_id.strip():
            raise CalendarError("calendar insert returned no event id")
        return self._turn(
            reply=booked_reply,
            done=True,
            outcome="booked",
            event_id=event_id,
        )

    def _appointment_type(self, slots: dict) -> AppointmentType:
        appointment_types = self._domain.appointment_types
        service_type = slots.get("service_type")
        if appointment_types is None or service_type not in appointment_types:
            raise CalendarError("validated appointment type is unavailable")
        return appointment_types[service_type]

    @staticmethod
    def _slot_start(slots: dict) -> datetime:
        value = slots.get("slot_start")
        if not isinstance(value, str):
            raise CalendarError("validated appointment start is unavailable")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CalendarError("validated appointment start is malformed") from exc
        # Last line of defense before a calendar write: a visit can only be
        # booked in the future. On 2026-07-27 a stale availability fixture had
        # the scheduler confidently offering days from the previous week; a
        # deterministic refusal turns that class of data rot into a caught
        # error instead of a silently wrong booking.
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        if parsed < now:
            raise CalendarError("validated appointment start is in the past")
        return parsed

    def _render_template(self, template: str, slots: dict) -> str:
        values = dict(slots)
        values["slot_start"] = _spoken_datetime(slots.get("slot_start"))
        if self._domain.appointment_types is not None:
            appointment = self._appointment_type(slots)
            values.update(
                {
                    "service_type": appointment.service_type,
                    "label": appointment.label,
                    "duration_minutes": appointment.duration_minutes,
                    "location": appointment.location,
                }
            )
        try:
            return template.format(**values)
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarError("booking template could not be rendered") from exc

    def _from_region(
        self,
        region_turn: RegionTurn,
        *,
        reply: str,
        done: bool = False,
        outcome: str | None = None,
    ) -> BookingTurn:
        return self._turn(
            reply=reply,
            done=done,
            outcome=outcome,
            slots=region_turn.slots,
            rejected=region_turn.rejected,
            supervisor_ms=region_turn.supervisor_ms,
        )

    def _unavailable_turn(self) -> BookingTurn:
        return self._turn(
            reply=self._domain.apology_unavailable,
            done=True,
            outcome="not_booked",
        )

    def _turn(
        self,
        *,
        reply: str,
        done: bool = False,
        outcome: str | None = None,
        slots: dict | None = None,
        event_id: str | None = None,
        rejected: list[str] | None = None,
        supervisor_ms: float | None = None,
    ) -> BookingTurn:
        current_slots = self._runner.slots if slots is None else slots
        return BookingTurn(
            reply=reply,
            done=done,
            outcome=outcome,
            slots=dict(current_slots),
            event_id=event_id,
            rejected=list(rejected or ()),
            supervisor_ms=supervisor_ms,
            turns_used=self._runner.turns_used,
            max_turns=self._runner.max_turns,
        )
