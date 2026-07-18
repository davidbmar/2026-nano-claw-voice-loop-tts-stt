# Authentication storage

Nano-claw keeps authentication and conversation history in a dedicated SQLite
database. `NANO_CLAW_AUTH_DB` selects the file and defaults to
`/app/data/auth-history.db`; it must never point at `metrics.db`.

This layer has no HTTP routes, Google integration, or browser UI. It is the
storage and policy boundary those layers use later.

## Tenant and store contract

`voice.webauth.store.AuthStore` is a semantic protocol, not a database-handle
interface. Nano-claw configures a trusted `nano-claw` tenant. A page never
supplies its tenant or subject. Identity rows are global, membership is
tenant-scoped, and a valid session resolves to exactly `{"sub": ..., "tenant":
...}`.

The operations are:

- `upsert_identity(sub, email, name)`: update display claims and ensure
  membership in the store's trusted tenant.
- `issue_hashed_session(sub, tenant, now)`: require membership, atomically
  rotate the subject's prior session in that tenant, persist only a SHA-256
  digest, and return the new raw bearer once.
- `resolve_session(raw_token, now)`: return subject and tenant for a valid
  session while advancing its idle timestamp. It never returns a raw bearer.
- `revoke(raw_token)`, `revoke_all(sub)`, and `sweep(now)`: delete one bearer,
  every bearer for an identity, or sessions past either expiry boundary.

The contract is fail-closed. A genuinely missing, revoked, or expired bearer
returns `None`. Locking, corruption, I/O, migration, and other store failures
raise; an adapter must not convert those errors into an anonymous identity.

Session bearers contain 256 random bits and are unpadded base64url strings. The
portable policy requires injected random-byte and clock callables; nano's
SQLite adapter supplies `secrets.token_bytes` and an aware UTC clock by
default. The database stores only the bearer's 64-character lowercase SHA-256
digest. The default absolute lifetime is seven days
(`NANO_CLAW_SESSION_TTL_DAYS` can override it) and the sliding idle lifetime is
24 hours. Reaching either deadline expires the session.

## Schema and migrations

Timestamps are UTC Unix seconds. `PRAGMA user_version` owns forward-only,
transactional migrations; schema version 1 creates:

- `users(sub, email, name, created_at, last_login)`
- `tenants(id, name)`, seeded with `nano-claw`
- `memberships(tenant_id, user_sub, created_at)` with a composite primary key
- `sessions(token_hash, user_sub, tenant_id, created_at, expires_at,
  last_seen)`
- `conversations(id, tenant_id, user_sub, started_at, ended_at, title,
  turn_count)`
- `conversation_turns(conversation_id, seq, role, text, ts)`

Membership, session, and conversation ownership is protected by foreign keys.
Deleting a user cascades through memberships, sessions, conversations, and
conversation turns; deleting a conversation cascades its turns. The history
tables are created here but are populated by the history task.

Each operation opens a short-lived connection with `foreign_keys=ON`, WAL
journaling, a bounded `busy_timeout`, and strict error propagation. There is an
expiry index on `sessions(expires_at)` and a descending history index on
`conversations(tenant_id, user_sub, started_at DESC)`.

## Backup and restore

Backups must live outside the application data volume. Do not copy the live
`.db`, `-wal`, and `-shm` files independently. Use SQLite's online backup API:

```python
from voice.webauth.sqlite_store import SQLiteAuthStore

store = SQLiteAuthStore()
store.backup("/mnt/nano-claw-backups/auth-history-2026-07-18.db")
```

Use a new destination pathname for every backup. The helper produces a
consistent snapshot while the live WAL database remains available and runs
`PRAGMA integrity_check` plus `PRAGMA foreign_key_check` before returning.

To restore, stop nano-claw so no process has the destination open. Preserve the
current database and any `-wal`/`-shm` sidecars as a rollback set. Restore the
chosen backup into a new staging file:

```python
from voice.webauth.sqlite_store import backup_database

backup_database(
    "/mnt/nano-claw-backups/auth-history-2026-07-18.db",
    "/app/data/auth-history.restore.db",
)
```

Then verify the staging file with `PRAGMA integrity_check`,
`PRAGMA foreign_key_check`, and `PRAGMA user_version`. With no old sidecars at
the live pathname, atomically rename the verified staging file to the configured
`NANO_CLAW_AUTH_DB` path and start nano-claw. Opening the store re-enables WAL,
foreign keys, and validates the supported migration version.

## Google ID-token verification

`voice.webauth.google_verifier.GoogleIDTokenVerifier` is the portable,
framework-free login verifier. Its async `verify_id_token(credential, *, now,
expected_aud, expected_nonce)` method returns only `sub`, `email`, and `name`.
Only Google's stable `sub` claim is an identity or authorization key;
`email` and `name` are bounded display values.

The verifier delegates JWT signature and audience verification to the direct
`google-auth` dependency. It accepts only `RS256`, a named Google key backed by
an RSA public key of at least 2048 bits, the exact configured client ID, and
either `accounts.google.com` issuer spelling. Issued-at, not-before, expiry,
and the one-hour maximum ID-token lifetime use the injected UTC time and at
most 60 seconds of clock skew. Credentials are ASCII and capped at 16 KiB;
malformed, oversized, wrongly signed, expired, or future tokens produce one
bounded error that contains no token or claims.

`GoogleKeyCache` retrieves Google's x509 key map with a five-second network
timeout and a 256 KiB decoded-response cap. A synchronous production fetch is
always run in a worker thread; an async fetcher or a complete key provider can
instead be injected. The cache honors `Cache-Control` `max-age` (and `Age`, or
`Expires` as a fallback), caps retention at 24 hours, validates every cached
key, and never uses an expired key after a refresh failure. Refreshes are
single-flight. A global 30-second refresh gate plus short negative entries for
unknown `kid` values prevents forged headers—even unique ones—from creating a
fetch storm.

Key retrieval or verification failure fails a new login closed. It never
falls back to unsigned decoding or Google's debugging endpoint. Existing nano
application sessions are local, independent of Google's one-hour token, and
continue to resolve through `AuthStore` during a Google-key outage.

## One-time nonce and login abuse boundary

`LoginNonceStore` issues independent 256-bit pre-auth and nonce values. The
transport puts the pre-auth value in a host-only same-site cookie and returns
the nonce to GIS. On login, it resolves the expected nonce through that cookie;
the verifier compares the signed claim and atomically consumes the challenge
only after every other check succeeds. A nonce is live for ten minutes, one
binding has one current challenge, pending state is memory-bounded, and a
restart safely invalidates outstanding challenges. Two concurrent replays can
therefore produce at most one successful login.

`LoginRateLimiter` is a reusable sliding-window boundary. Defaults allow ten
attempts per IP and five per verified `sub` per minute, with bounded bucket
storage and fail-closed behavior at capacity. An adapter performs the IP check
before expensive token verification and the `sub` check afterward. IP
extraction is injected: nano's aiohttp adapter chooses `CF-Connecting-IP` only
on its trusted tunnel host and otherwise uses the direct peer
(`request.remote`). The core never trusts forwarding headers itself.

## aiohttp routes and cookie contract

`voice.webauth.aiohttp_adapter.AiohttpAuthAdapter` registers the authentication
routes before nano's flat static-file route:

- `GET /api/auth/config` is public. With both
  `NANO_CLAW_AUTH=optional` and a nonempty
  `NANO_CLAW_GOOGLE_CLIENT_ID`, it returns `clientId`, `mode`, and a fresh
  one-time `nonce`. Partial, missing, explicit-off, or unknown configuration
  returns exactly `{"mode":"off"}` and does not initialize the auth database.
- `POST /api/auth/google` accepts only a `credential`. It resolves the expected
  signed nonce strictly through the host-only `nc_pre_auth` cookie created by
  the config route; a nonce in JSON is ignored. Successful verification
  upserts display claims, atomically rotates the tenant session, and returns
  the signed-in user.
- `GET /api/me` resolves and idle-touches `nc_session`, returning the trusted
  `sub` and `tenant`, or HTTP 401 for a missing, revoked, or expired bearer.
  Operational store failures remain distinct and fail closed with HTTP 503.
- `POST /api/auth/logout` revokes that exact bearer, clears the cookie, and
  actively closes every WebSocket bound to it. A store failure still clears
  the browser cookie and closes the local sockets, but reports HTTP 503 because
  durable revocation could not be confirmed.

All auth responses, including errors, use `Cache-Control: no-store`. Invalid ID
tokens and unknown Google key ids have the same HTTP 401 status and JSON body;
key-fetch outages use a generic HTTP 503 response. This avoids a key-id
validity oracle without misreporting an operational outage as a bad password.
Production constructs the Google key cache through its normal rotating fetch
path and never supplies `initial_keys`.

The application bearer cookie is `nc_session`: host-only, `HttpOnly`,
`Path=/`, and `SameSite=Lax`. Its `Max-Age` follows the store's absolute policy
(seven days by default), while the store independently enforces the 24-hour
sliding-idle boundary. `Secure` is controlled only by
`NANO_CLAW_PUBLIC_HTTPS=1`; aiohttp's `request.secure` and forwarded-proto
headers are intentionally ignored. Cookie deletion repeats the same path,
HttpOnly, SameSite, and Secure attributes so the exact cookie is removed.
The short-lived `nc_pre_auth` cookie has the same transport attributes and is
cleared after a successful login.

## HTTP and WebSocket request security

Every unsafe auth or conversation-history request requires all three of:

1. `Origin` exactly `http://localhost:9090` or
   `https://nano.chattychapters.com`;
2. `Sec-Fetch-Site: same-origin`; and
3. `X-NC-Auth: 1`.

The middleware emits no `Access-Control-Allow-*` headers and strips any such
headers from sensitive responses, including failures. Browser preflight is
therefore never an alternate authorization path. These checks intentionally
do not gate nano's existing anonymous voice/model/control endpoints; Google
login grants history identity only.

The browser WebSocket cannot supply the custom HTTP header, so `/ws` separately
requires one of the same two exact `Origin` values before upgrade. If a session
cookie exists, the adapter resolves it before `WebSocketResponse.prepare()`,
fails closed on a store outage, and fixes `user_sub`, `tenant`, and the
server-generated conversation id to the socket for its lifetime. Browser
messages cannot override them. Login or logout is visible on the next socket;
session rotation, logout, and detected expiry close any socket whose cached
identity has become invalid.

The main console and auth responses carry a CSP that permits only local assets
plus the declared GIS script, frame, style, and connection endpoints. It does
not permit a Google profile-image origin. They also carry
`Referrer-Policy: strict-origin-when-cross-origin` and
`X-Content-Type-Options: nosniff`.

## Deployment boundary

`run.sh` forwards `NANO_CLAW_GOOGLE_CLIENT_ID`, `NANO_CLAW_AUTH`, and
`NANO_CLAW_PUBLIC_HTTPS` and publishes the container only on
`127.0.0.1:9090:8080`. The public host is consequently the trusted local
cloudflared ingress path: only requests whose `Host` is
`nano.chattychapters.com` may use `CF-Connecting-IP` for login limiting. Direct
localhost traffic ignores that header and uses aiohttp's peer address.
