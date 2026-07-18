"""Bounded sentence synthesis look-ahead for real-time voice playback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
)
from dataclasses import dataclass
from typing import Generic, TypeVar


AudioT = TypeVar("AudioT")
_END = object()


@dataclass(frozen=True)
class SynthesizedSentence(Generic[AudioT]):
    """One source sentence paired with its synthesized representation."""

    text: str
    audio: AudioT


@dataclass(frozen=True)
class _SynthesisFailure:
    text: str
    error: Exception


class SentencePipeline(Generic[AudioT]):
    """Yield synthesized sentences in order with exactly one-item look-ahead.

    Look-ahead is deliberately capped at one sentence: it hides synthesis
    latency behind the sentence currently playing without wasting substantial
    work when the caller barges in.  Callers should use the pipeline as an
    async context manager; ``aclose()`` cancels and awaits the sole pending
    synthesis task so barge-in or hangup leaves no orphan task.  Individual
    synthesis errors are reported through ``on_error`` and skipped, isolating
    them from playback of the item already yielded and from later sentences.
    """

    def __init__(
        self,
        sentences: Iterable[str] | AsyncIterable[str],
        synthesize: Callable[[str], Awaitable[AudioT]],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
        on_ahead: Callable[[bool, float], None] | None = None,
    ) -> None:
        self._synthesize = synthesize
        self._on_error = on_error
        self._on_ahead = on_ahead
        self._async_sentences: AsyncIterator[str] | None = None
        self._sync_sentences: Iterator[str] | None = None
        if isinstance(sentences, AsyncIterable):
            self._async_sentences = sentences.__aiter__()
        else:
            self._sync_sentences = iter(sentences)
        self._pending: asyncio.Task[object] | None = None
        self._yielded = False
        self._closed = False
        self._source_closed = False
        self._close_lock = asyncio.Lock()

    def __aiter__(self) -> SentencePipeline[AudioT]:
        return self

    async def __anext__(self) -> SynthesizedSentence[AudioT]:
        if self._closed:
            raise StopAsyncIteration

        while True:
            was_ahead = self._pending is not None
            if self._pending is None:
                self._pending = asyncio.create_task(
                    self._fetch_and_synthesize(),
                    name="sentence-synthesis-prefetch",
                )
            pending = self._pending
            ready = was_ahead and pending.done()
            needed_at = time.monotonic()
            try:
                outcome = await pending
            finally:
                if self._pending is pending:
                    self._pending = None
            wait_s = time.monotonic() - needed_at

            if outcome is _END:
                await self.aclose()
                raise StopAsyncIteration

            if was_ahead or self._yielded:
                if self._on_ahead is not None:
                    self._on_ahead(ready, wait_s)

            if isinstance(outcome, _SynthesisFailure):
                continue

            self._yielded = True
            self._pending = asyncio.create_task(
                self._fetch_and_synthesize(),
                name="sentence-synthesis-prefetch",
            )
            return outcome

    async def __aenter__(self) -> SentencePipeline[AudioT]:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Cancel and await any look-ahead, then close the sentence source."""
        async with self._close_lock:
            self._closed = True
            if self._pending is not None:
                pending, self._pending = self._pending, None
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # An already-finished source exception is consumed during
                    # intentional shutdown; synthesis exceptions never escape
                    # because _fetch_and_synthesize converts them to values.
                    pass

            if self._source_closed:
                return
            self._source_closed = True
            close = getattr(self._async_sentences, "aclose", None)
            if close is not None:
                await close()

    async def _fetch_and_synthesize(self) -> object:
        sentence = await self._next_sentence()
        if sentence is _END:
            return _END
        try:
            audio = await self._synthesize(sentence)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._on_error is not None:
                self._on_error(sentence, error)
            return _SynthesisFailure(sentence, error)
        return SynthesizedSentence(sentence, audio)

    async def _next_sentence(self) -> str | object:
        if self._async_sentences is not None:
            try:
                return await self._async_sentences.__anext__()
            except StopAsyncIteration:
                return _END

        assert self._sync_sentences is not None
        try:
            return next(self._sync_sentences)
        except StopIteration:
            return _END
