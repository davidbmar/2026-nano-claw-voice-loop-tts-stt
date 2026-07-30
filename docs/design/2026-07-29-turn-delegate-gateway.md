# nano-claw as a voice gateway: implementing the turn delegate

**Status:** design, for review. Implements the seam marked "P3, to build in the
nano-claw repo" in riff-builder's `docs/turn-delegate-contract.md`.

## What this is

The contract already exists and riff-builder already implements the app side.
This document is only the nano-claw half.

> "One seam makes any app voice-capable: implement a single turn endpoint, and a
> voice gateway (nano-claw) does everything acoustic — mic/phone capture, VAD and
> endpointing, STT, TTS, barge-in. The gateway never knows what the app does; the
> app never knows audio exists."
> — `riff-builder/docs/turn-delegate-contract.md`

When a session has a delegate URL, nano-claw's **chat hop** becomes a POST to that
URL instead of a call to its own model. Everything acoustic stays here.

## The contract, restated as nano-claw sees it

```
POST <delegate_url>
{"text": "<what the human said>", "who": "caller" | "owner" | "operator"}

200 → {"reply": "<what to say back>", "focus": ["<app ids>"]}
```

- `focus` is app-defined and **the gateway ignores it** (a co-located UI uses it).
- Any non-200 → speak a fixed apology, keep the channel open. "The delegate must
  not be trusted to fail politely."
- Hard timeout 30 s; fill dead air past ~2 s.
- Barge-in stays entirely gateway-side. The delegate never learns it was
  interrupted; it just gets the next `text`.
- One delegate URL == one conversation. Apps that multiplex encode the session in
  the path (riff-builder: `/api/session/{id}/turn`). **The URL is opaque to us.**

## Where it lands in the code

Both transports already POST to `{NANO_CLAW_URL}/api/chat` with
`Accept: text/event-stream`, and — usefully — **both already have a non-SSE
branch**:

| transport | hop | non-SSE branch |
|---|---|---|
| browser WS | `voice/server.py:1498-1512` | `:1513-1517` → `_process_api_response` |
| phone | `voice/phone.py:1425-1441` | same pattern |

`_process_api_response` (`voice/server.py:2397`) reads
`{"type": "final", "response": "<text>"}` and emits `agent_reply` (`:2421`).

So the adapter is one translation, not a new pipeline:

```python
# delegate  {"reply": ..., "focus": [...]}
# becomes   {"type": "final", "response": <reply>}
```

`focus` is dropped at the boundary, per the contract. **Nothing downstream of
`_process_api_response` changes** — TTS, sentence pacing, playback tokens,
delivery receipts, barge-in and the metrics stash all keep working, because they
consume the translated shape.

## Config surface

Two carriers, matching the contract's "per-connection, not global":

**1. Per-session, for the browser console.** A new field on `Session`, set by
`POST /api/voice/delegate {"url": "..."}` — and it must be added to
`OPERATOR_PATHS` (`voice/webauth/aiohttp_adapter.py:85-92`) or it inherits no auth
and no same-origin guard. `GET` returns the current value. Persisted through
`persist_runtime_setting` the way `POST /api/voice/flow` already is
(`voice/server.py:2997`), so it survives a restart.

**2. Per-DID, for the phone.** A `did → delegate_url` map, so "a routing entry
mapping a DID to a delegate URL turns any contract implementer into a phone
agent." This is the contract's own stated prize: the phone-based builder interview
"becomes configuration, not a project."

`incoming_handler` (`voice/phone.py:2033`) already reads `payload["to"]` and keeps
a cid-keyed `_answered` dict (`:1945`); stash `cid → to` there and read it in
`media_ws_handler` (`:2073-2081`), where only `call_control_id` is in scope.
`CallSession.__init__` already accepts `_flow` / `_flow_domain_id` keyword
injection (`:600-602`), so the same door works for a delegate URL.

**Do NOT make it global.** The active mode is already a process-global singleton
(`voice/flow_session.py:191`) and that is the thing making one node serve one
business. Do not add a second global with the same defect.

## What happens to profiles

This is the question worth settling explicitly, because it looks like a
contradiction and is not.

**When a delegate is set, the profile does not apply to the turn.** The app owns
the brain: the system prompt, the tools, the knowledge, the model. Sending
`profile` to a delegate would be meaningless — riff-builder has its own interview
prompt and its own 21 graph tools.

**But three profile-adjacent things still belong to the gateway** and must keep
working:

| still gateway-owned | why |
|---|---|
| the spoken greeting | `_MODE_GREETINGS` (`voice/flow_session.py:154-178`) — the caller hears this before any delegate turn happens |
| the TTS voice | acoustic, by definition |
| barge-in / VAD / pacing | acoustic, by definition |

So a delegate is **a third kind of mode**, alongside `scheduler: true` (goal-region
flow) and plain persona modes. Proposal: `FlowModeConfig` gains an optional
`delegate_url`, and a mode carrying it skips the profile resolution entirely.
That keeps one registry rather than inventing a parallel one, and it means the
existing console dropdown can select a delegate the same way it selects a persona.

Modes are hardcoded today (`FLOW_MODES`, a Python dict) and profiles are baked
into the Docker image (`Dockerfile:46`), which is why "build out profiles as
needed" currently needs a `docker build`. That is a separate prerequisite
(nano-claw task 103's neighbour) and **not** on this critical path: a delegate
mode needs no profile at all.

## What this does NOT fix, stated plainly

**The delegate does not make riff-builder's chat faster.** Its response is atomic
by contract — "Streaming replies are a v1 extension, deliberately out of scope for
v0." riff-builder's measured 8.037s total / 8.036s ttfb is caused by its own
transport (`rb/agent.py`'s `ModelTransport` Protocol returns one whole
`ModelResponse`), and routing that through a delegate hop does not change it. It
will still be ~8 s, now with nano-claw waiting on it and filling dead air.

What the delegate DOES buy: nano-claw owns mic, VAD, endpointing, STT, TTS,
barge-in and the phone transport, so riff-builder stops needing any of that — and
the same seam makes the builder reachable by phone as configuration.

If speed is the goal, that is a different change (streaming the delegate response,
a v1 extension) and it should be designed separately rather than assumed to come
along with this.

## Failure behaviour

The contract is explicit that the delegate is untrusted. Concretely:

| case | behaviour |
|---|---|
| non-200 | speak a fixed apology line, keep the channel open, do not tear down the session |
| timeout > 30 s | same |
| malformed body (no `reply`, or `reply` not a string) | same — treat as non-200 |
| `reply` empty string | speak nothing, keep the channel open. Do NOT substitute a filler; an app may legitimately have nothing to say |
| connection refused | same as non-200 — this is the ordinary case when the app is not running |

Dead air past ~2 s gets an acknowledgment. nano-claw already has the machinery:
speech preparation and `stream_held` (`voice/server.py:2126-2140`).

Log every delegate failure with the URL and status. A silently-swallowed delegate
error is a caller hearing an apology for a reason nobody can find later.

## Testing

pytest, `tests/python/` (baseline **685 passed, 6 skipped, 0 failed**):

1. **The adapter translates.** `{"reply": "hello", "focus": ["a"]}` becomes
   `{"type": "final", "response": "hello"}`, and `focus` does not leak downstream.
2. **The delegate replaces the chat hop, and `/api/chat` is not called.** Assert
   with a fake that would fail if reached — the point is that nano-claw's model is
   not consulted.
3. **Every failure row above**, one test each. Especially: non-200 keeps the
   channel open, and an empty `reply` speaks nothing rather than a filler.
4. **Per-session, not global.** Two sessions, one with a delegate and one without,
   must not affect each other.
5. **Per-DID routing** picks the right delegate for the dialled number, and an
   unmapped DID falls back to the normal chat hop.
6. **Barge-in is unchanged.** The delegate receives the next `text` and never
   learns an interruption happened.
7. **The auth gate.** `POST /api/voice/delegate` without operator auth is refused —
   this is the test that catches forgetting `OPERATOR_PATHS`.

Every test gets a **vacuity check**: disable the behaviour it covers and confirm it
fails. This session has already found two suites that passed without exercising
their subject.

**End to end**, once it lands: point a browser session at riff-builder's
`/api/session/{id}/turn`, speak into nano-claw's console, and watch the builder's
goal canvas move — riff-builder rendering the screen from a turn it received over
someone else's microphone, which is the whole point.

## Open questions for review

1. Is `FlowModeConfig.delegate_url` the right carrier, or does a delegate deserve
   its own registry? One registry is simpler; a mode that skips profile resolution
   entirely is also a bit of a special case inside a structure that exists to
   select profiles.
2. Should `who` be derived from the transport (`phone` → `caller`, console →
   `owner`) or configured per delegate? The contract defines the values but not who
   decides them.
3. The phone greeting for a delegate mode: gateway-side `_MODE_GREETINGS`, or
   should the delegate get a "session start" call so the app owns its own opener?
   The contract does not cover session start at all, which may be a v0 gap worth
   naming.
4. Does anything need to stop the delegate being pointed at an arbitrary URL? It is
   operator-authed and loopback today, but it is an SSRF shape.
