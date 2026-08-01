"""The registry behind document spaces.

The assertions worth having here are the ones that protect a customer's
documents from being silently mis-scoped: exactly one active space, trash that
hides immediately, undo that does not resurrect something deleted earlier, and
a scope that never degrades into "everything" when the customer meant
"nothing".
"""

from __future__ import annotations

import pytest

from voice.document_store import DocumentNotFound, DocumentStore, SpaceNotFound


@pytest.fixture()
def store(tmp_path):
    return DocumentStore(tmp_path / "documents.db")


def _ready(store: DocumentStore, space_id: str, name: str) -> str:
    document_id = store.create_document(
        space_id,
        filename=f"{name}.pdf",
        title=name,
        media_type="application/pdf",
        byte_size=1024,
    )
    store.mark_indexing(document_id, char_count=500)
    store.mark_ready(document_id, platform_document_id=f"doc::{name}")
    return document_id


def test_the_first_space_is_active_and_later_ones_are_not(store):
    first = store.create_space("Taxes 2025")
    second = store.create_space("Handbook")
    assert first["is_active"] == 1
    assert second["is_active"] == 0
    assert store.active_scope()["space_id"] == first["id"]


def test_activating_a_space_deactivates_the_previous_one(store):
    first = store.create_space("Taxes 2025")
    second = store.create_space("Handbook")
    store.activate_space(second["id"])
    active = [s for s in store.list_spaces() if s["is_active"]]
    assert [s["id"] for s in active] == [second["id"]]
    assert store.get_space(first["id"])["is_active"] == 0


def test_a_collection_id_is_platform_shaped_and_unique_per_space(store):
    # Platform Identifier is ^[A-Za-z0-9][A-Za-z0-9._:@/-]*$ — an id that fails
    # it makes every ingest into that space a 422.
    import re

    pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
    one = store.create_space("Taxes 2025")
    two = store.create_space("Taxes 2025")
    assert pattern.match(one["collection_id"])
    assert one["collection_id"] != two["collection_id"], "same name must not collide"


def test_renaming_a_space_leaves_its_collection_id_frozen(store):
    # The tag is denormalized onto every ingested document over in the
    # platform and cannot be rewritten there, so it must never change here.
    space = store.create_space("Taxes")
    renamed = store.rename_space(space["id"], "Taxes 2025")
    assert renamed["name"] == "Taxes 2025"
    assert renamed["collection_id"] == space["collection_id"]


def test_scope_is_the_collection_when_everything_is_ticked(store):
    space = store.create_space("Taxes 2025")
    _ready(store, space["id"], "w2")
    _ready(store, space["id"], "1099")
    scope = store.active_scope()
    assert scope["all_selected"] is True
    assert scope["ready_count"] == 2
    assert sorted(scope["selected_document_ids"]) == ["doc::1099", "doc::w2"]


def test_unticking_narrows_the_scope_to_the_remaining_documents(store):
    space = store.create_space("Taxes 2025")
    w2 = _ready(store, space["id"], "w2")
    _ready(store, space["id"], "1099")
    store.set_selected(w2, selected=False)
    scope = store.active_scope()
    assert scope["all_selected"] is False
    assert scope["selected_document_ids"] == ["doc::1099"]


def test_unticking_everything_is_nothing_not_everything(store):
    # An empty selection must be distinguishable from "no filter": the
    # platform reads an empty collection filter as tenant-wide.
    space = store.create_space("Taxes 2025")
    for name in ("w2", "1099"):
        store.set_selected(_ready(store, space["id"], name), selected=False)
    scope = store.active_scope()
    assert scope["selected_document_ids"] == []
    assert scope["ready_count"] == 2, "the caller must be able to tell these apart"
    assert scope["all_selected"] is False


def test_no_active_space_means_fall_back_not_narrow_to_nothing(store):
    assert store.active_scope() is None


def test_a_document_still_indexing_is_not_in_scope(store):
    space = store.create_space("Taxes 2025")
    store.create_document(
        space["id"],
        filename="w2.pdf",
        title="W-2",
        media_type="application/pdf",
        byte_size=10,
    )
    assert store.active_scope()["ready_count"] == 0


def test_a_failed_document_stays_visible_with_its_error(store):
    space = store.create_space("Taxes 2025")
    document_id = store.create_document(
        space["id"],
        filename="scan.pdf",
        title="Scan",
        media_type="application/pdf",
        byte_size=10,
    )
    store.mark_failed(document_id, error="no text layer")
    listed = store.list_documents(space["id"])
    assert [d["status"] for d in listed] == ["failed"]
    assert listed[0]["error"] == "no text layer"
    assert store.active_scope()["ready_count"] == 0


def test_trashing_removes_from_scope_immediately_and_undo_restores(store):
    space = store.create_space("Taxes 2025")
    w2 = _ready(store, space["id"], "w2")
    _ready(store, space["id"], "1099")
    store.trash_document(w2)
    assert store.active_scope()["selected_document_ids"] == ["doc::1099"]
    assert len(store.list_documents(space["id"])) == 1
    assert len(store.list_documents(space["id"], include_trashed=True)) == 2
    store.restore_document(w2)
    assert len(store.active_scope()["selected_document_ids"]) == 2


def test_restoring_a_space_does_not_resurrect_earlier_deletions(store):
    # Deleting a space trashes what is in it. Undoing that must not undo a
    # document the customer deleted on purpose beforehand.
    space = store.create_space("Taxes 2025")
    old = _ready(store, space["id"], "old")
    kept = _ready(store, space["id"], "kept")
    store.trash_document(old)
    store.trash_space(space["id"])
    assert store.list_spaces() == []
    store.restore_space(space["id"])
    live = {d["id"] for d in store.list_documents(space["id"])}
    assert live == {kept}


def test_purge_only_takes_documents_past_the_horizon(store):
    space = store.create_space("Taxes 2025")
    document_id = _ready(store, space["id"], "w2")
    store.trash_document(document_id)
    deleted_at = store.get_document(document_id)["deleted_at"]
    assert store.documents_due_for_purge(older_than=deleted_at - 1) == []
    due = store.documents_due_for_purge(older_than=deleted_at + 1)
    assert [d["id"] for d in due] == [document_id]
    store.purge_document(document_id)
    with pytest.raises(DocumentNotFound):
        store.get_document(document_id)


def test_a_trashed_space_is_dropped_only_once_it_is_empty(store):
    space = store.create_space("Taxes 2025")
    document_id = _ready(store, space["id"], "w2")
    store.trash_space(space["id"])
    horizon = store.get_document(document_id)["deleted_at"] + 1
    assert store.purge_empty_spaces(older_than=horizon) == 0
    store.purge_document(document_id)
    assert store.purge_empty_spaces(older_than=horizon) == 1


def test_missing_ids_raise_rather_than_pass_silently(store):
    with pytest.raises(SpaceNotFound):
        store.get_space("spc_missing")
    with pytest.raises(DocumentNotFound):
        store.trash_document("doc_missing")
    with pytest.raises(SpaceNotFound):
        store.create_document(
            "spc_missing",
            filename="a.txt",
            title="a",
            media_type="text/plain",
            byte_size=1,
        )


def test_the_registry_survives_a_reopen(store, tmp_path):
    space = store.create_space("Taxes 2025")
    _ready(store, space["id"], "w2")
    reopened = DocumentStore(tmp_path / "documents.db")
    assert reopened.active_scope()["selected_document_ids"] == ["doc::w2"]
