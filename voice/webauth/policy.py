"""Framework-free session token and expiry policy.

This module deliberately contains no transport, persistence, or application
imports.  Randomness and time are injectable so the same policy can be used by
different applications and tested without patching global state.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

SESSION_TOKEN_BYTES = 32
"""Exactly 256 bits of entropy are used for each bearer token."""

DEFAULT_ABSOLUTE_TTL = timedelta(days=7)
DEFAULT_IDLE_TTL = timedelta(hours=24)

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
