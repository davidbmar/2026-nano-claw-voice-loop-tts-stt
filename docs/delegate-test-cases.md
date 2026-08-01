# Test cases for the turn-delegate work

Everything here runs against `./scripts/try_delegate.sh`, which starts its own
riff-builder (`:8795`) and nano-claw (`:8080`) and leaves your `:8790` and the
live phone node on `:9090` alone. Ctrl-C in that terminal shuts it down.

Operator password when the console asks: **`testing`**.

Each case says what to do, what should happen, and **what it would look like if
the thing being tested were broken** — several of these guard failures whose
symptom is nothing happening.

---

## A. The basic path

### A1 — Talk to riff-builder through nano-claw's voice

1. Open <http://127.0.0.1:8080/>
2. **MODE** → `Turn Delegate` (enter `testing` when prompted)
3. The **APP URL** field appears, already filled with a conversation URL
4. Hold the mic button, say *"we do emergency plumbing repairs"*

**Expect:** a spoken reply about your phone menu, 2–9 seconds later. The words
come from riff-builder; nano-claw only speaks them.

**Broken would look like:** *"Sorry — I couldn't reach the assistant just then"*
on every turn. That is the mode working correctly with nowhere to send.

### A2 — The wiring check

```bash
NANO_CLAW_DELEGATE_STARTS='{"+15125550100":{"start":"http://127.0.0.1:8795/api/delegate/start"}}' \
  ~/src/nano-claw/.venv-test/bin/python ~/src/nano-claw/scripts/check_delegate_setup.py
```

**Expect:** five `[ ok ]` lines and `1/1 lines ready`, exit 0. Three `[ .. ]`
notes are normal and none is a failure:

- the loopback-vs-container warning — this check runs on your host and cannot
  tell where the gateway runs;
- the turn exceeding 2s — that is riff-builder's model, not the seam
  (measured: the seam costs ~10 microseconds);
- "no greeting set" — because this command re-specifies the config with a bare
  URL. The rig itself sets a greeting; A3 will use it.

### A3 — A simulated phone call, no carrier

```bash
TOKEN=$(grep '^NANO_CLAW_PHONE_TOKEN=' ~/src/nano-claw/.env | cut -d= -f2- | tr -d '"')
curl -X POST "http://127.0.0.1:8080/api/phone/incoming?token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"data":{"event_type":"call.initiated","payload":{
       "call_control_id":"v3:tc-1","to":"+15125550100","from":"+15125559999"}}}'

LOOPBACK_WS_BASE=ws://localhost:8080 LOOPBACK_CALL_ID='v3:tc-1' \
  ~/src/nano-claw/.venv-test/bin/python \
  ~/src/nano-claw/scripts/phone_loopback_test.py 'we do emergency plumbing repairs'
```

**Expect:** `greeting done (~330 frames)`, then `FIRST ANSWER AUDIO: ~2700 ms`.
In the rig's log: `delegated for this call`, then `delegate turn ok=True`.

**Note:** the STT reliably mishears synthesized speech — you may see
`caller: I'm sorry, I'm sorry`. That is the test harness's audio, not a bug.
Read the timings and the `ok=True`, not the transcript.

---

## B. Failure behaviour — the point of most of this work

### B1 — The app goes down mid-conversation

With the rig running, kill riff-builder and take a turn:

```bash
lsof -ti :8795 | xargs kill -9
```

Then speak in the browser.

**Expect:** *"Sorry — I couldn't reach the assistant just then. Please try
again."* The line stays open — speak again and it is still there.

**Why it matters:** before this work, riff-builder's own 502 shape matched no
branch and produced a **silent turn**. "The app is down" sounded exactly like a
dead line.

Restart the rig for the remaining cases.

### B2 — A number nobody configured

```bash
curl -s -X POST http://127.0.0.1:8795/api/delegate/start \
  -H 'Content-Type: application/json' \
  -d '{"conversation_key":"tc","to":"+19995550000"}'
```

**Expect:** `404` — `no builder line is configured for '+19995550000'`.

**Why:** whoever dials a configured number can edit that business by voice. A
wrong number must not create one.

### B3 — Two callers on one number

```bash
curl -s -X POST http://127.0.0.1:8795/api/delegate/start -H 'Content-Type: application/json' \
  -d '{"conversation_key":"caller-A","to":"+15125550100"}'
curl -s -X POST http://127.0.0.1:8795/api/delegate/start -H 'Content-Type: application/json' \
  -d '{"conversation_key":"caller-B","to":"+15125550100"}'
```

**Expect:** two **different** `delegate_url`s.

**Broken would look like:** the same URL twice — two callers interleaving into
one conversation, which is the entire reason the start seam exists.

### B4 — A redelivered webhook

Repeat B3's first command with the same `conversation_key`.

**Expect:** the **same** URL as before. Telnyx redelivers webhooks; without this
one caller is split across two graphs.

### B5 — The conversation ceiling

Restart riff-builder with a ceiling of zero:

```bash
lsof -ti :8795 | xargs kill -9
cd ~/src/riff-builder-goal-driven
RB_BUILDER_DIDS='{"+15125550100":{"business_name":"Test Plumbing","industry":"plumbing"}}' \
RB_DELEGATE_MAX_LIVE=0 .venv/bin/python -m uvicorn rb.server:app --port 8795 &
```

Then run A2.

**Expect:** `status 503: {"detail":"0 live conversations, ceiling is 0; raise
RB_DELEGATE_MAX_LIVE if this is real traffic"}`.

**Why it matters:** the gateway fails **open**, so in production the only symptom
is calls quietly answering as the wrong assistant. The message has to name the
cause because nothing else will.

---

## C. Per-line identity

### C1 — The line greets as the business

In A3's loopback run, the greeting is *"Thanks for calling Test Plumbing."* —
not nano-claw's generic line, and not Space Channel's.

**Check it is the short one:** ~330 greeting frames. An undelegated call on the
same node gives ~780. That difference IS the per-DID greeting.

### C2 — Two lines can sound different

The multi-tenant case: one node answering for two businesses. Configure two
lines with different greetings and voices:

```bash
NANO_CLAW_DELEGATE_STARTS='{
  "+15125550100": {"start":"http://127.0.0.1:8795/api/delegate/start",
                   "greeting":"Thanks for calling Rivera Plumbing.","voice":"af_heart"},
  "+15125550200": {"start":"http://127.0.0.1:8795/api/delegate/start",
                   "greeting":"Lakeside Legal, how can I help?","voice":"bm_george"}
}'
```

**Expect** each line to resolve its own greeting and voice, and neither to fall
back to `NANO_CLAW_PHONE_VOICE`.

Verified against the live TTS — the same sentence in each voice:

| voice | bytes | rms |
|---|---|---|
| `af_heart` | 324,000 | 1405 |
| `bm_george` | 362,400 | 1657 |

Different length, different hash, different loudness. Not a config difference
that stops at the config.

**Broken would look like:** a law office answering in a plumber's voice — which
is what happens if a line without an explicit voice is treated the same as one
with it.

---

## D. Honesty — the fix worth checking by hand

### D1 — A flow that writes nothing must not say it booked

```bash
cd ~/src/nano-claw && .venv-test/bin/python - <<'PY'
from types import SimpleNamespace
from voice.booking import BookingFlow
from voice.goal_region import RegionTurn
from voice.scheduling_domains import DOMAINS

class R:
    config = SimpleNamespace(goal="g"); slots = {}; turns_used = 1; max_turns = 12
    def turn(self, t):
        return RegionTurn(reply="", exit="booked", rejected=[], supervisor_ms=1.0,
                          slots={"job": "burst pipe",
                                 "slot_start": "2026-08-03T09:00:00",
                                 "duration_minutes": 60})

turn = BookingFlow(R(), DOMAINS["plumber"], None).turn("monday at nine")
print("caller hears:", turn.reply)
print("event_id   :", turn.event_id)
PY
```

**Expect:** *"I've got that down… Someone will call you back to confirm it."*
with `event_id: None`.

**Before the fix:** *"You're booked: burst pipe on Monday August third at 9 AM"* —
for an appointment written to no calendar, no database, no file. The Plumber
Scheduler mode is selectable in the console, so this was reachable.

---

## E. Regression checks on the suites

```bash
cd ~/src/nano-claw && .venv-test/bin/python -m pytest tests/python -q
cd ~/src/riff-builder-goal-driven && .venv/bin/python -m pytest -q
```

**Expect:** 873 passed / 3 skipped, and 525 passed.

The riff-builder suite no longer writes into `sessions/` — that directory should
not grow. It had reached 43,882 directories (1.1 GB) of test debris before that
was fixed; those are still there and safe to delete.

---

## What none of this covers

A real PSTN call: carrier jitter, and barge-in over the wire. That needs a DID
pointed at a nano-claw with `NANO_CLAW_DELEGATE_STARTS` set — and if that node is
the containerized one, read the container section of
`docs/delegating-a-phone-line.md` first, because `127.0.0.1` inside a container
is the container.
