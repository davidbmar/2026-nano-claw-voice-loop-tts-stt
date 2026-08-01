"""Registry of document spaces and the files inside them.

A *space* is a named folder of documents that the assistant can be pointed at:
"Taxes 2025", "Handbook". It maps one-to-one onto an intelligence-platform
*collection*, which is not a row over there but a tag carried by every document
ingested with it — so the collection id is chosen here, once, and then frozen.

A *document* is one uploaded file. This registry owns everything a customer can
see and change (title, whether it is ticked, whether it is in the trash); the
platform owns the indexed copy and hands back a document id we store as the
handle to it.

Why its own database rather than a table in ``auth-history.db``: that store is
constructed only when Google sign-in is configured, and uploads are gated on
the operator password instead. Sharing it would mean a deployment without
sign-in could not hold documents. The idioms are the same as the auth store's —
WAL, ``user_version`` migrations, foreign keys on, bounded CHECK constraints,
short-lived connections, and no swallowed errors.

The selection state lives here rather than in the browser because the phone
line has to answer from the same documents the console shows; a per-tab
preference could not do that.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DOCUMENT_DB_PATH = "/app/data/documents.db"
DEFAULT_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 1

MAX_NAME_LENGTH = 120
MAX_TITLE_LENGTH = 200
MAX_FILENAME_LENGTH = 255
MAX_COLLECTION_ID_LENGTH = 240
MAX_ERROR_LENGTH = 500

DOCUMENT_STATUSES = ("extracting", "indexing", "ready", "failed")

# Platform ``Identifier`` is ^[A-Za-z0-9][A-Za-z0-9._:@/-]*$ — a slug of that
# shape is what we generate, so an ingest can never be rejected for its id.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class SpaceNotFound(LookupError):
    """Raised when a space id does not resolve."""


class DocumentNotFound(LookupError):
    """Raised when a document id does not resolve."""


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        f"""
        CREATE TABLE spaces (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL
            CHECK(length(name) BETWEEN 1 AND {MAX_NAME_LENGTH}),
          collection_id TEXT NOT NULL UNIQUE
            CHECK(length(collection_id) BETWEEN 1 AND {MAX_COLLECTION_ID_LENGTH}),
          created_at REAL NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),
          deleted_at REAL
        )
        """,
        # At most one active space, enforced by the database rather than by
        # remembering to clear the old one.
        """
        CREATE UNIQUE INDEX idx_spaces_single_active
          ON spaces(is_active) WHERE is_active = 1
        """,
        f"""
        CREATE TABLE space_documents (
          id TEXT PRIMARY KEY,
          space_id TEXT NOT NULL,
          platform_document_id TEXT,
          filename TEXT NOT NULL
            CHECK(length(filename) BETWEEN 1 AND {MAX_FILENAME_LENGTH}),
          title TEXT NOT NULL
            CHECK(length(title) BETWEEN 1 AND {MAX_TITLE_LENGTH}),
          media_type TEXT NOT NULL,
          byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
          char_count INTEGER NOT NULL DEFAULT 0 CHECK(char_count >= 0),
          status TEXT NOT NULL
            CHECK(status IN ('extracting', 'indexing', 'ready', 'failed')),
          selected INTEGER NOT NULL DEFAULT 1 CHECK(selected IN (0, 1)),
          error TEXT
            CHECK(error IS NULL OR length(error) <= {MAX_ERROR_LENGTH}),
          created_at REAL NOT NULL,
          deleted_at REAL,
          FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX idx_space_documents_space
          ON space_documents(space_id, created_at DESC)
        """,
        """
        CREATE INDEX idx_space_documents_deleted
          ON space_documents(deleted_at)
        """,
    ),
}


def slugify(text: str) -> str:
    """Reduce a display name to the character set platform ids allow."""

    slug = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    return slug[:80] or "space"


def _validate_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped or len(stripped) > maximum:
        raise ValueError(f"{field} must contain 1 to {maximum} characters")
    return stripped


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class DocumentStore:
    """SQLite registry of spaces and documents; errors are never swallowed."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Any = time.time,
    ) -> None:
        configured = (
            os.environ.get("NANO_CLAW_DOCUMENT_DB") or DEFAULT_DOCUMENT_DB_PATH
            if db_path is None
            else db_path
        )
        self.db_path = os.fspath(configured)
        if not self.db_path or self.db_path == ":memory:":
            raise ValueError("DocumentStore requires a file-backed database")
        self.busy_timeout_ms = busy_timeout_ms
        self._clock = clock

        # Uploaded documents are customer content — same 0700/0600 discipline
        # the transcript database uses.
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass  # best-effort; filesystems without permission bits

    # ---- connections -----------------------------------------------------

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
    def _write(self) -> Iterator[sqlite3.Connection]:
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
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal).lower() != "wal":
                raise sqlite3.DatabaseError("document database requires WAL mode")
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"document database version {current} is newer than "
                        f"supported version {SCHEMA_VERSION}"
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

    # ---- spaces ----------------------------------------------------------

    def create_space(self, name: str) -> dict[str, Any]:
        """Create a space; the first one created becomes the active one."""

        display = _validate_text(name, "name", MAX_NAME_LENGTH)
        space_id = f"spc_{secrets.token_hex(6)}"
        # The suffix keeps two spaces named "Taxes" from colliding, and keeps
        # the collection id stable if the space is later renamed — the tag is
        # baked into every ingested document and cannot be rewritten.
        collection_id = f"{slugify(display)}-{space_id[4:]}"
        now = float(self._clock())
        with self._write() as connection:
            has_active = connection.execute(
                "SELECT 1 FROM spaces WHERE is_active=1 AND deleted_at IS NULL"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO spaces(
                  id, name, collection_id, created_at, is_active, deleted_at
                ) VALUES(?, ?, ?, ?, ?, NULL)
                """,
                (space_id, display, collection_id, now, 0 if has_active else 1),
            )
        return self.get_space(space_id)

    def get_space(self, space_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM spaces WHERE id=? AND deleted_at IS NULL",
                (space_id,),
            ).fetchone()
        if row is None:
            raise SpaceNotFound(space_id)
        return _row_dict(row)

    def list_spaces(self) -> list[dict[str, Any]]:
        """Return live spaces, newest last, each with its live document count."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*, (
                  SELECT COUNT(*) FROM space_documents d
                  WHERE d.space_id = s.id AND d.deleted_at IS NULL
                ) AS document_count
                FROM spaces s
                WHERE s.deleted_at IS NULL
                ORDER BY s.created_at ASC
                """
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def rename_space(self, space_id: str, name: str) -> dict[str, Any]:
        """Change the display name only — the collection id stays frozen."""

        display = _validate_text(name, "name", MAX_NAME_LENGTH)
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE spaces SET name=? WHERE id=? AND deleted_at IS NULL",
                (display, space_id),
            ).rowcount
        if not changed:
            raise SpaceNotFound(space_id)
        return self.get_space(space_id)

    def activate_space(self, space_id: str) -> dict[str, Any]:
        """Make this the space both the console and the phone line answer from."""

        with self._write() as connection:
            exists = connection.execute(
                "SELECT 1 FROM spaces WHERE id=? AND deleted_at IS NULL",
                (space_id,),
            ).fetchone()
            if exists is None:
                raise SpaceNotFound(space_id)
            # Clear first: the partial unique index would reject two actives.
            connection.execute("UPDATE spaces SET is_active=0 WHERE is_active=1")
            connection.execute("UPDATE spaces SET is_active=1 WHERE id=?", (space_id,))
        return self.get_space(space_id)

    def trash_space(self, space_id: str) -> None:
        """Trash a space and everything in it, reversibly."""

        now = float(self._clock())
        with self._write() as connection:
            changed = connection.execute(
                """
                UPDATE spaces SET deleted_at=?, is_active=0
                WHERE id=? AND deleted_at IS NULL
                """,
                (now, space_id),
            ).rowcount
            if not changed:
                raise SpaceNotFound(space_id)
            connection.execute(
                """
                UPDATE space_documents SET deleted_at=?
                WHERE space_id=? AND deleted_at IS NULL
                """,
                (now, space_id),
            )

    def restore_space(self, space_id: str) -> dict[str, Any]:
        now_deleted = self._space_deleted_at(space_id)
        with self._write() as connection:
            connection.execute(
                "UPDATE spaces SET deleted_at=NULL WHERE id=?", (space_id,)
            )
            # Restore only the documents trashed *by* the space deletion, so a
            # document the customer deleted earlier stays deleted.
            connection.execute(
                """
                UPDATE space_documents SET deleted_at=NULL
                WHERE space_id=? AND deleted_at=?
                """,
                (space_id, now_deleted),
            )
        return self.get_space(space_id)

    def _space_deleted_at(self, space_id: str) -> float:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT deleted_at FROM spaces WHERE id=?", (space_id,)
            ).fetchone()
        if row is None or row["deleted_at"] is None:
            raise SpaceNotFound(space_id)
        return float(row["deleted_at"])

    # ---- documents -------------------------------------------------------

    def create_document(
        self,
        space_id: str,
        *,
        filename: str,
        title: str,
        media_type: str,
        byte_size: int,
    ) -> str:
        """Record an upload that is being extracted; returns the new id."""

        self.get_space(space_id)  # raises SpaceNotFound
        document_id = f"doc_{secrets.token_hex(8)}"
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO space_documents(
                  id, space_id, platform_document_id, filename, title,
                  media_type, byte_size, char_count, status, selected,
                  error, created_at, deleted_at
                ) VALUES(?, ?, NULL, ?, ?, ?, ?, 0, 'extracting', 1, NULL, ?, NULL)
                """,
                (
                    document_id,
                    space_id,
                    _validate_text(filename, "filename", MAX_FILENAME_LENGTH),
                    _validate_text(title, "title", MAX_TITLE_LENGTH),
                    media_type,
                    int(byte_size),
                    float(self._clock()),
                ),
            )
        return document_id

    def mark_indexing(self, document_id: str, *, char_count: int) -> None:
        self._update_document(
            document_id,
            "UPDATE space_documents SET status='indexing', char_count=? WHERE id=?",
            (int(char_count), document_id),
        )

    def mark_ready(self, document_id: str, *, platform_document_id: str) -> None:
        self._update_document(
            document_id,
            """
            UPDATE space_documents
            SET status='ready', platform_document_id=?, error=NULL
            WHERE id=?
            """,
            (platform_document_id, document_id),
        )

    def mark_failed(self, document_id: str, *, error: str) -> None:
        self._update_document(
            document_id,
            "UPDATE space_documents SET status='failed', error=? WHERE id=?",
            (str(error)[:MAX_ERROR_LENGTH], document_id),
        )

    def set_selected(self, document_id: str, *, selected: bool) -> None:
        self._update_document(
            document_id,
            "UPDATE space_documents SET selected=? WHERE id=?",
            (1 if selected else 0, document_id),
        )

    def rename_document(self, document_id: str, title: str) -> None:
        self._update_document(
            document_id,
            "UPDATE space_documents SET title=? WHERE id=?",
            (_validate_text(title, "title", MAX_TITLE_LENGTH), document_id),
        )

    def trash_document(self, document_id: str) -> None:
        """Hide immediately and reversibly; the platform copy is untouched."""

        self._update_document(
            document_id,
            "UPDATE space_documents SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (float(self._clock()), document_id),
        )

    def restore_document(self, document_id: str) -> None:
        self._update_document(
            document_id,
            "UPDATE space_documents SET deleted_at=NULL WHERE id=?",
            (document_id,),
        )

    def _update_document(
        self, document_id: str, statement: str, params: tuple[Any, ...]
    ) -> None:
        with self._write() as connection:
            if not connection.execute(statement, params).rowcount:
                raise DocumentNotFound(document_id)

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM space_documents WHERE id=?", (document_id,)
            ).fetchone()
        if row is None:
            raise DocumentNotFound(document_id)
        return _row_dict(row)

    def list_documents(
        self, space_id: str, *, include_trashed: bool = False
    ) -> list[dict[str, Any]]:
        clause = "" if include_trashed else "AND deleted_at IS NULL"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM space_documents
                WHERE space_id=? {clause}
                ORDER BY created_at ASC
                """,
                (space_id,),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    # ---- scope -----------------------------------------------------------

    def active_scope(self) -> dict[str, Any] | None:
        """What the assistant may retrieve from, derived server-side.

        Returns ``None`` when no space is active, which the caller reads as
        "fall back to the configured default" — a customer who has not created
        a space must not have the assistant silently narrowed to nothing.

        ``selected_document_ids`` is empty with ``ready_count`` above zero only
        when the customer has unticked everything. That case must NOT be sent
        to the platform as an empty filter: an empty ``collection_ids`` there
        means *tenant-wide, unfiltered*. Callers translate it into an explicit
        "nothing is loaded" instead.
        """

        with self._connection() as connection:
            space = connection.execute(
                "SELECT * FROM spaces WHERE is_active=1 AND deleted_at IS NULL"
            ).fetchone()
            if space is None:
                return None
            rows = connection.execute(
                """
                SELECT platform_document_id, selected FROM space_documents
                WHERE space_id=? AND deleted_at IS NULL
                  AND status='ready' AND platform_document_id IS NOT NULL
                """,
                (space["id"],),
            ).fetchall()

        ready = [row for row in rows]
        selected = [
            row["platform_document_id"] for row in ready if row["selected"]
        ]
        return {
            "space_id": space["id"],
            "space_name": space["name"],
            "collection_id": space["collection_id"],
            "selected_document_ids": selected,
            "ready_count": len(ready),
            # Every ready document ticked means the collection filter alone is
            # exact — sending document ids too would be redundant work.
            "all_selected": bool(ready) and len(selected) == len(ready),
        }

    # ---- purge -----------------------------------------------------------

    def documents_due_for_purge(self, *, older_than: float) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM space_documents
                WHERE deleted_at IS NOT NULL AND deleted_at < ?
                ORDER BY deleted_at ASC
                """,
                (float(older_than),),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def purge_document(self, document_id: str) -> None:
        """Remove the registry row for good; callers delete bytes first."""

        with self._write() as connection:
            connection.execute(
                "DELETE FROM space_documents WHERE id=?", (document_id,)
            )

    def purge_empty_spaces(self, *, older_than: float) -> int:
        """Drop trashed spaces once nothing of theirs is left to purge."""

        with self._write() as connection:
            return connection.execute(
                """
                DELETE FROM spaces
                WHERE deleted_at IS NOT NULL AND deleted_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM space_documents d WHERE d.space_id = spaces.id
                  )
                """,
                (float(older_than),),
            ).rowcount
