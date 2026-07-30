# The conversation-start seam (turn-delegate contract v0.1)

**Status:** proposed, for review before implementation
**Repos:** nano-claw (gateway), riff-builder-goal-driven (reference delegate)
**Supersedes nothing.** Adds one optional step to
`docs/turn-delegate-contract.md`; every existing v0 deployment keeps working.

## The problem this exists for

v0 says:

> Sessions: one delegate URL == one conversation. Apps that multiplex sessions
> encode the session in the URL path (riff-builder: `/api/session/{id}/turn`) —
> the gateway treats the URL as opaque.

That is satisfiable for the browser, where nano-claw is single-tenant and an
operator can paste one URL per tab. **It is not satisfiable for the phone.**

A DID is not a conversation. It is a phone number that several people can be
calling at the same moment. Config that maps `+15123569101 → <delegate_url>`
gives every simultaneous caller the *same* URL, so with riff-builder as the
delegate they all land in one session and their turns interleave — caller A's
answer about water heaters arriving as caller B's answer about a burst pipe,
into a single graph, with a single honesty controller trying to reconcile them.

The gateway cannot fix this alone. Minting a conversation means knowing the app
has a `POST /api/session` and what to put in it, which is exactly the
app-specific knowledge the contract exists to keep out of the gateway.

So the app must be able to hand out conversation URLs, and that requires one
new exchange.

## The seam

Config maps a DID (or a browser session) to a **start URL** instead of, or in
addition to, a delegate URL. When a conversation begins:

```
POST <start_url>
Content-Type: application/json

{"who": "caller", "channel": "phone" | "browser",
 "from": "<caller id, may be absent or withheld>",
 "to": "<the DID that was dialled, absent for browser>"}
```

Response, `200`:

```
{"delegate_url": "<opaque; this conversation only>",
 "greeting": "<optional; what to say first>"}
```

- `delegate_url` — opaque to the gateway, exactly as in v0. Every subsequent
  turn on this call POSTs here. The app decides what it means; riff-builder
  returns `/api/session/<fresh id>/turn`.
- `greeting` — optional. **This is a real gain, not decoration.** Delegate mode
  today greets with a line that deliberately names nobody, because contract v0
  gave the gateway no way to learn whose line it answered. With this field a
  plumbing company's phone opens in the plumbing company's own words, and the
  gateway still knows nothing about plumbing.
- Any non-200, malformed body, or missing/invalid `delegate_url` → the gateway
  speaks its fixed apology and ends the call. **It does not fall back to a
  static URL**, because the only static URL available is the shared one whose
  collision this seam exists to prevent.

### Backwards compatibility

A configuration that supplies a plain `delegate_url` and no start URL behaves
exactly as today: one URL, one conversation, no start call. That is correct for
a single-conversation app and is what the browser uses now. Nothing in v0
changes meaning.

## Security requirements

These are requirements, not open questions.

1. **The start URL is validated at set time** by the existing
   `validate_delegate_url` — http(s), loopback or `NANO_CLAW_DELEGATE_HOSTS`.
   Same reasoning as the delegate URL: it receives caller metadata on every
   call, and the routing map is persisted.

2. **The returned `delegate_url` is validated too, with the same allowlist.**
   This is the new hole and the important one. Without it, an app that is
   allowlisted for `builder.internal` can return
   `http://169.254.169.254/latest/meta-data/` and the gateway will POST
   everything the caller says there, on every turn, having "validated" only the
   start URL. A response is not more trusted than a config value — it is
   *less*, because the app may be compromised between the two.

3. **`greeting` is delegate-authored text that reaches TTS**, so it is exactly
   the injection surface `call_delegate` refuses to open for the `error` key.
   The difference is that `greeting` is *contractually* speech, where `error`
   was contractually a failure. It must still be type-checked (`str`), length-
   capped, and — because it is spoken before the caller has said anything —
   subject to the same treatment as any other reply. It must **not** be
   accepted from a non-200, and it must not be accepted alongside a missing or
   invalid `delegate_url`.

4. **No caller PII beyond what the app needs to route.** `from` may be absent
   or withheld; the app must tolerate that rather than 400.

## Failure modes, named

| What happens | What the caller hears | Why not something cleverer |
|---|---|---|
| Start URL unreachable | fixed apology, call ends | Silence is the failure this whole module exists to prevent |
| Start returns 200 with no `delegate_url` | fixed apology, call ends | A conversation with nowhere to send is not a conversation |
| Start returns a URL outside the allowlist | fixed apology, call ends | Refusing loudly beats dialling an unvetted host |
| Start succeeds, first turn fails | fixed apology, **call continues** | Per v0: a delegate failure is not a call failure |
| No start URL configured, static URL present | today's behaviour | v0 compatibility |
| Neither configured | fixed apology | Fail closed, as delegate mode already does |

## What this does NOT solve

- **Concurrency inside the app.** The seam guarantees each call gets its own
  conversation URL. Whether the app can actually run two conversations at once
  is the app's problem. riff-builder can (sessions are independent); noting it
  so the seam is not mistaken for a guarantee it does not make.
- **Call cleanup.** Nothing tells the app a call ended. A `POST <start_url>`
  sibling for teardown is a plausible v0.2; today the app times its own
  sessions out. Deliberately out of scope — adding an endpoint whose failure
  mode is a leak, before anything leaks, is speculative.
- **The dead-air problem.** Independent of this; tracked separately.

## Open question for review

Should the start call happen at `call.initiated` (before the Telnyx answer
command) or when the media WebSocket opens?

- **At initiate**: the greeting is ready before the caller hears anything, but
  it adds a round trip to answer latency, and Telnyx retries `call.initiated`
  webhooks — so the app could mint two sessions for one call unless the
  existing dedup covers it.
- **At media WS open**: no added answer latency and exactly one per call, but
  the DID must be carried forward from the webhook, which is why a
  `_call_routing` map keyed by `call_control_id` is needed either way.

Current preference is **media WS open**, on the grounds that minting two
sessions per call is a worse failure than a slightly later greeting, and the
dedup guard in `_answered` is about webhook retries rather than about session
minting. Reviewers should push back if the latency argument wins.
