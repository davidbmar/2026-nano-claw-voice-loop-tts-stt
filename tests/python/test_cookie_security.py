"""Regression tests for authentication-cookie transport guarantees."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from voice.webauth import aiohttp_adapter as adapter_module
from voice.webauth.aiohttp_adapter import (
    AUTH_MODE_OPTIONAL,
    LEGACY_SESSION_COOKIE_NAME,
    LOCAL_ORIGIN,
    PRE_AUTH_COOKIE_NAME,
    PUBLIC_HOST,
    PUBLIC_ORIGIN,
    SECURITY_HEADERS,
    SESSION_COOKIE_NAME,
    AiohttpAuthAdapter,
    request_security_middleware,
)
from voice.webauth.policy import LoginNonceStore


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class _MemoryPayload:
    def __init__(self, body: bytes):
        self.body = body

    def set_read_chunk_size(self, _size):
        return None

    async def readany(self):
        body, self.body = self.body, b""
        return body


class _Client:
    def __init__(self, app: web.Application, *, host: str):
        self.app = app
        self.host = host
        self.cookies: dict[str, str] = {}
        app.freeze()

    def make_request(self, method, path, *, headers=None, json_body=None):
        request_headers = {"Host": self.host, **(headers or {})}
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        transport = mock.Mock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            ("127.0.0.1", 40000) if name == "peername" else default
        )
        return make_mocked_request(
            method,
            path,
            headers=request_headers,
            app=self.app,
            transport=transport,
            payload=_MemoryPayload(body),
        )

    async def request(self, method, path, *, headers=None, json_body=None):
        response = await self.app._handle(
            self.make_request(
                method, path, headers=headers, json_body=json_body
            )
        )
        for name, morsel in response.cookies.items():
            if morsel["max-age"] == "0":
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = morsel.value
        return response


class _Store:
    tenant_id = "nano-claw"

    def __init__(self):
        self.policy = SimpleNamespace(
            absolute_ttl=timedelta(days=7),
            idle_ttl=timedelta(hours=24),
        )
        self.sessions: dict[str, dict[str, str]] = {}

    def upsert_identity(self, sub, email, name):
        return None

    def issue_hashed_session(self, sub, tenant, now):
        token = "session-token"
        self.sessions[token] = {"sub": sub, "tenant": tenant}
        return token

    def resolve_session(self, raw_token, now):
        return self.sessions.get(raw_token)

    def revoke(self, raw_token):
        return int(self.sessions.pop(raw_token, None) is not None)


class _Verifier:
    async def verify_id_token(
        self, credential, *, now, expected_aud, expected_nonce
    ):
        return {
            "sub": "google-sub",
            "email": "user@example.test",
            "name": "Test User",
        }


async def _root_handler(request):
    return web.Response(text="ok")


def _make_adapter(*, public_https: bool) -> AiohttpAuthAdapter:
    nonce_store = LoginNonceStore(
        random_bytes=lambda size: b"n" * size,
        clock=lambda: NOW,
    )
    return AiohttpAuthAdapter(
        client_id="client.apps.googleusercontent.com",
        mode=AUTH_MODE_OPTIONAL,
        public_https=public_https,
        store=_Store(),
        verifier=_Verifier(),
        nonce_store=nonce_store,
        clock=lambda: NOW,
    )


def _make_app(adapter: AiohttpAuthAdapter) -> web.Application:
    app = web.Application(middlewares=[request_security_middleware])
    adapter.register_routes(app)
    app.router.add_get("/", _root_handler)
    return app


def _auth_headers(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "X-NC-Auth": "1",
    }


async def _login(client: _Client, origin: str):
    config = await client.request("GET", "/api/auth/config")
    login = await client.request(
        "POST",
        "/api/auth/google",
        headers=_auth_headers(origin),
        json_body={"credential": "valid"},
    )
    assert config.status == login.status == 200
    return config, login


def _assert_cookie_policy(morsel, *, secure: bool):
    assert bool(morsel["secure"]) is secure
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Lax"
    assert morsel["path"] == "/"
    assert not morsel["domain"]
    assert "Domain=" not in morsel.OutputString()


def test_https_public_origin_secures_both_auth_cookies():
    async def exercise():
        client = _Client(
            _make_app(_make_adapter(public_https=True)),
            host=PUBLIC_HOST,
        )
        config, login = await _login(client, PUBLIC_ORIGIN)

        _assert_cookie_policy(
            config.cookies[PRE_AUTH_COOKIE_NAME], secure=True
        )
        _assert_cookie_policy(login.cookies[SESSION_COOKIE_NAME], secure=True)

    asyncio.run(exercise())


def test_https_public_origin_is_secure_when_flag_is_unset_or_zero(monkeypatch):
    async def one(configured: str | None):
        monkeypatch.setenv("NANO_CLAW_AUTH", AUTH_MODE_OPTIONAL)
        monkeypatch.setenv(
            "NANO_CLAW_GOOGLE_CLIENT_ID",
            "client.apps.googleusercontent.com",
        )
        if configured is None:
            monkeypatch.delenv("NANO_CLAW_PUBLIC_HTTPS", raising=False)
        else:
            monkeypatch.setenv("NANO_CLAW_PUBLIC_HTTPS", configured)
        monkeypatch.setattr(adapter_module, "SQLiteAuthStore", _Store)

        adapter = AiohttpAuthAdapter.from_environment()
        adapter.nonce_store = LoginNonceStore(
            random_bytes=lambda size: b"n" * size,
            clock=lambda: NOW,
        )
        adapter.verifier = _Verifier()
        adapter.clock = lambda: NOW
        client = _Client(_make_app(adapter), host=PUBLIC_HOST)
        config, login = await _login(client, PUBLIC_ORIGIN)

        assert config.cookies[PRE_AUTH_COOKIE_NAME]["secure"] is True
        assert login.cookies[SESSION_COOKIE_NAME]["secure"] is True

    for value in (None, "0"):
        asyncio.run(one(value))


def test_plaintext_localhost_uses_non_secure_compatible_cookie_names():
    async def exercise():
        client = _Client(
            _make_app(_make_adapter(public_https=False)),
            host="localhost:9090",
        )
        config, login = await _login(client, LOCAL_ORIGIN)

        _assert_cookie_policy(
            config.cookies[PRE_AUTH_COOKIE_NAME], secure=False
        )
        _assert_cookie_policy(
            login.cookies[LEGACY_SESSION_COOKIE_NAME], secure=False
        )
        assert LEGACY_SESSION_COOKIE_NAME in client.cookies
        assert SESSION_COOKIE_NAME not in client.cookies

        me = await client.request("GET", "/api/me")
        assert me.status == 200

    asyncio.run(exercise())


def test_hsts_is_present_on_root_and_sensitive_paths():
    async def exercise():
        client = _Client(
            _make_app(_make_adapter(public_https=False)),
            host=PUBLIC_HOST,
        )
        root = await client.request("GET", "/")
        sensitive = await client.request("GET", "/api/me")

        expected = "max-age=31536000; includeSubDomains"
        assert SECURITY_HEADERS["Strict-Transport-Security"] == expected
        assert root.headers["Strict-Transport-Security"] == expected
        assert sensitive.headers["Strict-Transport-Security"] == expected

    asyncio.run(exercise())


def test_host_session_cookie_obeys_prefix_constraints():
    async def exercise():
        client = _Client(
            _make_app(_make_adapter(public_https=False)),
            host=PUBLIC_HOST,
        )
        _, login = await _login(client, PUBLIC_ORIGIN)
        session = login.cookies[SESSION_COOKIE_NAME]

        assert SESSION_COOKIE_NAME == "__Host-nc_session"
        assert session["secure"] is True
        assert session["path"] == "/"
        assert not session["domain"]
        assert "Domain=" not in session.OutputString()

    asyncio.run(exercise())
