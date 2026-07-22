"""In-process aiohttp auth adapter tests.

These tests intentionally dispatch through ``Application._handle`` instead of
starting ``TestServer``: the managed sandbox prohibits even loopback binds.
They still exercise aiohttp's router, middleware chain, Request cookies/body,
and real Response cookie serialization without opening a socket.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from voice import server
from voice.webauth import aiohttp_adapter as adapter_module
from voice.webauth.aiohttp_adapter import (
    ALLOWED_ORIGINS,
    AUTH_MODE_OFF,
    AUTH_MODE_OPTIONAL,
    CONTENT_SECURITY_POLICY,
    PRE_AUTH_COOKIE_NAME,
    PUBLIC_ORIGIN,
    SESSION_COOKIE_NAME,
    AiohttpAuthAdapter,
    trusted_client_ip,
)
from voice.webauth.google_verifier import InvalidIDToken, UnknownKeyID
from voice.webauth.policy import LoginNonceStore


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


class _MemoryPayload:
    def __init__(self, body: bytes):
        self.body = body

    def set_read_chunk_size(self, _size):
        return None

    async def readany(self):
        body, self.body = self.body, b""
        return body


class InProcessAiohttpClient:
    """Tiny no-bind client that runs aiohttp's actual router/middlewares."""

    def __init__(self, app: web.Application):
        self.app = app
        self.cookies: dict[str, str] = {}
        app.freeze()

    def make_request(self, method, path, *, headers=None, json_body=None):
        request_headers = {
            "Host": "nano.chattychapters.com",
            **(headers or {}),
        }
        if self.cookies:
            request_headers.setdefault(
                "Cookie",
                "; ".join(f"{name}={value}" for name, value in self.cookies.items()),
            )
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
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
        request = self.make_request(
            method, path, headers=headers, json_body=json_body
        )
        response = await self.app._handle(request)
        for name, morsel in response.cookies.items():
            if morsel["max-age"] == "0":
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = morsel.value
        return response

    async def get(self, path, *, headers=None):
        return await self.request("GET", path, headers=headers)

    async def post(self, path, *, headers=None, json_body=None):
        return await self.request(
            "POST", path, headers=headers, json_body=json_body
        )


class FakeStore:
    tenant_id = "nano-claw"

    def __init__(self):
        self.policy = SimpleNamespace(
            absolute_ttl=timedelta(days=7), idle_ttl=timedelta(hours=24)
        )
        self.identities = {}
        self.sessions = {}
        self.expired = set()
        self.unavailable = False
        self._next_token = 0

    def upsert_identity(self, sub, email, name):
        self._fail_if_unavailable()
        self.identities[sub] = {"email": email, "name": name}

    def issue_hashed_session(self, sub, tenant, now):
        self._fail_if_unavailable()
        self.sessions = {
            token: identity
            for token, identity in self.sessions.items()
            if identity != {"sub": sub, "tenant": tenant}
        }
        self._next_token += 1
        token = f"session-token-{self._next_token}"
        self.sessions[token] = {"sub": sub, "tenant": tenant}
        return token

    def resolve_session(self, raw_token, now):
        self._fail_if_unavailable()
        if raw_token in self.expired:
            return None
        identity = self.sessions.get(raw_token)
        return dict(identity) if identity else None

    def revoke(self, raw_token):
        self._fail_if_unavailable()
        return int(self.sessions.pop(raw_token, None) is not None)

    def revoke_all(self, sub):
        self._fail_if_unavailable()
        removed = [
            token
            for token, identity in self.sessions.items()
            if identity["sub"] == sub
        ]
        for token in removed:
            self.sessions.pop(token)
        return len(removed)

    def sweep(self, now):
        self._fail_if_unavailable()
        return 0

    def _fail_if_unavailable(self):
        if self.unavailable:
            raise OSError("store unavailable")


class FakeVerifier:
    def __init__(self, nonce_store):
        self.nonce_store = nonce_store
        self.calls = []

    async def verify_id_token(
        self, credential, *, now, expected_aud, expected_nonce
    ):
        self.calls.append(
            {
                "credential": credential,
                "aud": expected_aud,
                "nonce": expected_nonce,
            }
        )
        if credential == "unknown-kid":
            raise UnknownKeyID()
        if credential != "valid-google-token":
            raise InvalidIDToken()
        if not self.nonce_store.consume(expected_nonce, now=now):
            raise InvalidIDToken()
        return {
            "sub": "google-sub-1",
            "email": "user@example.test",
            "name": "Test User",
        }


class FakeWebSocket(dict):
    def __init__(self):
        super().__init__()
        self.prepared = False
        self.closed = False
        self.close_code = None

    async def prepare(self, request):
        self.prepared = True
        return None

    async def close(self, *, code=None, message=None):
        if not self.prepared:
            raise RuntimeError("socket was closed before upgrade")
        self.closed = True
        self.close_code = code
        return True


def make_adapter(*, public_https=False, store=None, mode=AUTH_MODE_OPTIONAL):
    auth_store = FakeStore() if store is None else store
    nonce_store = LoginNonceStore(random_bytes=lambda size: b"n" * size, clock=lambda: NOW)
    # Repeated deterministic draws are safe here because every test has a new
    # store and config rotates the one binding rather than issuing concurrent
    # challenges.
    verifier = FakeVerifier(nonce_store)
    adapter = AiohttpAuthAdapter(
        client_id="google-client-id.apps.googleusercontent.com",
        mode=mode,
        public_https=public_https,
        store=auth_store,
        verifier=verifier,
        nonce_store=nonce_store,
        clock=lambda: NOW,
    )
    return adapter, auth_store, verifier


def make_client(adapter):
    return InProcessAiohttpClient(server.create_app(auth_adapter=adapter))


def auth_headers(**overrides):
    return {
        "Origin": PUBLIC_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-NC-Auth": "1",
        "CF-Connecting-IP": "203.0.113.10",
        **overrides,
    }


def payload(response):
    return json.loads(response.text)


def test_config_off_is_exact_and_enabled_config_issues_bound_nonce():
    async def exercise():
        off, _, _ = make_adapter(mode=AUTH_MODE_OFF)
        off_client = make_client(off)
        response = await off_client.get("/api/auth/config")
        assert response.status == 200
        assert payload(response) == {"mode": "off"}
        assert PRE_AUTH_COOKIE_NAME not in response.cookies

        adapter, _, verifier = make_adapter()
        client = make_client(adapter)
        response = await client.get("/api/auth/config")
        body = payload(response)
        assert body == {
            "clientId": "google-client-id.apps.googleusercontent.com",
            "mode": "optional",
            "nonce": body["nonce"],
        }
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        pre_auth = response.cookies[PRE_AUTH_COOKIE_NAME]
        assert pre_auth["httponly"] is True
        assert pre_auth["path"] == "/"
        assert pre_auth["samesite"] == "Lax"

        # A body nonce is never consulted; the verifier receives the challenge
        # selected strictly through the config-set cookie.
        login = await client.post(
            "/api/auth/google",
            headers=auth_headers(),
            json_body={"credential": "valid-google-token", "nonce": "attacker"},
        )
        assert login.status == 200
        assert verifier.calls[-1]["nonce"] == body["nonce"]

        console = await client.get("/")
        assert console.status == 200
        assert console.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert console.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert console.headers["X-Content-Type-Options"] == "nosniff"

    asyncio.run(exercise())


@pytest.mark.parametrize("public_https", [False, True])
def test_login_me_happy_path_and_session_cookie_policy(public_https):
    async def exercise():
        adapter, store, _ = make_adapter(public_https=public_https)
        client = make_client(adapter)
        await client.get("/api/auth/config")
        login = await client.post(
            "/api/auth/google",
            headers=auth_headers(),
            json_body={"credential": "valid-google-token"},
        )
        assert login.status == 200
        assert payload(login)["user"] == {
            "sub": "google-sub-1",
            "email": "user@example.test",
            "name": "Test User",
            "tenant": "nano-claw",
        }
        cookie = login.cookies[SESSION_COOKIE_NAME]
        assert cookie["httponly"] is True
        assert cookie["path"] == "/"
        assert cookie["samesite"] == "Lax"
        assert cookie["max-age"] == "604800"
        assert bool(cookie["secure"]) is public_https
        assert set(store.sessions) == {cookie.value}

        me = await client.get("/api/me")
        assert me.status == 200
        assert payload(me) == {
            "user": {"sub": "google-sub-1", "tenant": "nano-claw"}
        }
        assert me.headers["Cache-Control"] == "no-store"

    asyncio.run(exercise())


@pytest.mark.parametrize(("configured", "secure"), [("0", False), ("1", True)])
def test_public_https_environment_alone_drives_secure_cookie(
    monkeypatch, configured, secure
):
    async def exercise():
        monkeypatch.setenv("NANO_CLAW_AUTH", "optional")
        monkeypatch.setenv(
            "NANO_CLAW_GOOGLE_CLIENT_ID",
            "google-client-id.apps.googleusercontent.com",
        )
        monkeypatch.setenv("NANO_CLAW_PUBLIC_HTTPS", configured)
        monkeypatch.setattr(adapter_module, "SQLiteAuthStore", FakeStore)
        adapter = AiohttpAuthAdapter.from_environment()
        client = make_client(adapter)
        request = client.make_request("GET", "/api/auth/config")
        assert request.secure is False
        response = await client.request("GET", "/api/auth/config")
        assert bool(response.cookies[PRE_AUTH_COOKIE_NAME]["secure"]) is secure

    asyncio.run(exercise())


def test_unauthenticated_and_expired_me_return_401_and_expiry_clears_cookie():
    async def exercise():
        adapter, store, _ = make_adapter()
        client = make_client(adapter)
        missing = await client.get("/api/me")
        assert missing.status == 401
        assert payload(missing) == {"error": "unauthenticated"}

        store.sessions["expired-token"] = {
            "sub": "google-sub-1",
            "tenant": "nano-claw",
        }
        store.expired.add("expired-token")
        client.cookies[SESSION_COOKIE_NAME] = "expired-token"
        expired = await client.get("/api/me")
        assert expired.status == 401
        cleared = expired.cookies[SESSION_COOKIE_NAME]
        assert cleared.value == ""
        assert cleared["max-age"] == "0"
        assert SESSION_COOKIE_NAME not in client.cookies

    asyncio.run(exercise())


def test_logout_revokes_exact_cookie_and_closes_bound_socket():
    async def exercise():
        adapter, store, _ = make_adapter(public_https=True)
        client = make_client(adapter)
        await client.get("/api/auth/config")
        await client.post(
            "/api/auth/google",
            headers=auth_headers(),
            json_body={"credential": "valid-google-token"},
        )
        raw_token = client.cookies[SESSION_COOKIE_NAME]

        ws = FakeWebSocket()
        request = client.make_request(
            "GET", "/ws", headers={"Origin": PUBLIC_ORIGIN}
        )
        identity = await adapter.bind_websocket(
            request, ws, "voice-test", prepare=True
        )
        assert identity.user_sub == "google-sub-1"
        assert ws == {
            "user_sub": "google-sub-1",
            "tenant": "nano-claw",
            "conversation_id": "voice-test",
        }

        logout = await client.post(
            "/api/auth/logout", headers=auth_headers(), json_body={}
        )
        assert logout.status == 200
        assert payload(logout) == {"ok": True}
        assert raw_token not in store.sessions
        assert ws.closed is True
        cleared = logout.cookies[SESSION_COOKIE_NAME]
        assert cleared.value == ""
        assert cleared["max-age"] == "0"
        assert cleared["path"] == "/"
        assert cleared["httponly"] is True
        assert cleared["samesite"] == "Lax"
        assert cleared["secure"] is True

    asyncio.run(exercise())


def test_cross_origin_and_missing_custom_header_reject_before_verification():
    async def exercise():
        adapter, _, verifier = make_adapter()
        client = make_client(adapter)
        await client.get("/api/auth/config")

        cross_origin = await client.post(
            "/api/auth/google",
            headers=auth_headers(Origin="https://evil.example"),
            json_body={"credential": "valid-google-token"},
        )
        assert cross_origin.status == 403
        assert not verifier.calls
        assert not any(
            name.lower().startswith("access-control-")
            for name in cross_origin.headers
        )

        missing_header = auth_headers()
        missing_header.pop("X-NC-Auth")
        rejected = await client.post(
            "/api/auth/google",
            headers=missing_header,
            json_body={"credential": "valid-google-token"},
        )
        assert rejected.status == 403
        assert not verifier.calls

    asyncio.run(exercise())


def test_preflight_has_no_cors_allowance():
    async def exercise():
        adapter, _, _ = make_adapter()
        client = make_client(adapter)
        response = await client.request(
            "OPTIONS",
            "/api/auth/google",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-NC-Auth",
            },
        )
        assert response.status == 405
        assert not any(
            name.lower().startswith("access-control-") for name in response.headers
        )

    asyncio.run(exercise())


def test_invalid_token_and_unknown_kid_have_identical_response():
    async def one(credential):
        adapter, _, _ = make_adapter()
        client = make_client(adapter)
        await client.get("/api/auth/config")
        response = await client.post(
            "/api/auth/google",
            headers=auth_headers(),
            json_body={"credential": credential},
        )
        return response.status, response.text

    invalid = asyncio.run(one("invalid"))
    unknown = asyncio.run(one("unknown-kid"))
    assert invalid == unknown == (401, '{"error": "invalid_credential"}')


def test_cross_origin_websocket_is_rejected_before_upgrade():
    async def exercise():
        adapter, _, _ = make_adapter()
        client = make_client(adapter)
        response = await client.get(
            "/ws", headers={"Origin": "https://evil.example"}
        )
        assert response.status == 403
        assert response.prepared is False

        mismatched_host = await client.get(
            "/ws",
            headers={
                "Host": "localhost:9090",
                "Origin": PUBLIC_ORIGIN,
            },
        )
        assert mismatched_host.status == 403
        assert mismatched_host.prepared is False

    asyncio.run(exercise())


def test_cookie_with_unavailable_store_rejects_websocket_before_upgrade():
    async def exercise():
        store = FakeStore()
        store.sessions["live-token"] = {
            "sub": "google-sub-1",
            "tenant": "nano-claw",
        }
        adapter, _, _ = make_adapter(store=store)
        client = make_client(adapter)
        client.cookies[SESSION_COOKIE_NAME] = "live-token"
        store.unavailable = True
        response = await client.get("/ws", headers={"Origin": PUBLIC_ORIGIN})
        assert response.status == 503
        assert response.prepared is False

    asyncio.run(exercise())


def test_expired_bound_session_is_actively_closed():
    async def exercise():
        adapter, store, _ = make_adapter()
        store.sessions["live-token"] = {
            "sub": "google-sub-1",
            "tenant": "nano-claw",
        }
        client = make_client(adapter)
        client.cookies[SESSION_COOKIE_NAME] = "live-token"
        ws = FakeWebSocket()
        request = client.make_request(
            "GET", "/ws", headers={"Origin": PUBLIC_ORIGIN}
        )
        await adapter.bind_websocket(
            request, ws, "voice-expiry", prepare=True
        )
        store.expired.add("live-token")
        await adapter.close_expired_sockets(NOW + timedelta(hours=24))
        assert ws.closed is True

    asyncio.run(exercise())


def test_ws_allowlist_and_trusted_tunnel_ip_source_are_exact():
    assert ALLOWED_ORIGINS == {
        "http://localhost:9090",
        "https://nano.chattychapters.com",
    }
    tunnel = SimpleNamespace(
        headers={
            "Host": "nano.chattychapters.com",
            "CF-Connecting-IP": "203.0.113.22",
        },
        remote="127.0.0.1",
    )
    direct = SimpleNamespace(
        headers={
            "Host": "localhost:9090",
            "CF-Connecting-IP": "203.0.113.22",
        },
        remote="127.0.0.1",
    )
    assert trusted_client_ip(tunnel) == "203.0.113.22"
    assert trusted_client_ip(direct) == "127.0.0.1"
