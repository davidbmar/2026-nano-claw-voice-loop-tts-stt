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

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger("nano-claw.delegate")

# Spoken on every failure, whatever the failure was. Deliberately one string: the
# caller learns the assistant is unreachable, never why, and never in the
# delegate's words.
DELEGATE_APOLOGY = "Sorry — I couldn't reach the assistant just then. Please try again."

# The contract's ceiling. Both existing clients default to 120s, which is why this
# has to be passed per request rather than inherited.
DELEGATE_TIMEOUT_S = 30.0

# A stream has more than one useful deadline. The connect ceiling matches the
# already-established /start ceiling; the first complete event retains v0's
# 30-second turn ceiling. Reads after that are bounded by event-to-event idle
# time and by an independent wall clock, because HTTPX's timeout only measures
# individual periods of I/O inactivity.
DELEGATE_CONNECT_TIMEOUT_S = 10.0
DELEGATE_IDLE_TIMEOUT_S = 10.0
DELEGATE_WALL_TIMEOUT_S = 60.0

# A reply is delegate-authored text that reaches TTS on every turn. The start
# response is capped at 64 KB; leaving the far more frequent path unbounded was
# an inconsistency in this module, not a decision. A reply past this is a fault
# at the other end, not a long answer: 32 KB is roughly 40 minutes of speech.
_MAX_REPLY_CHARS = 32 * 1024
_MAX_SSE_LINE_BYTES = _MAX_REPLY_CHARS
_MAX_SSE_EVENT_BYTES = _MAX_REPLY_CHARS
_MAX_SSE_STREAM_BYTES = _MAX_REPLY_CHARS
_SSE_READ_CHUNK_BYTES = 4096
_STREAM_QUEUE_MAX = 2
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

    `terminal` means the app said this reply is the conversation's LAST
    (riff-builder sends `done: true` once its flow rests in a terminal state):
    speak it, then end the call from our side. Without this, "Goodbye" is just
    text — the gateway speaks it and nobody hangs up, so the caller sits on a
    live leg while every later utterance round-trips into the app's
    "session is complete" short-circuit (real calls, 2026-08-03). It is the
    one non-text signal we honor from the untrusted party, and the worst an
    abusive delegate can do with it is end its own call politely.
    """

    text: str
    ok: bool
    failure: str | None = None
    terminal: bool = False

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


def _turn_payload(text: str, who: str, turn_id: str | None) -> dict[str, object]:
    """Build a turn body without inventing identity at the transport seam.

    ``turn_id`` follows the v0 extension rule: delegates that have never heard
    of an optional field ignore it, so older backends keep working unchanged.
    """

    payload: dict[str, object] = {"text": text, "who": who, "speak": False}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return payload


async def call_delegate(
    client,
    url: str,
    text: str,
    *,
    who: str,
    turn_id: str | None = None,
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
            json=_turn_payload(text, who, turn_id),
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
    # text must never reach TTS. `done` is the strict-boolean exception — see
    # DelegateReply.terminal; anything but literal `true` reads as False.
    return DelegateReply(text.translate(_CONTROL_CHARS), ok=True,
                         terminal=body.get("done") is True)


# ── streaming turns (contract v1) ───────────────────────────────────────────

@dataclass
class DelegateStreamResult:
    """Terminal state populated as a :class:`DelegateStream` is consumed.

    The object is intentionally mutable and handed out before iteration.  A
    caller keeps the same object and reads it after the iterator ends; terminal
    metadata therefore never has to be smuggled through a speakable chunk.
    """

    canonical_reply: str = ""
    focus: list[str] = field(default_factory=list)
    done: bool = False
    fault: str | None = None
    failure_detail: str | None = None
    ok: bool = False
    finished: bool = False
    remote_truncated: bool = False
    locally_cancelled: bool = False
    apology_yielded: bool = False
    delta_mismatch: bool = False
    ack_kind: str | None = None
    chunks_yielded: int = 0
    delegate_chunks_yielded: int = 0

    @property
    def reply(self) -> str:
        """The v0 field name, retained as an alias for callers."""
        return self.canonical_reply

    @property
    def terminal(self) -> bool:
        """Compatibility with :class:`DelegateReply`."""
        return self.done

    @property
    def failure(self) -> str | None:
        """Compatibility alias for code that records a failure string."""
        return self.fault

    @property
    def fault_classification(self) -> str | None:
        return self.fault

    @property
    def local_cancelled(self) -> bool:
        return self.locally_cancelled


@dataclass(frozen=True)
class _QueuedStreamChunk:
    text: str
    delegate_authored: bool


@dataclass(frozen=True)
class _ValidatedFinal:
    reply: str
    focus: list[str]
    done: bool


@dataclass(frozen=True)
class _Deadline:
    at: float
    fault: str


class _StreamFault(Exception):
    def __init__(self, classification: str, detail: str | None = None) -> None:
        super().__init__(classification)
        self.classification = classification
        self.detail = detail


_STREAM_END = object()
_ACK_KINDS = frozenset({"working", "tool", "lookup"})


def _parsed_media_type(value: str) -> str:
    """Return only the lower-cased HTTP media type, never a substring match."""
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", {})
    try:
        value = headers.get(name, "")
    except AttributeError:
        return ""
    if value:
        return str(value)
    try:
        value = headers.get(name.lower(), "")
    except AttributeError:
        return ""
    if value:
        return str(value)
    try:
        for key, candidate in headers.items():
            if str(key).lower() == name.lower():
                return str(candidate)
    except AttributeError:
        pass
    return ""


def _validate_final_object(body: Any, *, strict_done: bool) -> _ValidatedFinal:
    if not isinstance(body, dict) or not isinstance(body.get("reply"), str):
        raise _StreamFault("wrong-field-type", "final.reply must be a string")

    reply = body["reply"]
    if len(reply) > _MAX_REPLY_CHARS:
        raise _StreamFault("reply-too-long")

    focus = body.get("focus", [])
    if focus is None and not strict_done:
        # v0 treated focus as app-defined and dropped it. Preserve that exact
        # fallback posture while exposing well-shaped values to v1 callers.
        focus = []
    if not isinstance(focus, list) or not all(isinstance(item, str) for item in focus):
        if strict_done:
            raise _StreamFault("wrong-field-type", "final.focus must be a string list")
        focus = []

    raw_done = body.get("done", False)
    if strict_done and not isinstance(raw_done, bool):
        raise _StreamFault("wrong-field-type", "final.done must be a boolean")
    done = raw_done is True
    return _ValidatedFinal(reply.translate(_CONTROL_CHARS), list(focus), done)


class DelegateStream(AsyncIterator[str]):
    """Single-use, bounded streaming turn consumer.

    ``stream.result`` is stable before iteration and populated afterward.  For
    callers that prefer the design's "iterator plus result" wording literally,
    ``chunks, result = stream_delegate(...)`` is also supported.
    """

    def __init__(
        self,
        client: Any,
        url: str,
        text: str,
        *,
        who: str,
        turn_id: str | None,
        connect_timeout: float,
        first_event_timeout: float,
        idle_timeout: float,
        wall_timeout: float,
        clock: Callable[[], float],
    ) -> None:
        self._client = client
        self._url = url
        self._text = text
        self._who = who
        self._turn_id = turn_id
        self._clock = clock
        self._started_at = clock()
        self._connect_deadline = self._started_at + connect_timeout
        self._first_event_deadline = self._started_at + first_event_timeout
        self._absolute_deadline = self._started_at + wall_timeout
        self._idle_timeout = idle_timeout
        self._last_event_at: float | None = None
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX)
        self._producer: asyncio.Task[None] | None = None
        self._closed = False
        self._iteration_ended = False
        self._had_delegate_chunk = False
        self._delta_parts: list[str] = []
        self._delta_chars = 0
        self.result = DelegateStreamResult()

    def __aiter__(self) -> AsyncIterator[str]:
        async def consume() -> AsyncIterator[str]:
            exhausted = False
            try:
                while True:
                    try:
                        yield await self.__anext__()
                    except StopAsyncIteration:
                        exhausted = True
                        return
            finally:
                if not exhausted:
                    await self.aclose()

        return consume()

    def __iter__(self) -> Iterator[DelegateStream | DelegateStreamResult]:
        # This does not participate in ``async for`` (which calls __aiter__).
        # It only makes ``chunks, result = stream_delegate(...)`` ergonomic.
        yield self
        yield self.result

    async def __aenter__(self) -> DelegateStream:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def __anext__(self) -> str:
        if self._closed or self._iteration_ended:
            raise StopAsyncIteration
        self._ensure_started()
        try:
            item = await self._queue.get()
        except asyncio.CancelledError:
            await self.aclose()
            raise

        if item is _STREAM_END:
            self._iteration_ended = True
            self.result.finished = True
            raise StopAsyncIteration

        assert isinstance(item, _QueuedStreamChunk)
        self.result.chunks_yielded += 1
        if item.delegate_authored:
            self.result.delegate_chunks_yielded += 1
        return item.text

    async def aclose(self) -> None:
        """Stop local consumption without converting it into a remote fault."""
        if self._closed:
            return
        self._closed = True
        if not self._iteration_ended and self.result.fault is None:
            self.result.locally_cancelled = True
        producer = self._producer
        if producer is not None and not producer.done():
            producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass
        self._discard_queued()
        self.result.finished = True

    def _ensure_started(self) -> None:
        if self._producer is None:
            self._producer = asyncio.create_task(
                self._produce(), name="delegate-stream-consumer"
            )

    def _discard_queued(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _read_deadlines(self) -> tuple[_Deadline, ...]:
        if self._last_event_at is None:
            event_deadline = _Deadline(
                self._first_event_deadline, "first-event-timeout"
            )
        else:
            event_deadline = _Deadline(
                self._last_event_at + self._idle_timeout, "idle-timeout"
            )
        return event_deadline, _Deadline(self._absolute_deadline, "absolute-timeout")

    async def _await_before(
        self,
        make_awaitable: Callable[[], Any],
        deadlines: tuple[_Deadline, ...],
    ) -> Any:
        deadline = min(deadlines, key=lambda candidate: candidate.at)
        remaining = deadline.at - self._clock()
        if remaining <= 0:
            raise _StreamFault(deadline.fault)
        try:
            value = await asyncio.wait_for(make_awaitable(), timeout=remaining)
        except StopAsyncIteration:
            now = self._clock()
            expired = [candidate for candidate in deadlines if now > candidate.at]
            if expired:
                raise _StreamFault(
                    min(expired, key=lambda candidate: candidate.at).fault
                )
            raise
        except TimeoutError as exc:
            raise _StreamFault(deadline.fault) from exc

        # A test clock can advance while an awaitable completes immediately;
        # checking again also closes scheduler-boundary races in production.
        now = self._clock()
        expired = [candidate for candidate in deadlines if now > candidate.at]
        if expired:
            raise _StreamFault(min(expired, key=lambda candidate: candidate.at).fault)
        return value

    async def _put(self, item: object) -> None:
        if self._clock() > self._absolute_deadline:
            raise _StreamFault("absolute-timeout")
        if not self._queue.full():
            # Keep validation of events already present in the same transport
            # chunk transactional. A later malformed event can discard these
            # prefetched items before the waiting consumer is scheduled.
            self._queue.put_nowait(item)
            return
        await self._await_before(
            lambda: self._queue.put(item),
            (_Deadline(self._absolute_deadline, "absolute-timeout"),),
        )

    async def _put_delegate_chunk(self, text: str) -> None:
        safe = text.translate(_CONTROL_CHARS)
        if not safe:
            return
        await self._put(_QueuedStreamChunk(safe, delegate_authored=True))
        self._had_delegate_chunk = True

    def _remote_fault(self, classification: str, detail: str | None = None) -> None:
        if self.result.fault is not None or self.result.locally_cancelled:
            return
        self.result.fault = classification
        self.result.failure_detail = detail
        self.result.ok = False
        # Anything validated but still prefetched has not reached the caller and
        # must not leak out after the stream is known to be faulty.
        self._discard_queued()
        if self.result.delegate_chunks_yielded == 0:
            self.result.apology_yielded = True
            self._queue.put_nowait(
                _QueuedStreamChunk(DELEGATE_APOLOGY, delegate_authored=False)
            )
        else:
            self.result.remote_truncated = True
        self._queue.put_nowait(_STREAM_END)
        log.warning(
            "delegate stream %s fault=%s%s",
            safe_url_for_log(self._url),
            classification,
            f" ({detail})" if detail else "",
        )

    async def _produce(self) -> None:
        manager = None
        entered = False
        try:
            manager = self._client.stream(
                "POST",
                self._url,
                json=_turn_payload(self._text, self._who, self._turn_id),
                headers={
                    "Accept": "text/event-stream, application/json;q=0.9"
                },
                timeout=httpx.Timeout(None),
                follow_redirects=False,
            )
            response = await self._await_before(
                manager.__aenter__,
                (
                    _Deadline(self._connect_deadline, "connect-timeout"),
                    _Deadline(self._absolute_deadline, "absolute-timeout"),
                ),
            )
            entered = True

            status = getattr(response, "status_code", None)
            if status != 200:
                raise _StreamFault("http-status", str(status))

            media_type = _parsed_media_type(_header(response, "content-type"))
            if media_type == "application/json":
                await self._consume_atomic_json(response)
            elif media_type == "text/event-stream":
                await self._consume_sse(response)
            else:
                raise _StreamFault(
                    "unexpected-content-type", media_type or "missing"
                )
        except asyncio.CancelledError:
            raise
        except _StreamFault as exc:
            self._remote_fault(exc.classification, exc.detail)
        except Exception as exc:
            self._remote_fault("transport-error", type(exc).__name__)
        finally:
            if entered and manager is not None:
                try:
                    await manager.__aexit__(None, None, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self.result.ok and self.result.fault is None:
                        self._remote_fault("transport-error", type(exc).__name__)

    def _response_byte_iterator(self, response: Any) -> AsyncIterator[bytes]:
        method = getattr(response, "aiter_bytes", None)
        if method is None:
            method = getattr(response, "aiter_raw", None)
        if method is None:
            raise _StreamFault("transport-error", "response has no byte iterator")
        try:
            return method(chunk_size=_SSE_READ_CHUNK_BYTES)
        except TypeError:
            return method()

    async def _next_body_chunk(self, iterator: AsyncIterator[bytes]) -> bytes:
        return await self._await_before(iterator.__anext__, self._read_deadlines())

    async def _consume_atomic_json(self, response: Any) -> None:
        iterator = self._response_byte_iterator(response)
        raw = bytearray()
        while True:
            try:
                chunk = await self._next_body_chunk(iterator)
            except StopAsyncIteration:
                break
            if not isinstance(chunk, (bytes, bytearray)):
                raise _StreamFault("transport-error", "non-byte response chunk")
            if len(raw) + len(chunk) > _MAX_SSE_STREAM_BYTES:
                raise _StreamFault("stream-too-large")
            raw.extend(chunk)

        try:
            decoded = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _StreamFault("malformed-utf8") from exc
        try:
            body = json.loads(decoded)
        except (TypeError, ValueError) as exc:
            raise _StreamFault("malformed-json") from exc

        final = _validate_final_object(body, strict_done=False)
        self.result.canonical_reply = final.reply
        self.result.focus = final.focus
        self.result.done = final.done
        self.result.ok = True
        if final.reply:
            await self._put_delegate_chunk(final.reply)
        await self._put(_STREAM_END)

    async def _consume_sse(self, response: Any) -> None:
        iterator = self._response_byte_iterator(response)
        line_buffer = bytearray()
        stream_bytes = 0
        event_bytes = 0
        event_name: str | None = None
        data_lines: list[str] = []
        event_field_seen = False
        ack_seen = False
        delta_seen = False
        terminal_kind: str | None = None
        terminal_final: _ValidatedFinal | None = None
        post_terminal_content = False

        async def dispatch() -> None:
            nonlocal ack_seen, delta_seen, terminal_kind, terminal_final
            nonlocal event_name, data_lines, event_field_seen, event_bytes

            name = (event_name or "").strip()
            if terminal_kind is not None:
                if terminal_kind == "final" and name == "final":
                    raise _StreamFault("duplicate-final")
                raise _StreamFault(f"event-after-{terminal_kind}")

            try:
                body = json.loads("\n".join(data_lines))
            except (TypeError, ValueError) as exc:
                raise _StreamFault("malformed-json") from exc

            if name == "ack":
                if (
                    not isinstance(body, dict)
                    or not isinstance(body.get("kind"), str)
                    or body["kind"] not in _ACK_KINDS
                ):
                    raise _StreamFault("wrong-field-type")
                if ack_seen:
                    raise _StreamFault("duplicate-ack")
                if delta_seen:
                    raise _StreamFault("ack-after-delta")
                ack_seen = True
                self.result.ack_kind = body["kind"]
            elif name == "delta":
                if not isinstance(body, dict) or not isinstance(body.get("text"), str):
                    raise _StreamFault("wrong-field-type")
                delta_seen = True
                text = body["text"]
                self._delta_chars += len(text)
                if self._delta_chars > _MAX_REPLY_CHARS:
                    raise _StreamFault("reply-too-long")
                safe = text.translate(_CONTROL_CHARS)
                self._delta_parts.append(safe)
                await self._put_delegate_chunk(safe)
            elif name == "final":
                terminal_final = _validate_final_object(body, strict_done=True)
                terminal_kind = "final"
            elif name == "error":
                if not isinstance(body, dict):
                    raise _StreamFault("wrong-field-type")
                terminal_kind = "error"
            else:
                raise _StreamFault("unknown-event", name or "(missing)")

            self._last_event_at = self._clock()
            event_name = None
            data_lines = []
            event_field_seen = False
            event_bytes = 0

        while True:
            try:
                chunk = await self._next_body_chunk(iterator)
            except StopAsyncIteration:
                break
            if not isinstance(chunk, (bytes, bytearray)):
                raise _StreamFault("transport-error", "non-byte response chunk")
            stream_bytes += len(chunk)
            if stream_bytes > _MAX_SSE_STREAM_BYTES:
                raise _StreamFault("stream-too-large")
            line_buffer.extend(chunk)
            if b"\n" not in line_buffer and len(line_buffer) > _MAX_SSE_LINE_BYTES:
                raise _StreamFault("line-too-large")

            while True:
                newline = line_buffer.find(b"\n")
                if newline < 0:
                    break
                raw_line = bytes(line_buffer[:newline])
                del line_buffer[: newline + 1]
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
                if len(raw_line) > _MAX_SSE_LINE_BYTES:
                    raise _StreamFault("line-too-large")

                if raw_line == b"":
                    if event_field_seen or data_lines or event_name is not None:
                        await dispatch()
                    else:
                        event_bytes = 0
                    continue

                if terminal_kind is not None:
                    post_terminal_content = True
                event_bytes += len(raw_line) + 1
                if event_bytes > _MAX_SSE_EVENT_BYTES:
                    raise _StreamFault("event-too-large")
                try:
                    line = raw_line.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise _StreamFault("malformed-utf8") from exc
                if line.startswith(":"):
                    continue

                field_name, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field_name == "event":
                    if event_name is not None:
                        raise _StreamFault("malformed-sse", "duplicate event field")
                    event_name = value
                    event_field_seen = True
                elif field_name == "data":
                    data_lines.append(value)
                    event_field_seen = True
                elif field_name in {"id", "retry"}:
                    # Standard SSE fields that have no meaning on this seam.
                    event_field_seen = True
                else:
                    # The SSE specification ignores unknown fields. Event *types*
                    # remain strict and are rejected in dispatch().
                    event_field_seen = True

        if line_buffer or event_field_seen or data_lines or event_name is not None:
            if terminal_kind is not None:
                raise _StreamFault(f"event-after-{terminal_kind}")
            raise _StreamFault("eof-without-final")
        if post_terminal_content:
            raise _StreamFault(f"event-after-{terminal_kind}")
        if terminal_kind == "error":
            raise _StreamFault("remote-error")
        if terminal_kind != "final" or terminal_final is None:
            raise _StreamFault("eof-without-final")

        self.result.canonical_reply = terminal_final.reply
        self.result.focus = terminal_final.focus
        self.result.done = terminal_final.done
        emitted = "".join(self._delta_parts)
        if delta_seen and emitted != terminal_final.reply:
            self.result.delta_mismatch = True
            log.warning(
                "delegate stream %s final reply diverged from deltas",
                safe_url_for_log(self._url),
            )
        self.result.ok = True
        if not self._had_delegate_chunk and terminal_final.reply:
            await self._put_delegate_chunk(terminal_final.reply)
        await self._put(_STREAM_END)


def stream_delegate(
    client: Any,
    url: str,
    text: str,
    *,
    who: str,
    turn_id: str | None = None,
    connect_timeout: float = DELEGATE_CONNECT_TIMEOUT_S,
    first_event_timeout: float = DELEGATE_TIMEOUT_S,
    idle_timeout: float = DELEGATE_IDLE_TIMEOUT_S,
    wall_timeout: float = DELEGATE_WALL_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> DelegateStream:
    """Start a v1 turn lazily and return its chunks plus terminal result.

    No network work begins until the first ``__anext__``. The returned object is
    both the async iterator and the owner of ``.result``; it can alternatively
    be unpacked as ``chunks, result``.
    """
    for name, value in (
        ("connect_timeout", connect_timeout),
        ("first_event_timeout", first_event_timeout),
        ("idle_timeout", idle_timeout),
        ("wall_timeout", wall_timeout),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    return DelegateStream(
        client,
        url,
        text,
        who=who,
        turn_id=turn_id,
        connect_timeout=connect_timeout,
        first_event_timeout=first_event_timeout,
        idle_timeout=idle_timeout,
        wall_timeout=wall_timeout,
        clock=clock,
    )


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
