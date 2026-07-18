from __future__ import annotations

import ast
import base64
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import voice.webauth.policy as policy_module
from voice.webauth.policy import (
    DEFAULT_ABSOLUTE_TTL,
    DEFAULT_IDLE_TTL,
    SESSION_TOKEN_BYTES,
    SessionPolicy,
    hash_session_token,
)
from voice.webauth.sqlite_store import (
    DEFAULT_TENANT_ID,
    SCHEMA_VERSION,
    MembershipNotFound,
    SQLiteAuthStore,
)
from voice.webauth.store import AuthStore


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


class SequenceRng:
    def __init__(self, start: int = 1) -> None:
        self.value = start
        self.calls: list[int] = []

    def __call__(self, size: int) -> bytes:
        self.calls.append(size)
        value = self.value
        self.value += 1
        return value.to_bytes(size, "big")


def make_policy(
    *,
    rng: SequenceRng | None = None,
    clock: datetime = NOW,
    absolute_ttl: timedelta = DEFAULT_ABSOLUTE_TTL,
    idle_ttl: timedelta = DEFAULT_IDLE_TTL,
) -> SessionPolicy:
    return SessionPolicy(
        absolute_ttl=absolute_ttl,
        idle_ttl=idle_ttl,
        random_bytes=rng or SequenceRng(),
        clock=lambda: clock,
    )


def make_store(
    path: Path,
    *,
    rng: SequenceRng | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    busy_timeout_ms: int = 2_000,
) -> SQLiteAuthStore:
    return SQLiteAuthStore(
        path,
        policy=make_policy(rng=rng),
        tenant_id=tenant_id,
        busy_timeout_ms=busy_timeout_ms,
    )


def add_identity(store: SQLiteAuthStore, sub: str) -> None:
    store.upsert_identity(sub, f"{sub}@example.test", f"Name {sub}")


def test_policy_uses_injected_rng_clock_sha256_and_both_expiry_boundaries():
    entropy = bytes(range(SESSION_TOKEN_BYTES))
    calls: list[int] = []

    def rng(size: int) -> bytes:
        calls.append(size)
        return entropy

    policy = SessionPolicy(
        absolute_ttl=timedelta(days=2),
        idle_ttl=timedelta(hours=3),
        random_bytes=rng,
        clock=lambda: NOW.replace(tzinfo=None),
    )
    issued = policy.issue()
    expected_raw = base64.urlsafe_b64encode(entropy).rstrip(b"=").decode("ascii")

    assert calls == [SESSION_TOKEN_BYTES]
    assert issued.raw_token == expected_raw
    assert issued.token_hash == hashlib.sha256(expected_raw.encode()).hexdigest()
    assert issued.created_at == NOW
    assert issued.expires_at == NOW + timedelta(days=2)
    assert not policy.is_expired(
        expires_at=issued.expires_at,
        last_seen=issued.last_seen,
        now=NOW + timedelta(hours=2, minutes=59),
    )
    assert policy.is_expired(
        expires_at=issued.expires_at,
        last_seen=issued.last_seen,
        now=NOW + timedelta(hours=3),
    )
    assert policy.is_expired(
        expires_at=issued.expires_at,
        last_seen=NOW + timedelta(days=1, hours=23),
        now=NOW + timedelta(days=2),
    )


def test_policy_import_isolation_has_no_aiohttp_sql_or_voice_imports():
    source_path = Path(policy_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(name == "aiohttp" or name.startswith("aiohttp.") for name in imported)
    assert not any(name == "sqlite3" or name.startswith("sqlite3.") for name in imported)
    assert not any(name == "voice" or name.startswith("voice.") for name in imported)


def test_schema_pragmas_indexes_seed_and_user_version_are_idempotent(tmp_path):
    db_path = tmp_path / "auth-history.db"
    store = make_store(db_path)
    assert isinstance(store, AuthStore)

    connection = sqlite3.connect(db_path)
    tables_before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    schema_before = connection.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    indexes = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    }
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert tables_before == {
        "users",
        "tenants",
        "memberships",
        "sessions",
        "conversations",
        "conversation_turns",
    }
    assert connection.execute("SELECT id, name FROM tenants").fetchall() == [
        ("nano-claw", "nano-claw")
    ]
    assert "idx_sessions_expires_at" in indexes
    assert "idx_conversations_tenant_user_started_at" in indexes
    assert "STARTED_AT DESC" in indexes[
        "idx_conversations_tenant_user_started_at"
    ].upper()
    connection.close()

    make_store(db_path, rng=SequenceRng(start=100))
    second = sqlite3.connect(db_path)
    assert second.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert second.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall() == schema_before
    assert second.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
    second.close()


def test_auth_db_environment_path_is_used_and_metrics_path_is_rejected(
    monkeypatch, tmp_path
):
    auth_path = tmp_path / "configured-auth.db"
    monkeypatch.setenv("NANO_CLAW_AUTH_DB", str(auth_path))
    store = SQLiteAuthStore(policy=make_policy())
    assert store.path == auth_path
    assert auth_path.is_file()

    monkeypatch.setenv("METRICS_DB_PATH", str(tmp_path / "metrics.db"))
    monkeypatch.setenv("NANO_CLAW_AUTH_DB", str(tmp_path / "metrics.db"))
    with pytest.raises(ValueError, match="metrics database"):
        SQLiteAuthStore(policy=make_policy())


def test_issue_resolve_and_rotation_are_tenant_scoped(tmp_path):
    store = make_store(tmp_path / "auth.db")
    add_identity(store, "user-1")

    first = store.issue_hashed_session("user-1", "nano-claw", NOW)
    assert store.resolve_session(first, NOW) == {
        "sub": "user-1",
        "tenant": "nano-claw",
    }
    second = store.issue_hashed_session(
        "user-1", "nano-claw", NOW + timedelta(minutes=1)
    )

    assert second != first
    assert store.resolve_session(first, NOW + timedelta(minutes=2)) is None
    assert store.resolve_session(second, NOW + timedelta(minutes=2)) == {
        "sub": "user-1",
        "tenant": "nano-claw",
    }

    other_tenant_store = make_store(
        tmp_path / "auth.db",
        rng=SequenceRng(start=100),
        tenant_id="tenant-b",
    )
    with pytest.raises(MembershipNotFound):
        other_tenant_store.issue_hashed_session(
            "user-1", "tenant-b", NOW + timedelta(minutes=3)
        )
    other_tenant_store.upsert_identity("user-1", None, "Updated")
    other = other_tenant_store.issue_hashed_session(
        "user-1", "tenant-b", NOW + timedelta(minutes=3)
    )
    assert other_tenant_store.resolve_session(
        other, NOW + timedelta(minutes=4)
    ) == {"sub": "user-1", "tenant": "tenant-b"}
    assert store.resolve_session(second, NOW + timedelta(minutes=4)) == {
        "sub": "user-1",
        "tenant": "nano-claw",
    }


def test_idle_expiry_slides_but_absolute_expiry_never_moves(tmp_path):
    store = make_store(tmp_path / "auth.db")
    add_identity(store, "idle-user")
    idle_token = store.issue_hashed_session("idle-user", "nano-claw", NOW)
    assert store.resolve_session(idle_token, NOW + DEFAULT_IDLE_TTL) is None

    add_identity(store, "active-user")
    active_token = store.issue_hashed_session("active-user", "nano-claw", NOW)
    for elapsed_hours in range(23, 7 * 24, 23):
        assert store.resolve_session(
            active_token, NOW + timedelta(hours=elapsed_hours)
        ) == {"sub": "active-user", "tenant": "nano-claw"}
    assert store.resolve_session(active_token, NOW + DEFAULT_ABSOLUTE_TTL) is None


def test_revoke_and_revoke_all_remove_the_expected_sessions(tmp_path):
    db_path = tmp_path / "auth.db"
    nano_store = make_store(db_path)
    add_identity(nano_store, "user-1")
    first = nano_store.issue_hashed_session("user-1", "nano-claw", NOW)
    assert nano_store.revoke(first) == 1
    assert nano_store.revoke(first) == 0
    assert nano_store.resolve_session(first, NOW) is None

    nano_token = nano_store.issue_hashed_session("user-1", "nano-claw", NOW)
    tenant_store = make_store(
        db_path, rng=SequenceRng(start=100), tenant_id="tenant-b"
    )
    tenant_store.upsert_identity("user-1", "new@example.test", "User")
    tenant_token = tenant_store.issue_hashed_session("user-1", "tenant-b", NOW)

    assert nano_store.revoke_all("user-1") == 2
    assert nano_store.resolve_session(nano_token, NOW) is None
    assert tenant_store.resolve_session(tenant_token, NOW) is None


def test_sweep_removes_only_absolute_or_idle_expired_sessions(tmp_path):
    store = make_store(tmp_path / "auth.db")
    for sub in ("absolute", "idle", "live"):
        add_identity(store, sub)

    absolute_token = store.issue_hashed_session(
        "absolute", "nano-claw", NOW - timedelta(days=8)
    )
    idle_token = store.issue_hashed_session(
        "idle", "nano-claw", NOW - timedelta(days=2)
    )
    live_token = store.issue_hashed_session(
        "live", "nano-claw", NOW - timedelta(hours=1)
    )

    assert store.sweep(NOW) == 2
    assert store.resolve_session(absolute_token, NOW) is None
    assert store.resolve_session(idle_token, NOW) is None
    assert store.resolve_session(live_token, NOW) == {
        "sub": "live",
        "tenant": "nano-claw",
    }
    connection = sqlite3.connect(store.db_path)
    assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    connection.close()


def test_raw_tokens_are_hashed_at_rest_and_a_db_value_is_not_replayable(tmp_path):
    store = make_store(tmp_path / "auth.db")
    add_identity(store, "user-1")
    raw_token = store.issue_hashed_session("user-1", "nano-claw", NOW)

    connection = sqlite3.connect(store.db_path)
    connection.row_factory = sqlite3.Row
    row = dict(connection.execute("SELECT * FROM sessions").fetchone())
    connection.close()
    assert row["token_hash"] == hash_session_token(raw_token)
    assert raw_token not in row.values()
    assert store.resolve_session(row["token_hash"], NOW) is None

    for path in (store.path, Path(f"{store.db_path}-wal")):
        if path.exists():
            assert raw_token.encode("ascii") not in path.read_bytes()


def test_foreign_key_delete_user_cascades_sessions_and_history(tmp_path):
    store = make_store(tmp_path / "auth.db")
    add_identity(store, "user-1")
    store.issue_hashed_session("user-1", "nano-claw", NOW)

    connection = sqlite3.connect(store.db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        INSERT INTO conversations(
          id, tenant_id, user_sub, started_at, title, turn_count
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        ("conversation-1", "nano-claw", "user-1", NOW.timestamp(), "Title", 1),
    )
    connection.execute(
        """
        INSERT INTO conversation_turns(conversation_id, seq, role, text, ts)
        VALUES(?, ?, ?, ?, ?)
        """,
        ("conversation-1", 0, "user", "private text", NOW.timestamp()),
    )
    connection.execute("DELETE FROM users WHERE sub=?", ("user-1",))
    connection.commit()
    assert {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "users",
            "memberships",
            "sessions",
            "conversations",
            "conversation_turns",
        )
    } == {
        "users": 0,
        "memberships": 0,
        "sessions": 0,
        "conversations": 0,
        "conversation_turns": 0,
    }
    connection.close()


def test_concurrent_resolvers_use_independent_connections(tmp_path):
    store = make_store(tmp_path / "auth.db", busy_timeout_ms=5_000)
    add_identity(store, "user-1")
    token = store.issue_hashed_session("user-1", "nano-claw", NOW)

    def resolve(offset: int):
        return store.resolve_session(token, NOW + timedelta(seconds=offset))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(resolve, range(32)))

    assert results == [
        {"sub": "user-1", "tenant": "nano-claw"}
    ] * 32


def test_locked_database_raises_instead_of_returning_anonymous(tmp_path):
    store = make_store(tmp_path / "auth.db", busy_timeout_ms=20)
    add_identity(store, "user-1")
    token = store.issue_hashed_session("user-1", "nano-claw", NOW)

    locker = sqlite3.connect(store.db_path, isolation_level=None)
    locker.execute("PRAGMA busy_timeout=20")
    locker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.resolve_session(token, NOW)
    finally:
        locker.rollback()
        locker.close()

    assert store.resolve_session(token, NOW) == {
        "sub": "user-1",
        "tenant": "nano-claw",
    }


def test_online_backup_and_restore_round_trip(tmp_path):
    source_path = tmp_path / "auth.db"
    backup_path = tmp_path / "backups" / "auth.db"
    store = make_store(source_path)
    add_identity(store, "user-1")
    token = store.issue_hashed_session("user-1", "nano-claw", NOW)

    connection = sqlite3.connect(source_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        INSERT INTO conversations(id, tenant_id, user_sub, started_at, turn_count)
        VALUES(?, ?, ?, ?, ?)
        """,
        ("conversation-1", "nano-claw", "user-1", NOW.timestamp(), 1),
    )
    connection.execute(
        """
        INSERT INTO conversation_turns(conversation_id, seq, role, text, ts)
        VALUES(?, ?, ?, ?, ?)
        """,
        ("conversation-1", 0, "agent", "saved reply", NOW.timestamp()),
    )
    connection.commit()
    connection.close()

    assert store.backup(backup_path) == backup_path
    store.revoke(token)

    restored = make_store(backup_path, rng=SequenceRng(start=200))
    assert restored.resolve_session(token, NOW + timedelta(minutes=1)) == {
        "sub": "user-1",
        "tenant": "nano-claw",
    }
    backup = sqlite3.connect(backup_path)
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    assert backup.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert backup.execute("SELECT text FROM conversation_turns").fetchone()[0] == (
        "saved reply"
    )
    backup.close()
