# Delegating a phone line to another app

nano-claw answers the phone and owns everything acoustic — mic, VAD,
endpointing, STT, TTS, barge-in. Another app answers the *words*, one HTTP POST
per turn. This is the operator's side of that.

Contract: riff-builder `docs/turn-delegate-contract.md`.
Design and review: `docs/design/2026-07-30-conversation-start-seam.md`.

> **Nothing here has carried a real call yet.** Every phone-side test stubs the
> carrier. The preflight below exercises the full chain over real HTTP, which is
> the closest thing to proof that exists today.

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

Non-loopback start URLs need an allowlist:

```bash
NANO_CLAW_DELEGATE_HOSTS='builder.internal,other.internal'
```

Everything is read **per call**, so adding a line needs no restart — and
restarting drops live calls.

## The app side (riff-builder)

```bash
RB_BUILDER_DIDS='{
  "+15125550100": {"business_name": "Rivera Plumbing", "industry": "plumbing"}
}'
```

**This is an authorization boundary.** Whoever dials a configured number can
edit that business by voice — no PIN, no caller-id check. It is acceptable only
for unpublished builder lines. That is also why an unknown DID gets a 404 rather
than a new session: otherwise any wrong number could create graphs without limit.

A phone-started session is pinned to the **owner** role. The gateway sends
`who: "caller"`, correctly — it has no idea this DID is a builder line — but
`who == "owner"` is what gates voice approval, so a phone owner marked "caller"
could not approve anything.

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

## What the caller experiences

| Situation | What they hear |
|---|---|
| Line not in `NANO_CLAW_DELEGATE_STARTS` | today's persona — nothing changes |
| Start fails (app down, 404, bad URL) | the call is answered and handled undelegated |
| A turn fails | a fixed apology; the line stays open |
| The app has nothing to say | silence for that turn, which the contract permits |
| A turn takes ~6s | the thinking cue — a chime, then ticks |

The start failure **fails open** on purpose, unlike everything else in this
seam. The alternative is dropping a real phone call because another service is
down.

## Known gaps

- **No real call has run through this.** See the note at the top.
- **`hangup_after_playback` is unwired.** It exists and is tested — it waits the
  pacer's measured surplus so a goodbye is not cut mid-word — but with start
  failures falling open, no path currently needs to end a call.
- **Nothing tells the app a call ended.** The app times its own conversations
  out. A teardown message is plausible for v0.2; adding an endpoint whose
  failure mode is a leak, before anything leaks, is speculative.
- **The app cannot supply a greeting.** By design, for now. Doing it safely
  needs byte/duration/TTS-time limits, plain-text enforcement with SSML and
  control-character escaping, and a fixed fallback for every violation.
