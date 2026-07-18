"""Portable semantic storage boundary for authentication sessions.

An :class:`AuthStore` is trusted with a tenant context supplied by the server,
never by a browser.  Identity is global, membership is tenant-scoped, and every
session resolves to both its subject and tenant.  A missing or expired bearer
returns ``None``; operational storage failures must raise so callers can fail
closed instead of silently treating an authenticated request as anonymous.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypedDict, runtime_checkable


class ResolvedSession(TypedDict):
    """The non-secret identity returned by a successful session read."""

    sub: str
    tenant: str


@runtime_checkable
class AuthStore(Protocol):
    """Semantic, tenant-aware auth persistence protocol.

    The only read operation, :meth:`resolve_session`, never returns a raw or
    stored bearer.  Implementations may return the newly generated raw token
    from :meth:`issue_hashed_session` because issuance is a write boundary and
    the transport must set that value as a cookie exactly once.
    """

    def upsert_identity(
        self, sub: str, email: str | None, name: str | None
    ) -> None:
        """Upsert display claims and membership in the trusted tenant."""

        ...

    def issue_hashed_session(
        self, sub: str, tenant: str, now: datetime
    ) -> str:
        """Rotate the subject's tenant session and return its new raw token."""

        ...

    def resolve_session(
        self, raw_token: str, now: datetime
    ) -> ResolvedSession | None:
        """Resolve and idle-touch a valid bearer, or return ``None``."""

        ...

    def revoke(self, raw_token: str) -> int:
        """Revoke one bearer and return the number of deleted sessions."""

        ...

    def revoke_all(self, sub: str) -> int:
        """Revoke every tenant session for an identity."""

        ...

    def sweep(self, now: datetime) -> int:
        """Delete sessions past either expiry boundary and return the count."""

        ...
