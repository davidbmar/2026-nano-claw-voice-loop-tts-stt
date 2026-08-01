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
from urllib.parse import urljoin, urlparse

log = logging.getLogger("nano-claw.delegate")

# Spoken on every failure, whatever the failure was. Deliberately one string: the
# caller learns the assistant is unreachable, never why, and never in the
# delegate's words.
DELEGATE_APOLOGY = "Sorry — I couldn't reach the assistant just then. Please try again."

# The contract's ceiling. Both existing clients default to 120s, which is why this
# has to be passed per request rather than inherited.
DELEGATE_TIMEOUT_S = 30.0

# A reply is delegate-authored text that reaches TTS on every turn. The start
# response is capped at 64 KB; leaving the far more frequent path unbounded was
# an inconsistency in this module, not a decision. A reply past this is a fault
# at the other end, not a long answer: 32 KB is roughly 40 minutes of speech.
_MAX_REPLY_CHARS = 32 * 1024
# A spoken greeting is a sentence or two. The reply cap is far too
# generous for something a caller hears before they can say anything.
_MAX_OPENING_CHARS = 1000

# C0 controls except tab and newline. Null bytes are the specific hazard —
# `PROCESSING_CUE_SENTINEL` is "\0nano-claw-processing-cue\0", and in raw speech
# mode a reply equal to it is compared BY VALUE in `_synthesize_sentence`
# (phone.py:1931), so a delegate could replace its own turn with the gateway's
# internal chime and have it recorded as though words were spoken. Stripping
# controls at the boundary removes that and every neighbouring trick, rather than
# special-casing one sentinel that may be joined by others.
_CONTROL_CHARS = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_CONTROL_CHARS[0x7F] = None

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

    # `urlparse().hostname` strips BOTH userinfo and port, which makes it the
    # right accessor for identifying a host and the wrong one for authorizing a
    # destination. Checking it alone accepted every one of these (verified, not
    # supposed) before the 2026-07-30 Codex review:
    #
    #   http://user:pass@127.0.0.1:2375/containers/json   → the Docker daemon
    #   http://evil.com@127.0.0.1/t                       → reads as evil.com
    #   http://127.0.0.1:99999/t                          → not even a port
    #
    # So userinfo and port are checked explicitly, before the host.
    if parsed.username is not None or parsed.password is not None:
        # Never legitimate here, and doubly unwanted: it is a credential leak to
        # whatever host is dialled, and `user@host` reads to a human skimming a
        # config as though `user` were the destination.
        raise DelegateUrlRefused("delegate URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        # `.port` is the only thing that parses it. Without touching the
        # attribute an unparseable port is simply never noticed.
        raise DelegateUrlRefused(f"delegate URL has an invalid port: {exc}")
    if port is not None and not (0 < port < 65536):
        raise DelegateUrlRefused(f"delegate URL port {port} is out of range")

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


def safe_url_for_log(url: str) -> str:
    """`scheme://host[:port]` — never the path, query, or credentials.

    Failure paths log the URL they could not reach, and a delegate URL can carry
    a capability in its path (riff-builder's is a session id). Logging the whole
    thing writes that capability to disk on every outage.
    """

    try:
        parsed = urlparse(url)
    except ValueError:
        return "(unparseable url)"
    host = parsed.hostname or "?"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}"


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
            # `speak: False` is unconditional because a gateway always owns
            # audio — there is no configuration in which we want the delegate to
            # synthesize a reply we are about to synthesize ourselves. Measured
            # against a live riff-builder before this was sent: 5 WAV files and
            # 1.0 MB written across two turns, none of them ever fetched.
            #
            # A delegate that has never heard of the field ignores it, which is
            # the ordinary behaviour of every JSON body parser and is now stated
            # in the contract.
            json={"text": text, "who": who, "speak": False},
            timeout=timeout,
            follow_redirects=False,
        )
    except Exception as exc:  # connection refused, timeout, DNS, TLS — all the same
        log.warning("delegate %s unreachable: %s: %s", safe_url_for_log(url), type(exc).__name__, exc)
        return DelegateReply(DELEGATE_APOLOGY, ok=False,
                             failure=f"{type(exc).__name__}: {exc}")

    status = getattr(response, "status_code", None)
    if status != 200:
        # The status code is the gateway's ONLY failure signal, per the contract.
        log.warning("delegate %s returned %s", safe_url_for_log(url), status)
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure=f"status {status}")

    try:
        body = response.json()
    except Exception as exc:
        log.warning("delegate %s returned unparseable body: %s",
                    safe_url_for_log(url), exc)
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure="unparseable body")

    if not isinstance(body, dict) or not isinstance(body.get("reply"), str):
        # Includes riff-builder's own 502 shape, {"detail": ...}. Without this the
        # turn falls through _process_api_response and the caller hears nothing.
        log.warning("delegate %s returned a body with no string reply: %r",
                    safe_url_for_log(url),
                    list(body) if isinstance(body, dict) else type(body))
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure="no reply field")

    text = body["reply"]
    if len(text) > _MAX_REPLY_CHARS:
        log.warning("delegate %s returned %d chars, over the %d limit",
                    safe_url_for_log(url), len(text), _MAX_REPLY_CHARS)
        return DelegateReply(DELEGATE_APOLOGY, ok=False, failure="reply too long")

    # `focus` is app-defined and the gateway ignores it. Any `error` key is
    # deliberately NOT read: a 200 is a success by contract, and delegate-authored
    # text must never reach TTS.
    return DelegateReply(text.translate(_CONTROL_CHARS), ok=True)


# ── conversation start (contract v0.1) ───────────────────────────────────────
#
# A DID is not a conversation — several people can dial one number at once —
# but the contract pairs one delegate URL with one conversation. So the app
# hands out a fresh conversation URL per call, and the gateway stays ignorant of
# what that URL means. Design and review:
# `docs/design/2026-07-30-conversation-start-seam.md`.

START_TIMEOUT_S = 10.0
_MAX_START_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class ConversationStart:
    """Where this conversation's turns go, or why we could not find out."""

    delegate_url: str
    ok: bool
    failure: str | None = None
    # What the app says the conversation opened with. CARRIED, not spoken: only
    # a line whose operator set `speak_app_opening` will ever voice it. Parsing
    # it here is not the capability — speaking it is, and that decision lives
    # with the operator in DelegateProfile.
    app_opening: str = ""


def resolve_returned_url(start_url: str, returned: str) -> str:
    """Resolve a returned delegate URL against the start URL, or raise.

    The rule is **same origin as the start URL** — identical scheme, host and
    port — with relative URLs resolved against it.

    An earlier draft said "validate the returned URL against the same host
    allowlist". Codex review showed that is not enough: the allowlist admits any
    port on an allowed host and unconditionally admits all of loopback, so an
    allowlisted app could return `http://127.0.0.1:3001/...` (the Node agent API,
    which has no auth) or `http://127.0.0.1:8000/...` (the platform, which reads
    tenant_id and permissions from the request body) and the gateway would POST
    everything the caller says there, on every turn.

    Same origin closes the class instead of narrowing it: a *response* cannot
    introduce a destination that *config* did not already authorize. It costs
    nothing real — an app handing out its own conversation URLs serves them from
    itself.
    """

    if not isinstance(returned, str) or not returned.strip():
        raise DelegateUrlRefused("start response had no delegate_url")

    absolute = urljoin(start_url, returned.strip())
    start, target = urlparse(start_url), urlparse(absolute)

    # Compare the parsed origin rather than a string prefix: a prefix test would
    # accept `http://127.0.0.1:8790.evil.com/`.
    try:
        same_origin = (
            start.scheme == target.scheme
            and (start.hostname or "").lower() == (target.hostname or "").lower()
            and start.port == target.port
        )
    except ValueError as exc:
        raise DelegateUrlRefused(f"start response URL has an invalid port: {exc}")
    if not same_origin:
        raise DelegateUrlRefused(
            f"start response pointed at {safe_url_for_log(absolute)}, which is "
            f"not the start origin {safe_url_for_log(start_url)}")

    # Still refuse credentials: same-origin says nothing about userinfo, and the
    # returned string is attacker-influenced in a way the config value is not.
    if target.username is not None or target.password is not None:
        raise DelegateUrlRefused("start response URL must not contain credentials")
    return absolute


async def start_conversation(
    client,
    start_url: str,
    *,
    conversation_key: str,
    who: str = "caller",
    channel: str = "phone",
    from_: str | None = None,
    to: str | None = None,
    timeout: float = START_TIMEOUT_S,
) -> ConversationStart:
    """Ask the app for a conversation URL. Never raises.

    `conversation_key` is a stable, call-derived idempotency key. The delegate
    must return the SAME conversation for the same key, because retries are not
    hypothetical — Telnyx redelivers webhooks and media streams reconnect, and
    neither the gateway nor any placement of this call gives exactly-once
    delivery across workers or restarts. Correctness comes from the delegate
    being idempotent, not from the gateway managing never to retry.
    """

    payload: dict = {"who": who, "channel": channel,
                     "conversation_key": conversation_key}
    # Absent rather than null when withheld: the contract says the app must
    # tolerate a missing caller id, and an explicit null invites a delegate to
    # treat "withheld" as a distinct routing case it then gets wrong.
    if from_:
        payload["from"] = from_
    if to:
        payload["to"] = to

    try:
        response = await client.post(
            start_url, json=payload, timeout=timeout, follow_redirects=False)
    except Exception as exc:
        log.warning("conversation start %s unreachable: %s: %s",
                    safe_url_for_log(start_url), type(exc).__name__, exc)
        return ConversationStart("", ok=False,
                                 failure=f"{type(exc).__name__}: {exc}")

    status = getattr(response, "status_code", None)
    if status != 200:
        # The body goes into `failure`, which is for logs and operators and is
        # NEVER spoken — the app is the only thing that knows WHY it refused, and
        # without this a ceiling, a misconfigured line and a crash all present as
        # the same bare status. Truncated because it is untrusted text.
        detail = ""
        raw_body = getattr(response, "text", None)
        if isinstance(raw_body, str) and raw_body.strip():
            detail = f": {raw_body.strip()[:200]}"
        log.warning("conversation start %s returned %s%s",
                    safe_url_for_log(start_url), status, detail)
        return ConversationStart("", ok=False, failure=f"status {status}{detail}")

    raw = getattr(response, "content", None)
    if isinstance(raw, (bytes, bytearray)) and len(raw) > _MAX_START_BODY_BYTES:
        log.warning("conversation start %s returned %d bytes, over the limit",
                    safe_url_for_log(start_url), len(raw))
        return ConversationStart("", ok=False, failure="body too large")

    try:
        body = response.json()
    except Exception as exc:
        log.warning("conversation start %s returned unparseable body: %s",
                    safe_url_for_log(start_url), exc)
        return ConversationStart("", ok=False, failure="unparseable body")

    if not isinstance(body, dict):
        return ConversationStart("", ok=False, failure="body is not an object")

    try:
        url = resolve_returned_url(start_url, body.get("delegate_url", ""))
    except DelegateUrlRefused as exc:
        log.warning("conversation start %s: %s", safe_url_for_log(start_url), exc)
        return ConversationStart("", ok=False, failure=str(exc))

    opening = body.get("greeting")
    if not isinstance(opening, str):
        opening = ""
    if len(opening) > _MAX_OPENING_CHARS:
        # A greeting is one or two sentences. Anything past this is a fault at
        # the app's end, not a long hello, and it would be read aloud to a
        # caller who cannot skip it.
        log.warning("conversation start %s returned a %d-char opening; dropping it",
                    safe_url_for_log(start_url), len(opening))
        opening = ""
    return ConversationStart(url, ok=True, app_opening=opening.strip())
