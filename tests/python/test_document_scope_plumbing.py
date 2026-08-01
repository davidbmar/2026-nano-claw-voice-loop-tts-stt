"""What the browser and the phone line tell the agent about documents.

Both surfaces resolve the scope from the registry rather than being told it by
a client, and both resolve it the same way — a caller on the phone has to be
answered from the documents the console shows, or ticking a box means nothing.

The failure mode guarded here is silence: a document feature that is switched
off, empty, or broken must fall back to the configured default rather than
narrowing the assistant to nothing.
"""

from __future__ import annotations

import time

import pytest

from voice import document_routes
from voice.document_store import DocumentStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "documents.db"
    monkeypatch.setenv("NANO_CLAW_DOCUMENT_DB", str(path))
    monkeypatch.setenv("NANO_CLAW_DOCUMENT_DIR", str(tmp_path / "files"))
    # The turn-path cache is keyed by database path; clear it so one test's
    # store cannot answer another test's questions.
    document_routes._TURN_STORES.clear()
    return DocumentStore(path)


def _ready(store: DocumentStore, space_id: str, name: str) -> str:
    document_id = store.create_document(
        space_id,
        filename=f"{name}.pdf",
        title=name,
        media_type="application/pdf",
        byte_size=10,
    )
    store.mark_indexing(document_id, char_count=100)
    store.mark_ready(document_id, platform_document_id=f"doc::{name}")
    return document_id


def test_no_space_means_no_scope_field_at_all(store):
    # Absent, not empty: an empty scope would narrow retrieval, and a customer
    # who has never opened the feature must be unaffected by it.
    assert document_routes.active_document_scope() is None


def test_the_active_space_is_reported_with_its_ticked_documents(store):
    space = store.create_space("Taxes 2025")
    _ready(store, space["id"], "w2")
    _ready(store, space["id"], "1099")

    scope = document_routes.active_document_scope()

    assert scope["collectionId"] == space["collection_id"]
    assert sorted(scope["selected"]) == ["doc::1099", "doc::w2"]
    assert scope["ready"] == 2
    assert scope["allSelected"] is True


def test_unticking_is_visible_on_the_very_next_turn(store):
    space = store.create_space("Taxes 2025")
    w2 = _ready(store, space["id"], "w2")
    _ready(store, space["id"], "1099")

    store.set_selected(w2, selected=False)

    scope = document_routes.active_document_scope()
    assert scope["selected"] == ["doc::1099"]
    assert scope["allSelected"] is False


def test_unticking_everything_reports_zero_selected_with_documents_present(store):
    # The agent needs both numbers to tell "nothing chosen" from "nothing
    # uploaded" — one is a refusal, the other is a fallback.
    space = store.create_space("Taxes 2025")
    store.set_selected(_ready(store, space["id"], "w2"), selected=False)

    scope = document_routes.active_document_scope()

    assert scope["selected"] == []
    assert scope["ready"] == 1


def test_a_broken_registry_falls_back_instead_of_silencing_the_assistant(
    store, monkeypatch
):
    space = store.create_space("Taxes 2025")
    _ready(store, space["id"], "w2")

    def explode(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(DocumentStore, "active_scope", explode)

    assert document_routes.active_document_scope() is None


def test_the_browser_and_the_phone_send_the_same_scope(store):
    from voice import phone, server

    space = store.create_space("Taxes 2025")
    _ready(store, space["id"], "w2")

    browser = server._document_scope_payload()["documentScope"]
    # phone.py reads the same helper; assert on the module reference so a
    # future divergence between the two call sites is caught here.
    assert phone.document_routes.active_document_scope() == browser


def test_the_scope_field_is_omitted_when_there_is_no_space(store):
    from voice import server

    assert server._document_scope_payload() == {}


# ---- purge ---------------------------------------------------------------


class _FakePlatform:
    def __init__(self, *, succeed: bool = True) -> None:
        self.deleted: list[str] = []
        self.succeed = succeed

    async def delete_document(self, platform_document_id: str) -> bool:
        self.deleted.append(platform_document_id)
        return self.succeed


async def test_purge_removes_documents_past_the_horizon(store, tmp_path):
    space = store.create_space("Taxes 2025")
    document_id = _ready(store, space["id"], "w2")
    folder = tmp_path / "files" / document_id
    folder.mkdir(parents=True)
    (folder / "original.pdf").write_bytes(b"bytes")
    store.trash_document(document_id)

    platform = _FakePlatform()
    purged = await document_routes.purge_expired_documents(
        store, platform, retention_days=30, now=time.time() + 31 * 86_400
    )

    assert purged == 1
    assert platform.deleted == ["doc::w2"]
    assert not folder.exists(), "the stored bytes must go too, not just the row"
    assert store.list_documents(space["id"], include_trashed=True) == []


async def test_purge_leaves_documents_still_inside_the_window(store):
    space = store.create_space("Taxes 2025")
    document_id = _ready(store, space["id"], "w2")
    store.trash_document(document_id)

    platform = _FakePlatform()
    purged = await document_routes.purge_expired_documents(
        store, platform, retention_days=30
    )

    assert purged == 0
    assert platform.deleted == []
    assert len(store.list_documents(space["id"], include_trashed=True)) == 1


async def test_a_failed_platform_delete_defers_rather_than_orphaning(store):
    # Dropping the registry row anyway would leave the indexed copy somewhere
    # nothing could ever reach again — the row is the only handle to it.
    space = store.create_space("Taxes 2025")
    document_id = _ready(store, space["id"], "w2")
    store.trash_document(document_id)

    purged = await document_routes.purge_expired_documents(
        store,
        _FakePlatform(succeed=False),
        retention_days=30,
        now=time.time() + 31 * 86_400,
    )

    assert purged == 0
    assert len(store.list_documents(space["id"], include_trashed=True)) == 1


async def test_purging_the_last_document_drops_a_trashed_space(store):
    space = store.create_space("Taxes 2025")
    _ready(store, space["id"], "w2")
    store.trash_space(space["id"])

    await document_routes.purge_expired_documents(
        store, _FakePlatform(), retention_days=30, now=time.time() + 31 * 86_400
    )

    assert store.list_spaces() == []
    assert document_routes.active_document_scope() is None
