"""Small Google Calendar REST adapter for nano-claw.

This module is the production isolation boundary for Google Calendar
service-account authentication: other calendar modules must not import the
Google auth transport or service-account libraries. Set
``NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON`` to a service-account JSON path and
``NANO_CLAW_GCAL_CALENDAR_ID`` to the calendar that nano-claw may access.

All public datetime inputs and outputs use naive business-local wall time in
the configured timezone. Timezone offsets are added only at the REST boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


_API_ROOT = "https://www.googleapis.com/calendar/v3"
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
_SA_ENV = "NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON"
_CALENDAR_ENV = "NANO_CLAW_GCAL_CALENDAR_ID"


class CalendarError(Exception):
    """Normalized Google Calendar authentication, transport, or data error."""


@dataclass(frozen=True)
class CalendarSettings:
    """Configuration whose timezone defines naive business-local wall time."""

    sa_path: str
    calendar_id: str
    timezone: str = "America/Chicago"


def load_calendar_settings() -> CalendarSettings | None:
    """Load settings; client datetimes are naive business-local wall times.

    Returns ``None`` when either required environment variable is absent,
    empty, or contains only whitespace.
    """

    sa_path = os.getenv(_SA_ENV, "").strip()
    calendar_id = os.getenv(_CALENDAR_ENV, "").strip()
    if not sa_path or not calendar_id:
        return None
    return CalendarSettings(sa_path=sa_path, calendar_id=calendar_id)


class CalendarClient:
    """Synchronous Calendar v3 client using naive business-local datetimes."""

    def __init__(
        self,
        settings: CalendarSettings,
        timeout_s: float = 5.0,
        session: Any | None = None,
    ) -> None:
        self.settings = settings
        self.timeout_s = timeout_s
        self._session = session

    def freebusy(
        self,
        time_min: datetime,
        time_max: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """Return busy intervals as naive business-local half-open datetimes.

        ``time_min`` and ``time_max`` are interpreted as naive wall times in
        ``settings.timezone``. Aware values are accepted and converted to that
        timezone before the request.
        """

        zone = self._zone()
        start = _business_aware(time_min, zone, "time_min")
        end = _business_aware(time_max, zone, "time_max")
        if end <= start:
            raise CalendarError("freebusy time_max must be after time_min")

        payload = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": self.settings.timezone,
            "items": [{"id": self.settings.calendar_id}],
        }
        response = self._request_json(
            "post",
            f"{_API_ROOT}/freeBusy",
            json=payload,
        )
        calendars = response.get("calendars")
        if not isinstance(calendars, dict):
            raise CalendarError("Google freebusy response has no calendars object")
        calendar = calendars.get(self.settings.calendar_id)
        if not isinstance(calendar, dict):
            raise CalendarError(
                "Google freebusy response has no result for the configured calendar"
            )
        errors = calendar.get("errors")
        if errors:
            raise CalendarError(f"Google freebusy calendar error: {errors!r}")
        if errors is not None and not isinstance(errors, list):
            raise CalendarError("Google freebusy calendar errors field is malformed")

        raw_busy = calendar.get("busy")
        if not isinstance(raw_busy, list):
            raise CalendarError("Google freebusy response has no busy list")

        busy: list[tuple[datetime, datetime]] = []
        for block in raw_busy:
            if not isinstance(block, dict):
                raise CalendarError("Google freebusy busy entry is not an object")
            block_start = _parse_api_datetime(block.get("start"), zone, "busy start")
            block_end = _parse_api_datetime(block.get("end"), zone, "busy end")
            if block_end <= block_start:
                raise CalendarError(
                    "Google freebusy busy entry end must be after start"
                )
            busy.append((block_start, block_end))
        busy.sort(key=lambda interval: interval[0])
        return busy

    def is_free(self, start: datetime, end: datetime) -> bool:
        """Check a naive business-local half-open interval for any overlap.

        Touching a busy interval at either edge is free. Aware inputs are
        normalized to the configured business timezone before comparison.
        """

        zone = self._zone()
        local_start = _business_naive(start, zone, "start")
        local_end = _business_naive(end, zone, "end")
        if local_end <= local_start:
            raise CalendarError("availability end must be after start")
        busy = self.freebusy(local_start, local_end)
        return not any(
            busy_start < local_end and busy_end > local_start
            for busy_start, busy_end in busy
        )

    def insert_event(
        self,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        private_props: dict,
    ) -> str:
        """Insert an event whose bounds are naive business-local wall times.

        Aware bounds are converted to ``settings.timezone``. Every inserted
        event receives the private ``nanoclaw_booking=1`` cleanup marker.
        """

        zone = self._zone()
        event_start = _business_aware(start, zone, "start")
        event_end = _business_aware(end, zone, "end")
        if event_end <= event_start:
            raise CalendarError("event end must be after start")
        if not isinstance(private_props, dict):
            raise CalendarError("private_props must be a dict")

        marker_props = {"nanoclaw_booking": "1", **private_props}
        marker_props["nanoclaw_booking"] = "1"
        event = {
            "summary": summary,
            "description": description,
            "start": {
                "dateTime": event_start.isoformat(),
                "timeZone": self.settings.timezone,
            },
            "end": {
                "dateTime": event_end.isoformat(),
                "timeZone": self.settings.timezone,
            },
            "extendedProperties": {"private": marker_props},
        }
        response = self._request_json(
            "post",
            self._events_url(),
            json=event,
        )
        event_id = response.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise CalendarError("Google event insert response has no event id")
        return event_id

    def get_event(self, event_id: str) -> dict:
        """Fetch an event; its API datetime fields retain their explicit zones.

        Callers converting returned event bounds should treat naive datetimes
        as business-local wall time in ``settings.timezone``.
        """

        response = self._request_json("get", self._event_url(event_id))
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise CalendarError("Google event get response has no event id")
        return response

    def delete_event(self, event_id: str) -> None:
        """Delete an event by id; all client datetime conventions remain local.

        This operation has no datetime argument or return value. Elsewhere in
        this client, naive datetimes mean business-local wall time.
        """

        self._request("delete", self._event_url(event_id))

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.timezone)
        except Exception as exc:
            raise CalendarError(
                f"invalid business timezone {self.settings.timezone!r}"
            ) from exc

    def _authorized_session(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            credentials = service_account.Credentials.from_service_account_file(
                self.settings.sa_path,
                scopes=[_CALENDAR_SCOPE],
            )
            self._session = AuthorizedSession(credentials)
        except Exception as exc:
            raise CalendarError("Google Calendar authentication failed") from exc
        return self._session

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            request = getattr(self._authorized_session(), method)
            response = request(url, timeout=self.timeout_s, **kwargs)
            response.raise_for_status()
            return response
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError(
                f"Google Calendar {method.upper()} request failed"
            ) from exc

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict:
        response = self._request(method, url, **kwargs)
        try:
            payload = response.json()
        except Exception as exc:
            raise CalendarError("Google Calendar response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CalendarError("Google Calendar response JSON is not an object")
        return payload

    def _events_url(self) -> str:
        calendar_id = quote(self.settings.calendar_id, safe="")
        return f"{_API_ROOT}/calendars/{calendar_id}/events"

    def _event_url(self, event_id: str) -> str:
        if not isinstance(event_id, str) or not event_id:
            raise CalendarError("event_id must be a non-empty string")
        return f"{self._events_url()}/{quote(event_id, safe='')}"


def availability_snapshot(
    client: CalendarClient,
    *,
    days: int = 7,
    day_start: time = time(8),
    day_end: time = time(18),
) -> dict:
    """Return free windows whose ISO bounds are naive business-local times.

    The snapshot starts tomorrow in the configured business timezone and
    contains ``days`` consecutive dates. Busy intervals are clipped to each
    half-open ``[day_start, day_end)`` frame and touching/overlapping blocks
    are merged before their complement is emitted.
    """

    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        raise CalendarError("days must be a non-negative integer")
    if not isinstance(day_start, time) or not isinstance(day_end, time):
        raise CalendarError("day_start and day_end must be time values")
    if day_start.tzinfo is not None or day_end.tzinfo is not None:
        raise CalendarError("business-hour bounds must be naive local times")
    if day_end <= day_start:
        raise CalendarError("day_end must be after day_start")

    zone = client._zone()
    first_day = datetime.now(zone).date() + timedelta(days=1)
    output: dict[str, Any] = {
        "timezone": client.settings.timezone,
        "days": {},
    }
    if days == 0:
        return output

    range_start = datetime.combine(first_day, day_start)
    final_day = first_day + timedelta(days=days - 1)
    range_end = datetime.combine(final_day, day_end)
    busy = client.freebusy(range_start, range_end)
    normalized_busy: list[tuple[datetime, datetime]] = []
    try:
        for interval in busy:
            if not isinstance(interval, (tuple, list)) or len(interval) != 2:
                raise CalendarError("freebusy returned a malformed busy interval")
            start = _business_naive(interval[0], zone, "busy start")
            end = _business_naive(interval[1], zone, "busy end")
            if end <= start:
                raise CalendarError("freebusy returned a non-positive busy interval")
            normalized_busy.append((start, end))
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("freebusy returned malformed busy intervals") from exc

    for offset in range(days):
        day = first_day + timedelta(days=offset)
        frame_start = datetime.combine(day, day_start)
        frame_end = datetime.combine(day, day_end)
        clipped = sorted(
            (
                (max(start, frame_start), min(end, frame_end))
                for start, end in normalized_busy
                if start < frame_end and end > frame_start
            ),
            key=lambda interval: interval[0],
        )

        merged: list[tuple[datetime, datetime]] = []
        for start, end in clipped:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            elif end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)

        cursor = frame_start
        windows = []
        for start, end in merged:
            if start > cursor:
                windows.append(
                    {"start": cursor.isoformat(), "end": start.isoformat()}
                )
            cursor = max(cursor, end)
        if cursor < frame_end:
            windows.append(
                {"start": cursor.isoformat(), "end": frame_end.isoformat()}
            )
        output["days"][day.isoformat()] = windows
    return output


def _business_aware(value: datetime, zone: ZoneInfo, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise CalendarError(f"{label} must be a datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=zone)
        return value.astimezone(zone)
    except Exception as exc:
        raise CalendarError(f"{label} has an invalid timezone") from exc


def _business_naive(value: datetime, zone: ZoneInfo, label: str) -> datetime:
    return _business_aware(value, zone, label).replace(tzinfo=None)


def _parse_api_datetime(value: Any, zone: ZoneInfo, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CalendarError(f"Google freebusy {label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("RFC3339 offset is missing")
        return parsed.astimezone(zone).replace(tzinfo=None)
    except Exception as exc:
        raise CalendarError(f"Google freebusy {label} is not RFC3339") from exc
