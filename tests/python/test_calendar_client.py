from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from voice import calendar_client
from voice.calendar_client import (
    CalendarClient,
    CalendarError,
    CalendarSettings,
    availability_snapshot,
    load_calendar_settings,
)


SETTINGS = CalendarSettings(
    sa_path="/tmp/test-calendar-service-account.json",
    calendar_id="calendar@example.com",
)


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        http_error: Exception | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.http_error = http_error
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.http_error is not None:
            raise self.http_error

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, *results: FakeResponse | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("post", url, kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("get", url, kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("delete", url, kwargs)

    def _call(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def freebusy_response(
    busy: list[dict] | None = None,
    *,
    calendar_id: str = SETTINGS.calendar_id,
) -> dict:
    return {
        "calendars": {
            calendar_id: {
                "busy": busy if busy is not None else [],
            }
        }
    }


def utc_rfc3339(value: datetime, zone: ZoneInfo) -> str:
    return (
        value.replace(tzinfo=zone)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_load_calendar_settings_requires_two_nonblank_env_values(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("NANO_CLAW_GCAL_CALENDAR_ID", raising=False)
    assert load_calendar_settings() is None

    monkeypatch.setenv("NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON", "  /secrets/sa.json ")
    monkeypatch.setenv("NANO_CLAW_GCAL_CALENDAR_ID", "  ")
    assert load_calendar_settings() is None

    monkeypatch.setenv("NANO_CLAW_GCAL_CALENDAR_ID", " test@example.com ")
    assert load_calendar_settings() == CalendarSettings(
        sa_path="/secrets/sa.json",
        calendar_id="test@example.com",
        timezone="America/Chicago",
    )


def test_freebusy_posts_localized_bounds_and_returns_local_naive_intervals():
    session = FakeSession(
        FakeResponse(
            freebusy_response(
                [
                    {
                        "start": "2026-01-15T16:00:00Z",
                        "end": "2026-01-15T17:30:00Z",
                    },
                    {
                        "start": "2026-01-15T15:30:00Z",
                        "end": "2026-01-15T16:00:00Z",
                    },
                ]
            )
        )
    )
    client = CalendarClient(SETTINGS, session=session)

    busy = client.freebusy(
        datetime(2026, 1, 15, 8),
        datetime(2026, 1, 15, 12),
    )

    assert busy == [
        (datetime(2026, 1, 15, 9, 30), datetime(2026, 1, 15, 10)),
        (datetime(2026, 1, 15, 10), datetime(2026, 1, 15, 11, 30)),
    ]
    assert session.calls == [
        (
            "post",
            "https://www.googleapis.com/calendar/v3/freeBusy",
            {
                "timeout": 5.0,
                "json": {
                    "timeMin": "2026-01-15T08:00:00-06:00",
                    "timeMax": "2026-01-15T12:00:00-06:00",
                    "timeZone": "America/Chicago",
                    "items": [{"id": "calendar@example.com"}],
                },
            },
        )
    ]


@pytest.mark.parametrize(
    ("requested_start", "requested_end", "expected"),
    [
        (datetime(2026, 1, 15, 9), datetime(2026, 1, 15, 10), True),
        (datetime(2026, 1, 15, 11), datetime(2026, 1, 15, 12), True),
        (datetime(2026, 1, 15, 9, 59), datetime(2026, 1, 15, 10, 1), False),
        (datetime(2026, 1, 15, 10), datetime(2026, 1, 15, 11), False),
        (datetime(2026, 1, 15, 10, 30), datetime(2026, 1, 15, 11, 30), False),
        (datetime(2026, 1, 15, 9), datetime(2026, 1, 15, 12), False),
    ],
)
def test_is_free_uses_half_open_overlap_rules(
    requested_start,
    requested_end,
    expected,
):
    session = FakeSession(
        FakeResponse(
            freebusy_response(
                [
                    {
                        "start": "2026-01-15T16:00:00Z",
                        "end": "2026-01-15T17:00:00Z",
                    }
                ]
            )
        )
    )
    client = CalendarClient(SETTINGS, session=session)

    assert client.is_free(requested_start, requested_end) is expected

    request_body = session.calls[0][2]["json"]
    zone = ZoneInfo(SETTINGS.timezone)
    assert request_body["timeMin"] == requested_start.replace(tzinfo=zone).isoformat()
    assert request_body["timeMax"] == requested_end.replace(tzinfo=zone).isoformat()


def test_availability_snapshot_clips_and_merges_busy_intervals():
    zone = ZoneInfo(SETTINGS.timezone)
    first_day = datetime.now(zone).date() + timedelta(days=1)
    day_one = datetime.combine(first_day, time.min)
    day_two = day_one + timedelta(days=1)
    busy = [
        (day_one.replace(hour=7, minute=30), day_one.replace(hour=8, minute=30)),
        (day_one.replace(hour=10), day_one.replace(hour=11)),
        (day_one.replace(hour=10, minute=30), day_one.replace(hour=12)),
        (day_one.replace(hour=12), day_one.replace(hour=13)),
        (day_one.replace(hour=17, minute=30), day_one.replace(hour=19)),
        (day_two.replace(hour=7), day_two.replace(hour=19)),
    ]
    session = FakeSession(
        FakeResponse(
            freebusy_response(
                [
                    {
                        "start": utc_rfc3339(start, zone),
                        "end": utc_rfc3339(end, zone),
                    }
                    for start, end in busy
                ]
            )
        )
    )
    client = CalendarClient(SETTINGS, session=session)

    snapshot = availability_snapshot(client, days=3)

    day_three = first_day + timedelta(days=2)
    assert snapshot == {
        "timezone": "America/Chicago",
        "days": {
            first_day.isoformat(): [
                {
                    "start": f"{first_day.isoformat()}T08:30:00",
                    "end": f"{first_day.isoformat()}T10:00:00",
                },
                {
                    "start": f"{first_day.isoformat()}T13:00:00",
                    "end": f"{first_day.isoformat()}T17:30:00",
                },
            ],
            (first_day + timedelta(days=1)).isoformat(): [],
            day_three.isoformat(): [
                {
                    "start": f"{day_three.isoformat()}T08:00:00",
                    "end": f"{day_three.isoformat()}T18:00:00",
                }
            ],
        },
    }
    request_body = session.calls[0][2]["json"]
    assert request_body["timeMin"].startswith(f"{first_day.isoformat()}T08:00:00")
    assert request_body["timeMax"].startswith(f"{day_three.isoformat()}T18:00:00")


def test_availability_snapshot_zero_days_does_not_make_a_request():
    session = FakeSession()
    client = CalendarClient(SETTINGS, session=session)

    assert availability_snapshot(client, days=0) == {
        "timezone": "America/Chicago",
        "days": {},
    }
    assert session.calls == []


def test_insert_event_returns_id_and_forces_cleanup_marker():
    session = FakeSession(FakeResponse({"id": "event-123"}))
    client = CalendarClient(SETTINGS, timeout_s=2.5, session=session)

    event_id = client.insert_event(
        summary="Initial consultation",
        description="service_type=initial_consultation",
        start=datetime(2026, 1, 15, 10),
        end=datetime(2026, 1, 15, 10, 30),
        private_props={
            "nanoclaw_domain": "lawyer",
            "nanoclaw_booking": "do-not-override",
        },
    )

    assert event_id == "event-123"
    method, url, kwargs = session.calls[0]
    assert method == "post"
    assert url.endswith("/calendars/calendar%40example.com/events")
    assert kwargs["timeout"] == 2.5
    assert kwargs["json"] == {
        "summary": "Initial consultation",
        "description": "service_type=initial_consultation",
        "start": {
            "dateTime": "2026-01-15T10:00:00-06:00",
            "timeZone": "America/Chicago",
        },
        "end": {
            "dateTime": "2026-01-15T10:30:00-06:00",
            "timeZone": "America/Chicago",
        },
        "extendedProperties": {
            "private": {
                "nanoclaw_booking": "1",
                "nanoclaw_domain": "lawyer",
            }
        },
    }


def test_get_and_delete_event_use_escaped_ids():
    session = FakeSession(
        FakeResponse({"id": "event/123", "summary": "Test"}),
        FakeResponse(None),
    )
    client = CalendarClient(SETTINGS, session=session)

    assert client.get_event("event/123")["summary"] == "Test"
    assert client.delete_event("event/123") is None

    assert [call[:2] for call in session.calls] == [
        (
            "get",
            "https://www.googleapis.com/calendar/v3/calendars/"
            "calendar%40example.com/events/event%2F123",
        ),
        (
            "delete",
            "https://www.googleapis.com/calendar/v3/calendars/"
            "calendar%40example.com/events/event%2F123",
        ),
    ]
    assert [call[2]["timeout"] for call in session.calls] == [5.0, 5.0]


def test_authorization_is_lazy_and_auth_failure_is_wrapped(monkeypatch):
    calls = []

    def fail_credentials(*args, **kwargs):
        calls.append((args, kwargs))
        raise ValueError("bad credentials")

    monkeypatch.setattr(
        calendar_client.service_account.Credentials,
        "from_service_account_file",
        fail_credentials,
    )
    client = CalendarClient(SETTINGS)
    assert calls == []

    with pytest.raises(CalendarError, match="authentication failed") as error:
        client.freebusy(datetime(2026, 1, 15, 8), datetime(2026, 1, 15, 9))

    assert isinstance(error.value.__cause__, ValueError)
    assert calls == [
        (
            (SETTINGS.sa_path,),
            {"scopes": ["https://www.googleapis.com/auth/calendar"]},
        )
    ]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (OSError("network down"), "POST request failed"),
        (
            FakeResponse({}, http_error=RuntimeError("HTTP 503")),
            "POST request failed",
        ),
        (
            FakeResponse(json_error=ValueError("not json")),
            "response is not valid JSON",
        ),
        (FakeResponse([]), "response JSON is not an object"),
    ],
)
def test_transport_http_and_json_failures_are_calendar_errors(result, message):
    client = CalendarClient(SETTINGS, session=FakeSession(result))

    with pytest.raises(CalendarError, match=message):
        client.freebusy(datetime(2026, 1, 15, 8), datetime(2026, 1, 15, 9))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"calendars": []},
        {"calendars": {}},
        {"calendars": {SETTINGS.calendar_id: {"errors": [{"reason": "notFound"}]}}},
        {"calendars": {SETTINGS.calendar_id: {}}},
        {"calendars": {SETTINGS.calendar_id: {"busy": "not-a-list"}}},
        {"calendars": {SETTINGS.calendar_id: {"busy": [None]}}},
        {"calendars": {SETTINGS.calendar_id: {"busy": [{}]}}},
        {
            "calendars": {
                SETTINGS.calendar_id: {
                    "busy": [
                        {
                            "start": "not-a-timestamp",
                            "end": "2026-01-15T17:00:00Z",
                        }
                    ]
                }
            }
        },
        {
            "calendars": {
                SETTINGS.calendar_id: {
                    "busy": [
                        {
                            "start": "2026-01-15T10:00:00",
                            "end": "2026-01-15T11:00:00",
                        }
                    ]
                }
            }
        },
        {
            "calendars": {
                SETTINGS.calendar_id: {
                    "busy": [
                        {
                            "start": "2026-01-15T17:00:00Z",
                            "end": "2026-01-15T16:00:00Z",
                        }
                    ]
                }
            }
        },
    ],
)
def test_freebusy_response_shape_failures_are_calendar_errors(payload):
    client = CalendarClient(SETTINGS, session=FakeSession(FakeResponse(payload)))

    with pytest.raises(CalendarError):
        client.freebusy(datetime(2026, 1, 15, 8), datetime(2026, 1, 15, 9))


@pytest.mark.parametrize(
    "method",
    ["insert", "get"],
)
def test_event_response_without_id_is_a_calendar_error(method):
    client = CalendarClient(SETTINGS, session=FakeSession(FakeResponse({})))

    with pytest.raises(CalendarError, match="no event id"):
        if method == "insert":
            client.insert_event(
                summary="Test",
                description="Test",
                start=datetime(2026, 1, 15, 8),
                end=datetime(2026, 1, 15, 9),
                private_props={},
            )
        else:
            client.get_event("event-id")


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.freebusy(
            datetime(2026, 1, 15, 9),
            datetime(2026, 1, 15, 9),
        ),
        lambda client: client.is_free(
            datetime(2026, 1, 15, 10),
            datetime(2026, 1, 15, 9),
        ),
        lambda client: client.insert_event(
            summary="Test",
            description="Test",
            start=datetime(2026, 1, 15, 10),
            end=datetime(2026, 1, 15, 9),
            private_props={},
        ),
        lambda client: client.delete_event(""),
        lambda client: availability_snapshot(client, days=-1),
        lambda client: availability_snapshot(
            client,
            day_start=time(18),
            day_end=time(8),
        ),
    ],
)
def test_invalid_inputs_fail_as_calendar_errors_without_request(operation):
    session = FakeSession()
    client = CalendarClient(SETTINGS, session=session)

    with pytest.raises(CalendarError):
        operation(client)
    assert session.calls == []


def test_invalid_timezone_is_a_calendar_error():
    settings = CalendarSettings(
        sa_path=SETTINGS.sa_path,
        calendar_id=SETTINGS.calendar_id,
        timezone="Not/A_Timezone",
    )
    client = CalendarClient(settings, session=FakeSession())

    with pytest.raises(CalendarError, match="invalid business timezone"):
        client.is_free(datetime(2026, 1, 15, 8), datetime(2026, 1, 15, 9))
