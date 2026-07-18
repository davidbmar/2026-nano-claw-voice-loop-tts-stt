"""Framework-free session token and expiry policy.

This module deliberately contains no transport, persistence, or application
imports.  Randomness and time are injectable so the same policy can be used by
different applications and tested without patching global state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

SESSION_TOKEN_BYTES = 32
"""Exactly 256 bits of entropy are used for each bearer token."""

DEFAULT_ABSOLUTE_TTL = timedelta(days=7)
DEFAULT_IDLE_TTL = timedelta(hours=24)

LOGIN_NONCE_BYTES = 32
PRE_AUTH_TOKEN_BYTES = 32
DEFAULT_LOGIN_NONCE_TTL = timedelta(minutes=10)
DEFAULT_MAX_PENDING_NONCES = 4_096
MAX_NONCE_GENERATION_ATTEMPTS = 16
MAX_OPAQUE_VALUE_LENGTH = 512

RandomBytes = Callable[[int], bytes]
Clock = Callable[[], datetime]


def normalize_datetime(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating a naive value as UTC.

    Treating naive values as UTC keeps storage semantics independent of the
    host timezone while remaining friendly to simple injected test clocks.
    """

    if not isinstance(value, datetime):
        raise TypeError("session times must be datetime instances")
    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def generate_session_token(random_bytes: RandomBytes) -> str:
    """Generate an unpadded URL-safe token from exactly 256 random bits."""

    if not callable(random_bytes):
        raise TypeError("random_bytes must be callable")
    entropy = random_bytes(SESSION_TOKEN_BYTES)
    if not isinstance(entropy, (bytes, bytearray, memoryview)):
        raise TypeError("random_bytes must return bytes")
    entropy_bytes = bytes(entropy)
    if len(entropy_bytes) != SESSION_TOKEN_BYTES:
        raise ValueError(
            f"random_bytes must return exactly {SESSION_TOKEN_BYTES} bytes"
        )
    return base64.urlsafe_b64encode(entropy_bytes).rstrip(b"=").decode("ascii")


def _generate_opaque_token(
    random_bytes: RandomBytes, size: int, *, purpose: str
) -> str:
    if not callable(random_bytes):
        raise TypeError("random_bytes must be callable")
    entropy = random_bytes(size)
    if not isinstance(entropy, (bytes, bytearray, memoryview)):
        raise TypeError("random_bytes must return bytes")
    entropy_bytes = bytes(entropy)
    if len(entropy_bytes) != size:
        raise ValueError(
            f"random_bytes must return exactly {size} bytes for {purpose}"
        )
    return base64.urlsafe_b64encode(entropy_bytes).rstrip(b"=").decode("ascii")


def hash_session_token(raw_token: str) -> str:
    """Return the lowercase SHA-256 hex digest stored for a bearer token."""

    if not isinstance(raw_token, str):
        raise TypeError("raw_token must be a string")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def hash_token(raw_token: str) -> str:
    """Short alias for :func:`hash_session_token`."""

    return hash_session_token(raw_token)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """The transient output of session issuance.

    ``raw_token`` is returned to the transport exactly once.  Persistence
    implementations store ``token_hash`` and the timestamps, never the raw
    bearer.
    """

    raw_token: str
    token_hash: str
    created_at: datetime
    expires_at: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Generate tokens and apply absolute plus sliding-idle expiry.

    A session is expired at either boundary (``now >= deadline``).  The
    absolute deadline never moves; a successful resolution advances
    ``last_seen`` without allowing it to move backward.
    """

    random_bytes: RandomBytes = field(repr=False, compare=False)
    clock: Clock = field(repr=False, compare=False)
    absolute_ttl: timedelta = DEFAULT_ABSOLUTE_TTL
    idle_ttl: timedelta = DEFAULT_IDLE_TTL

    def __post_init__(self) -> None:
        if self.absolute_ttl <= timedelta(0):
            raise ValueError("absolute_ttl must be positive")
        if self.idle_ttl <= timedelta(0):
            raise ValueError("idle_ttl must be positive")
        if not callable(self.random_bytes):
            raise TypeError("random_bytes must be callable")
        if not callable(self.clock):
            raise TypeError("clock must be callable")

    def now(self) -> datetime:
        """Read the injected clock once and normalize it to UTC."""

        return normalize_datetime(self.clock())

    def generate_token(self) -> str:
        """Generate a bearer using the injected random-byte source."""

        return generate_session_token(self.random_bytes)

    def hash_token(self, raw_token: str) -> str:
        """Hash a bearer with the policy's fixed SHA-256 representation."""

        return hash_session_token(raw_token)

    def issue(self, now: datetime | None = None) -> IssuedSession:
        """Create the transient values needed to persist a new session."""

        created_at = self.now() if now is None else normalize_datetime(now)
        raw_token = self.generate_token()
        return IssuedSession(
            raw_token=raw_token,
            token_hash=hash_session_token(raw_token),
            created_at=created_at,
            expires_at=created_at + self.absolute_ttl,
            last_seen=created_at,
        )

    def idle_expires_at(self, last_seen: datetime) -> datetime:
        """Return the sliding idle deadline for ``last_seen``."""

        return normalize_datetime(last_seen) + self.idle_ttl

    def effective_expires_at(
        self, expires_at: datetime, last_seen: datetime
    ) -> datetime:
        """Return the earlier of the absolute and idle deadlines."""

        return min(
            normalize_datetime(expires_at), self.idle_expires_at(last_seen)
        )

    def is_expired(
        self,
        *,
        expires_at: datetime,
        last_seen: datetime,
        now: datetime | None = None,
    ) -> bool:
        """Return whether either expiry boundary has been reached."""

        current = self.now() if now is None else normalize_datetime(now)
        return current >= self.effective_expires_at(expires_at, last_seen)

    def touch(
        self, last_seen: datetime, now: datetime | None = None
    ) -> datetime:
        """Advance ``last_seen`` without moving it backward on clock skew."""

        previous = normalize_datetime(last_seen)
        current = self.now() if now is None else normalize_datetime(now)
        return max(previous, current)


class LoginNonceCapacityError(RuntimeError):
    """Raised when the bounded pending-login nonce store is full."""


@dataclass(frozen=True, slots=True)
class IssuedLoginNonce:
    """One login challenge and the same-site value to which it is bound.

    ``pre_auth_value`` is suitable for a host-only, SameSite cookie.  It is
    never accepted as identity.  The browser sends ``nonce`` to GIS and Google
    copies it into the signed ID token; only the server looks the nonce up via
    the pre-auth value.
    """

    pre_auth_value: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _PendingLoginNonce:
    pre_auth_digest: str
    nonce_digest: str
    nonce: str
    expires_at: datetime


def _validate_opaque_value(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or len(value) > MAX_OPAQUE_VALUE_LENGTH:
        raise ValueError(
            f"{field_name} must contain 1 to {MAX_OPAQUE_VALUE_LENGTH} characters"
        )
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be ASCII") from exc
    return value


def _opaque_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class LoginNonceStore:
    """Bounded, thread-safe store for one-time Google login challenges.

    Issuance binds exactly one fresh nonce to a server-generated same-site
    pre-auth value.  A transport first calls :meth:`expected_nonce` with the
    cookie value, then gives that result to the ID-token verifier.  The
    verifier calls :meth:`consume` only after every signed claim succeeds.
    Concurrent replay attempts therefore race on one atomic consume and only
    one can succeed.

    This store intentionally has no persistence or HTTP dependency.  Pending
    challenges are short-lived and disappear on process restart; that safely
    fails an in-progress login closed.
    """

    def __init__(
        self,
        *,
        random_bytes: RandomBytes,
        clock: Clock,
        ttl: timedelta = DEFAULT_LOGIN_NONCE_TTL,
        max_pending: int = DEFAULT_MAX_PENDING_NONCES,
    ) -> None:
        if not callable(random_bytes):
            raise TypeError("random_bytes must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if ttl <= timedelta(0):
            raise ValueError("nonce ttl must be positive")
        if isinstance(max_pending, bool) or not isinstance(max_pending, int):
            raise TypeError("max_pending must be an integer")
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")

        self.random_bytes = random_bytes
        self.clock = clock
        self.ttl = ttl
        self.max_pending = max_pending
        self._by_pre_auth: dict[str, _PendingLoginNonce] = {}
        self._by_nonce: dict[str, _PendingLoginNonce] = {}
        self._lock = threading.RLock()

    def now(self) -> datetime:
        return normalize_datetime(self.clock())

    def _remove_locked(self, record: _PendingLoginNonce) -> None:
        if self._by_pre_auth.get(record.pre_auth_digest) is record:
            self._by_pre_auth.pop(record.pre_auth_digest, None)
        if self._by_nonce.get(record.nonce_digest) is record:
            self._by_nonce.pop(record.nonce_digest, None)

    def _sweep_locked(self, now: datetime) -> int:
        expired = [
            record
            for record in self._by_pre_auth.values()
            if now >= record.expires_at
        ]
        for record in expired:
            self._remove_locked(record)
        return len(expired)

    def issue(
        self,
        pre_auth_value: str | None = None,
        *,
        now: datetime | None = None,
    ) -> IssuedLoginNonce:
        """Issue a nonce, replacing any challenge for the same binding.

        When no pre-auth value exists yet, both values are generated from
        independent 256-bit draws.  A caller may pass an existing cookie value
        to rotate only its nonce.
        """

        current = self.now() if now is None else normalize_datetime(now)
        with self._lock:
            self._sweep_locked(current)
            if pre_auth_value is None:
                for _ in range(MAX_NONCE_GENERATION_ATTEMPTS):
                    candidate = _generate_opaque_token(
                        self.random_bytes,
                        PRE_AUTH_TOKEN_BYTES,
                        purpose="pre-auth token",
                    )
                    if _opaque_digest(candidate) not in self._by_pre_auth:
                        binding = candidate
                        break
                else:
                    raise RuntimeError("could not generate a unique pre-auth token")
            else:
                binding = _validate_opaque_value(
                    pre_auth_value, "pre_auth_value"
                )

            binding_digest = _opaque_digest(binding)
            previous = self._by_pre_auth.get(binding_digest)
            if previous is None and len(self._by_pre_auth) >= self.max_pending:
                raise LoginNonceCapacityError(
                    "too many pending login challenges"
                )

            for _ in range(MAX_NONCE_GENERATION_ATTEMPTS):
                nonce = _generate_opaque_token(
                    self.random_bytes, LOGIN_NONCE_BYTES, purpose="login nonce"
                )
                nonce_digest = _opaque_digest(nonce)
                if nonce_digest not in self._by_nonce:
                    break
            else:
                raise RuntimeError("could not generate a unique login nonce")

            if previous is not None:
                self._remove_locked(previous)
            record = _PendingLoginNonce(
                pre_auth_digest=binding_digest,
                nonce_digest=nonce_digest,
                nonce=nonce,
                expires_at=current + self.ttl,
            )
            self._by_pre_auth[binding_digest] = record
            self._by_nonce[nonce_digest] = record
            return IssuedLoginNonce(
                pre_auth_value=binding,
                nonce=nonce,
                expires_at=record.expires_at,
            )

    def expected_nonce(
        self, pre_auth_value: str, *, now: datetime | None = None
    ) -> str | None:
        """Resolve a live challenge through its same-site binding."""

        binding = _validate_opaque_value(pre_auth_value, "pre_auth_value")
        current = self.now() if now is None else normalize_datetime(now)
        with self._lock:
            record = self._by_pre_auth.get(_opaque_digest(binding))
            if record is None:
                return None
            if current >= record.expires_at:
                self._remove_locked(record)
                return None
            return record.nonce

    def consume(
        self,
        nonce: str,
        *,
        now: datetime | None = None,
        pre_auth_value: str | None = None,
    ) -> bool:
        """Atomically consume a live challenge, optionally rechecking binding."""

        expected = _validate_opaque_value(nonce, "nonce")
        binding_digest = None
        if pre_auth_value is not None:
            binding = _validate_opaque_value(
                pre_auth_value, "pre_auth_value"
            )
            binding_digest = _opaque_digest(binding)
        current = self.now() if now is None else normalize_datetime(now)
        with self._lock:
            record = self._by_nonce.get(_opaque_digest(expected))
            if record is None:
                return False
            if current >= record.expires_at:
                self._remove_locked(record)
                return False
            if binding_digest is not None and not hmac.compare_digest(
                record.pre_auth_digest, binding_digest
            ):
                return False
            if not hmac.compare_digest(record.nonce, expected):
                return False
            self._remove_locked(record)
            return True

    def sweep(self, now: datetime | None = None) -> int:
        """Remove expired pending challenges and return the count."""

        current = self.now() if now is None else normalize_datetime(now)
        with self._lock:
            return self._sweep_locked(current)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_pre_auth)
