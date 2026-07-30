# Delegating a phone line to another app

nano-claw answers the phone and owns everything acoustic — mic, VAD,
endpointing, STT, TTS, barge-in. Another app answers the *words*, one HTTP POST
per turn. This is the operator's side of that.

Contract: riff-builder `docs/turn-delegate-contract.md`.
Design and review: `docs/design/2026-07-30-conversation-start-seam.md`.

> **A full call has now run through this — simulated, not over the PSTN.**
> `scripts/phone_loopback_test.py` connects to `/ws/phone-media` exactly as the
> carrier would. Against a second node with a delegated line (2026-07-30):
> greeting played, caller audio transcribed, **delegate turn ok=True in 1.9s**,
> first answer audio 2698 ms after the caller stopped. The per-DID greeting was
> used — 320 greeting frames against 778 for an undelegated call on the same
> node. Still untested: a real carrier, real network jitter, and barge-in.

## Why two settings and not one

A DID is not a conversation — several people dial one number at once, while the
contract pairs one delegate URL with one conversation. So the gateway does not
hold a URL per line; it holds a **start URL** per line, and asks for a fresh
conversation on every call.

That is why the app needs configuring too. Both sides fail closed on an unknown
number, which is deliberate: a wrong number reaching either one is not an
invitation to create a conversation.

## The gateway side (nano-claw)

```bash
NANO_CLAW_DELEGATE_STARTS='{
  "+15125550100": {
    "start": "http://127.0.0.1:8790/api/delegate/start",
    "greeting": "Thanks for calling Rivera Plumbing.",
    "voice": "af_heart",
    "speed": 1.0
  }
}'
```

A bare string works too — `{"+15125550100": "http://127.0.0.1:8790/api/delegate/start"}` —
and means "no profile, node defaults".

- **`greeting`** is authored by *you*, never by the app. The start exchange
  deliberately refuses to accept a greeting over the wire: delegate-authored
  text going straight to TTS is an arbitrary-speech capability. Without one, the
  line answers naming nobody, which is correct but impersonal.
- **`greeting` outranks `NANO_CLAW_PHONE_GREETING`.** One node answering two
  businesses must not greet both as the first one.
- **`voice` / `speed`** are per line, so two businesses on one node do not have
  to sound alike. Omit to inherit the node defaults.
- **`record_notice`** is the recording disclosure this line speaks. It was
  node-wide, which is wrong once one node answers for two businesses: the
  wording is a legal statement made to a caller **on behalf of that business**,
  and what it must say differs by jurisdiction. Omit to inherit;
  `"off"` says nothing.

  **It controls the sentence, not the recording** — see `record` below for that.
- **`record`** (default `true`) is whether this line's calls are captured at
  all. `false` means no recording, and therefore **no call review** for that
  line: that is the trade, and it is yours to make on behalf of a business that
  has not agreed to be recorded.

  A line with `record: false` speaks **no** disclosure, whatever
  `record_notice` says — a line that does not record must not announce that it
  does. Setting both logs an error and drops the notice. That is the same rule
  riff-builder's honest-copy gate enforces: copy may not promise what the system
  does not do, and "this call may be recorded" is a claim about behaviour rather
  than a courtesy.

Non-loopback start URLs need an allowlist:

```bash
NANO_CLAW_DELEGATE_HOSTS='builder.internal,other.internal'
```

### If the gateway runs in a container — and here, it does

`127.0.0.1` inside a container is the **container**, not your machine. The
deployed node reaches every host service that way already —
`STT_SERVICE_URL`, `TTS_SERVICE_URL` and `LUX_SERVICE_URL` all default to
`host.docker.internal` — and the delegate is no different:

```bash
NANO_CLAW_DELEGATE_STARTS='{"+15125550100":{"start":"http://host.docker.internal:8790/api/delegate/start"}}'
NANO_CLAW_DELEGATE_HOSTS='host.docker.internal'
```

Both lines are needed. Without the first, the start request goes nowhere; without
the second, `validate_delegate_url` refuses the URL, because only loopback is
allowed unnamed.

**`scripts/check_delegate_setup.py` cannot catch this.** It runs on the host,
where a loopback URL works perfectly — so it passes, and production fails
silently: the start fails, the gateway falls open, and calls are answered
undelegated. The preflight now says so when it sees a loopback URL, but saying is
all it can do.

Everything is read **per call**, so adding a line needs no restart — and
restarting drops live calls.

## The app side (riff-builder)

```bash
RB_BUILDER_DIDS='{
  "+15125550100": {"business_name": "Rivera Plumbing", "industry": "plumbing"}
}'
```

```bash
# Ceiling on LIVE conversations. Every call mints a session and a ~60 KB
# workspace that nothing deletes, and this is the one creation path with no
# human present. Past it the app returns 503, the gateway falls open, and calls
# are answered UNDELEGATED — so a busy real line needs this raised.
RB_DELEGATE_MAX_LIVE=500

# Where those workspaces go. Nothing deletes them; put it on a volume you are
# willing to let grow, or clean it on a schedule.
RB_SESSIONS_DIR=/var/lib/riff-builder/sessions
```

**This is an authorization boundary.** Whoever dials a configured number can
edit that business by voice — no PIN, no caller-id check. It is acceptable only
for unpublished builder lines. That is also why an unknown DID gets a 404 rather
than a new session: otherwise any wrong number could create graphs without limit.

A phone-started session is pinned to the **owner** role. The gateway sends
`who: "caller"`, correctly — it has no idea this DID is a builder line — but
`who == "owner"` is what gates voice approval, so a phone owner marked "caller"
could not approve anything.

## Exercising it without a phone number

`scripts/check_delegate_setup.py` (below) proves the HTTP chain. To drive the
whole phone path — STT, the delegate hop, TTS, frame pacing — run a second node
and a loopback caller, leaving the node that serves the live line alone:

```bash
# a local sink so no fake call id reaches a real carrier
TELNYX_API_BASE=http://127.0.0.1:8399 \
VOICE_PORT=8080 NANO_CLAW_PHONE=1 \
NANO_CLAW_DELEGATE_STARTS='{"+15125550100":{"start":"http://127.0.0.1:8790/api/delegate/start"}}' \
  .venv-test/bin/python -m voice

# mint the conversation the way call.initiated does
curl -X POST "http://127.0.0.1:8080/api/phone/incoming?token=$NANO_CLAW_PHONE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"data":{"event_type":"call.initiated","payload":{
       "call_control_id":"v3:loop-1","to":"+15125550100","from":"+15125559999"}}}'

# then call it, correlating on the same id
LOOPBACK_WS_BASE=ws://localhost:8080 LOOPBACK_CALL_ID='v3:loop-1' \
  .venv-test/bin/python scripts/phone_loopback_test.py "we do emergency repairs"
```

`TELNYX_API_BASE` and the two `LOOPBACK_*` variables all default to production
behaviour; they exist so this can be exercised without a carrier.

## Preflight before pointing a real number at it

```bash
cd ~/src/nano-claw
set -a; source .env; set +a
.venv-test/bin/python scripts/check_delegate_setup.py
.venv-test/bin/python scripts/check_delegate_setup.py --did +15125550100
```

It calls the same functions the live path calls, so a pass means the chain
works rather than that a copy of it does. Exit status is 0 only if every line
completed a round trip: start URL allowed, conversation minted, returned URL
same-origin, a repeated key returning the *same* conversation, and a real turn.

Worth running because every failure here is **quiet by design**. A delegate that
cannot be reached makes the gateway speak a fixed apology; a line that is not
configured behaves exactly as it always did. Both are correct, and both look
like nothing happening.

## Where the caller's wait comes from

Measured, because it decides where effort is worth spending:

| | |
|---|---|
| the seam itself | **~10 microseconds** per turn |
| a real delegate turn | **1.8–8.8 seconds** |

The gap is the delegate's own model. Nothing in this gateway is worth optimising
to change it — the phone thinking cue covers the wait (a chime, then ticks), and
genuinely closing it needs streaming, which contract v0 defers to v1 on purpose.

Same measurement from the other side: turns taken through the seam were no slower
than POSTing the delegate directly (5.7s / 3.4s versus 10.9s / 4.3s) — the spread
is turn-to-turn variance in the app, not overhead here.

`test_the_seam_adds_nothing_measurable_to_a_turn` keeps it that way, with a bound
100x the measured cost so it flags something real reaching the per-turn path
rather than noise.

## What the caller experiences

| Situation | What they hear |
|---|---|
| Line not in `NANO_CLAW_DELEGATE_STARTS` | today's persona — nothing changes |
| Start fails (app down, 404, bad URL) | the call is answered and handled undelegated |
| A turn fails | a fixed apology; the line stays open |
| The app has nothing to say | silence for that turn, which the contract permits |
| A turn takes ~6s | the thinking cue — a chime, then ticks |
| The app is at its conversation ceiling | answered undelegated; the reason is in the preflight and the gateway log, never in the call |

The start failure **fails open** on purpose, unlike everything else in this
seam. The alternative is dropping a real phone call because another service is
down.

## Known gaps

- **No PSTN call has run through this.** A loopback call has; see the top.
- **`hangup_after_playback` is unwired.** It exists and is tested — it waits the
  pacer's measured surplus so a goodbye is not cut mid-word — but with start
  failures falling open, no path currently needs to end a call.
- **Nothing tells the app a call ended.** The app times its own conversations
  out. A teardown message is plausible for v0.2; adding an endpoint whose
  failure mode is a leak, before anything leaks, is speculative.
- **A reply is capped at 32 KB** (~40 minutes of speech) and has C0 control
  characters stripped. Past the cap the caller hears the fixed apology, because
  a reply that long is a fault at the other end rather than a long answer.
- **The browser has no start seam.** A phone call mints its own conversation
  through `POST <start_url>`; a browser session cannot, so an operator must
  obtain a conversation URL by hand and paste it into APP URL.
  `scripts/try_delegate.sh` does that minting for you, which is how the gap was
  found — the console offered "Turn Delegate" with nowhere to send, and every
  turn was an apology. Tolerable because an operator pastes it once; worth
  closing if browser delegation becomes more than a way to try the seam.
- **The app cannot supply a greeting.** By design, for now. Doing it safely
  needs byte/duration/TTS-time limits, plain-text enforcement with SSML and
  control-character escaping, and a fixed fallback for every violation.
