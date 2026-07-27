"""Reusable asynchronous client for the STT service's incremental API.

The phone gateway is synchronous at its media-frame boundary, so
``feed_nowait`` owns a small PCM buffer and serializes HTTP requests in the
background.  ``finish`` is the synchronization point: it flushes any short
batch, waits for every feed, and surfaces the first streaming error so callers
can fail open to the existing one-shot endpoint.

Nothing in this module is phone-specific.  The browser voice server can reuse
the same session protocol in a follow-up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("nano-claw.streaming_stt")


class StreamingSTTError(RuntimeError):
    """A start/feed/finish request failed and the caller should fall back."""


@dataclass(frozen=True)
class StreamingSTTResult:
    text: str
    committed_chars: int
    finish_ms: float
    pass_count: int
    duration_s: float


PassCallback = Callable[[dict[str, Any]], None]


def _check_response(response: Any) -> None:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()


def _json_object(response: Any) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise StreamingSTTError("STT service returned a non-object response")
    return value


class StreamingSTTSession:
    """One ordered incremental STT session over an async HTTP client."""

    def __init__(
        self,
        http: Any,
        service_url: str,
        *,
        sample_rate: int,
        model_size: str,
        batch_ms: int = 500,
        on_pass: PassCallback | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if batch_ms <= 0:
            raise ValueError("batch_ms must be positive")
        self._http = http
        self._service_url = service_url.rstrip("/")
        self.sample_rate = int(sample_rate)
        self.model_size = str(model_size)
        self.batch_ms = int(batch_ms)
        self._batch_bytes = max(
            2, self.sample_rate * 2 * self.batch_ms // 1000
        )
        self._on_pass = on_pass
        self._pending = bytearray()
        self._session_id: str | None = None
        self._error: BaseException | None = None
        self._closed = False
        self._start_task = asyncio.create_task(self._start())
        self._chain: asyncio.Task[None] = self._start_task

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    async def _start(self) -> None:
        try:
            response = await self._http.post(
                f"{self._service_url}/stream/start",
                headers={"X-Model-Size": self.model_size},
            )
            _check_response(response)
            body = _json_object(response)
            session_id = body.get("session_id") or body.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise StreamingSTTError(
                    "STT stream start response omitted session_id"
                )
            self._session_id = session_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remember_error(exc)

    def _remember_error(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc

    def _emit_passes(self, body: dict[str, Any]) -> None:
        if self._on_pass is None:
            return
        passes = body.get("passes") or ()
        if not isinstance(passes, (list, tuple)):
            return
        for item in passes:
            if not isinstance(item, dict):
                continue
            try:
                self._on_pass(item)
            except Exception:
                # Telemetry is observational and must never turn a healthy STT
                # request into a fallback.
                log.exception("streaming STT pass callback failed")

    def feed_nowait(self, pcm16: bytes) -> None:
        """Queue PCM16 and send approximately ``batch_ms`` chunks in order."""
        if self._closed or not pcm16:
            return
        if len(pcm16) % 2:
            self._remember_error(
                ValueError("PCM16 feed must contain an even number of bytes")
            )
            return
        self._pending.extend(pcm16)
        while len(self._pending) >= self._batch_bytes:
            chunk = bytes(self._pending[: self._batch_bytes])
            del self._pending[: self._batch_bytes]
            self._enqueue_feed(chunk)

    def _enqueue_feed(self, chunk: bytes) -> None:
        previous = self._chain

        async def send_after_previous() -> None:
            try:
                await previous
                if self._error is not None:
                    return
                if self._session_id is None:
                    raise StreamingSTTError("STT stream did not start")
                response = await self._http.post(
                    f"{self._service_url}/stream/{self._session_id}/feed",
                    content=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Sample-Rate": str(self.sample_rate),
                    },
                )
                _check_response(response)
                self._emit_passes(_json_object(response))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._remember_error(exc)

        self._chain = asyncio.create_task(send_after_previous())

    def _raise_saved_error(self) -> None:
        if self._error is None:
            return
        raise StreamingSTTError(str(self._error)) from self._error

    async def finish(self, *, keep_session: bool = False) -> StreamingSTTResult:
        """Flush queued audio, decode the tail, and return the full text."""
        if self._closed:
            raise StreamingSTTError("STT stream is already closed")
        if self._pending:
            chunk = bytes(self._pending)
            self._pending.clear()
            self._enqueue_feed(chunk)
        await self._chain
        self._raise_saved_error()
        if self._session_id is None:
            raise StreamingSTTError("STT stream did not start")
        try:
            headers = {"X-Keep-Session": "1"} if keep_session else None
            response = await self._http.post(
                f"{self._service_url}/stream/{self._session_id}/finish",
                headers=headers,
            )
            _check_response(response)
            body = _json_object(response)
            self._emit_passes(body)
            result = StreamingSTTResult(
                text=str(body.get("text") or ""),
                committed_chars=int(body.get("committed_chars") or 0),
                finish_ms=float(body.get("finish_ms") or 0.0),
                pass_count=int(body.get("pass_count") or 0),
                duration_s=float(body.get("duration_s") or 0.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remember_error(exc)
            self._raise_saved_error()
            raise AssertionError("unreachable")
        if not keep_session:
            self._closed = True
            self._session_id = None
        return result

    async def close(self) -> None:
        """Best-effort cleanup; safe after a failed or already-finished stream."""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        try:
            await self._chain
        except asyncio.CancelledError:
            raise
        session_id, self._session_id = self._session_id, None
        if session_id is None:
            return
        try:
            response = await self._http.delete(
                f"{self._service_url}/stream/{session_id}"
            )
            _check_response(response)
        except Exception:
            log.debug("streaming STT cleanup failed", exc_info=True)
