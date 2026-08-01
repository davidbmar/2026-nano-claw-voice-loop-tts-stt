"""The document API, driven through the real router and middleware.

The gate is the point of most of these: uploading, re-scoping or deleting a
document changes what every caller on this deployment is answered from, so a
request that is merely same-origin must not be enough.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from voice import document_routes, server
from voice.webauth.aiohttp_adapter import (
    AUTH_MODE_OPTIONAL,
    PUBLIC_ORIGIN,
    AiohttpAuthAdapter,
)

OPERATOR_PASSWORD = "operator-secret"


def _payload(body: bytes) -> streams.StreamReader:
    """A real StreamReader, because multipart parsing needs more than readany.

    The lighter shim other suites use exposes only ``readany``; aiohttp's
    multipart reader calls ``readline`` as well, so a genuine reader is the
    only way to exercise the upload path rather than a mock of it.
    """

    protocol = mock.Mock(_reading_paused=False)
    reader = streams.StreamReader(
        protocol, limit=2**16, loop=asyncio.get_running_loop()
    )
    reader.feed_data(body)
    reader.feed_eof()
    return reader


class InProcessClient:
    """Dispatch through aiohttp's router/middleware without a TCP bind."""

    def __init__(self, app: web.Application):
        self.app = app
        app.freeze()

    async def request(self, method, path, *, headers=None, json_body=None, body=None):
        request_headers = {"Host": "nano.chattychapters.com", **(headers or {})}
        raw = b""
        if json_body is not None:
            raw = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
        elif body is not None:
            raw = body
        request_headers["Content-Length"] = str(len(raw))
        transport = mock.Mock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            ("127.0.0.1", 40000) if name == "peername" else default
        )
        request = make_mocked_request(
            method,
            path,
            headers=request_headers,
            app=self.app,
            transport=transport,
            payload=_payload(raw),
        )
        return await self.app._handle(request)

    async def get(self, path, *, headers=None):
        return await self.request("GET", path, headers=headers)


def operator_headers():
    return {
        "Origin": PUBLIC_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-NC-Auth": "1",
        "X-NC-Operator": OPERATOR_PASSWORD,
    }


def same_origin_only():
    return {
        "Origin": PUBLIC_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-NC-Auth": "1",
    }


def payload(response):
    return json.loads(response.text)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CLAW_OPERATOR_PASSWORD", OPERATOR_PASSWORD)
    monkeypatch.setenv("NANO_CLAW_DOCUMENT_DB", str(tmp_path / "documents.db"))
    monkeypatch.setenv("NANO_CLAW_DOCUMENT_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("NANO_CLAW_AUTH_DB", str(tmp_path / "auth.db"))
    adapter = AiohttpAuthAdapter(
        client_id="client.apps.googleusercontent.com",
        mode=AUTH_MODE_OPTIONAL,
        public_https=False,
        store=None,
        verifier=object(),
    )
    return InProcessClient(server.create_app(auth_adapter=adapter))


async def make_space(client, name="Taxes 2025"):
    response = await client.request(
        "POST", "/api/spaces", headers=operator_headers(), json_body={"name": name}
    )
    assert response.status == 201, response.text
    return payload(response)


def multipart(filename: str, data: bytes, boundary: str = "----nanoclaw") -> tuple:
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    return headers, body


# ---- the gate ------------------------------------------------------------


async def test_creating_a_space_without_the_operator_password_is_refused(client):
    response = await client.request(
        "POST", "/api/spaces", headers=same_origin_only(), json_body={"name": "X"}
    )
    assert response.status == 403
    assert payload(await client.get("/api/spaces"))["spaces"] == []


async def test_a_cross_site_request_is_refused_even_with_the_password(client):
    headers = operator_headers() | {"Origin": "https://evil.example"}
    response = await client.request(
        "POST", "/api/spaces", headers=headers, json_body={"name": "X"}
    )
    assert response.status == 403


async def test_mutating_a_document_by_id_is_gated_too(client):
    # The operator check matches exact paths for the older controls; these
    # routes carry an id segment, so a prefix rule has to cover them or every
    # per-document mutation would be anonymous.
    response = await client.request(
        "POST", "/api/documents/doc_whatever", headers=same_origin_only(),
        json_body={"selected": False},
    )
    assert response.status == 403


async def test_reading_the_space_list_needs_no_password(client):
    response = await client.get("/api/spaces")
    assert response.status == 200
    assert payload(response) == {"spaces": [], "scope": None}


# ---- spaces --------------------------------------------------------------


async def test_the_first_space_is_active_and_shows_in_the_scope(client):
    space = await make_space(client)
    body = payload(await client.get("/api/spaces"))
    assert body["spaces"][0]["isActive"] is True
    assert body["scope"]["spaceId"] == space["id"]
    assert body["scope"]["readyCount"] == 0


async def test_a_space_can_be_renamed_and_switched(client):
    first = await make_space(client, "Taxes 2025")
    second = await make_space(client, "Handbook")
    renamed = await client.request(
        "POST", f"/api/spaces/{first['id']}",
        headers=operator_headers(), json_body={"name": "Taxes FY25"},
    )
    assert payload(renamed)["name"] == "Taxes FY25"
    # The collection id is baked into every ingested document over on the
    # platform and must survive a rename.
    assert payload(renamed)["collectionId"] == first["collectionId"]

    await client.request(
        "POST", f"/api/spaces/{second['id']}",
        headers=operator_headers(), json_body={"active": True},
    )
    body = payload(await client.get("/api/spaces"))
    assert body["scope"]["spaceId"] == second["id"]


async def test_an_unknown_space_is_a_404_not_a_500(client):
    response = await client.request(
        "POST", "/api/spaces/spc_nope", headers=operator_headers(),
        json_body={"name": "x"},
    )
    assert response.status == 404


# ---- upload --------------------------------------------------------------


async def test_uploading_a_text_file_indexes_it_and_lists_it(client, monkeypatch):
    space = await make_space(client)

    ingested = {}

    async def fake_ingest(self, **kwargs):
        ingested.update(kwargs)
        return "doc::platform-1"

    monkeypatch.setattr(
        document_routes.documents_module.PlatformClient, "ingest_text", fake_ingest
    )

    headers, body = multipart("w2.txt", b"Form W-2 wages and tax statement 2025")
    response = await client.request(
        "POST", f"/api/spaces/{space['id']}/documents",
        headers=operator_headers() | headers, body=body,
    )
    assert response.status == 201, response.text
    document = payload(response)
    assert document["status"] == "ready"
    assert document["title"] == "w2"
    assert ingested["collection_id"] == space["collectionId"]
    assert "wages and tax" in ingested["text"]

    listing = payload(await client.get(f"/api/spaces/{space['id']}/documents"))
    assert [d["id"] for d in listing["documents"]] == [document["id"]]
    assert payload(await client.get("/api/spaces"))["scope"]["readyCount"] == 1


async def test_an_unreadable_pdf_is_refused_and_leaves_no_document(client):
    space = await make_space(client)
    headers, body = multipart("scan.pdf", b"%PDF-1.4\nnot a real pdf")
    response = await client.request(
        "POST", f"/api/spaces/{space['id']}/documents",
        headers=operator_headers() | headers, body=body,
    )
    assert response.status in (422, 502)
    listing = payload(await client.get(f"/api/spaces/{space['id']}/documents"))
    assert listing["documents"] == [], "a file we cannot read must not become a row"


async def test_an_unsupported_type_says_what_is_accepted(client):
    space = await make_space(client)
    headers, body = multipart("photo.tiff", b"II*\x00 some tiff bytes here")
    response = await client.request(
        "POST", f"/api/spaces/{space['id']}/documents",
        headers=operator_headers() | headers, body=body,
    )
    assert response.status == 415
    assert "PDF" in payload(response)["error"]


async def test_a_failed_ingest_leaves_the_document_visible_with_its_error(
    client, monkeypatch
):
    space = await make_space(client)

    async def boom(self, **kwargs):
        raise document_routes.documents_module.DocumentError(
            "the document service rejected this file", status=502
        )

    monkeypatch.setattr(
        document_routes.documents_module.PlatformClient, "ingest_text", boom
    )
    headers, body = multipart("w2.txt", b"Form W-2 wages and tax statement 2025")
    response = await client.request(
        "POST", f"/api/spaces/{space['id']}/documents",
        headers=operator_headers() | headers, body=body,
    )
    assert response.status == 502
    listed = payload(await client.get(f"/api/spaces/{space['id']}/documents"))
    # It stays in the list so the customer can see why, rather than the upload
    # silently vanishing.
    assert [d["status"] for d in listed["documents"]] == ["failed"]
    assert payload(await client.get("/api/spaces"))["scope"]["readyCount"] == 0


# ---- scope and trash -----------------------------------------------------


async def _upload(client, space, name, monkeypatch, platform_id):
    async def fake_ingest(self, **kwargs):
        return platform_id

    monkeypatch.setattr(
        document_routes.documents_module.PlatformClient, "ingest_text", fake_ingest
    )
    headers, body = multipart(name, b"Form W-2 wages and tax statement 2025")
    response = await client.request(
        "POST", f"/api/spaces/{space['id']}/documents",
        headers=operator_headers() | headers, body=body,
    )
    assert response.status == 201, response.text
    return payload(response)


async def test_unticking_a_document_narrows_the_scope(client, monkeypatch):
    space = await make_space(client)
    first = await _upload(client, space, "w2.txt", monkeypatch, "doc::a")
    await _upload(client, space, "1099.txt", monkeypatch, "doc::b")

    await client.request(
        "POST", f"/api/documents/{first['id']}",
        headers=operator_headers(), json_body={"selected": False},
    )
    scope = payload(await client.get("/api/spaces"))["scope"]
    assert scope["readyCount"] == 2
    assert scope["selectedCount"] == 1
    assert scope["allSelected"] is False


async def test_trashing_a_document_hides_it_immediately_and_undo_restores(
    client, monkeypatch
):
    space = await make_space(client)
    document = await _upload(client, space, "w2.txt", monkeypatch, "doc::a")

    await client.request(
        "POST", f"/api/documents/{document['id']}/delete", headers=operator_headers()
    )
    assert payload(await client.get(f"/api/spaces/{space['id']}/documents"))[
        "documents"
    ] == []
    assert payload(await client.get("/api/spaces"))["scope"]["readyCount"] == 0
    trashed = payload(
        await client.get(f"/api/spaces/{space['id']}/documents?trashed=1")
    )
    assert len(trashed["documents"]) == 1

    await client.request(
        "POST", f"/api/documents/{document['id']}/restore", headers=operator_headers()
    )
    assert payload(await client.get("/api/spaces"))["scope"]["readyCount"] == 1


async def test_the_original_file_can_be_downloaded_back(client, monkeypatch):
    space = await make_space(client)
    document = await _upload(client, space, "w2.txt", monkeypatch, "doc::a")
    response = await client.get(f"/api/documents/{document['id']}/download")
    assert response.status == 200
    assert "attachment" in response.headers["Content-Disposition"]
