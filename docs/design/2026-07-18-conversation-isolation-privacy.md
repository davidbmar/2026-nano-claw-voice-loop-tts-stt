# Conversation isolation + transcript privacy (tight, no-auth)

Status: DESIGN — the narrow, standalone-secure slice carved out of the
Google-auth design after review 037. Ships FIRST, alone. No Google, no
OAuth, no new external dependency, no new network surface. It fixes two
live security defects and creates the isolation foundation any later
per-user feature needs.

Author: Claude (Fable 5). Security-hardening pass: Opus 4.8. Gate: Codex.

## Why this is its own design

Review 037 verified two defects that exist TODAY, independent of any
login feature:
- **Shared model context.** `SESSION_ID = "voice-default"`
  (voice/server.py:56) is sent as `sessionId` for every browser turn
  (:310, :619), keying one shared Node `Memory` that writes
  `<memory>/voice-default.json`. Any browser user can see prompts and
  answers from any other concurrent user's conversation.
- **Unauthenticated transcript side-channel.** `/api/metrics` returns
  `asked_text`/`said_text` for recent turns with no auth
  (metrics_db.py:41, server.py:711); the DB persists in the named
  volume and the console is on a public tunnel.

Neither needs auth to fix, and both must be fixed before any per-user
history is meaningful. Shipping this slice alone keeps the review
surface small and the change reversible.

## Non-goals (explicitly deferred)

- Google sign-in / any identity (separate design, blocked on this one).
- Persisting history (this slice makes conversations ISOLATED and
  EPHEMERAL; durable history rides on the later auth design).
- Phone-path changes (phone transcript logging and raw-caller storage
  are out of scope; the privacy note is narrowed to say so).
- Gating the existing anonymous mutation endpoints (flow/region-model
  updates) — tracked separately.

## Change 1 — per-socket conversation isolation

- Add `conversation_id` to the `Session` object, generated
  server-side as `voice-<32 lowercase hex>` (`uuid4().hex`) — MUST match
  the enforcing `EPHEMERAL_SESSION_RE = /^voice-[0-9a-f]{32}$/`
  (memory.ts:15); any other shape silently disables sweep + DELETE
  validation (Opus H-4) — when the WebSocket is accepted
  (`ws.prepare()`, server.py:86-89). The browser never supplies it and
  any client-sent id is ignored.
- Replace every use of the `SESSION_ID` module constant with
  `session.conversation_id`: the `/api/chat` call (server.py:310), the
  cancel call (:619), and the metrics `session_id` field (:587).
- Delete `SESSION_ID` the constant so it cannot be reintroduced.
- On socket close, delete that conversation's Node memory file
  (best-effort); a periodic sweep removes orphans whose sockets died
  without cleanup. Sweep bounds: age-based, capped scan.
- The existing `_spawn_agent` guard only serializes ONE socket
  (server.py:99-113); isolation here is by distinct memory id per
  conversation, so two concurrent sockets never share a `Memory`.

## Change 2 — metrics owns numbers, not text

- Stop writing `asked_text`/`said_text` from the browser metrics path
  (`_write_turn_metrics`, server.py:555+). New rows carry token counts,
  latencies, model, provider — no transcript columns.
- `/api/metrics` stops returning any text field.
- One-shot migration scrubs `asked_text`/`said_text` from existing rows.
  NULLing alone leaves the bytes recoverable on disk (SQLite doesn't
  zero freed cells; `secure_delete` is off) — the threat model is
  "read the DB file at rest," so the scrub MUST set
  `PRAGMA secure_delete=ON` before the UPDATE, or run a one-shot
  `VACUUM` after (Opus H-1). Self-check must include a raw-byte
  (`strings`) scan of the file, not just a `SELECT` (the SQL-layer
  check gives false confidence).
- Gate the whole migration on `PRAGMA user_version`: if `< 1`, run
  scrub+VACUUM then set `user_version=1`; else skip — otherwise it
  re-scans (and, with VACUUM, rewrites) the DB every boot (Opus H-2).
- Wire safety is two layers, both required: the `/api/metrics` handler
  strips text keys AND `recent()` projects an explicit non-text column
  allowlist instead of `SELECT *`, so transcript columns are never even
  read into process memory (Opus H-3).
- Keep the columns physically (avoid a table rebuild) but guarantee
  they are never written or served again for the browser path.

## Explicitly in scope for hardening review

- Memory-file cleanup must not delete another live conversation's file
  (id namespace, race between close and sweep).
- The migration must be idempotent and safe on a locked/partial DB
  (metrics is best-effort; a failed scrub must not break startup, but
  MUST NOT leave text served — the load-bearing wire guarantee is the
  handler key-strip + `recent()` allowlist, NOT the scrub).
- **Filename sanitization (Opus H-7):** `memoryPathFor`/`getMemory`
  MUST reject any `sessionId` outside an allowlist before building
  `<memory>/<id>.json` — `POST /api/chat` (server.ts, `body.sessionId`)
  and the phone path (`phone-{call_id[:24]}`, phone.py:307) currently
  trust the id, allowing `../` traversal outside the memory dir. This
  is pre-existing but squarely in scope since this design centralizes
  that path builder.
- **Known remaining transcript copies (name them; do not claim they're
  gone):** (a) `phone_calls.caller` = raw caller number
  (metrics_db.py:49-55); (b) the Node `voice-*.json`/`phone-*.json`
  memory files hold the actual conversation and are governed only by
  delete/sweep, not scrubbed; (c) the separate gateway `SessionManager`
  store (getConfigDir()/sessions) is off this path and untouched. The
  scheduler also logs extracted slots at INFO (server.py:~397) — out of
  scope, named for honesty.
- Confirm the Node agent side has no second public route exposing
  memory contents (Opus confirmed none today — keep it that way).
- **Ephemerality is bounded, not instant:** socket-close delete is
  best-effort; a failed close leaves the memory file until the idle
  sweep (currently 24h — recommend lowering). Startup sweep purges all
  prior-process anonymous files. State this bound honestly rather than
  claiming "ephemeral."

## Self-check contract (for the implementation task)

- Two concurrent WebSocket sockets, distinct sentinel utterances:
  prove neither appears in the other's prompt, reply, or memory file.
- After a signed-out session, `/api/metrics` returns zero transcript
  text AND a raw-byte `strings` scan of the DB file finds neither
  marker (NOT just a `SELECT … IS NULL` check — that passes while bytes
  remain; Opus H-1).
- Migration run twice is a no-op the second time via `user_version`;
  run on a DB with pre-existing text scrubs+VACUUMs it once and
  preserves numeric columns.
- A `sessionId` of `../../x` to `POST /api/chat` and a crafted phone
  `call_id` are both rejected before any file path is built (Opus H-7).
- Existing python + node suites pass unchanged.

## Status note

A reference implementation is already in the working tree (task 038,
in flight): `voice/server.py`, `voice/metrics_db.py`, `src/agent/memory.ts`,
`src/api/server.ts` + two new tests. This design's gate reviews THAT
diff. The Opus hardening items above (H-1 VACUUM, H-2 user_version,
H-4 id shape, H-7 filename allowlist) are the acceptance bar its result
must meet before commit.

## Rollout

One implementation task after this design passes. It touches
`voice/server.py`, `voice/metrics_db.py`, and the Node memory-id/cleanup
path only. Fully reversible (revert restores prior behavior; the
scrub is one-way but only removes data the design says should never
have been retained).
