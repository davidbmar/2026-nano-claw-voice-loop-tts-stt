"""HTTP surface for document spaces.

Reads are same-origin only; every mutation additionally requires the operator
password, enforced by ``request_security_middleware`` before a handler runs
(see ``OPERATOR_PATH_PREFIXES``). Uploading or re-scoping a document changes
what every caller on this deployment is answered from, which is the same blast
radius as the existing operator controls, so it gets the same gate.

Only GET and POST are used, matching the rest of this server. Deletion is
``POST .../delete`` rather than the DELETE verb so the trash reads as an
ordinary reversible action and shares one code path with restore.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from voice import documents as documents_module
from voice.document_store import (
    DEFAULT_DOCUMENT_DB_PATH,
    DocumentNotFound,
    DocumentStore,
    SpaceNotFound,
)

log = logging.getLogger("nano-claw.documents")

DEFAULT_DOCUMENT_DIR = "/app/data/documents"

# Whole-request ceiling. The per-file limit customers are held to is
# documents.MAX_DOCUMENT_BYTES; this leaves headroom for multipart framing and
# a second field without letting an unbounded body reach the parser.
MAX_UPLOAD_REQUEST_BYTES = documents_module.MAX_DOCUMENT_BYTES + (4 * 1024 * 1024)

_ORIGINAL_STEM = "original"
_TEXT_NAME = "text.txt"


def document_dir() -> Path:
    return Path(os.environ.get("NANO_CLAW_DOCUMENT_DIR") or DEFAULT_DOCUMENT_DIR)


class DocumentRuntime:
    """Opens the registry on first use, not at startup.

    Building it eagerly would make every deployment — and every test that
    constructs the application — depend on the document directory being
    writable. A database that cannot be opened should fail the document
    routes with a 503, not stop the voice server from serving calls.
    """

    def __init__(self) -> None:
        self._store: DocumentStore | None = None
        self._platform: documents_module.PlatformClient | None = None

    @property
    def store(self) -> DocumentStore:
        if self._store is None:
            self._store = DocumentStore()
        return self._store

    @property
    def platform(self) -> documents_module.PlatformClient:
        if self._platform is None:
            self._platform = documents_module.PlatformClient()
        return self._platform


DOCUMENT_RUNTIME_KEY = web.AppKey("document_runtime", DocumentRuntime)


class _StoreUnavailable(Exception):
    """The registry could not be opened; the caller answers 503."""


def _store(request: web.Request) -> DocumentStore:
    try:
        return request.app[DOCUMENT_RUNTIME_KEY].store
    except Exception as exc:  # noqa: BLE001 - any storage failure is the same 503
        log.warning("document store unavailable: %s", exc)
        raise _StoreUnavailable() from exc


def _platform(request: web.Request) -> documents_module.PlatformClient:
    return request.app[DOCUMENT_RUNTIME_KEY].platform


def _json(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _public_space(space: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": space["id"],
        "name": space["name"],
        "collectionId": space["collection_id"],
        "isActive": bool(space["is_active"]),
        "documentCount": space.get("document_count", 0),
    }


def _public_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "spaceId": row["space_id"],
        "filename": row["filename"],
        "title": row["title"],
        "mediaType": row["media_type"],
        "bytes": row["byte_size"],
        "charCount": row["char_count"],
        "status": row["status"],
        "selected": bool(row["selected"]),
        "error": row["error"],
        "createdAt": row["created_at"],
        "deletedAt": row["deleted_at"],
    }


async def _read_json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "expected a JSON body"}),
            content_type="application/json",
        )
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "expected a JSON object"}),
            content_type="application/json",
        )
    return body


# ---- spaces --------------------------------------------------------------


async def spaces_list_handler(request: web.Request) -> web.Response:
    store = _store(request)
    scope = store.active_scope()
    return _json(
        {
            "spaces": [_public_space(space) for space in store.list_spaces()],
            "scope": _scope_payload(scope),
        }
    )


def _scope_payload(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    return {
        "spaceId": scope["space_id"],
        "spaceName": scope["space_name"],
        "readyCount": scope["ready_count"],
        "selectedCount": len(scope["selected_document_ids"]),
        "allSelected": scope["all_selected"],
    }


async def spaces_create_handler(request: web.Request) -> web.Response:
    body = await _read_json(request)
    try:
        space = _store(request).create_space(str(body.get("name", "")))
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400)
    return _json(_public_space(space), status=201)


async def space_update_handler(request: web.Request) -> web.Response:
    store = _store(request)
    space_id = request.match_info["space_id"]
    body = await _read_json(request)
    try:
        if "name" in body:
            store.rename_space(space_id, str(body["name"]))
        if body.get("active"):
            store.activate_space(space_id)
        space = store.get_space(space_id)
    except SpaceNotFound:
        return _error("no such space", 404)
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400)
    return _json(_public_space(space))


async def space_trash_handler(request: web.Request) -> web.Response:
    try:
        _store(request).trash_space(request.match_info["space_id"])
    except SpaceNotFound:
        return _error("no such space", 404)
    return _json({"ok": True})


async def space_restore_handler(request: web.Request) -> web.Response:
    try:
        space = _store(request).restore_space(request.match_info["space_id"])
    except SpaceNotFound:
        return _error("no such space", 404)
    return _json(_public_space(space))


# ---- documents -----------------------------------------------------------


async def documents_list_handler(request: web.Request) -> web.Response:
    store = _store(request)
    space_id = request.match_info["space_id"]
    try:
        store.get_space(space_id)
    except SpaceNotFound:
        return _error("no such space", 404)
    include_trashed = request.query.get("trashed") == "1"
    rows = store.list_documents(space_id, include_trashed=include_trashed)
    return _json({"documents": [_public_document(row) for row in rows]})


async def _read_upload(request: web.Request) -> tuple[str, str, bytes]:
    """Pull one file part off a multipart body without buffering the world.

    aiohttp's own ``client_max_size`` is a whole-request guard applied when a
    handler calls ``request.post()``; this streams instead and stops at the
    first chunk that crosses the per-file limit, so a hostile 2 GB upload costs
    us one buffer, not two gigabytes.
    """

    reader = await request.multipart()
    limit = documents_module.MAX_DOCUMENT_BYTES
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name != "file":
            await part.read()  # drain, so the next part parses
            continue
        filename = part.filename or ""
        media_type = part.headers.get("Content-Type", "application/octet-stream")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise documents_module.DocumentTooLarge(
                    f"That file is larger than the {limit // (1024 * 1024)} MB limit."
                )
            chunks.append(chunk)
        return filename, media_type, b"".join(chunks)
    raise documents_module.DocumentError("no file was uploaded", status=400)


async def document_upload_handler(request: web.Request) -> web.Response:
    store = _store(request)
    space_id = request.match_info["space_id"]
    try:
        space = store.get_space(space_id)
    except SpaceNotFound:
        return _error("no such space", 404)

    try:
        filename, media_type, data = await _read_upload(request)
        kind, text = documents_module.extract(filename, data)
    except documents_module.DocumentError as exc:
        return _error(exc.message, exc.status)

    title = documents_module.derive_title(filename, data)
    document_id = store.create_document(
        space_id,
        filename=os.path.basename(filename),
        title=title,
        media_type=media_type,
        byte_size=len(data),
    )

    # Keep the original alongside the extracted text: it is what the customer
    # uploaded and can be downloaded back, and it is what a future re-index
    # (a better parser, a re-created space) would start from.
    folder = document_dir() / document_id
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        suffix = os.path.splitext(filename)[1].lower()
        (folder / f"{_ORIGINAL_STEM}{suffix}").write_bytes(data)
        (folder / _TEXT_NAME).write_text(text, encoding="utf-8")
    except OSError as exc:
        store.mark_failed(document_id, error=f"could not be saved ({exc})")
        return _error("the document could not be saved on the server", 500)

    store.mark_indexing(document_id, char_count=len(text))
    try:
        platform_document_id = await _platform(request).ingest_text(
            document_key=f"{space['collection_id']}:{document_id}",
            title=title,
            text=text,
            collection_id=space["collection_id"],
            source_ref=f"nano-claw://documents/{document_id}",
            metadata={"filename": os.path.basename(filename), "kind": kind},
        )
    except documents_module.DocumentError as exc:
        store.mark_failed(document_id, error=exc.message)
        return _error(exc.message, exc.status)
    except Exception as exc:  # noqa: BLE001 - the platform may be down entirely
        log.exception("ingest failed for %s", document_id)
        # Several httpx exceptions stringify to "", which showed up in the UI
        # as a failed row with no reason at all. Fall back to the class name so
        # there is always something to act on.
        reason = str(exc).strip() or type(exc).__name__
        store.mark_failed(document_id, error=reason[:200])
        return _error(f"the document service could not index this ({reason})", 502)

    store.mark_ready(document_id, platform_document_id=platform_document_id)
    return _json(_public_document(store.get_document(document_id)), status=201)


async def document_update_handler(request: web.Request) -> web.Response:
    store = _store(request)
    document_id = request.match_info["document_id"]
    body = await _read_json(request)
    try:
        if "title" in body:
            store.rename_document(document_id, str(body["title"]))
        if "selected" in body:
            store.set_selected(document_id, selected=bool(body["selected"]))
        row = store.get_document(document_id)
    except DocumentNotFound:
        return _error("no such document", 404)
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400)
    return _json(_public_document(row))


async def document_trash_handler(request: web.Request) -> web.Response:
    try:
        _store(request).trash_document(request.match_info["document_id"])
    except DocumentNotFound:
        return _error("no such document", 404)
    return _json({"ok": True})


async def document_restore_handler(request: web.Request) -> web.Response:
    store = _store(request)
    document_id = request.match_info["document_id"]
    try:
        store.restore_document(document_id)
        row = store.get_document(document_id)
    except DocumentNotFound:
        return _error("no such document", 404)
    return _json(_public_document(row))


async def document_download_handler(request: web.Request) -> web.Response:
    store = _store(request)
    document_id = request.match_info["document_id"]
    try:
        row = store.get_document(document_id)
    except DocumentNotFound:
        return _error("no such document", 404)

    # Resolve inside the document directory and refuse anything that escapes
    # it, the same discipline the static handler applies to web assets.
    folder = (document_dir() / document_id).resolve()
    root = document_dir().resolve()
    if not str(folder).startswith(str(root)):
        return _error("no such document", 404)
    matches = sorted(folder.glob(f"{_ORIGINAL_STEM}.*")) if folder.is_dir() else []
    if not matches:
        return _error("the original file is no longer stored", 404)
    return web.FileResponse(
        matches[0],
        headers={
            "Content-Disposition": f'attachment; filename="{row["filename"]}"',
            "Cache-Control": "no-store",
        },
    )


# ---- scope for a turn ----------------------------------------------------

_TURN_STORES: dict[str, DocumentStore] = {}


def _turn_store() -> DocumentStore:
    """A store for the hot turn path, cached per configured database path.

    Constructing one runs the migration check, which is too much to repeat on
    every turn — but caching a single instance forever would ignore a changed
    ``NANO_CLAW_DOCUMENT_DB``, so the path is the key.
    """

    path = os.environ.get("NANO_CLAW_DOCUMENT_DB") or DEFAULT_DOCUMENT_DB_PATH
    store = _TURN_STORES.get(path)
    if store is None:
        store = DocumentStore(path)
        _TURN_STORES[path] = store
    return store


def active_document_scope() -> dict[str, Any] | None:
    """The active space's scope, in the shape ``/api/chat`` accepts.

    Both the browser and the phone line call this, and both get the same
    answer: a document space is a property of the deployment, not of a browser
    tab, because a caller on the phone has to be answered from the same
    documents the console shows.

    Returns ``None`` — meaning "use the configured default" — when there is no
    active space or the registry cannot be read. A document feature that is
    switched off or broken must not be able to silence the assistant.
    """

    try:
        scope = _turn_store().active_scope()
    except Exception as exc:  # noqa: BLE001 - storage problems must not end turns
        log.warning("document scope unavailable, using configured default: %s", exc)
        return None
    if scope is None:
        return None
    return {
        "collectionId": scope["collection_id"],
        "selected": scope["selected_document_ids"],
        "ready": scope["ready_count"],
        "allSelected": scope["all_selected"],
    }


# ---- purge ---------------------------------------------------------------


def purge_document_files(document_id: str) -> None:
    """Remove a purged document's stored bytes; missing files are fine."""

    shutil.rmtree(document_dir() / document_id, ignore_errors=True)


async def purge_expired_documents(
    store: DocumentStore,
    platform: documents_module.PlatformClient,
    *,
    retention_days: float,
    now: float | None = None,
) -> int:
    """Purge trashed documents past the retention horizon. Returns the count.

    A document whose platform delete fails is left in the trash so the next
    sweep retries it — the alternative, dropping the registry row anyway, would
    orphan the indexed copy where nothing could ever reach it again.
    """

    horizon = (now if now is not None else time.time()) - retention_days * 86_400
    purged = 0
    for row in store.documents_due_for_purge(older_than=horizon):
        platform_id = row["platform_document_id"]
        if platform_id:
            try:
                removed = await platform.delete_document(platform_id)
            except Exception:  # noqa: BLE001 - the platform may be unreachable
                log.warning("purge deferred for %s: platform unreachable", row["id"])
                continue
            if not removed:
                continue
        purge_document_files(row["id"])
        store.purge_document(row["id"])
        purged += 1
    store.purge_empty_spaces(older_than=horizon)
    return purged


# ---- registration --------------------------------------------------------


def _guard(handler):
    """Turn an unopenable registry into a 503 instead of a 500."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except _StoreUnavailable:
            return _error("document storage is unavailable on this deployment", 503)

    return wrapper


def register_document_routes(app: web.Application) -> None:
    """Attach the document API and its lazily-opened store to an application."""

    app[DOCUMENT_RUNTIME_KEY] = DocumentRuntime()

    routes = (
        ("GET", "/api/spaces", spaces_list_handler),
        ("POST", "/api/spaces", spaces_create_handler),
        ("POST", "/api/spaces/{space_id}", space_update_handler),
        ("POST", "/api/spaces/{space_id}/delete", space_trash_handler),
        ("POST", "/api/spaces/{space_id}/restore", space_restore_handler),
        ("GET", "/api/spaces/{space_id}/documents", documents_list_handler),
        ("POST", "/api/spaces/{space_id}/documents", document_upload_handler),
        ("POST", "/api/documents/{document_id}", document_update_handler),
        ("POST", "/api/documents/{document_id}/delete", document_trash_handler),
        ("POST", "/api/documents/{document_id}/restore", document_restore_handler),
        ("GET", "/api/documents/{document_id}/download", document_download_handler),
    )
    for method, path, handler in routes:
        app.router.add_route(method, path, _guard(handler))
