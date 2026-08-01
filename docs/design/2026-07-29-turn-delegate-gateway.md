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

**The shape translation is one line. The adapter is not.** An earlier draft of
this document claimed "nothing downstream of `_process_api_response` changes";
Codex review (2026-07-30) showed that is wrong in five separate ways, and the
corrections below are the design.

```python
# delegate  {"reply": ..., "focus": [...]}
# becomes   {"type": "final", "response": <reply>}
```

`focus` is dropped at the boundary, per the contract. But the adapter must ALSO:

**Validate status and shape BEFORE translating, and own every failure itself.**
`_process_api_response` has no `else` branch (`voice/server.py:2431-2463`): a JSON
body that is not `type=="final"`, not `tool_pending`, and has a falsy `error` falls
off the end — no speech, no apology, no log, and the turn opened by
`_begin_agent_turn` is never completed or abandoned. riff-builder's own failure
produces exactly that shape: `session_turn` raises `HTTPException(502, "interview
agent failed: …")` (`rb/server.py:1677`), whose body is `{"detail": …}`. Fed
through a naive translation that is a **silent caller-facing turn**.

**Never forward delegate-authored text through the `error` path.**
`voice/server.py:2461-2463` speaks `data.get("error")` verbatim. A delegate
returning `200 {"error": "<anything>"}` is **TTS injection from a party the
contract explicitly calls untrusted**.

**Pass `timeout=30.0` per request and check the status code.** The contract's 30 s
hard timeout is enforced nowhere: both clients are built with `timeout=120.0`
(`voice/server.py:467`, `voice/phone.py:685`). With a naive adapter a hung delegate
holds a live phone caller for **two minutes**. The hop also never checks
`resp.status`, so "any non-200 → apology" is not inherited either — a non-200
either parses into the silent fall-through above or throws `JSONDecodeError` into
the generic handler.

**Build the dead-air filler as new work on both transports.** The cue machinery
this document previously cited (`stream_held`, the processing chime) lives *inside
the SSE-consumption loop* — cues fire between streamed events. A delegate response
is atomic and lands on the non-SSE branch, where `await resp.aread()`
(`voice/server.py:1525`, `voice/phone.py:1443`) blocks with zero events until the
whole body arrives. Nothing fills the ~2 s. A timer task racing the POST has to be
written, per transport.

## Config surface

Two carriers, matching the contract's "per-connection, not global":

**Delegate URLs are validated at set-time against an allowlist** — loopback plus
hosts named in `NANO_CLAW_DELEGATE_HOSTS` — the client uses
`follow_redirects=False`, attaches no cookies or operator headers, and refuses
non-`http(s)` schemes. This is a requirement, not an open question: the endpoint
accepts an arbitrary URL and then forwards *everything the human says* to it, every
turn. `OPERATOR_PATHS`' same-origin check is self-documented as CSRF mitigation
only — "It stops nothing else" (`voice/webauth/aiohttp_adapter.py:80-84`) — and the
per-DID map persisted via `persist_runtime_setting` writes into `.env`, which makes
a one-time compromise durable across restarts.

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

`incoming_handler` reads `payload.get("to")`, and `PhoneCall` (**not**
`CallSession` — that class does not exist) already accepts `_flow` /
`_flow_domain_id` keyword injection (`voice/phone.py:601-602`), so the same door
works for a delegate URL.

Use a **separate TTL'd `_call_routing: dict[cid, did]`, not `_answered`.**
`_answered` exists for webhook-retry dedup, is keyed by the raw
`call_control_id`, and is popped at hangup (`voice/phone.py:2049`) — so a retry
after the pop, or reordered events, loses the mapping. And `media_ws_handler`
explicitly supports calls that never hit the webhook at all — "loopback tests,
direct media connections" (`:2083-2085`) — where any stash is empty. **The
unmapped-DID fallback test must include that webhook-less path.**

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
flow) and plain persona modes. `FlowModeConfig` gains
`delegate_url: NotRequired[str]`.

**"Skips profile resolution" is not free: `profile: str` is REQUIRED**
(`voice/flow_session.py:45-52`). A delegate mode therefore carries
`profile: "none"` — the magic id that resolves to defaults with empty knowledge —
rather than omitting the field.

**And three pinned assertions break, which the earlier test plan missed:**
`tests/python/test_voice_flow.py:965-976` pins the exact mode list,
`:981-989` pins the `lawyer` dict by equality, and `:1006-1010` requires every
mode's profile be `"none"` or registered in `docker/default-config.json`. All three
are updated in the same commit that adds the mode.

**A delegate mode must supply its own greeting.** With no `_MODE_GREETINGS` entry,
`flow_mode_greeting` falls back to "You've reached the nano-claw voice assistant"
(`voice/flow_session.py:179-190`) — riff-builder's callers would be greeted by
another product's name. No generic fallback for delegate modes.

**The pre-answer health gate must probe the delegate.** Today it checks the local
pipeline only (`voice/phone.py:2092-2094`), so a dead delegate still answers the
phone, plays the wrong greeting, and then apologises on every turn — an apology
loop with no escape, which is the silent-call shape the gate exists to prevent.

Modes are hardcoded today (`FLOW_MODES`, a Python dict) and profiles are baked
into the Docker image (`Dockerfile:46`), which is why "build out profiles as
needed" currently needs a `docker build`. That is a separate prerequisite
(nano-claw task 103's neighbour) and **not** on this critical path: a delegate
mode needs no profile at all.

## What this does NOT fix — and the cheap win nobody had named

**The delegate does not make riff-builder's chat faster.** Its response is atomic
by contract — "Streaming replies are a v1 extension, deliberately out of scope for
v0."

**But the ~8 s figure was wrong in both directions.** `_run_turn` *awaits* `_speak`
before returning (`rb/server.py:1750-1752`), and `_speak` synthesises every sentence
to WAV (`:1768-1783`). So a delegate response is 8 s of LLM **plus a full turn of
TTS** — worse than advertised. And behind a gateway it is worse still:

> **riff renders audio nobody fetches, while nano-claw synthesises the same text
> again — two TTS passes per turn, on the same LuxTTS box.**

So the first delegate-path optimisation is **not** the v1 streaming extension. It is
suppressing riff's `_speak` side effect when the gateway owns audio: seconds saved
and TTS cost halved, with **zero contract change**. That belongs in riff-builder, and
it needs a signal that a gateway is driving the turn — the contract does not carry
one today, which is a v0 gap worth naming rather than inventing a private field for.

(Also: riff's own doc pins the root cause to `"stream": False` at
`rb/agent.py:333`. That line is the **Ollama** transport; the DeepSeek path is a
different class that sends no `stream` key at all. The atomicity is the
`ModelTransport` Protocol itself, not a flag.)

What the delegate DOES buy: nano-claw owns mic, VAD, endpointing, STT, TTS,
barge-in and the phone transport, so riff-builder stops needing any of it — and the
same seam makes the builder reachable by phone as configuration.

## Delegate turns and the money

"The metrics stash keeps working" is mechanically true and semantically false. With
no Node `debug` block, `_write_turn_metrics` records `model=""`, `provider=None`,
`tokens_in/out=None`, `est_cost_usd=None`, and — because there is no `first_delta` —
`llm_ttft_ms=None` (`voice/server.py:2319-2332`). So `/api/costs`
(`cost_ledger.build_report`) cannot attribute delegate traffic, and the latency
dashboard loses TTFT for **exactly the slowest turns in the system**.

Delegate turns record `model=f"delegate:{host}"` with `llm_total_ms` = the POST
duration. Token and cost fields are legitimately empty; the cost report shows a
delegate bucket rather than blanks.

## Conversation identity is a contract gap, not a greeting question

Nothing stops two browser sessions — or a browser session and a DID — carrying the
same delegate URL. That multiplexes two conversations into one riff session,
serialised only by `session.interview_lock` (`rb/server.py:1733`), **interleaving two
humans into one interview**.

And the reset is asymmetric: `_refresh_agent_conversation_on_mode_switch`
(`voice/server.py:1331-1343`) wipes Node-side memory on a mode switch, but the
delegate app's state survives — so switching away and back resumes a stale app
conversation the gateway believes is fresh.

So the v0 gap is **session lifecycle**, not just the greeting: there is no start
signal and no reset signal. The gateway must treat a delegate URL as exclusive
conversation identity, and warn or refuse when two live sessions share one.

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

pytest via the repo's `.venv-test` convention — baseline **688 passed, 3 skipped** at `af49816` (a bare count against a dirty tree is unverifiable):

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
