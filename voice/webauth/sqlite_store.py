"""Fail-closed SQLite implementation of the portable auth store.

Nano-claw has one trusted tenant (``nano-claw``) by default.  Identities are
global, memberships and sessions are tenant-scoped, and a resolved session
always returns both values.  Each operation uses its own short-lived
connection with foreign keys enabled.  Unlike the metrics database, no error
is swallowed: callers must distinguish a genuinely missing/expired session
from an operational failure and fail closed on the latter.
"""

from __future__ import annotations

import math
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from .policy import SessionPolicy, normalize_datetime
from .store import ResolvedSession

DEFAULT_AUTH_DB_PATH = "/app/data/auth-history.db"
DEFAULT_TENANT_ID = "nano-claw"
DEFAULT_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 2
MAX_TOKEN_GENERATION_ATTEMPTS = 16

MAX_SUB_LENGTH = 255
MAX_EMAIL_LENGTH = 320
MAX_NAME_LENGTH = 256
MAX_TENANT_LENGTH = 255
MAX_TITLE_LENGTH = 200
MAX_TURN_TEXT_LENGTH = 100_000
MAX_CONVERSATION_ID_LENGTH = 128
MAX_CONVERSATION_PAGE_SIZE = 50
MAX_TURN_PAGE_SIZE = 100

TurnRole = Literal["user", "agent"]


class UnsupportedSchemaVersion(RuntimeError):
    """Raised when a database was created by a newer implementation."""


class MembershipNotFound(LookupError):
    """Raised when session issuance lacks tenant membership."""


class SessionTokenCollision(RuntimeError):
    """Raised if the injected RNG repeatedly generates an existing bearer."""


class ConversationNotFound(LookupError):
    """Raised when an owner-scoped conversation mutation finds no row."""


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        f"""
        CREATE TABLE users (
          sub TEXT PRIMARY KEY
            CHECK(length(sub) BETWEEN 1 AND {MAX_SUB_LENGTH}),
          email TEXT
            CHECK(email IS NULL OR length(email) <= {MAX_EMAIL_LENGTH}),
          name TEXT
            CHECK(name IS NULL OR length(name) <= {MAX_NAME_LENGTH}),
          created_at REAL NOT NULL,
          last_login REAL NOT NULL
        )
        """,
        f"""
        CREATE TABLE tenants (
          id TEXT PRIMARY KEY
            CHECK(length(id) BETWEEN 1 AND {MAX_TENANT_LENGTH}),
          name TEXT NOT NULL
            CHECK(length(name) BETWEEN 1 AND {MAX_NAME_LENGTH})
        )
        """,
        """
        CREATE TABLE memberships (
          tenant_id TEXT NOT NULL,
          user_sub TEXT NOT NULL,
          created_at REAL NOT NULL,
          PRIMARY KEY (tenant_id, user_sub),
          FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
          FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE sessions (
          token_hash TEXT PRIMARY KEY NOT NULL
            CHECK(length(token_hash) = 64),
          user_sub TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          created_at REAL NOT NULL,
          expires_at REAL NOT NULL,
          last_seen REAL NOT NULL,
          FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, user_sub)
            REFERENCES memberships(tenant_id, user_sub) ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE conversations (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_sub TEXT NOT NULL,
          started_at REAL NOT NULL,
          ended_at REAL,
          title TEXT
            CHECK(title IS NULL OR length(title) <= {MAX_TITLE_LENGTH}),
          turn_count INTEGER NOT NULL DEFAULT 0 CHECK(turn_count >= 0),
          FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, user_sub)
            REFERENCES memberships(tenant_id, user_sub) ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE conversation_turns (
          conversation_id TEXT NOT NULL,
          seq INTEGER NOT NULL CHECK(seq >= 0),
          role TEXT NOT NULL CHECK(role IN ('user', 'agent')),
          text TEXT NOT NULL CHECK(length(text) <= {MAX_TURN_TEXT_LENGTH}),
          ts REAL NOT NULL,
          PRIMARY KEY (conversation_id, seq),
          FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            ON DELETE CASCADE
        )
        """,
        "CREATE INDEX idx_sessions_expires_at ON sessions(expires_at)",
        """
        CREATE INDEX idx_conversations_tenant_user_started_at
          ON conversations(tenant_id, user_sub, started_at DESC)
        """,
        """
        INSERT INTO tenants(id, name) VALUES('nano-claw', 'nano-claw')
        """,
    ),
    2: (
        """
        ALTER TABLE conversations
          ADD COLUMN history_incomplete INTEGER NOT NULL DEFAULT 0
            CHECK(history_incomplete IN (0, 1))
        """,
    ),
}


def _validate_required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or len(value) > maximum:
        raise ValueError(f"{field} must contain 1 to {maximum} characters")
    return value


def _validate_optional_text(
    value: str | None, field: str, maximum: int
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    if len(value) > maximum:
        raise ValueError(f"{field} must contain at most {maximum} characters")
    return value


def _validate_conversation_id(value: str) -> str:
    return _validate_required_text(
        value, "conversation_id", MAX_CONVERSATION_ID_LENGTH
    )


def _validate_turn_role(value: str) -> TurnRole:
    if value not in ("user", "agent"):
        raise ValueError("role must be 'user' or 'agent'")
    return value


def _validate_turn_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("text must be a string")
    if len(value) > MAX_TURN_TEXT_LENGTH:
        raise ValueError(
            f"text must contain at most {MAX_TURN_TEXT_LENGTH} characters"
        )
    return value


def normalize_conversation_title(text: str) -> str:
    """Collapse whitespace and bound the first utterance used as a title."""

    value = _validate_turn_text(text)
    normalized = " ".join(value.split())
    return normalized[:MAX_TITLE_LENGTH]


def _validate_query_limit(value: int, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    # Callers request one extra row to determine whether a next page exists.
    if value < 1 or value > maximum + 1:
        raise ValueError(f"limit must be between 1 and {maximum + 1}")
    return value


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _timestamp(value: datetime) -> float:
    return normalize_datetime(value).timestamp()


def _datetime(value: object) -> datetime:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("stored session timestamp is not numeric")
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _default_policy() -> SessionPolicy:
    raw_days = os.environ.get("NANO_CLAW_SESSION_TTL_DAYS")
    if raw_days is None:
        return SessionPolicy(
            random_bytes=secrets.token_bytes,
            clock=lambda: datetime.now(timezone.utc),
        )
    try:
        days = float(raw_days)
    except ValueError as exc:
        raise ValueError("NANO_CLAW_SESSION_TTL_DAYS must be a number") from exc
    if not math.isfinite(days) or days <= 0:
        raise ValueError("NANO_CLAW_SESSION_TTL_DAYS must be positive and finite")
    return SessionPolicy(
        random_bytes=secrets.token_bytes,
        clock=lambda: datetime.now(timezone.utc),
        absolute_ttl=timedelta(days=days),
    )


def _same_path(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    return os.path.realpath(os.path.abspath(os.fspath(first))) == os.path.realpath(
        os.path.abspath(os.fspath(second))
    )


def backup_database(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Path:
    """Create a consistent online backup using SQLite's backup API.

    The source may remain in WAL mode and continue serving other connections.
    Errors propagate and a source path is never created implicitly.  Restore is
    intentionally an operator action documented in ``docs/AUTH.md``.
    """

    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if _same_path(source, destination):
        raise ValueError("backup destination must differ from the source database")
    if destination.exists():
        raise FileExistsError(
            "backup destination must be a new path so stale WAL data cannot apply"
        )
    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
        raise TypeError("busy_timeout_ms must be an integer")
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must not be negative")

    # This DB holds private conversation transcripts (task 043) — the backup
    # and its parent must not be world/group readable on an external mount.
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    timeout_seconds = busy_timeout_ms / 1_000
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            os.fspath(source), timeout=timeout_seconds, isolation_level=None
        )
        destination_connection = sqlite3.connect(
            os.fspath(destination), timeout=timeout_seconds, isolation_level=None
        )
        os.chmod(os.fspath(destination), 0o600)
        source_connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        destination_connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.DatabaseError("auth backup failed integrity_check")
        if destination_connection.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.DatabaseError("auth backup failed foreign_key_check")
    except BaseException:
        if destination_connection is not None:
            destination_connection.close()
            destination_connection = None
        if source_connection is not None:
            source_connection.close()
            source_connection = None
        destination.unlink(missing_ok=True)
        Path(f"{destination}-journal").unlink(missing_ok=True)
        Path(f"{destination}-wal").unlink(missing_ok=True)
        Path(f"{destination}-shm").unlink(missing_ok=True)
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    return destination


class SQLiteAuthStore:
    """Nano-claw's tenant-aware, fail-closed SQLite auth store.

    The configured ``tenant_id`` is trusted server configuration.  Upserting an
    identity creates membership in that tenant; issuing for any tenant still
    requires an existing membership.  All database exceptions escape to the
    caller, including lookup failures caused by a locked or unavailable file.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        policy: SessionPolicy | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        configured_path = (
            os.environ.get("NANO_CLAW_AUTH_DB") or DEFAULT_AUTH_DB_PATH
            if db_path is None
            else db_path
        )
        self.db_path = os.fspath(configured_path)
        if not self.db_path:
            raise ValueError("auth database path must not be empty")
        if self.db_path == ":memory:":
            raise ValueError("SQLiteAuthStore requires a file-backed database")
        metrics_path = os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db")
        if _same_path(self.db_path, metrics_path):
            raise ValueError("the auth store must not use the metrics database")
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise TypeError("busy_timeout_ms must be an integer")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")

        self.tenant_id = _validate_required_text(
            tenant_id, "tenant_id", MAX_TENANT_LENGTH
        )
        self.busy_timeout_ms = busy_timeout_ms
        self.policy = policy or _default_policy()

        # Restrict the live DB dir/file — it holds private transcripts.
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()
        try:
            if os.path.exists(self.db_path):
                os.chmod(self.db_path, 0o600)
        except OSError:
            pass  # best-effort; non-file paths (:memory:) or FS without perms
        if self.tenant_id != DEFAULT_TENANT_ID:
            self._ensure_configured_tenant()

    @property
    def path(self) -> Path:
        """Return the configured database path."""

        return Path(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")
            enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            if enabled != 1:
                raise sqlite3.DatabaseError("failed to enable SQLite foreign keys")
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _migrate(self) -> None:
        with self._connection() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise sqlite3.DatabaseError("auth database requires WAL journal mode")

            connection.execute("BEGIN IMMEDIATE")
            try:
                current = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if current > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersion(
                        f"auth database version {current} is newer than supported "
                        f"version {SCHEMA_VERSION}"
                    )
                for version in range(current + 1, SCHEMA_VERSION + 1):
                    for statement in _MIGRATIONS[version]:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={version}")
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _ensure_configured_tenant(self) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO tenants(id, name) VALUES(?, ?)",
                (self.tenant_id, self.tenant_id),
            )

    def upsert_identity(
        self, sub: str, email: str | None, name: str | None
    ) -> None:
        """Upsert display claims and membership in the configured tenant."""

        subject = _validate_required_text(sub, "sub", MAX_SUB_LENGTH)
        display_email = _validate_optional_text(email, "email", MAX_EMAIL_LENGTH)
        display_name = _validate_optional_text(name, "name", MAX_NAME_LENGTH)
        now = _timestamp(self.policy.now())
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO users(sub, email, name, created_at, last_login)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(sub) DO UPDATE SET
                  email=excluded.email,
                  name=excluded.name,
                  last_login=excluded.last_login
                """,
                (subject, display_email, display_name, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO memberships(tenant_id, user_sub, created_at)
                VALUES(?, ?, ?)
                """,
                (self.tenant_id, subject, now),
            )

    def issue_hashed_session(
        self, sub: str, tenant: str, now: datetime
    ) -> str:
        """Atomically rotate a tenant session and return the new raw bearer."""

        subject = _validate_required_text(sub, "sub", MAX_SUB_LENGTH)
        tenant_id = _validate_required_text(
            tenant, "tenant", MAX_TENANT_LENGTH
        )
        issued_at = normalize_datetime(now)

        # Defense-in-depth (Opus F2): this store is single-tenant; never issue
        # for a tenant other than the configured one, even if an adapter bug
        # forwards a browser-influenced value. Membership is the real gate.
        if tenant_id != self.tenant_id:
            raise MembershipNotFound(
                "identity is not a member of the requested tenant"
            )

        with self._write_transaction() as connection:
            membership = connection.execute(
                """
                SELECT 1 FROM memberships
                WHERE tenant_id=? AND user_sub=?
                """,
                (tenant_id, subject),
            ).fetchone()
            if membership is None:
                raise MembershipNotFound(
                    "identity is not a member of the requested tenant"
                )

            issued = None
            for _ in range(MAX_TOKEN_GENERATION_ATTEMPTS):
                candidate = self.policy.issue(issued_at)
                collision = connection.execute(
                    "SELECT 1 FROM sessions WHERE token_hash=?",
                    (candidate.token_hash,),
                ).fetchone()
                if collision is None:
                    issued = candidate
                    break
            if issued is None:
                raise SessionTokenCollision(
                    "random source repeatedly generated an existing session token"
                )

            connection.execute(
                "DELETE FROM sessions WHERE user_sub=? AND tenant_id=?",
                (subject, tenant_id),
            )
            connection.execute(
                """
                INSERT INTO sessions(
                  token_hash, user_sub, tenant_id,
                  created_at, expires_at, last_seen
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    issued.token_hash,
                    subject,
                    tenant_id,
                    _timestamp(issued.created_at),
                    _timestamp(issued.expires_at),
                    _timestamp(issued.last_seen),
                ),
            )
        return issued.raw_token

    def resolve_session(
        self, raw_token: str, now: datetime
    ) -> ResolvedSession | None:
        """Resolve and idle-touch a session; database errors always escape."""

        token_hash = self.policy.hash_token(raw_token)
        current = normalize_datetime(now)
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT user_sub, tenant_id, expires_at, last_seen
                FROM sessions WHERE token_hash=?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            expires_at = _datetime(row["expires_at"])
            last_seen = _datetime(row["last_seen"])
            if self.policy.is_expired(
                expires_at=expires_at, last_seen=last_seen, now=current
            ):
                return None

            touched = self.policy.touch(last_seen, current)
            connection.execute(
                "UPDATE sessions SET last_seen=? WHERE token_hash=?",
                (_timestamp(touched), token_hash),
            )
            return {"sub": str(row["user_sub"]), "tenant": str(row["tenant_id"])}

    def revoke(self, raw_token: str) -> int:
        """Revoke one raw bearer without ever querying it back from storage."""

        token_hash = self.policy.hash_token(raw_token)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (token_hash,)
            )
            return max(cursor.rowcount, 0)

    def revoke_all(self, sub: str) -> int:
        """Revoke every session for the identity across all tenant scopes."""

        subject = _validate_required_text(sub, "sub", MAX_SUB_LENGTH)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE user_sub=?", (subject,)
            )
            return max(cursor.rowcount, 0)

    def sweep(self, now: datetime) -> int:
        """Delete only sessions past their absolute or idle deadline."""

        current = _timestamp(normalize_datetime(now))
        idle_cutoff = current - self.policy.idle_ttl.total_seconds()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM sessions
                WHERE expires_at <= ? OR last_seen <= ?
                """,
                (current, idle_cutoff),
            )
            return max(cursor.rowcount, 0)

    @staticmethod
    def _history_owner(tenant: str, user_sub: str) -> tuple[str, str]:
        tenant_id = _validate_required_text(
            tenant, "tenant", MAX_TENANT_LENGTH
        )
        subject = _validate_required_text(
            user_sub, "user_sub", MAX_SUB_LENGTH
        )
        return tenant_id, subject

    @staticmethod
    def _append_turn_in_transaction(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        tenant_id: str,
        subject: str,
        role: TurnRole,
        text: str,
        timestamp: float,
    ) -> int:
        """Allocate and insert one sequence number in the current transaction."""

        updated = connection.execute(
            """
            UPDATE conversations
            SET turn_count=turn_count + 1, ended_at=?
            WHERE id=? AND tenant_id=? AND user_sub=?
            RETURNING turn_count
            """,
            (timestamp, conversation_id, tenant_id, subject),
        ).fetchone()
        if updated is None:
            raise ConversationNotFound(conversation_id)
        sequence = int(updated["turn_count"]) - 1
        inserted = connection.execute(
            """
            INSERT INTO conversation_turns(conversation_id, seq, role, text, ts)
            SELECT id, ?, ?, ?, ?
            FROM conversations
            WHERE id=? AND tenant_id=? AND user_sub=?
            """,
            (
                sequence,
                role,
                text,
                timestamp,
                conversation_id,
                tenant_id,
                subject,
            ),
        )
        if inserted.rowcount != 1:
            raise ConversationNotFound(conversation_id)
        return sequence

    def open_conversation(
        self,
        conversation_id: str,
        tenant: str,
        user_sub: str,
        first_utterance: str,
        now: datetime,
    ) -> int:
        """Create an owned conversation and its first user turn atomically."""

        identifier = _validate_conversation_id(conversation_id)
        tenant_id, subject = self._history_owner(tenant, user_sub)
        text = _validate_turn_text(first_utterance)
        if not text.strip():
            raise ValueError("first_utterance must not be blank")
        timestamp = _timestamp(now)
        title = normalize_conversation_title(text)
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO conversations(
                  id, tenant_id, user_sub, started_at, ended_at, title,
                  turn_count, history_incomplete
                ) VALUES(?, ?, ?, ?, NULL, ?, 0, 0)
                """,
                (identifier, tenant_id, subject, timestamp, title),
            )
            return self._append_turn_in_transaction(
                connection,
                conversation_id=identifier,
                tenant_id=tenant_id,
                subject=subject,
                role="user",
                text=text,
                timestamp=timestamp,
            )

    def append_conversation_turn(
        self,
        conversation_id: str,
        tenant: str,
        user_sub: str,
        role: str,
        text: str,
        now: datetime,
    ) -> int:
        """Append one owned turn with an atomic counter/sequence allocation."""

        identifier = _validate_conversation_id(conversation_id)
        tenant_id, subject = self._history_owner(tenant, user_sub)
        turn_role = _validate_turn_role(role)
        bounded_text = _validate_turn_text(text)
        timestamp = _timestamp(now)
        with self._write_transaction() as connection:
            return self._append_turn_in_transaction(
                connection,
                conversation_id=identifier,
                tenant_id=tenant_id,
                subject=subject,
                role=turn_role,
                text=bounded_text,
                timestamp=timestamp,
            )

    def mark_conversation_incomplete(
        self, conversation_id: str, tenant: str, user_sub: str
    ) -> bool:
        """Persist that at least one best-effort history write was lost."""

        identifier = _validate_conversation_id(conversation_id)
        tenant_id, subject = self._history_owner(tenant, user_sub)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE conversations SET history_incomplete=1
                WHERE id=? AND tenant_id=? AND user_sub=?
                """,
                (identifier, tenant_id, subject),
            )
            return cursor.rowcount == 1

    def list_conversations(
        self,
        tenant: str,
        user_sub: str,
        *,
        limit: int,
        before_started_at: float | None = None,
        before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one stable, descending owner-scoped conversation page."""

        tenant_id, subject = self._history_owner(tenant, user_sub)
        page_limit = _validate_query_limit(
            limit, maximum=MAX_CONVERSATION_PAGE_SIZE
        )
        if (before_started_at is None) != (before_id is None):
            raise ValueError("both conversation cursor fields are required")
        if before_started_at is not None:
            if (
                isinstance(before_started_at, bool)
                or not isinstance(before_started_at, (int, float))
                or not math.isfinite(float(before_started_at))
            ):
                raise ValueError("before_started_at must be finite")
            cursor_timestamp: float | None = float(before_started_at)
            cursor_id = _validate_conversation_id(before_id)
        else:
            cursor_timestamp = None
            cursor_id = None

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, started_at, ended_at, title, turn_count,
                       history_incomplete
                FROM conversations
                WHERE tenant_id=? AND user_sub=?
                  AND (
                    ? IS NULL
                    OR started_at < ?
                    OR (started_at = ? AND id < ?)
                  )
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (
                    tenant_id,
                    subject,
                    cursor_timestamp,
                    cursor_timestamp,
                    cursor_timestamp,
                    cursor_id,
                    page_limit,
                ),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def get_conversation_page(
        self,
        conversation_id: str,
        tenant: str,
        user_sub: str,
        *,
        limit: int,
        after_seq: int = -1,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Fetch owned metadata and a turn page with one filtered statement."""

        identifier = _validate_conversation_id(conversation_id)
        tenant_id, subject = self._history_owner(tenant, user_sub)
        page_limit = _validate_query_limit(limit, maximum=MAX_TURN_PAGE_SIZE)
        if isinstance(after_seq, bool) or not isinstance(after_seq, int):
            raise TypeError("after_seq must be an integer")
        if after_seq < -1:
            raise ValueError("after_seq must be at least -1")

        with self._connection() as connection:
            rows = connection.execute(
                """
                WITH owned AS (
                  SELECT id, started_at, ended_at, title, turn_count,
                         history_incomplete
                  FROM conversations
                  WHERE id=? AND tenant_id=? AND user_sub=?
                )
                SELECT owned.id, owned.started_at, owned.ended_at, owned.title,
                       owned.turn_count, owned.history_incomplete,
                       conversation_turns.seq, conversation_turns.role,
                       conversation_turns.text, conversation_turns.ts
                FROM owned
                LEFT JOIN conversation_turns
                  ON conversation_turns.conversation_id=owned.id
                 AND conversation_turns.seq > ?
                ORDER BY conversation_turns.seq ASC
                LIMIT ?
                """,
                (
                    identifier,
                    tenant_id,
                    subject,
                    after_seq,
                    page_limit,
                ),
            ).fetchall()
        if not rows:
            return None
        first = rows[0]
        conversation = {
            "id": first["id"],
            "started_at": first["started_at"],
            "ended_at": first["ended_at"],
            "title": first["title"],
            "turn_count": first["turn_count"],
            "history_incomplete": first["history_incomplete"],
        }
        turns = [
            {
                "seq": row["seq"],
                "role": row["role"],
                "text": row["text"],
                "ts": row["ts"],
            }
            for row in rows
            if row["seq"] is not None
        ]
        return conversation, turns

    def delete_conversation(
        self, conversation_id: str, tenant: str, user_sub: str
    ) -> bool:
        """Delete one owner-scoped conversation and cascade all of its turns."""

        identifier = _validate_conversation_id(conversation_id)
        tenant_id, subject = self._history_owner(tenant, user_sub)
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                DELETE FROM conversations
                WHERE id=? AND tenant_id=? AND user_sub=?
                RETURNING id
                """,
                (identifier, tenant_id, subject),
            ).fetchone()
            return row is not None

    def delete_all_conversations(
        self, tenant: str, user_sub: str
    ) -> list[str]:
        """Delete all owner-scoped conversations in one cascading transaction."""

        tenant_id, subject = self._history_owner(tenant, user_sub)
        with self._write_transaction() as connection:
            rows = connection.execute(
                """
                DELETE FROM conversations
                WHERE tenant_id=? AND user_sub=?
                RETURNING id
                """,
                (tenant_id, subject),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def backup(self, destination_path: str | os.PathLike[str]) -> Path:
        """Create a checked online backup of this auth/history database."""

        return backup_database(
            self.db_path,
            destination_path,
            busy_timeout_ms=self.busy_timeout_ms,
        )
