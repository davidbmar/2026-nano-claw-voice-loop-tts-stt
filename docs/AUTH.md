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
