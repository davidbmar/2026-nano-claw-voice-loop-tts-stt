"""The turn-delegate hop: POST what the human said to an app, speak its reply.

Implements the app-facing half of riff-builder's `docs/turn-delegate-contract.md`.
When a session carries a delegate URL, this replaces the call to nano-claw's own
model. Everything acoustic — mic, VAD, endpointing, STT, TTS, barge-in — stays in
the gateway; the app never learns audio exists.

The contract calls the delegate UNTRUSTED, and that word does the work here. An
earlier draft of the design treated this as a one-line shape translation feeding
`_process_api_response`. Codex review (2026-07-30) showed why that is unsafe:

- `_process_api_response` has no `else` branch, so a body that is not
  `type=="final"`, not `tool_pending`, and has a falsy `error` falls off the end —
  no speech, no apology, no log. riff-builder's own failure is exactly that shape
  (`HTTPException(502)` serialises to `{"detail": ...}`), so "the app is down"
  would become a SILENT caller-facing turn.
- That handler speaks `data.get("error")` verbatim, so a delegate returning
  `200 {"error": "..."}` is TTS injection from the untrusted party.
- Both HTTP clients are built with `timeout=120.0`, so the contract's 30 s ceiling
  is enforced nowhere and a hung delegate holds a live phone caller for two
  minutes.

So this module never hands a raw delegate body downstream. It validates the status
and the shape itself, maps every failure to its own fixed apology, and emits an
already-safe payload.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger("nano-claw.delegate")

# Spoken on every failure, whatever the failure was. Deliberately one string: the
# caller learns the assistant is unreachable, never why, and never in the
# delegate's words.
DELEGATE_APOLOGY = "Sorry — I couldn't reach the assistant just then. Please try again."

# The contract's ceiling. Both existing clients default to 120s, which is why this
# has to be passed per request rather than inherited.
DELEGATE_TIMEOUT_S = 30.0

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALWAYS_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class DelegateUrlRefused(ValueError):
    """A delegate URL that must not be dialled."""


@dataclass(frozen=True)
class DelegateReply:
    """What came back, already safe to speak.

    `text` is what to say — never delegate-authored on a failure. Empty string
    means "say nothing", which the contract permits: an app may legitimately have
    nothing to add.

    `failure` is for logs and metrics only and is NEVER spoken.
    """

    text: str
    ok: bool
    failure: str | None = None

    def as_agent_response(self) -> dict:
        """The shape `_process_api_response` already understands."""
        return {"type": "final", "response": self.text}


def validate_delegate_url(url: str, *, allowed_hosts: frozenset[str] | set[str] = frozenset()) -> str:
    """Return `url` if it may be dialled, else raise.

    Checked at SET time, not at call time, so a bad URL is refused by the operator
    API rather than discovered mid-call.

    This is not paranoia about a config field: the endpoint forwards *everything
    the human says* to this URL, on every turn, for the life of the session. The
    operator same-origin guard does not help — it is self-documented as CSRF
    mitigation only ("It stops nothing else",
    `voice/webauth/aiohttp_adapter.py:80-84`) — and the per-DID map is persisted,
    so one bad value survives restarts.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise DelegateUrlRefused(
            f"delegate URL scheme {parsed.scheme!r} is not http or https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise DelegateUrlRefused("delegate URL has no host")
    if host in _ALWAYS_ALLOWED_HOSTS:
        return url
    if host not in {h.lower() for h in allowed_hosts}:
        raise DelegateUrlRefused(
            f"delegate host {host!r} is not loopback and not in "
            "NANO_CLAW_DELEGATE_HOSTS")
    return url


async def call_delegate(
    client,
    url: str,
    text: str,
    *,
    who: str,
    timeout: float = DELEGATE_TIMEOUT_S,
) -> DelegateReply:
    """One turn through the delegate. Never raises; never speaks its words.

    `client` is injected so this is testable without a network or a WebSocket.
    """
    try:
        response = await client.post(
            url,
            json={"text": text, "who": who},
            timeout=timeout,
            follow_redirects=False,
        )
    except Exception as exc:  # connection refused, timeout, DNS, TLS — all the same
        log.warning("delegate %s unreachable: %s: %s", url, type(exc).__name__, exc)
        return DelegateReply(DELEGATE_APOLOGY, ok=False,
                             failure=f"{type(exc).__name__}: {exc}")

    status = getattr(response, "status_code", None)
    if status != 200:
        # The status code is the gateway's ONLY failure signal, per the contract.
        log.warning("delegate %s returned %s", url, status)
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure=f"status {status}")

    try:
        body = response.json()
    except Exception as exc:
        log.warning("delegate %s returned unparseable body: %s", url, exc)
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure="unparseable body")

    if not isinstance(body, dict) or not isinstance(body.get("reply"), str):
        # Includes riff-builder's own 502 shape, {"detail": ...}. Without this the
        # turn falls through _process_api_response and the caller hears nothing.
        log.warning("delegate %s returned a body with no string reply: %r",
                    url, list(body) if isinstance(body, dict) else type(body))
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure="no reply field")

    # `focus` is app-defined and the gateway ignores it. Any `error` key is
    # deliberately NOT read: a 200 is a success by contract, and delegate-authored
    # text must never reach TTS.
    return DelegateReply(body["reply"], ok=True)
