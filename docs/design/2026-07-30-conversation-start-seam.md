# The conversation-start seam (turn-delegate contract v0.1)

**Status:** revised after Codex review, 2026-07-30. Ready to implement.
**Repos:** nano-claw (gateway), riff-builder-goal-driven (reference delegate)
**Adds** one optional step to `docs/turn-delegate-contract.md`.

Review outcome: 3 HIGH, 4 MEDIUM, 1 LOW. Two HIGHs changed the design rather
than its wording — the returned-URL rule and the placement of the start call.
One HIGH removed a feature. What each finding changed is recorded inline, so a
later reader can see which sentences were bought with evidence.

## The problem this exists for

v0 says:

> Sessions: one delegate URL == one conversation. Apps that multiplex sessions
> encode the session in the URL path (riff-builder: `/api/session/{id}/turn`) —
> the gateway treats the URL as opaque.

That is satisfiable for the browser, where nano-claw is single-tenant and an
operator sets one URL per conversation. **It is not satisfiable for the phone.**

A DID is not a conversation. It is a phone number several people can be calling
at the same moment. Config mapping `+15123569101 → <delegate_url>` gives every
simultaneous caller the *same* URL, so with riff-builder as the delegate they
land in one session and their turns interleave — one caller's answer about water
heaters arriving as another's answer about a burst pipe, into a single graph,
with a single honesty controller trying to reconcile them.

The gateway cannot fix this alone. Minting a conversation means knowing the app
has a `POST /api/session` and what to put in it, which is exactly the
app-specific knowledge the contract exists to keep out of the gateway.

**There is no phone delegate wiring today at all.** `PhoneCall.__init__` carries
no delegate or DID state (`voice/phone.py:596-604`), the webhook does no routing
(`:2013-2045`), and the per-turn call goes to local `/api/chat` (`:1422-1450`).
An earlier draft of this document described the phone as already having a static
per-DID URL whose collisions this seam would fix. That was wrong — there is
nothing to be backward-compatible *with* on the phone, and this seam is a
prerequisite for phone delegation rather than an improvement to it. (Review
MEDIUM-4.)

## The seam

Config maps a DID to a **start URL**. When a call begins:

```
POST <start_url>
Content-Type: application/json

{"who": "caller", "channel": "phone",
 "conversation_key": "<stable per-call idempotency key>",
 "from": "<caller id, may be absent or withheld>",
 "to": "<the DID that was dialled>"}
```

Response, `200`:

```
{"delegate_url": "<this conversation only>"}
```

- `delegate_url` — every subsequent turn on this call POSTs here. May be
  relative (`/api/session/abc/turn`), in which case it resolves against the
  start URL. If absolute it must be **same-origin with the start URL**; see
  below.
- `conversation_key` — a stable, call-derived key. **The delegate must return
  the same conversation for the same key** rather than minting a second one.
  This is what makes the exchange safe to retry, and retries are not
  hypothetical: Telnyx redelivers webhooks and media streams can reconnect.
  (Review HIGH-2.)
- Any non-200, malformed body, or missing/invalid `delegate_url` → the gateway
  speaks its fixed apology and ends the call. It does **not** fall back to a
  static URL, because the only static URL available is the shared one whose
  collision this seam exists to prevent.

### `greeting` is deferred to v0.2, deliberately

The first draft had the start response carry a `greeting`, so a plumbing
company's phone could open in the plumbing company's own words.

Review HIGH-3 is right that this is an arbitrary-speech capability, and that
"type-check it and cap the length" is not a policy. It is the same surface
`call_delegate` refuses to open for the `error` key, except *contractually*
speech, which makes it harder to argue away. Doing it properly needs response-
byte, text-length, synthesized-duration and TTS-time limits; plain-text
enforcement with SSML and control-character escaping; a fixed fallback for every
violation; and a decision about whether a configured start service is trusted to
author speech at all.

That is a feature with its own design. The seam's job is per-call conversation
URLs, and it does that without ever accepting speech. Delegate mode's existing
greeting — deliberately naming nobody, because contract v0 gives the gateway no
way to learn whose line it answered — remains what the caller hears first, and
the delegate introduces itself in its first reply.

## Where the start call goes: at `call.initiated`

The first draft preferred the media WebSocket, reasoning that `_answered` was a
webhook-retry dedup rather than a session-minting guard. **That reasoning was
wrong** (review HIGH-2), and the correction inverts the decision:

- `_answered` checks and records the raw `call_control_id` *before any await*
  (`voice/phone.py:2013-2024`). A start POST placed after that guard is
  therefore already protected from ordinary webhook redelivery within a worker.
- The media handler has **no mint dedup at all**, and media streams can
  reconnect — so the placement chosen for its supposed exactly-once property
  has strictly less of it.
- The media WebSocket also cannot correlate to a routing map as the draft
  assumed: every call is handed the *same* stream URL, carrying only the shared
  token (`voice/phone.py:2025-2034`). `from` and `to` are used solely for
  logging and metrics. Correlation would require reading the raw
  `call_control_id` out of the media start message, or a signed nonce in the
  stream URL. (Review MEDIUM-1.)

So: start at `call.initiated`, after the `_answered` guard, **issued
concurrently with the `answer` command** rather than before it. That removes
the latency objection that motivated the media-WS placement in the first place.

The result is held in a `_call_routing` map keyed by the **raw**
`call_control_id` — not the sanitized id — which the media handler reads when
the stream opens. Entries expire on a TTL and are removed on `call.hangup`,
because they hold caller PII. (Review MEDIUM-1.)

Neither placement gives exactly-once across workers or restarts. That is what
`conversation_key` is for: correctness comes from the delegate being idempotent,
not from the gateway managing never to retry.

## Security requirements

These are requirements, not open questions.

1. **The start URL is validated at set time** by `validate_delegate_url` —
   http(s), no credentials, valid port, loopback or `NANO_CLAW_DELEGATE_HOSTS`.

2. **A returned `delegate_url` must be same-origin with the start URL** —
   identical scheme, host and port — or be relative and resolved against it.

   The first draft said "validate the returned URL with the same allowlist".
   Review HIGH-1 showed that is not enough: the allowlist admits *any port* on
   an allowed host and unconditionally admits **all** of loopback, so an
   allowlisted app could return `http://127.0.0.1:3001/...` (the Node agent API,
   no auth) or `http://127.0.0.1:8000/...` (the platform, which reads
   `tenant_id` and `permissions` from the request body) and the gateway would
   POST everything the caller says there.

   Same-origin closes the class rather than narrowing it: a response cannot
   introduce a destination that config did not already authorize. It costs
   nothing real — an app handing out its own conversation URLs is serving them
   from itself.

   The credential and port holes that review found in `validate_delegate_url`
   were live in shipped code and are already fixed (commit `5a5b47a`).

3. **The start exchange gets the same bounded controls as a turn**: explicit
   timeout, `follow_redirects=False`, a response-size limit, strict
   JSON-object validation, and cancellation if the call ends first. It sends
   caller metadata, so a followed redirect is a PII leak. (Review MEDIUM-3.)

4. **No caller PII beyond routing.** `from` may be absent or withheld; the app
   must tolerate that rather than 400.

## Ending the call on start failure needs machinery that does not exist

The draft said "speak the apology and end the call" as though ending were
available. It is not: the gateway issues `answer`, while `call.hangup` is an
inbound notification only, and `PhoneCall.close` is local teardown
(`voice/phone.py:603-614,757-760,2036-2050`). Tearing down the WebSocket
immediately would truncate the apology mid-word. (Review MEDIUM-2.)

`_telnyx_cmd` exists and the raw id is retained, so this is implementable — as
an explicit hangup command issued **after playback has drained**. It is real
work, not a line, and it is a prerequisite for this seam rather than a
consequence of it.

## Failure modes, named

| What happens | What the caller hears | Why not something cleverer |
|---|---|---|
| Start URL unreachable | apology, then hangup once drained | Silence is the failure this module exists to prevent |
| Start returns 200, no `delegate_url` | apology, then hangup | A conversation with nowhere to send is not a conversation |
| Returned URL is not same-origin | apology, then hangup | Refusing loudly beats dialling an unvetted origin |
| Start succeeds, first turn fails | apology, **call continues** | Per v0: a delegate failure is not a call failure |
| Start retried after a lost response | one conversation, not two | `conversation_key` — the delegate deduplicates |
| Neither start nor static URL configured | apology | Fail closed, as delegate mode already does |

## What this does NOT solve

- **Concurrency inside the app.** The seam guarantees each call its own
  conversation URL. Whether the app can run two at once is the app's problem.
  riff-builder can; noting it so the seam is not mistaken for a guarantee it
  does not make.
- **Call cleanup.** Nothing tells the app a call ended; the app times its own
  conversations out. A teardown sibling is plausible for v0.2 — deliberately
  not now, since an endpoint whose failure mode is a leak, added before anything
  leaks, is speculative.
- **The dead-air problem.** Independent; tracked separately.

## Implementation order

1. **DONE** (`d283662`) — `PhoneCall.hangup_after_playback`. Waits the pacer's
   measured surplus plus an unobservable-transit margin, and addresses the RAW
   carrier id. Not yet called by anything.
2. **DONE** — `_call_routing`, keyed by raw `call_control_id`, TTL'd, cleared on
   hangup.
3. **DONE** (`3cd953e`) — `start_conversation()` in `voice/turn_delegate.py`,
   same bounded controls as `call_delegate`, plus the same-origin rule.
4. **DONE** — the `call.initiated` hook, concurrent with `answer`, after the
   `_answered` guard.
5. **DONE** — the phone turn hop, mirroring the browser hop.
6. **DONE** (riff-builder `42636e6`) — `POST /api/delegate/start`, returning a
   relative `/api/session/<fresh id>/turn` and honouring `conversation_key`.

### The two ends are verified against each other

Over real HTTP, nano-claw's own `start_conversation` and `call_delegate` against
a live riff-builder, 2026-07-30:

- two callers on ONE DID got two conversations — the collision this seam exists
  to prevent;
- a redelivered webhook (same `conversation_key`) returned the SAME conversation
  rather than splitting one caller across two graphs;
- a withheld caller id was accepted rather than 400'd;
- a turn came back spoken in 5.9s, writing **0 WAVs** — `speak: false` holds on
  this path;
- the turn was attributed `who="owner"` even though the gateway sent
  `who="caller"`, so voice approval works on a phone builder line.

All six items are now implemented. What has NOT happened is a real phone call
through the seam — every phone-side test uses a stubbed carrier, because
exercising it for real means routing a DID at a live Telnyx number. The browser
path and both halves of the start exchange have been run against live services;
the phone path has not.

Two behaviours are deliberately unlike the rest of this module and worth
re-reading before changing:

- **A failed start fails OPEN.** The call is answered and handled as it is
  today rather than refused, because the alternative is dropping a real phone
  call because another service is down. Everywhere else in this seam fails
  closed.
- **The hangup path is still unwired.** `hangup_after_playback` exists and is
  tested, but nothing calls it: with a failed start now falling open, there is
  no path that needs to end a call. It is there for when one does.
