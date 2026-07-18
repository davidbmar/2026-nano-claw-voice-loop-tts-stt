"""aiohttp transport adapter for nano-claw authentication.

The portable auth policy, Google verifier, and :class:`AuthStore` deliberately
know nothing about HTTP.  This module owns the deployment-specific boundary:
same-origin request checks, host-only cookies, route response shapes, trusted
client-IP selection, and live WebSocket bindings.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from aiohttp import WSCloseCode, web

from .google_verifier import (
    GoogleIDTokenVerifier,
    GoogleKeysUnavailable,
    InvalidIDToken,
    LoginRateLimitExceeded,
    LoginRateLimiter,
    UnknownKeyID,
)
from .policy import (
    DEFAULT_ABSOLUTE_TTL,
    DEFAULT_IDLE_TTL,
    LoginNonceCapacityError,
    LoginNonceStore,
    normalize_datetime,
)
from .sqlite_store import DEFAULT_TENANT_ID, SQLiteAuthStore
from .store import AuthStore, ResolvedSession

log = logging.getLogger("webauth-aiohttp")

SESSION_COOKIE_NAME = "nc_session"
PRE_AUTH_COOKIE_NAME = "nc_pre_auth"

AUTH_MODE_OFF = "off"
AUTH_MODE_OPTIONAL = "optional"

LOCAL_ORIGIN = "http://localhost:9090"
PUBLIC_ORIGIN = "https://nano.chattychapters.com"
ALLOWED_ORIGINS = frozenset({LOCAL_ORIGIN, PUBLIC_ORIGIN})
PUBLIC_HOST = "nano.chattychapters.com"
PUBLIC_HOST_HEADERS = frozenset({PUBLIC_HOST, f"{PUBLIC_HOST}:443"})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SENSITIVE_PATH_PREFIXES = (
    "/api/auth/",
    "/api/conversations",
    "/api/history",
)

# GIS documents these four browser origins.  No googleusercontent image origin
# is allowed: the UI uses local initials and never renders Google's picture
# claim.  The main voice console contains no inline application script.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://accounts.google.com/gsi/client; "
    "frame-src https://accounts.google.com/gsi/; "
    "connect-src 'self' ws://localhost:9090 "
    "wss://nano.chattychapters.com https://accounts.google.com/gsi/; "
    "style-src 'self' https://accounts.google.com/gsi/style; "
    "img-src 'self' data:; "
    "media-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
}

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _configured_mode(raw_mode: str | None, client_id: str | None) -> str:
    """Return the only enabled mode, failing closed on partial configuration."""

    mode = (raw_mode or "").strip().lower()
    configured_client_id = (client_id or "").strip()
    if mode == AUTH_MODE_OPTIONAL and configured_client_id:
        return AUTH_MODE_OPTIONAL
    return AUTH_MODE_OFF


def _request_host(request: web.Request) -> str:
    """Return a normalized Host name without trusting forwarded headers."""

    value = request.headers.get("Host", "")
    if value.startswith("["):
        closing = value.find("]")
        return value[1:closing].lower() if closing > 0 else ""
    return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()


def trusted_client_ip(request: object) -> str | None:
    """Use Cloudflare's address only on nano's tunnel host.

    The Docker publish is loopback-only, making the public host the trusted
    cloudflared ingress path.  Direct/local requests ignore forwarding headers
    and use aiohttp's peer address.
    """

    if not isinstance(request, web.Request):
        headers = getattr(request, "headers", {})
        host = str(headers.get("Host", ""))
        host = host.rsplit(":", 1)[0].lower()
    else:
        headers = request.headers
        host = _request_host(request)
    raw_host = str(headers.get("Host", "")).lower()
    if host == PUBLIC_HOST and raw_host in PUBLIC_HOST_HEADERS:
        tunnel_ip = headers.get("CF-Connecting-IP")
        if tunnel_ip:
            return tunnel_ip
    remote = getattr(request, "remote", None)
    return remote if isinstance(remote, str) or remote is None else str(remote)


def _is_sensitive_path(path: str) -> bool:
    return path == "/api/me" or any(
        path.startswith(prefix) for prefix in SENSITIVE_PATH_PREFIXES
    )


def _needs_mutation_guard(request: web.Request) -> bool:
    return request.method.upper() not in SAFE_METHODS and any(
        request.path.startswith(prefix) for prefix in SENSITIVE_PATH_PREFIXES
    )


def _same_origin_request(request: web.Request) -> bool:
    return (
        _origin_matches_request(request)
        and request.headers.get("Sec-Fetch-Site", "").lower() == "same-origin"
        and request.headers.get("X-NC-Auth") == "1"
    )


def _origin_matches_request(request: web.Request) -> bool:
    """Match each allowed browser origin to its deployment Host value."""

    origin = request.headers.get("Origin")
    raw_host = request.headers.get("Host", "").lower()
    if origin == LOCAL_ORIGIN:
        return raw_host == "localhost:9090"
    if origin == PUBLIC_ORIGIN:
        return raw_host in PUBLIC_HOST_HEADERS
    return False


def _strip_cors_headers(response: web.StreamResponse) -> None:
    for name in tuple(response.headers):
        if name.lower().startswith("access-control-"):
            response.headers.popall(name, None)


def _decorate_response(
    request: web.Request, response: web.StreamResponse
) -> web.StreamResponse:
    """Apply security/cache policy after handlers, including HTTP errors."""

    if request.path == "/" or _is_sensitive_path(request.path):
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
    if _is_sensitive_path(request.path):
        response.headers["Cache-Control"] = "no-store"
        _strip_cors_headers(response)
    return response


@web.middleware
async def request_security_middleware(
    request: web.Request, handler: Callable[[web.Request], Any]
) -> web.StreamResponse:
    """Guard unsafe auth/history requests and add response security headers."""

    if _needs_mutation_guard(request) and not _same_origin_request(request):
        response: web.StreamResponse = web.json_response(
            {"error": "request_rejected"}, status=403
        )
        return _decorate_response(request, response)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    return _decorate_response(request, response)


def _json_error(error: str, status: int) -> web.Response:
    return web.json_response({"error": error}, status=status)


async def _invoke(callable_: Callable[..., Any], *args: Any) -> Any:
    """Run synchronous store operations off-loop while remaining test-friendly."""

    result = await asyncio.to_thread(callable_, *args)
    if inspect.isawaitable(result):
        return await result
    return result


def _validated_identity(value: object) -> ResolvedSession:
    if not isinstance(value, Mapping):
        raise RuntimeError("auth store returned an invalid session")
    sub = value.get("sub")
    tenant = value.get("tenant")
    if (
        not isinstance(sub, str)
        or not sub
        or not isinstance(tenant, str)
        or not tenant
    ):
        raise RuntimeError("auth store returned an invalid session")
    return {"sub": sub, "tenant": tenant}


@dataclass(frozen=True, slots=True)
class WebSocketIdentity:
    """Identity fixed to one WebSocket for its complete lifetime."""

    user_sub: str | None
    tenant: str | None
    conversation_id: str


@dataclass(slots=True)
class _BoundToken:
    raw_token: str
    identity: ResolvedSession
    sockets: dict[int, Any]
    expiry_task: asyncio.Task[None] | None = None


class AiohttpAuthAdapter:
    """Bind the portable authentication core to nano's aiohttp application."""

    def __init__(
        self,
        *,
        client_id: str | None,
        mode: str,
        public_https: bool,
        store: AuthStore | None,
        verifier: GoogleIDTokenVerifier | Any | None = None,
        nonce_store: LoginNonceStore | None = None,
        rate_limiter: LoginRateLimiter | None = None,
        clock: Clock = _utc_now,
        tenant_id: str | None = None,
        absolute_ttl: timedelta | None = None,
        idle_ttl: timedelta | None = None,
    ) -> None:
        configured_client_id = (client_id or "").strip()
        self.mode = _configured_mode(mode, configured_client_id)
        self.client_id = configured_client_id if self.mode != AUTH_MODE_OFF else ""
        self.public_https = bool(public_https)
        self.store = store
        self.clock = clock
        self.tenant_id = tenant_id or getattr(store, "tenant_id", DEFAULT_TENANT_ID)

        policy = getattr(store, "policy", None)
        self.absolute_ttl = (
            absolute_ttl
            if absolute_ttl is not None
            else getattr(policy, "absolute_ttl", DEFAULT_ABSOLUTE_TTL)
        )
        self.idle_ttl = (
            idle_ttl
            if idle_ttl is not None
            else getattr(policy, "idle_ttl", DEFAULT_IDLE_TTL)
        )
        if self.absolute_ttl <= timedelta(0) or self.idle_ttl <= timedelta(0):
            raise ValueError("session lifetimes must be positive")

        if nonce_store is None and verifier is not None:
            candidate = getattr(verifier, "nonce_store", None)
            if isinstance(candidate, LoginNonceStore):
                nonce_store = candidate
        self.nonce_store = (
            nonce_store
            if nonce_store is not None
            else LoginNonceStore(
                random_bytes=secrets.token_bytes,
                clock=clock,
            )
        )

        # Production deliberately takes GoogleKeyCache's default rotating fetch
        # path.  In particular, no adapter path supplies ``initial_keys``.
        self.verifier = (
            verifier
            if verifier is not None
            else GoogleIDTokenVerifier(nonce_store=self.nonce_store)
        )
        self.rate_limiter = (
            rate_limiter
            if rate_limiter is not None
            else LoginRateLimiter(ip_source=trusted_client_ip)
        )

        self._socket_lock = asyncio.Lock()
        self._bound_tokens: dict[str, _BoundToken] = {}
        self._socket_token_hashes: dict[int, str] = {}

    @classmethod
    def from_environment(cls) -> "AiohttpAuthAdapter":
        client_id = os.environ.get("NANO_CLAW_GOOGLE_CLIENT_ID")
        mode = _configured_mode(os.environ.get("NANO_CLAW_AUTH"), client_id)
        public_https = os.environ.get("NANO_CLAW_PUBLIC_HTTPS") == "1"
        store: AuthStore | None = (
            SQLiteAuthStore() if mode != AUTH_MODE_OFF else None
        )
        return cls(
            client_id=client_id,
            mode=mode,
            public_https=public_https,
            store=store,
        )

    @property
    def enabled(self) -> bool:
        return self.mode != AUTH_MODE_OFF and bool(self.client_id)

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/auth/config", self.config_handler)
        app.router.add_post("/api/auth/google", self.google_handler)
        app.router.add_post("/api/auth/logout", self.logout_handler)
        app.router.add_get("/api/me", self.me_handler)

    def _now(self) -> datetime:
        return normalize_datetime(self.clock())

    def _set_cookie(
        self,
        response: web.StreamResponse,
        name: str,
        value: str,
        *,
        max_age: int,
    ) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            secure=self.public_https,
            httponly=True,
            samesite="Lax",
        )

    def _clear_cookie(self, response: web.StreamResponse, name: str) -> None:
        response.del_cookie(
            name,
            path="/",
            secure=self.public_https,
            httponly=True,
            samesite="Lax",
        )

    async def config_handler(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return web.json_response({"mode": AUTH_MODE_OFF})

        now = self._now()
        reusable_binding: str | None = None
        existing = request.cookies.get(PRE_AUTH_COOKIE_NAME)
        if existing:
            try:
                if self.nonce_store.expected_nonce(existing, now=now) is not None:
                    reusable_binding = existing
            except (TypeError, ValueError):
                reusable_binding = None
        try:
            challenge = self.nonce_store.issue(reusable_binding, now=now)
        except LoginNonceCapacityError:
            return _json_error("login_unavailable", 503)
        except Exception:
            log.exception("Could not issue a login challenge")
            return _json_error("login_unavailable", 503)

        response = web.json_response(
            {
                "clientId": self.client_id,
                "mode": self.mode,
                "nonce": challenge.nonce,
            }
        )
        nonce_max_age = max(
            1, int((challenge.expires_at - now).total_seconds())
        )
        self._set_cookie(
            response,
            PRE_AUTH_COOKIE_NAME,
            challenge.pre_auth_value,
            max_age=nonce_max_age,
        )
        return response

    def _invalid_credential_response(self) -> web.Response:
        # UnknownKeyID and every other InvalidIDToken deliberately share this
        # exact response to avoid turning the endpoint into a kid-validity oracle.
        return _json_error("invalid_credential", 401)

    def _rate_limit_response(
        self, error: LoginRateLimitExceeded
    ) -> web.Response:
        response = _json_error("rate_limited", 429)
        response.headers["Retry-After"] = str(error.retry_after)
        return response

    async def google_handler(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return _json_error("login_unavailable", 503)

        now = self._now()
        try:
            self.rate_limiter.check_request(request, now=now)
        except LoginRateLimitExceeded as exc:
            return self._rate_limit_response(exc)
        except (TypeError, ValueError, RuntimeError):
            # An invalid/missing trusted IP source fails closed without echoing it.
            return _json_error("rate_limited", 429)

        try:
            body = await request.json()
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            web.HTTPBadRequest,
        ):
            return self._invalid_credential_response()
        if not isinstance(body, dict):
            return self._invalid_credential_response()
        credential = body.get("credential")

        # This is the critical nonce boundary: a JSON nonce (or any other
        # client-supplied field) is ignored.  Only the host-only pre-auth cookie
        # set by config can select the expected signed nonce.
        pre_auth_value = request.cookies.get(PRE_AUTH_COOKIE_NAME)
        if not pre_auth_value:
            return self._invalid_credential_response()
        try:
            expected_nonce = self.nonce_store.expected_nonce(
                pre_auth_value, now=now
            )
        except (TypeError, ValueError):
            expected_nonce = None
        if expected_nonce is None:
            return self._invalid_credential_response()

        try:
            identity = await self.verifier.verify_id_token(
                credential,
                now=now,
                expected_aud=self.client_id,
                expected_nonce=expected_nonce,
            )
        except (UnknownKeyID, InvalidIDToken):
            return self._invalid_credential_response()
        except GoogleKeysUnavailable:
            return _json_error("login_unavailable", 503)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let an unexpected third-party exception stringify the JWT
            # or decoded claims into application logs.
            log.error("Unexpected Google login verification failure")
            return _json_error("login_unavailable", 503)

        try:
            self.rate_limiter.check_sub(identity["sub"], now=now)
        except LoginRateLimitExceeded as exc:
            return self._rate_limit_response(exc)
        except (KeyError, TypeError, ValueError):
            return self._invalid_credential_response()

        if self.store is None:
            return _json_error("auth_unavailable", 503)
        try:
            await _invoke(
                self.store.upsert_identity,
                identity["sub"],
                identity.get("email"),
                identity.get("name"),
            )
            # Serialize rotation with pre-upgrade session resolution.  A socket
            # can therefore only bind before rotation (and be closed below) or
            # observe the revoked bearer afterward; it cannot slip between the
            # store write and live-socket registration.
            async with self._socket_lock:
                raw_token = await _invoke(
                    self.store.issue_hashed_session,
                    identity["sub"],
                    self.tenant_id,
                    now,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Authentication store failed during login")
            return _json_error("auth_unavailable", 503)
        if not isinstance(raw_token, str) or not raw_token:
            log.error("Authentication store returned an invalid session bearer")
            return _json_error("auth_unavailable", 503)

        # Session rotation revoked any older bearer for this subject.  Close a
        # bound old socket so its cached identity cannot outlive that rotation.
        await self._close_subject_sockets(identity["sub"], self.tenant_id)

        response = web.json_response(
            {
                "user": {
                    "sub": identity["sub"],
                    "email": identity.get("email"),
                    "name": identity.get("name"),
                    "tenant": self.tenant_id,
                }
            }
        )
        self._set_cookie(
            response,
            SESSION_COOKIE_NAME,
            raw_token,
            max_age=max(1, int(self.absolute_ttl.total_seconds())),
        )
        self._clear_cookie(response, PRE_AUTH_COOKIE_NAME)
        return response

    async def _resolve_raw_token(
        self, raw_token: str, *, now: datetime | None = None
    ) -> ResolvedSession | None:
        if self.store is None:
            raise RuntimeError("authentication store is unavailable")
        value = await _invoke(
            self.store.resolve_session,
            raw_token,
            self._now() if now is None else normalize_datetime(now),
        )
        if value is None:
            return None
        identity = _validated_identity(value)
        if identity["tenant"] != self.tenant_id:
            raise RuntimeError("auth store returned an unexpected tenant")
        return identity

    async def me_handler(self, request: web.Request) -> web.Response:
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not raw_token:
            return _json_error("unauthenticated", 401)
        try:
            identity = await self._resolve_raw_token(raw_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Authentication store failed while resolving /api/me")
            return _json_error("auth_unavailable", 503)
        if identity is None:
            await self.close_bound_sockets(raw_token)
            response = _json_error("unauthenticated", 401)
            self._clear_cookie(response, SESSION_COOKIE_NAME)
            return response
        return web.json_response({"user": dict(identity)})

    async def logout_handler(self, request: web.Request) -> web.Response:
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        store_failed = False
        if raw_token and self.store is not None:
            try:
                # This lock pairs with bind_websocket's resolve/register
                # critical section, closing the revocation race at upgrade.
                async with self._socket_lock:
                    await _invoke(self.store.revoke, raw_token)
            except asyncio.CancelledError:
                raise
            except Exception:
                store_failed = True
                log.exception("Authentication store failed during logout")
        elif raw_token:
            store_failed = True

        if raw_token:
            await self.close_bound_sockets(raw_token)
        response = (
            _json_error("auth_unavailable", 503)
            if store_failed
            else web.json_response({"ok": True})
        )
        self._clear_cookie(response, SESSION_COOKIE_NAME)
        return response

    async def bind_websocket(
        self,
        request: web.Request,
        ws: web.WebSocketResponse,
        conversation_id: str,
        *,
        prepare: bool = False,
    ) -> WebSocketIdentity:
        """Authorize and register a socket, optionally performing its upgrade.

        Production passes ``prepare=True`` so the session resolve, HTTP upgrade,
        and registry insertion share the same lock as revocation.  Tests and
        alternate transports may bind a socket double without preparing it.
        """

        if not _origin_matches_request(request):
            raise web.HTTPForbidden(text="WebSocket origin rejected")

        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not raw_token:
            identity = WebSocketIdentity(None, None, conversation_id)
            self._attach_websocket_identity(ws, identity)
            if prepare:
                await ws.prepare(request)
            return identity

        token_hash = self._token_hash(raw_token)
        resolved: ResolvedSession | None
        identity: WebSocketIdentity | None = None
        # Resolution, optional upgrade, and registration are one critical
        # section relative to logout and login rotation.  Revocation therefore
        # either precedes resolution or closes an already-prepared socket.
        async with self._socket_lock:
            try:
                resolved = await self._resolve_raw_token(raw_token)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Authentication store failed before WebSocket upgrade")
                raise web.HTTPServiceUnavailable(
                    text="Authentication unavailable"
                ) from None
            if resolved is not None:
                identity = WebSocketIdentity(
                    resolved["sub"], resolved["tenant"], conversation_id
                )
                self._attach_websocket_identity(ws, identity)
                if prepare:
                    await ws.prepare(request)
                entry = self._bound_tokens.get(token_hash)
                if entry is None:
                    entry = _BoundToken(raw_token, resolved, {})
                    self._bound_tokens[token_hash] = entry
                socket_id = id(ws)
                entry.sockets[socket_id] = ws
                self._socket_token_hashes[socket_id] = token_hash
                if entry.expiry_task is None or entry.expiry_task.done():
                    entry.expiry_task = asyncio.create_task(
                        self._watch_token_expiry(token_hash)
                    )
        if resolved is None:
            await self.close_bound_sockets(raw_token)
            raise web.HTTPUnauthorized(text="Session expired")
        assert identity is not None
        return identity

    @staticmethod
    def _attach_websocket_identity(
        ws: web.WebSocketResponse, identity: WebSocketIdentity
    ) -> None:
        ws["user_sub"] = identity.user_sub
        ws["tenant"] = identity.tenant
        ws["conversation_id"] = identity.conversation_id

    @staticmethod
    def _token_hash(raw_token: str) -> str:
        # Use the portable fixed digest without retaining bearer keys in maps.
        from .policy import hash_session_token

        return hash_session_token(raw_token)

    async def _watch_token_expiry(self, token_hash: str) -> None:
        """Close a bound token at its idle/absolute boundary.

        Checking more frequently would touch a valid sliding-idle session and
        keep it alive.  The first check is therefore exactly one idle TTL after
        binding; any real intervening HTTP/socket bind may extend it once.
        """

        try:
            while True:
                await asyncio.sleep(max(1.0, self.idle_ttl.total_seconds()))
                async with self._socket_lock:
                    entry = self._bound_tokens.get(token_hash)
                    if entry is None:
                        return
                    raw_token = entry.raw_token
                try:
                    resolved = await self._resolve_raw_token(raw_token)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "Authentication store failed during socket expiry check"
                    )
                    resolved = None
                if resolved is None:
                    await self._close_token_hash(token_hash)
                    return
        except asyncio.CancelledError:
            return

    async def unbind_websocket(self, ws: Any) -> None:
        task: asyncio.Task[None] | None = None
        async with self._socket_lock:
            socket_id = id(ws)
            token_hash = self._socket_token_hashes.pop(socket_id, None)
            if token_hash is None:
                return
            entry = self._bound_tokens.get(token_hash)
            if entry is None:
                return
            entry.sockets.pop(socket_id, None)
            if not entry.sockets:
                self._bound_tokens.pop(token_hash, None)
                task = entry.expiry_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _close_token_hash(self, token_hash: str) -> None:
        async with self._socket_lock:
            entry = self._bound_tokens.pop(token_hash, None)
            if entry is None:
                return
            sockets = tuple(entry.sockets.values())
            for ws in sockets:
                self._socket_token_hashes.pop(id(ws), None)
            task = entry.expiry_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(
            *(
                ws.close(
                    code=WSCloseCode.POLICY_VIOLATION,
                    message=b"session ended",
                )
                for ws in sockets
                if not getattr(ws, "closed", False)
            ),
            return_exceptions=True,
        )

    async def close_bound_sockets(self, raw_token: str) -> None:
        await self._close_token_hash(self._token_hash(raw_token))

    async def _close_subject_sockets(self, sub: str, tenant: str) -> None:
        async with self._socket_lock:
            token_hashes = tuple(
                token_hash
                for token_hash, entry in self._bound_tokens.items()
                if entry.identity["sub"] == sub
                and entry.identity["tenant"] == tenant
            )
        for token_hash in token_hashes:
            await self._close_token_hash(token_hash)

    async def close_expired_sockets(self, now: datetime | None = None) -> None:
        """Resolve all live bindings once and actively close invalid sessions."""

        current = self._now() if now is None else normalize_datetime(now)
        async with self._socket_lock:
            entries = tuple(
                (token_hash, entry.raw_token)
                for token_hash, entry in self._bound_tokens.items()
            )
        for token_hash, raw_token in entries:
            try:
                resolved = await self._resolve_raw_token(raw_token, now=current)
            except asyncio.CancelledError:
                raise
            except Exception:
                resolved = None
            if resolved is None:
                await self._close_token_hash(token_hash)

    async def close(self) -> None:
        async with self._socket_lock:
            token_hashes = tuple(self._bound_tokens)
        for token_hash in token_hashes:
            await self._close_token_hash(token_hash)


AUTH_ADAPTER_KEY = web.AppKey("nano_claw_auth_adapter", AiohttpAuthAdapter)


async def close_auth_adapter(app: web.Application) -> None:
    adapter = app.get(AUTH_ADAPTER_KEY)
    if adapter is not None:
        await adapter.close()
