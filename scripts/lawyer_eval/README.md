# Lawyer scheduling eval

Use only a dedicated Google Calendar test calendar. The normal path is:

```bash
# 1. Write ground_truth.json and preview the fixture week (no Google access).
python3 scripts/lawyer_eval/populate_fake_calendar.py

# Create the previewed fixture events.
python3 scripts/lawyer_eval/populate_fake_calendar.py --apply

# 2. Capture the calendar's actual free windows.
python3 scripts/lawyer_eval/fetch_availability.py

# 3. Exercise BookingFlow with fixture truth and a fake commit calendar.
set -a; source .env; set +a
python3 scripts/lawyer_eval/run_eval.py

# 4. Explicit opt-in: create real bookings and verify each with events.get.
python3 scripts/lawyer_eval/run_eval.py --live
```

The dry-run populate step needs no environment variables. Populate with
`--apply`, fetch, cleanup, and `run_eval.py --live` need:

```dotenv
NANO_CLAW_GCAL_SERVICE_ACCOUNT_JSON=/absolute/path/to/service-account.json
NANO_CLAW_GCAL_CALENDAR_ID=test-calendar-id
```

Offline `run_eval.py` does not read either Google setting. It does make model
calls for the supervisor and caller simulator, so it needs the provider keys
selected by `SCHED_EVAL_MODEL` and `SCHED_EVAL_CALLER_MODEL` (both default to
Anthropic and therefore `ANTHROPIC_API_KEY`).

Every fixture has the private `nanoclaw_lawyer_eval=1` property, and every
nano-claw booking has `nanoclaw_booking=1`. Preview cleanup first, then apply
it; the applied command removes both kinds of marked event and leaves every
unmarked calendar event alone:

```bash
python3 scripts/lawyer_eval/populate_fake_calendar.py --cleanup
python3 scripts/lawyer_eval/populate_fake_calendar.py --cleanup --apply
```
