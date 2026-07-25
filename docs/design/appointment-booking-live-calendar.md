# Live calendar setup for appointment booking

The `lawyer` voice mode books against Google Calendar. Bind it only to a
dedicated **TEST** calendar while developing or demonstrating the flow. Do not
use a person's primary calendar or a production office calendar.

## Prerequisites

Use the existing Google Cloud service account for this deployment. You need:

- its service-account JSON file;
- permission to enable the Google Calendar API in its Google Cloud project;
- permission to create and share a Google Calendar.

The service account needs calendar event read/write access. It does not need a
user OAuth client secret, and the browser Google sign-in settings are unrelated.

## Create and share the test calendar

1. In Google Calendar, create a new calendar named something unmistakable, such
   as `nano-claw Lawyer Scheduler TEST`.
2. Set the calendar timezone to `America/Chicago`. The current booking adapter
   interprets all unzoned appointment values as Chicago business-local time.
3. Open the service-account JSON and copy its `client_email` value.
4. In the test calendar's **Settings and sharing** page, share the calendar with
   that `client_email` and grant **Make changes to events**.
5. In **Integrate calendar**, copy the **Calendar ID**. Use this ID, not the
   calendar's display name.
6. In the Google Cloud project that owns the service account, verify that the
   Google Calendar API is enabled.

Keep the JSON credential outside the repository and restrict its host file
permissions. Never paste the credential into `.env`, `.env.example`, a test
fixture, or a committed document.

## Configure nano-claw

Add these values to this repository's host-side `.env`:

```dotenv
NANO_CLAW_VOICE_FLOW=lawyer
NANO_CLAW_GCAL_CALENDAR_ID=your-test-calendar-id@group.calendar.google.com
NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON=/absolute/host/path/to/gcal-service-account.json
```

`NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON` is the path visible on the host. When
`./run.sh` starts Docker, it:

- forwards `NANO_CLAW_GCAL_CALENDAR_ID`;
- mounts that host JSON read-only at `/app/secrets/gcal-sa.json`; and
- sets the container's `NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON` to that mounted
  path.

The mount and environment arguments are added only when their host values are
set. If the voice server is run directly outside Docker, it uses the host path
from `.env` as-is.

Start the service:

```bash
./run.sh
```

Select **Lawyer Scheduler** in the MODE dropdown, or set it through the API:

```bash
curl -sS -X POST http://localhost:9090/api/voice/flow \
  -H 'content-type: application/json' \
  -d '{"mode":"lawyer"}'
```

The configuration endpoint can confirm that both settings are present without
contacting Google:

```bash
curl -sS http://localhost:9090/api/voice/flow
```

For `lawyer`, `availability_ok: true` means only that both calendar environment
settings are configured. This GET is intentionally not a network health probe.
Authentication, sharing, and transport are verified when a new lawyer session
fetches its live availability.

## Runtime and degraded behavior

A lawyer session fetches a fresh availability snapshot before negotiating. The
blocking Google request runs in an executor so phone setup and the browser event
loop remain responsive. The same calendar client is retained for the final
availability recheck and event insertion.

If either setting is missing, or Google authentication/free-busy fails, the
session does not use a cached snapshot and does not fall through to persona
chat. It keeps the law-office greeting, then gives the domain's calendar
unavailable apology and ends with `outcome: "not_booked"`.

An unavailable or unreadable mounted JSON can therefore produce
`availability_ok: true` on the configuration GET but still degrade safely at
session start. Check the voice-service log, the JSON host path, Calendar API
enablement, Calendar ID, and test-calendar sharing permission.

## Booking commit contract

`BOOKED` is a commit result, not a conversational claim. The flow may return
`outcome: "booked"` only after all of the following succeed:

1. the caller explicitly confirms a validated service type and time;
2. the calendar is checked again and the interval is still free;
3. Google Calendar accepts the event insertion; and
4. the insert response contains a non-empty `event_id`.

If the time was taken, the stale window is removed and negotiation resumes. If
the calendar call fails or Google returns no event ID, the terminal result is
`not_booked` and the caller hears the unavailable apology. A spoken confirmation
without an event ID is never `BOOKED`.

Test events include the private `nanoclaw_booking=1` marker and the
`nanoclaw_domain=lawyer` property. Inspect the dedicated test calendar after a
call to verify the event, then remove test events there without touching any
other calendar.
