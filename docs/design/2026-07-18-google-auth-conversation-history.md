# Google sign-in + per-user conversation history (v2 — secure)

Status: DESIGN v2 — revised after adversarial review (037 REJECTED v1).
Scope: nano-claw first; portable auth CORE ports to riff via a semantic
store protocol (§7), not a shared DB handle. Author: Claude.
Re-reviewer: Codex (task 038).

v1 was rejected on three P0 facts, all verified in code:
- Every browser shares `SESSION_ID = "voice-default"` (server.py:56),
  so the agent's actual model memory is shared across users.
- `/api/metrics` returns `asked_text`/`said_text` unauthenticated
  (metrics_db.py:41, server.py:711) — a live transcript side-channel.
- The metrics DB already owns a `turns` table (metrics_db.py:36-45),
  so the v1 schema collides.

This version fixes the model first, treats deletion/ephemerality as
real invariants, and reframes portability around riff's actual
threaded-stdlib server + business-scoped Postgres/RLS.

## 0. Ordering (hard): privacy prerequisite BEFORE auth

The privacy/isolation defects exist TODAY, independent of login.
Task ordering therefore front-loads them; no auth UI ships until
isolation + the metrics leak are closed. See §9 decomposition.

## 1. Goals / non-goals

Goals
- "Sign in with Google" gives a user persistent, private conversation
  history in the console; signed-out is genuinely ephemeral.
- Server-enforced per-user isolation of BOTH stored history AND the
  live agent model context.
- A portable auth core (verification + session policy) reusable by riff
  against its own storage.

Non-goals (v1)
- Google API access on the user's behalf (login only; no refresh
  tokens, no offline scope).
- Linking phone callers to Google identities (phone stays hashed-caller
  keyed per the cost-ledger convention).
- Gating model/voice/tool spend behind login. See §8 — login grants
  HISTORY ONLY in v1; this is stated in the UI. (Protecting the
  existing anonymous mutation endpoints is tracked as its own item.)
- Rendering the Google profile picture (avoids a second external fetch
  and a privacy leak; use local initials).

## 2. Conversation isolation (P0 — the prerequisite)

Today `SESSION_ID` is a module constant sent as `sessionId` to the Node
agent (server.py:56, :310, :619), which keys a shared `Memory` and
writes `<memory>/voice-default.json`. Fix, regardless of auth:

- Each browser SOCKET gets a server-generated conversation id
  (`conv_<128-bit>`), created at `ws.prepare()` time and stored on the
  `Session` object. Every `/api/chat`, cancel, and metrics call uses
  `session.conversation_id`, never the module constant. The browser
  never supplies it.
- Signed-in: the conversation id is the persistent conversation's id,
  so history and model memory share one namespace.
- Signed-out: a fresh ephemeral id per socket; its Node memory file is
  deleted on socket close (best-effort with a swept fallback).
- Parallel-socket isolation test is mandatory (two sockets, distinct
  markers, prove no crossover in prompt, reply, tool approval, or
  memory file). The existing `_spawn_agent` guard only serializes ONE
  socket (server.py:99-113), so isolation is per-conversation-id, not
  per-process.

## 3. The transcript side-channel (P0 — must close before auth)

- Stop writing `asked_text`/`said_text` into metrics from the browser
  path (server.py:_write_turn_metrics), OR move them into the
  history store where they are owner-scoped and deletable. Decision:
  **metrics owns numbers, history owns text.** Metrics keeps token
  counts, latencies, model — no transcript columns for new rows;
  existing rows get scrubbed by a one-shot migration.
- `/api/metrics` stops returning any text column and requires no change
  in shape beyond dropping those fields (it's a numbers dashboard).
- Phone transcript logging (phone.py:610-624) and raw caller storage
  (phone.py:1009, metrics_db.py:49) are OUT of v1 history scope; the
  privacy statement is narrowed to say so explicitly rather than
  claiming "no transcript in logs."

## 4. Authentication mechanism

GIS ID-token flow, hardened:
1. Page fetches `GET /api/auth/config` (public, `Cache-Control:
   no-store`) → `{clientId, mode, nonce}` or `{mode:"off"}`. The GIS
   script and button load ONLY when a clientId is present. `nonce` is a
   one-time server-issued value bound to a pre-auth same-site cookie.
2. GIS returns a signed ID token carrying that nonce.
3. Page POSTs `{credential}` to `POST /api/auth/google` with header
   `X-NC-Auth: 1`.
4. Server verifies with the **`google-auth` library** (direct
   dependency, added to requirements.txt AND requirements.lock — not
   relying on aiortc's transitive cryptography): signature, `aud` ==
   configured client id, `iss` in the two Google issuers, `exp` with
   bounded skew, algorithm/key-type pinned, and the nonce matches +
   is consumed. Key fetch runs off the event loop (executor) with
   network timeout, response/JWT size caps, cache honoring Google's
   Cache-Control, single-flight refresh, and negative-cache/throttle
   on unknown `kid` (so a forged header can't force a fetch storm).
   The one-hour ID-token lifetime is the replay window; the nonce is
   what defeats replay — not a 5-minute assumption (v1 was wrong).
5. On success: upsert identity (store `sub` only as key; email/name are
   display), rotate session (delete any prior row for this login),
   issue a 256-bit random cookie token, store only its SHA-256 hash in
   `sessions`, set cookie `nc_session`: HttpOnly, `Path=/`, SameSite=Lax,
   Secure from **deployment config** (`NANO_CLAW_PUBLIC_HTTPS=1`), NOT
   `request.secure` (aiohttp ignores X-Forwarded-Proto; the tunnel's
   origin leg is plain HTTP so request.secure is false → v1 would have
   shipped a non-Secure cookie on HTTPS).
6. Logout closes the session row AND every live socket bound to it.

Config env (all forwarded by run.sh — currently none are, run.sh:221):
`NANO_CLAW_GOOGLE_CLIENT_ID`, `NANO_CLAW_AUTH` (optional|off),
`NANO_CLAW_PUBLIC_HTTPS`, `NANO_CLAW_AUTH_DB`,
`NANO_CLAW_SESSION_TTL_DAYS` (default 7). No session-hash pepper: tokens
are 256-bit CSPRNG so SHA-256-at-rest needs no salt/pepper (confirmed by
Opus review of task 040); dropped from this env list.
Missing client id → button hidden, console identical to today.
Also bind Docker publish to loopback: `127.0.0.1:9090:8080`
(run.sh:221 currently exposes on all interfaces).

## 5. Session hardening & request-security model

- Cookie token: 256-bit random; DB stores only the hash; a DB read
  never yields a replayable bearer.
- Absolute TTL 7 days (configurable) + 24h idle expiry; app-session
  lifetime is independent of Google's 1h `exp`.
- Periodic sweep of expired sessions; per-user session cap; revoke-all.
- Every MUTATING auth/history endpoint (incl. logout) requires:
  same-origin `Origin`/`Sec-Fetch-Site` check + the `X-NC-Auth: 1`
  header + zero CORS on these routes. Lax + custom-header is sufficient
  vs classic CSRF ONLY with these invariants tested (037 answer 5).
- WebSocket: validate `Origin` against the allowlist
  (`http://localhost:9090`, `https://nano.chattychapters.com`) BEFORE
  `ws.prepare()` — the browser WS constructor cannot add custom
  headers, so the HTTP CSRF header is not a WS substitute.
- Security headers on console + auth responses: CSP (with the GIS
  origins in script/frame/connect-src), `Referrer-Policy`,
  `X-Content-Type-Options: nosniff`; `no-store` on `/api/me` and all
  auth/history responses.
- Rate-limit `/api/auth/google` and JWKS refresh; behind the tunnel use
  `CF-Connecting-IP` only on the trusted path, else `request.remote`.

## 6. WebSocket identity binding

Resolve the `nc_session` cookie BEFORE `ws.prepare()` (server.py:86-89)
and attach `user_sub` (or None) + the conversation id to the socket for
its lifetime. Rules:
- The client never sends an identity or conversation field; server
  ignores any it finds.
- Login/logout take effect on the NEXT socket (reconnect); the contract
  requires the page to reconnect after both, and an in-flight turn is
  allowed to finish on the old (now-anonymous or now-authed) socket.
- Logout and session expiry actively CLOSE bound sockets — a cached
  identity must not outlive revocation.
- If a cookie is present but the auth store is unavailable: fail closed
  (reject the socket), do not silently treat as anonymous.

## 7. Data model (separate DB) + riff portability

New file `/app/data/auth-history.db` (`NANO_CLAW_AUTH_DB`), NOT tables
in metrics.db. Strict (fail-closed) — never inherits telemetry's
best-effort contract. `PRAGMA foreign_keys=ON`, WAL, `busy_timeout`,
`user_version` migrations, indexes, online-backup helper.

```
users(sub PRIMARY KEY, email, name, created_at, last_login)
tenants(id PRIMARY KEY, name)            -- nano seeds one: 'nano-claw'
memberships(tenant_id, user_sub, created_at, PRIMARY KEY(tenant_id,user_sub),
            FOREIGN KEY(user_sub) REFERENCES users(sub) ON DELETE CASCADE)
sessions(token_hash PRIMARY KEY, user_sub NOT NULL, tenant_id NOT NULL,
         created_at, expires_at, last_seen,
         FOREIGN KEY(user_sub) REFERENCES users(sub) ON DELETE CASCADE)
conversations(id PRIMARY KEY, tenant_id NOT NULL, user_sub NOT NULL,
              started_at, ended_at, title, turn_count DEFAULT 0,
              FOREIGN KEY(user_sub) REFERENCES users(sub) ON DELETE CASCADE)
conversation_turns(conversation_id, seq, role CHECK(role IN('user','agent')),
              text, ts, PRIMARY KEY(conversation_id, seq),
              FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)
INDEX conversations(tenant_id, user_sub, started_at DESC)
INDEX sessions(expires_at)
```
- Renamed `conversation_turns` (v1 `turns` collided).
- Tenant scope is present NOW (nano supplies fixed `nano-claw`); every
  ownership key and query is `(tenant_id, user_sub)`; tenant is never
  page-supplied. Identity (`users.sub`) is modeled separately from
  membership so a Google identity can belong to multiple businesses —
  the shape riff needs.
- Cascading deletes make "delete all" one transaction; item-delete
  cascades turns; deletion also removes the conversation's Node memory
  file and closes an active socket before it can append.
- Bounded text/title lengths; cursor pagination + hard page sizes on
  list and turn APIs.

Portable boundary (037's correction — NOT "a DB handle in"):
1. Pure claim/session value objects + policy (clock/random injected),
   zero HTTP framework, zero SQL.
2. A semantic `AuthStore` protocol: `upsert_identity`,
   `issue_hashed_session`, `resolve_session`, `revoke`, `sweep`, all
   tenant-scoped. Nano implements it over the SQLite file; riff
   implements it against its Postgres data-service (which already has
   `business`, memberships, scoped tokens, RLS —
   services/data-service/migrations/0001_core.sql, 0003_calls.sql).
3. An injected Google verifier / key-cache interface; each app declares
   `google-auth` directly.
4. Transport adapters are per-app: nano has one aiohttp adapter; riff
   needs TWO — a threaded-`BaseHTTPRequestHandler` cookie/route adapter
   and a pre-upgrade WebSocket handshake hook (riff's live WS on
   :8766 parses identity AFTER upgrade today — transport.py:1863; it
   must move to a handshake hook, and the browser's hard-coded
   `ws://127.0.0.1:<port>/live` must become same-host `wss` so cookies
   are delivered). riff also emits wildcard CORS (web_server.py:2390) —
   auth routes must be excluded from it.

Verification + token policy port unchanged; cookies, routing, WS
handshake, storage, and tenant authorization are per-app.

## 8. Product authorization scope (explicit)

Login grants HISTORY ONLY in v1. Anonymous users can still open `/ws`,
spend model/voice, generate previews, mutate flow/region-model
(server.py:740, :771 — currently unauthenticated), and approve tools.
The UI states this. Protecting those mutation endpoints is a SEPARATE
tracked item (proposal), not silently implied by "optional auth".

## 9. Implementation decomposition (7 tasks, ordered)

Adopted from the 037 review; P0 privacy work is NOT combined with UI.
1. Conversation isolation + metrics-text removal (§2, §3) — ships
   first, no auth. Gate: two-socket crossover test; DB inspection finds
   no persisted text signed-out; `/api/metrics` has no text fields.
2. Portable auth policy + nano SQLite store (§7.1-2) — offline tests:
   expiry/rotation, ownership, cascades, migrations, concurrent
   readers, locked-DB, backup+restore, and an import check proving the
   core imports no aiohttp / nano-claw.
3. Google verifier + login abuse boundary (§4) — locally-signed fake
   JWT matrix (no network), wrong alg/kid/aud/iss/nonce, expired/
   oversized, cache rotation/outage, replayed nonce, refresh storm,
   rate-limit cases.
4. Nano aiohttp auth/session adapter (§4-6) — localhost vs simulated
   trusted-tunnel cookie flags, 401/404/503, logout cookie clearing,
   cross-origin HTTP/WS rejection, no-CORS, a cloudflared sentinel-
   cookie WS probe.
5. History capture + owner-only API (§7) — all real completion paths
   (streaming `agent_reply_done` server.py:483; scheduler :326;
   non-stream :649 — which differ), bounded text, pagination, single-
   query owner filter, non-owner 404, transaction-safe seq/count,
   delete leaves zero transcript copies.
6. GIS + history UI + security headers (§4-5) — auth off / no client id
   / blocked-slow GIS / offline JWKS / CSP capture / XSS-shaped
   names+titles+turns (no innerHTML for stored text; local initials,
   no avatar fetch) / logout during live socket / mobile.
7. Riff portability acceptance (in riff) — reuse the unchanged pure
   core; implement riff's tenant store over its data-service + the two
   adapters + same-host wss; business A/B 404 wall; wildcard-CORS
   regression; threaded concurrency; existing session/archive compat.

## 10. Human prerequisite

- [DONE] OAuth client id created (Google Cloud project
  `picture-qr-album-login`, Web application) and stored in `.env` as
  `NANO_CLAW_GOOGLE_CLIENT_ID`; no client secret (ID-token flow).
  `NANO_CLAW_AUTH=optional`, `NANO_CLAW_PUBLIC_HTTPS=0`; run.sh forwards
  all three.
- [TODO — user] Authorized JS origins on that client must include
  `http://localhost:9090` and `https://nano.chattychapters.com` (decide
  `127.0.0.1` explicitly — default: NOT added).
- [TODO — user] The consent screen is in Testing mode, so add
  `david.bryan.mar@gmail.com` (and any other testers) under OAuth
  consent screen → Test users, or Google blocks sign-in even once the
  button exists.

## 12. Implementation tasks (queued)

Task 1 (isolation + metrics-text) shipped as nano-claw 038+039
(committed 9744f3b, Opus-hardened). Remaining, dependency-ordered:
- nano-claw 040 — portable auth policy + SQLite AuthStore (§7.1-2)
- nano-claw 041 — Google verifier + login abuse boundary (§4)
- nano-claw 042 — aiohttp auth/session adapter + WS binding (§4-6)
- nano-claw 043 — history capture + owner-only API (§7)
- nano-claw 044 — GIS button + history UI + security headers (§4-5)
- riff 036 — riff portability acceptance (§7)
Each: Codex authors → Opus 4.8 security-hardens the security-critical
ones (040/041/042) → Claude judges against that bar → commit.

## 11. Resolved review questions

1. Storage: separate `auth-history.db`, backed up OUTSIDE the volume.
2. Emotion/flow metadata: NOT persisted in v1 (client-side/overridable,
   not historical truth); text + role only.
3. Cookie lifetime: 7-day absolute + 24h idle default (was 30d).
4. Cookie behind tunnel: transport works; Secure comes from deploy
   config, not `request.secure`; add a sentinel-cookie WS test.
5. Lax + custom header: sufficient vs classic CSRF only with same-
   origin + Fetch-Metadata + zero-CORS + WS Origin + nonce, all tested.
