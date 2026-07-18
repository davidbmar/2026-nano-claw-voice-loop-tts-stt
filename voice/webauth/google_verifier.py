"""Injectable Google ID-token verification and login abuse controls.

The public verifier is asynchronous because certificate retrieval must never
block an application's event loop.  Signature and standard JWT validation are
delegated to ``google-auth``; this module adds strict Google issuer, claim type,
nonce, key-type, size, cache, and abuse boundaries around that library.

No route or request-framework behavior lives here.  The aiohttp adapter chooses
the trusted source of a request's IP address and supplies the same-site
pre-auth value used to look up a pending nonce.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import ipaddress
import json
import math
import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypedDict

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import exceptions as google_exceptions
from google.auth import jwt as google_jwt
from google.auth.crypt import RSAVerifier

from .policy import IssuedLoginNonce, LoginNonceStore, normalize_datetime


GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = frozenset(
    {"accounts.google.com", "https://accounts.google.com"}
)
PINNED_ALGORITHM = "RS256"

MAX_JWT_BYTES = 16 * 1024
MAX_JWT_HEADER_BYTES = 4 * 1024
MAX_CERT_RESPONSE_BYTES = 256 * 1024
MAX_CERT_BYTES = 16 * 1024
MAX_CACHED_KEYS = 32
MAX_KID_LENGTH = 256
MIN_RSA_KEY_BITS = 2_048
MAX_SUB_LENGTH = 255
MAX_EMAIL_LENGTH = 320
MAX_NAME_LENGTH = 256
MAX_AUDIENCE_LENGTH = 2_048
MAX_NONCE_LENGTH = 512

DEFAULT_NETWORK_TIMEOUT_SECONDS = 5.0
MAX_NETWORK_TIMEOUT_SECONDS = 30.0
DEFAULT_KEY_CACHE_TTL = timedelta(minutes=5)
MAX_KEY_CACHE_TTL = timedelta(hours=24)
DEFAULT_REFRESH_THROTTLE = timedelta(seconds=30)
DEFAULT_UNKNOWN_KID_TTL = timedelta(minutes=1)
DEFAULT_CLOCK_SKEW = timedelta(seconds=60)
MAX_GOOGLE_ID_TOKEN_LIFETIME = timedelta(hours=1)

DEFAULT_IP_ATTEMPTS = 10
DEFAULT_SUB_ATTEMPTS = 5
DEFAULT_RATE_WINDOW = timedelta(minutes=1)
DEFAULT_MAX_RATE_BUCKETS = 4_096

_CACHE_MAX_AGE_RE = re.compile(r"^max-age\s*=\s*\"?(\d+)\"?$", re.I)
_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)


class VerifiedGoogleIdentity(TypedDict):
    """The only identity and display claims allowed beyond this boundary."""

    sub: str
    email: str | None
    name: str | None


class InvalidIDToken(ValueError):
    """A bounded, non-secret rejection for an invalid login credential."""

    def __init__(self) -> None:
        super().__init__("Google ID token was rejected")


class UnknownKeyID(InvalidIDToken):
    """The signed-key identifier is absent after a throttled refresh."""


class GoogleKeysUnavailable(RuntimeError):
    """A bounded outage error for new logins; existing sessions are separate."""

    def __init__(self) -> None:
        super().__init__("Google sign-in is temporarily unavailable")


class LoginRateLimitExceeded(RuntimeError):
    """Raised by a login limiter without exposing the limited IP or subject."""

    def __init__(self, scope: str, retry_after: int) -> None:
        self.scope = scope
        self.retry_after = max(1, int(retry_after))
        super().__init__("Too many login attempts")


@dataclass(frozen=True, slots=True)
class KeyFetchResponse:
    """Bounded transport result accepted by :class:`GoogleKeyCache`."""

    status: int
    body: bytes
    headers: Mapping[str, str]


class GoogleKeyProvider(Protocol):
    """Injectable asynchronous key-cache interface used by the verifier."""

    async def get_key(self, kid: str, *, now: datetime) -> str:
        """Return one validated RSA certificate or raise a bounded error."""

        ...


class RequestIPSource(Protocol):
    """Adapter-supplied trusted extraction of a request's client IP."""

    def __call__(self, request: object) -> str | None:
        ...


KeyFetcher = Callable[
    [str, float, int], KeyFetchResponse | Awaitable[KeyFetchResponse]
]


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _validate_kid(kid: str) -> str:
    if not isinstance(kid, str):
        raise InvalidIDToken()
    if not kid or len(kid) > MAX_KID_LENGTH:
        raise InvalidIDToken()
    try:
        kid.encode("ascii")
    except UnicodeEncodeError:
        raise InvalidIDToken() from None
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in kid):
        raise InvalidIDToken()
    return kid


def _decode_header(credential: str) -> tuple[Mapping[str, Any], str]:
    segments = credential.split(".")
    if len(segments) != 3 or any(not segment for segment in segments):
        raise InvalidIDToken()
    encoded_header = segments[0]
    if len(encoded_header) > MAX_JWT_HEADER_BYTES * 2:
        raise InvalidIDToken()
    try:
        padding = "=" * (-len(encoded_header) % 4)
        raw_header = base64.b64decode(
            encoded_header + padding, altchars=b"-_", validate=True
        )
        if len(raw_header) > MAX_JWT_HEADER_BYTES:
            raise InvalidIDToken()
        header = json.loads(
            raw_header.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise InvalidIDToken() from None
    if not isinstance(header, dict):
        raise InvalidIDToken()
    if header.get("alg") != PINNED_ALGORITHM:
        raise InvalidIDToken()
    if "crit" in header or "jku" in header or "x5u" in header:
        raise InvalidIDToken()
    token_type = header.get("typ")
    if token_type is not None and token_type != "JWT":
        raise InvalidIDToken()
    return MappingProxyType(header), _validate_kid(header.get("kid"))


def _load_pinned_rsa_key(certificate: str) -> None:
    if not isinstance(certificate, str):
        raise GoogleKeysUnavailable()
    try:
        encoded = certificate.encode("ascii")
    except UnicodeEncodeError:
        raise GoogleKeysUnavailable() from None
    if not encoded or len(encoded) > MAX_CERT_BYTES:
        raise GoogleKeysUnavailable()

    try:
        try:
            public_key = x509.load_pem_x509_certificate(encoded).public_key()
        except ValueError:
            public_key = serialization.load_pem_public_key(encoded)
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise GoogleKeysUnavailable()
        if public_key.key_size < MIN_RSA_KEY_BITS:
            raise GoogleKeysUnavailable()
        # Exercise the exact google-auth verifier parser used during decode.
        RSAVerifier.from_string(encoded)
    except GoogleKeysUnavailable:
        raise
    except (TypeError, ValueError):
        raise GoogleKeysUnavailable() from None


def _default_key_fetcher(
    url: str, timeout_seconds: float, max_response_bytes: int
) -> KeyFetchResponse:
    """Fetch Google's x509 map synchronously; the cache runs this in a thread."""

    import requests

    try:
        with requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=(min(timeout_seconds, 3.0), timeout_seconds),
            stream=True,
            allow_redirects=False,
        ) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_response_bytes:
                        raise GoogleKeysUnavailable()
                except ValueError:
                    raise GoogleKeysUnavailable() from None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=8_192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_response_bytes:
                    raise GoogleKeysUnavailable()
                chunks.append(chunk)
            headers = {
                str(key): str(value) for key, value in response.headers.items()
            }
            return KeyFetchResponse(
                status=int(response.status_code),
                body=b"".join(chunks),
                headers=MappingProxyType(headers),
            )
    except GoogleKeysUnavailable:
        raise
    except requests.RequestException:
        raise GoogleKeysUnavailable() from None


def _header_value(headers: Mapping[str, str], wanted: str) -> str | None:
    wanted_lower = wanted.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted_lower:
            return str(value)
    return None


def _cache_ttl(
    headers: Mapping[str, str],
    *,
    now: datetime,
    default_ttl: timedelta,
    maximum_ttl: timedelta,
) -> timedelta:
    cache_control = _header_value(headers, "cache-control")
    max_age: int | None = None
    if cache_control is not None:
        directives = [part.strip() for part in cache_control.split(",")]
        lowered = {part.lower() for part in directives}
        if "no-store" in lowered or "no-cache" in lowered:
            return timedelta(0)
        for directive in directives:
            match = _CACHE_MAX_AGE_RE.match(directive)
            if match:
                max_age = int(match.group(1))
                break

    if max_age is not None:
        age_value = _header_value(headers, "age")
        if age_value is not None:
            try:
                age = max(0, int(age_value))
            except ValueError:
                age = 0
            max_age = max(0, max_age - age)
        return min(timedelta(seconds=max_age), maximum_ttl)

    expires = _header_value(headers, "expires")
    if expires is not None:
        try:
            expires_at = normalize_datetime(parsedate_to_datetime(expires))
            return min(max(expires_at - now, timedelta(0)), maximum_ttl)
        except (TypeError, ValueError, OverflowError):
            pass
    return min(default_ttl, maximum_ttl)


class GoogleKeyCache:
    """Cache Google's rotating RSA certificates with bounded refresh behavior.

    Fresh-cache misses may force one rotation refresh.  A global refresh gate
    plus per-``kid`` negative entries prevents random forged headers from
    causing outbound traffic.  All refreshes share one async lock, so concurrent
    misses are single-flight.  Expired keys are never used when refresh fails.
    """

    def __init__(
        self,
        *,
        fetcher: KeyFetcher | None = None,
        certs_url: str = GOOGLE_CERTS_URL,
        network_timeout_seconds: float = DEFAULT_NETWORK_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_CERT_RESPONSE_BYTES,
        default_ttl: timedelta = DEFAULT_KEY_CACHE_TTL,
        maximum_ttl: timedelta = MAX_KEY_CACHE_TTL,
        refresh_throttle: timedelta = DEFAULT_REFRESH_THROTTLE,
        unknown_kid_ttl: timedelta = DEFAULT_UNKNOWN_KID_TTL,
        initial_keys: Mapping[str, str] | None = None,
        initial_expires_at: datetime | None = None,
    ) -> None:
        if not isinstance(certs_url, str) or not certs_url.startswith("https://"):
            raise ValueError("certs_url must be an HTTPS URL")
        if isinstance(network_timeout_seconds, bool) or not isinstance(
            network_timeout_seconds, (int, float)
        ):
            raise TypeError("network_timeout_seconds must be numeric")
        if (
            not math.isfinite(network_timeout_seconds)
            or network_timeout_seconds <= 0
            or network_timeout_seconds > MAX_NETWORK_TIMEOUT_SECONDS
        ):
            raise ValueError("network_timeout_seconds must be positive and finite")
        if isinstance(max_response_bytes, bool) or not isinstance(
            max_response_bytes, int
        ):
            raise TypeError("max_response_bytes must be an integer")
        if max_response_bytes <= 0 or max_response_bytes > MAX_CERT_RESPONSE_BYTES:
            raise ValueError("max_response_bytes exceeds the certificate cap")
        for value, field_name, allow_zero in (
            (default_ttl, "default_ttl", True),
            (maximum_ttl, "maximum_ttl", False),
            (refresh_throttle, "refresh_throttle", False),
            (unknown_kid_ttl, "unknown_kid_ttl", False),
        ):
            if value < timedelta(0) or (not allow_zero and value == timedelta(0)):
                raise ValueError(f"{field_name} has an invalid duration")
        if default_ttl > maximum_ttl:
            raise ValueError("default_ttl must not exceed maximum_ttl")
        if maximum_ttl > MAX_KEY_CACHE_TTL:
            raise ValueError("maximum_ttl exceeds the key-cache cap")

        self.fetcher = fetcher or _default_key_fetcher
        self.certs_url = certs_url
        self.network_timeout_seconds = float(network_timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.default_ttl = default_ttl
        self.maximum_ttl = maximum_ttl
        self.refresh_throttle = refresh_throttle
        self.unknown_kid_ttl = unknown_kid_ttl
        self._keys: dict[str, str] = {}
        self._expires_at = _MIN_UTC
        self._next_refresh_at = _MIN_UTC
        self._unknown_until: dict[str, datetime] = {}
        self._refresh_lock = asyncio.Lock()
        self._fetch_count = 0

        if initial_keys:
            self._keys = self._validate_keys(initial_keys)
            self._expires_at = (
                normalize_datetime(initial_expires_at)
                if initial_expires_at is not None
                else datetime.max.replace(tzinfo=timezone.utc)
            )
        elif initial_expires_at is not None:
            raise ValueError("initial_expires_at requires initial_keys")

    @property
    def fetch_count(self) -> int:
        return self._fetch_count

    @property
    def cached_kids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def _validate_keys(self, keys: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(keys, Mapping) or not keys or len(keys) > MAX_CACHED_KEYS:
            raise GoogleKeysUnavailable()
        validated: dict[str, str] = {}
        for raw_kid, certificate in keys.items():
            try:
                kid = _validate_kid(raw_kid)
            except InvalidIDToken:
                raise GoogleKeysUnavailable() from None
            _load_pinned_rsa_key(certificate)
            validated[kid] = certificate
        return validated

    def _parse_response(
        self, response: KeyFetchResponse, now: datetime
    ) -> tuple[dict[str, str], datetime]:
        if not isinstance(response, KeyFetchResponse):
            raise GoogleKeysUnavailable()
        if response.status != 200:
            raise GoogleKeysUnavailable()
        if not isinstance(response.body, bytes):
            raise GoogleKeysUnavailable()
        if not response.body or len(response.body) > self.max_response_bytes:
            raise GoogleKeysUnavailable()
        try:
            decoded = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise GoogleKeysUnavailable() from None
        keys = self._validate_keys(decoded)
        ttl = _cache_ttl(
            response.headers,
            now=now,
            default_ttl=self.default_ttl,
            maximum_ttl=self.maximum_ttl,
        )
        return keys, now + ttl

    async def _fetch(self, now: datetime) -> tuple[dict[str, str], datetime]:
        fetcher = self.fetcher
        self._fetch_count += 1
        try:
            is_async = inspect.iscoroutinefunction(fetcher) or inspect.iscoroutinefunction(
                getattr(fetcher, "__call__", None)
            )
            if is_async:
                result = fetcher(
                    self.certs_url,
                    self.network_timeout_seconds,
                    self.max_response_bytes,
                )
            else:
                result = await asyncio.to_thread(
                    fetcher,
                    self.certs_url,
                    self.network_timeout_seconds,
                    self.max_response_bytes,
                )
            if inspect.isawaitable(result):
                result = await result
            return await asyncio.to_thread(self._parse_response, result, now)
        except asyncio.CancelledError:
            raise
        except GoogleKeysUnavailable:
            raise
        except Exception:
            raise GoogleKeysUnavailable() from None

    def _mark_unknown(self, kid: str, now: datetime) -> None:
        expired = [
            value for value, deadline in self._unknown_until.items() if now >= deadline
        ]
        for value in expired:
            self._unknown_until.pop(value, None)
        if len(self._unknown_until) < MAX_CACHED_KEYS * 4:
            self._unknown_until[kid] = now + self.unknown_kid_ttl

    def _known_and_fresh(self, kid: str, now: datetime) -> str | None:
        if now < self._expires_at:
            return self._keys.get(kid)
        return None

    async def get_key(self, kid: str, *, now: datetime) -> str:
        requested_kid = _validate_kid(kid)
        current = normalize_datetime(now)
        known = self._known_and_fresh(requested_kid, current)
        if known is not None:
            return known

        cache_was_fresh = current < self._expires_at
        negative_until = self._unknown_until.get(requested_kid)
        if cache_was_fresh and negative_until is not None and current < negative_until:
            raise UnknownKeyID()

        async with self._refresh_lock:
            known = self._known_and_fresh(requested_kid, current)
            if known is not None:
                return known

            cache_is_fresh = current < self._expires_at
            negative_until = self._unknown_until.get(requested_kid)
            if cache_is_fresh and negative_until is not None and current < negative_until:
                raise UnknownKeyID()

            if current < self._next_refresh_at:
                if cache_is_fresh:
                    self._mark_unknown(requested_kid, current)
                    raise UnknownKeyID()
                raise GoogleKeysUnavailable()

            # Set the gate before awaiting so cancellations and outages cannot
            # turn immediate retries into a fetch storm.
            self._next_refresh_at = current + self.refresh_throttle
            try:
                keys, expires_at = await self._fetch(current)
            except GoogleKeysUnavailable:
                raise
            self._keys = keys
            self._expires_at = expires_at
            self._unknown_until.clear()

            certificate = keys.get(requested_kid)
            if certificate is None:
                self._mark_unknown(requested_kid, current)
                raise UnknownKeyID()
            return certificate


def _validate_required_string(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise InvalidIDToken()
    return value


def _validate_required_ascii_string(value: object, maximum: int) -> str:
    validated = _validate_required_string(value, maximum)
    try:
        validated.encode("ascii")
    except UnicodeEncodeError:
        raise InvalidIDToken() from None
    return validated


def _validate_optional_string(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise InvalidIDToken()
    return value


def _numeric_date(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidIDToken()
    numeric = float(value)
    if not math.isfinite(numeric):
        raise InvalidIDToken()
    return numeric


def _google_auth_decode(
    credential: str,
    kid: str,
    certificate: str,
    expected_aud: str,
    now: datetime,
    clock_skew_seconds: int,
) -> Mapping[str, Any]:
    # google-auth reads its own wall clock.  Bridge only the difference to the
    # injected server clock, then re-enforce every time boundary below with the
    # configured bounded skew.  Signature and audience verification remain on.
    system_now = datetime.now(timezone.utc)
    clock_bridge = math.ceil(abs((system_now - now).total_seconds()))
    library_skew = clock_bridge + clock_skew_seconds + 1
    _load_pinned_rsa_key(certificate)
    try:
        claims = google_jwt.decode(
            credential,
            certs={kid: certificate},
            verify=True,
            audience=expected_aud,
            clock_skew_in_seconds=library_skew,
        )
    except (
        google_exceptions.GoogleAuthError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
    ):
        raise InvalidIDToken() from None
    if not isinstance(claims, Mapping):
        raise InvalidIDToken()
    return claims


class GoogleIDTokenVerifier:
    """Verify one Google ID token and atomically consume its login nonce."""

    def __init__(
        self,
        *,
        key_cache: GoogleKeyProvider | None = None,
        nonce_store: LoginNonceStore | None = None,
        clock_skew: timedelta = DEFAULT_CLOCK_SKEW,
        max_jwt_bytes: int = MAX_JWT_BYTES,
        max_token_lifetime: timedelta = MAX_GOOGLE_ID_TOKEN_LIFETIME,
    ) -> None:
        if clock_skew < timedelta(0) or clock_skew > timedelta(minutes=5):
            raise ValueError("clock_skew must be between zero and five minutes")
        if isinstance(max_jwt_bytes, bool) or not isinstance(max_jwt_bytes, int):
            raise TypeError("max_jwt_bytes must be an integer")
        if max_jwt_bytes <= 0 or max_jwt_bytes > MAX_JWT_BYTES:
            raise ValueError("max_jwt_bytes exceeds the JWT cap")
        if max_token_lifetime <= timedelta(0) or max_token_lifetime > timedelta(
            hours=1
        ):
            raise ValueError("max_token_lifetime must be positive and at most one hour")

        self.key_cache = key_cache or GoogleKeyCache()
        self.nonce_store = nonce_store or LoginNonceStore(
            random_bytes=secrets.token_bytes,
            clock=lambda: datetime.now(timezone.utc),
        )
        self.clock_skew = clock_skew
        self.max_jwt_bytes = max_jwt_bytes
        self.max_token_lifetime = max_token_lifetime

    async def verify_id_token(
        self,
        credential: str,
        *,
        now: datetime,
        expected_aud: str,
        expected_nonce: str,
    ) -> VerifiedGoogleIdentity:
        """Verify signature/claims and consume the nonce, or fail closed.

        All outward errors are bounded and contain neither the JWT nor its
        claims.  Operational key-fetch failures are distinct from invalid
        credentials so an adapter can return a generic temporary outage for a
        new login without affecting already-issued application sessions.
        """

        current = normalize_datetime(now)
        if not isinstance(credential, str) or not credential:
            raise InvalidIDToken()
        # Reject oversized input BEFORE allocating the encoded copy (Opus LOW-3):
        # ascii is one byte/char, so the char count is an exact pre-encode bound.
        if len(credential) > self.max_jwt_bytes:
            raise InvalidIDToken()
        try:
            encoded = credential.encode("ascii")
        except UnicodeEncodeError:
            raise InvalidIDToken() from None
        if len(encoded) > self.max_jwt_bytes:
            raise InvalidIDToken()
        audience = _validate_required_ascii_string(
            expected_aud, MAX_AUDIENCE_LENGTH
        )
        nonce = _validate_required_ascii_string(expected_nonce, MAX_NONCE_LENGTH)

        _, kid = _decode_header(credential)
        try:
            certificate = await self.key_cache.get_key(kid, now=current)
        except asyncio.CancelledError:
            raise
        except (InvalidIDToken, GoogleKeysUnavailable):
            raise
        except Exception:
            raise GoogleKeysUnavailable() from None
        claims = await asyncio.to_thread(
            _google_auth_decode,
            credential,
            kid,
            certificate,
            audience,
            current,
            int(self.clock_skew.total_seconds()),
        )

        if claims.get("aud") != audience:
            raise InvalidIDToken()
        issuer = claims.get("iss")
        if not isinstance(issuer, str) or issuer not in GOOGLE_ISSUERS:
            raise InvalidIDToken()

        issued_at = _numeric_date(claims.get("iat"))
        expires_at = _numeric_date(claims.get("exp"))
        current_timestamp = current.timestamp()
        skew_seconds = self.clock_skew.total_seconds()
        if issued_at > current_timestamp + skew_seconds:
            raise InvalidIDToken()
        if current_timestamp >= expires_at + skew_seconds:
            raise InvalidIDToken()
        if expires_at <= issued_at:
            raise InvalidIDToken()
        if expires_at - issued_at > self.max_token_lifetime.total_seconds():
            raise InvalidIDToken()
        not_before = claims.get("nbf")
        if not_before is not None and _numeric_date(not_before) > (
            current_timestamp + skew_seconds
        ):
            raise InvalidIDToken()

        signed_nonce = _validate_required_ascii_string(
            claims.get("nonce"), MAX_NONCE_LENGTH
        )
        if not secrets.compare_digest(signed_nonce, nonce):
            raise InvalidIDToken()

        subject = _validate_required_string(claims.get("sub"), MAX_SUB_LENGTH)
        email = _validate_optional_string(claims.get("email"), MAX_EMAIL_LENGTH)
        name = _validate_optional_string(claims.get("name"), MAX_NAME_LENGTH)

        try:
            consumed = self.nonce_store.consume(nonce, now=current)
        except (TypeError, ValueError):
            raise InvalidIDToken() from None
        if not consumed:
            raise InvalidIDToken()
        return {"sub": subject, "email": email, "name": name}


@dataclass(slots=True)
class _RateBucket:
    attempts: deque[float]


class LoginRateLimiter:
    """Bounded sliding-window login limiter for IP and Google ``sub``.

    The adapter injects ``ip_source`` so only it decides when a tunnel header
    is trusted.  IP checks run before token verification; subject checks run
    after a verified token reveals ``sub``.  Neither key is included in an
    exception or public message.
    """

    def __init__(
        self,
        *,
        ip_source: RequestIPSource | None = None,
        ip_attempts: int = DEFAULT_IP_ATTEMPTS,
        sub_attempts: int = DEFAULT_SUB_ATTEMPTS,
        window: timedelta = DEFAULT_RATE_WINDOW,
        max_buckets: int = DEFAULT_MAX_RATE_BUCKETS,
    ) -> None:
        for value, field_name in (
            (ip_attempts, "ip_attempts"),
            (sub_attempts, "sub_attempts"),
            (max_buckets, "max_buckets"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if window <= timedelta(0):
            raise ValueError("window must be positive")

        self.ip_source = ip_source
        self.ip_attempts = ip_attempts
        self.sub_attempts = sub_attempts
        self.window = window
        self.max_buckets = max_buckets
        self._buckets: dict[tuple[str, str], _RateBucket] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _ip_key(ip: str | None) -> str:
        if ip is None:
            return "<unknown>"
        if not isinstance(ip, str) or not ip or len(ip) > 64:
            raise ValueError("IP source returned an invalid address")
        try:
            return str(ipaddress.ip_address(ip.strip()))
        except ValueError:
            raise ValueError("IP source returned an invalid address") from None

    @staticmethod
    def _sub_key(sub: str) -> str:
        if not isinstance(sub, str) or not sub or len(sub) > MAX_SUB_LENGTH:
            raise ValueError("sub must be a non-empty bounded string")
        return sub

    def _purge_bucket(self, bucket: _RateBucket, cutoff: float) -> None:
        while bucket.attempts and bucket.attempts[0] <= cutoff:
            bucket.attempts.popleft()

    def _sweep_locked(self, cutoff: float) -> None:
        empty: list[tuple[str, str]] = []
        for key, bucket in self._buckets.items():
            self._purge_bucket(bucket, cutoff)
            if not bucket.attempts:
                empty.append(key)
        for key in empty:
            self._buckets.pop(key, None)

    def _check(
        self, scope: str, key: str, limit: int, now: datetime
    ) -> None:
        current = normalize_datetime(now).timestamp()
        window_seconds = self.window.total_seconds()
        cutoff = current - window_seconds
        bucket_key = (scope, key)
        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                if len(self._buckets) >= self.max_buckets:
                    self._sweep_locked(cutoff)
                if len(self._buckets) >= self.max_buckets:
                    raise LoginRateLimitExceeded(
                        scope, math.ceil(window_seconds)
                    )
                bucket = _RateBucket(attempts=deque())
                self._buckets[bucket_key] = bucket
            self._purge_bucket(bucket, cutoff)
            if len(bucket.attempts) >= limit:
                retry_after = math.ceil(
                    min(
                        window_seconds,
                        max(1.0, bucket.attempts[0] + window_seconds - current),
                    )
                )
                raise LoginRateLimitExceeded(scope, retry_after)
            bucket.attempts.append(current)

    def check_ip(self, ip: str | None, *, now: datetime) -> None:
        """Record one pre-verification attempt for a canonical client IP."""

        self._check("ip", self._ip_key(ip), self.ip_attempts, now)

    def check_request(self, request: object, *, now: datetime) -> None:
        """Extract an IP through the adapter's injected trusted source."""

        if self.ip_source is None:
            raise RuntimeError("no request IP source is configured")
        self.check_ip(self.ip_source(request), now=now)

    def check_sub(self, sub: str, *, now: datetime) -> None:
        """Record one post-verification attempt for a stable Google subject."""

        self._check("sub", self._sub_key(sub), self.sub_attempts, now)


_default_nonce_store = LoginNonceStore(
    random_bytes=secrets.token_bytes,
    clock=lambda: datetime.now(timezone.utc),
)
_default_verifier = GoogleIDTokenVerifier(nonce_store=_default_nonce_store)


def issue_login_nonce(
    pre_auth_value: str | None = None, *, now: datetime | None = None
) -> IssuedLoginNonce:
    """Issue through the module-level store used by :func:`verify_id_token`."""

    return _default_nonce_store.issue(pre_auth_value, now=now)


def expected_login_nonce(
    pre_auth_value: str, *, now: datetime | None = None
) -> str | None:
    """Resolve a module-level challenge through its same-site binding."""

    return _default_nonce_store.expected_nonce(pre_auth_value, now=now)


async def verify_id_token(
    credential: str,
    *,
    now: datetime,
    expected_aud: str,
    expected_nonce: str,
) -> VerifiedGoogleIdentity:
    """Verify with the production cache and module-level one-time nonce store."""

    return await _default_verifier.verify_id_token(
        credential,
        now=now,
        expected_aud=expected_aud,
        expected_nonce=expected_nonce,
    )
