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
extraction is injected: task 042 will choose `CF-Connecting-IP` only on its
trusted tunnel path and otherwise use the direct peer (`request.remote`). The
core never trusts forwarding headers itself.
