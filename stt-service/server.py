"""Standalone STT service — runs natively on Mac for Metal GPU acceleration.

The original one-shot ``POST /transcribe`` API remains available.  The
``/stream`` endpoints add a small LocalAgreement-2 session layer: hypotheses
are decoded while audio arrives, stable token prefixes are committed after two
consecutive passes agree, and committed audio is removed from the next decode
window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, NamedTuple

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from scipy.signal import resample

log = logging.getLogger("stt-service")

SIZES = ["tiny", "base", "small", "medium"]
DEFAULT_SIZE = "base"  # ~75MB, good accuracy for short utterances
WHISPER_RATE = 16000  # faster-whisper expects 16kHz input
STREAM_PASS_MS = 700
STREAM_IDLE_SECONDS = 60.0
MODEL_CPU_THREADS = min(6, os.cpu_count() or 4)

_reaper_task: asyncio.Task | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _reaper_task

    async def reap() -> None:
        try:
            while True:
                await asyncio.sleep(10.0)
                _sessions.expire_idle()
        except asyncio.CancelledError:
            return

    _reaper_task = asyncio.create_task(reap())
    try:
        yield
    finally:
        _reaper_task.cancel()
        await asyncio.gather(_reaper_task, return_exceptions=True)
        _reaper_task = None


app = FastAPI(lifespan=_lifespan)

# Lazy-loaded Whisper models, cached per size.  The cache is deliberately
# additive: changing X-Model-Size never evicts a model held by a live stream.
_models: dict[str, object] = {}
_model_locks: dict[str, threading.Lock] = {}
_models_lock = threading.Lock()
_model_use_counts: dict[str, int] = {}
_model_load_ms: dict[str, float] = {}


def _valid_size(size: str) -> bool:
    return size in SIZES


def _get_model(size: str):
    """Load + cache a faster-whisper model per size (auto-downloads on first use)."""
    with _models_lock:
        if size in _models:
            return _models[size]

        from faster_whisper import WhisperModel

        log.info("Loading faster-whisper model: %s ...", size)
        started = time.perf_counter()
        model = WhisperModel(
            size,
            device="cpu",
            compute_type="int8",
            cpu_threads=MODEL_CPU_THREADS,
        )
        _models[size] = model
        _model_locks.setdefault(size, threading.Lock())
        _model_load_ms[size] = (time.perf_counter() - started) * 1000.0
        log.info(
            "Whisper model loaded: %s (%.0fms)", size, _model_load_ms[size]
        )
        return model


def _sample_rate(request: Request, default: int = 48000) -> int:
    raw = request.headers.get("X-Sample-Rate", str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid X-Sample-Rate") from exc
    if value <= 0 or value > 384_000:
        raise HTTPException(status_code=400, detail="invalid X-Sample-Rate")
    return value


def _model_size(request: Request) -> str:
    size = request.headers.get("X-Model-Size", DEFAULT_SIZE)
    return size if _valid_size(size) else DEFAULT_SIZE


def _pcm16(audio_bytes: bytes) -> np.ndarray:
    if len(audio_bytes) % 2:
        raise ValueError("PCM16 body must contain an even number of bytes")
    return np.frombuffer(audio_bytes, dtype=np.int16)


def _whisper_samples(audio_bytes: bytes, sample_rate: int) -> np.ndarray:
    samples = _pcm16(audio_bytes).astype(np.float32) / 32768.0
    if sample_rate != WHISPER_RATE and len(samples):
        num_output = int(len(samples) * WHISPER_RATE / sample_rate)
        samples = resample(samples, num_output).astype(np.float32)
    return samples


def _trailing_padding_ms(
    audio_bytes: bytes,
    sample_rate: int,
    *,
    frame_ms: int = 20,
    silence_rms: float = 120.0,
) -> int:
    """Estimate low-energy audio at the end of a one-shot request.

    This exposes endpoint/VAD tail padding in logs without changing the
    transcription contract or applying a second VAD inside the STT service.
    """
    samples = _pcm16(audio_bytes)
    frame_samples = max(1, sample_rate * frame_ms // 1000)
    silent_frames = 0
    for end in range(len(samples), 0, -frame_samples):
        frame = samples[max(0, end - frame_samples) : end].astype(np.float64)
        rms = float(np.sqrt(np.mean(frame * frame))) if len(frame) else 0.0
        if rms > silence_rms:
            break
        silent_frames += 1
    return silent_frames * frame_ms


class HypothesisToken(NamedTuple):
    """One display token and its time span in the current decode window."""

    text: str
    start: float
    end: float


class DecodeHypothesis(NamedTuple):
    """Text plus token timestamps returned by a stream decoder."""

    text: str
    tokens: tuple[HypothesisToken, ...]

    @classmethod
    def from_text(cls, text: str, duration_s: float) -> "DecodeHypothesis":
        pieces = tuple(piece for piece in text.strip().split() if piece)
        if not pieces:
            return cls("", ())
        width = max(0.0, duration_s) / len(pieces)
        tokens = tuple(
            HypothesisToken(piece, index * width, (index + 1) * width)
            for index, piece in enumerate(pieces)
        )
        return cls(" ".join(pieces), tokens)


def longest_common_token_prefix(
    previous: tuple[HypothesisToken, ...],
    current: tuple[HypothesisToken, ...],
) -> int:
    """Return the number of leading tokens two hypotheses agree on."""
    count = 0
    for left, right in zip(previous, current):
        if left.text != right.text:
            break
        count += 1
    return count


def _join_token_text(tokens: tuple[HypothesisToken, ...]) -> str:
    text = " ".join(token.text.strip() for token in tokens if token.text.strip())
    # Word timestamps normally keep punctuation attached.  The cleanup also
    # makes fake/scripted decoders with standalone punctuation read naturally.
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()


def _hypothesis_from_segments(segments) -> DecodeHypothesis:
    tokens: list[HypothesisToken] = []
    segment_text: list[str] = []
    for segment in segments:
        clean = segment.text.strip()
        if clean:
            segment_text.append(clean)
        words = getattr(segment, "words", None) or ()
        if words:
            for word in words:
                word_text = str(word.word).strip()
                if word_text:
                    tokens.append(
                        HypothesisToken(
                            word_text, float(word.start), float(word.end)
                        )
                    )
            continue
        pieces = tuple(piece for piece in clean.split() if piece)
        if not pieces:
            continue
        start = float(getattr(segment, "start", 0.0))
        end = max(start, float(getattr(segment, "end", start)))
        width = (end - start) / len(pieces)
        tokens.extend(
            HypothesisToken(piece, start + index * width, start + (index + 1) * width)
            for index, piece in enumerate(pieces)
        )
    text = _join_token_text(tuple(tokens)) or " ".join(segment_text).strip()
    if not tokens and text:
        return DecodeHypothesis.from_text(text, 0.0)
    return DecodeHypothesis(text, tuple(tokens))


class WhisperStreamDecoder:
    """Pins one model size for the lifetime of a streaming session."""

    def __init__(self, size: str) -> None:
        self.size = size
        self._model = None

    def __call__(
        self, samples: np.ndarray, initial_prompt: str
    ) -> DecodeHypothesis:
        if self._model is None:
            self._model = _get_model(self.size)
        lock = _model_locks.setdefault(self.size, threading.Lock())
        with lock:
            first_use = _model_use_counts.get(self.size, 0) == 0
            segments, _info = self._model.transcribe(
                samples,
                # Greedy decoding keeps in-speech passes close to real time;
                # LocalAgreement supplies stability across passes.
                beam_size=1,
                best_of=1,
                language="en",
                word_timestamps=True,
                initial_prompt=initial_prompt or None,
            )
            hypothesis = _hypothesis_from_segments(segments)
            _model_use_counts[self.size] = (
                _model_use_counts.get(self.size, 0) + 1
            )
        log.info(
            "Streaming pass size=%s cache=hit first_use=%s samples=%d",
            self.size,
            first_use,
            len(samples),
        )
        return hypothesis


StreamDecoder = Callable[[np.ndarray, str], DecodeHypothesis | str]


class LocalAgreementSession:
    """Incremental PCM buffer with LocalAgreement-2 prefix commitment."""

    def __init__(
        self,
        session_id: str,
        size: str,
        decoder: StreamDecoder,
        *,
        pass_ms: int = STREAM_PASS_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_id = session_id
        self.size = size
        self._decoder = decoder
        self._pass_ms = pass_ms
        self._clock = clock
        self._lock = threading.Lock()
        self._sample_rate: int | None = None
        self._audio = bytearray()
        self._new_samples = 0
        self._previous: tuple[HypothesisToken, ...] | None = None
        self._committed_tokens: list[str] = []
        self.committed_text = ""
        self.pass_count = 0
        self.total_audio_seconds = 0.0
        self.last_active = clock()

    @property
    def sample_rate(self) -> int | None:
        return self._sample_rate

    @property
    def window_ms(self) -> float:
        if not self._sample_rate:
            return 0.0
        return len(self._audio) / 2.0 / self._sample_rate * 1000.0

    def _touch(self) -> None:
        self.last_active = self._clock()

    def feed(self, audio_bytes: bytes, sample_rate: int) -> dict:
        with self._lock:
            self._touch()
            samples = _pcm16(audio_bytes)
            if self._sample_rate is None:
                self._sample_rate = sample_rate
            elif sample_rate != self._sample_rate:
                raise ValueError(
                    f"sample rate changed from {self._sample_rate} to {sample_rate}"
                )
            self._audio.extend(audio_bytes)
            self._new_samples += len(samples)
            self.total_audio_seconds += len(samples) / sample_rate
            passes: list[dict] = []
            threshold = sample_rate * self._pass_ms // 1000
            if self._new_samples >= threshold:
                passes.append(self._decode_pass(final=False))
            return {
                "session_id": self.session_id,
                "passes": passes,
                "pass_count": self.pass_count,
                "committed_text": self.committed_text,
                "committed_chars": len(self.committed_text),
            }

    def _coerce_hypothesis(
        self, value: DecodeHypothesis | str, duration_s: float
    ) -> DecodeHypothesis:
        if isinstance(value, DecodeHypothesis):
            return value
        if isinstance(value, str):
            return DecodeHypothesis.from_text(value, duration_s)
        raise TypeError("stream decoder must return DecodeHypothesis or str")

    def _strip_prompt_echo(
        self, tokens: tuple[HypothesisToken, ...]
    ) -> tuple[HypothesisToken, ...]:
        """Drop a decoder's accidental repetition of the committed prompt."""
        if not self._committed_tokens or len(tokens) < len(self._committed_tokens):
            return tokens
        prefix = tuple(token.text for token in tokens[: len(self._committed_tokens)])
        if prefix == tuple(self._committed_tokens):
            return tokens[len(self._committed_tokens) :]
        return tokens

    def _append_committed(self, tokens: tuple[HypothesisToken, ...]) -> None:
        if not tokens:
            return
        self._committed_tokens.extend(token.text for token in tokens)
        self.committed_text = _join_token_text(
            tuple(
                HypothesisToken(text, 0.0, 0.0)
                for text in self._committed_tokens
            )
        )

    def _trim_audio(self, seconds: float) -> None:
        if not self._sample_rate or seconds <= 0.0:
            return
        samples = min(
            len(self._audio) // 2,
            max(0, int(round(seconds * self._sample_rate))),
        )
        del self._audio[: samples * 2]

    @staticmethod
    def _shift_tokens(
        tokens: tuple[HypothesisToken, ...], seconds: float
    ) -> tuple[HypothesisToken, ...]:
        return tuple(
            HypothesisToken(
                token.text,
                max(0.0, token.start - seconds),
                max(0.0, token.end - seconds),
            )
            for token in tokens
        )

    def _decode_pass(self, *, final: bool) -> dict:
        if not self._sample_rate:
            raise RuntimeError("stream has no sample rate")
        window_ms = self.window_ms
        audio_bytes = bytes(self._audio)
        samples = _whisper_samples(audio_bytes, self._sample_rate)
        started = time.perf_counter()
        decoded = self._decoder(samples, self.committed_text)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        hypothesis = self._coerce_hypothesis(decoded, window_ms / 1000.0)
        current = self._strip_prompt_echo(hypothesis.tokens)

        trim_seconds = 0.0
        committed_now: tuple[HypothesisToken, ...] = ()
        if final:
            committed_now = current
        elif self._previous is not None:
            agreed = longest_common_token_prefix(self._previous, current)
            committed_now = current[:agreed]

        if committed_now:
            self._append_committed(committed_now)
            trim_seconds = max(0.0, committed_now[-1].end)
            self._trim_audio(trim_seconds)

        remaining = current[len(committed_now) :]
        self._previous = (
            None if final else self._shift_tokens(remaining, trim_seconds)
        )
        if final:
            # The transcript is now authoritative through this endpoint.  Tail
            # silence has no linguistic value and must not burden a continued
            # dynamic-endpoint session.
            self._audio.clear()
        self._new_samples = 0
        self.pass_count += 1
        return {
            "pass_count": self.pass_count,
            "window_ms": round(window_ms, 1),
            "ms": round(elapsed_ms, 1),
            "committed_chars": len(self.committed_text),
            "final": final,
        }

    def finish(self) -> dict:
        with self._lock:
            self._touch()
            incrementally_committed = len(self.committed_text)
            finish_started = time.perf_counter()
            passes: list[dict] = []
            if (
                self.pass_count
                and self._new_samples == 0
                and self._previous is not None
            ):
                # The last feed already decoded every sample.  Reuse that
                # newest hypothesis instead of paying for an identical final
                # pass after endpointing.
                self._append_committed(self._previous)
                self._previous = None
                self._audio.clear()
            elif self._audio and self._sample_rate:
                passes.append(self._decode_pass(final=True))
            finish_ms = (time.perf_counter() - finish_started) * 1000.0
            return {
                "session_id": self.session_id,
                "text": self.committed_text,
                "duration_s": round(self.total_audio_seconds, 2),
                "pass_count": self.pass_count,
                "passes": passes,
                # Stable text produced before endpointing; the final pass may
                # commit the remainder but is intentionally not counted here.
                "committed_chars": incrementally_committed,
                "finish_ms": round(finish_ms, 1),
            }


class SessionRegistry:
    """Thread-safe session ownership and lazy/periodic idle expiry."""

    def __init__(
        self,
        *,
        idle_seconds: float = STREAM_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        decoder_factory: Callable[[str], StreamDecoder] = WhisperStreamDecoder,
    ) -> None:
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._decoder_factory = decoder_factory
        self._lock = threading.Lock()
        self._sessions: dict[str, LocalAgreementSession] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def create(self, size: str) -> LocalAgreementSession:
        self.expire_idle()
        session_id = uuid.uuid4().hex
        session = LocalAgreementSession(
            session_id,
            size,
            self._decoder_factory(size),
            clock=self._clock,
        )
        with self._lock:
            self._sessions[session_id] = session
            count = len(self._sessions)
        if count > 2:
            log.warning("STT streaming CPU guard: %d concurrent sessions", count)
        return session

    def get(self, session_id: str) -> LocalAgreementSession:
        self.expire_idle()
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def pop(self, session_id: str) -> LocalAgreementSession | None:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def expire_idle(self) -> list[str]:
        now = self._clock()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.last_active >= self.idle_seconds
            ]
            for session_id in expired:
                self._sessions.pop(session_id, None)
        for session_id in expired:
            log.info("Expired idle STT stream: %s", session_id)
        return expired


_sessions = SessionRegistry()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe(request: Request):
    """Transcribe raw PCM int16 audio bytes to text.

    Headers:
        X-Sample-Rate: sample rate of the audio (default 48000)
        X-Model-Size: Whisper model size to use (default "base"; one of SIZES)

    Body:
        Raw PCM int16 mono audio bytes (application/octet-stream)

    Returns:
        JSON: { "text": "...", "duration_s": 4.56 }
    """
    audio_bytes = await request.body()
    sample_rate = _sample_rate(request)
    size = _model_size(request)

    if not audio_bytes:
        return {"text": "", "duration_s": 0.0, "processing_ms": 0}
    try:
        samples = _whisper_samples(audio_bytes, sample_rate)
        padding_ms = _trailing_padding_ms(audio_bytes, sample_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    start = time.perf_counter()
    cached_before = size in _models
    first_use = _model_use_counts.get(size, 0) == 0
    model = _get_model(size)
    duration_s = len(audio_bytes) / 2.0 / sample_rate

    log.info(
        "Received %.2fs audio (%d bytes, %dHz) size=%s cache=%s "
        "first_use=%s tail_padding_ms=%d load_ms=%.0f",
        duration_s,
        len(audio_bytes),
        sample_rate,
        size,
        "hit" if cached_before else "miss",
        first_use,
        padding_ms,
        _model_load_ms.get(size, 0.0),
    )

    # Opt-in word timing for the call-review audio inspector: aligning words
    # against the energy envelope is how a seam gets pinned to a syllable.
    want_words = str(request.headers.get("X-Word-Timestamps", "")).strip() in (
        "1", "true", "yes",
    )
    lock = _model_locks.setdefault(size, threading.Lock())
    with lock:
        first_use = _model_use_counts.get(size, 0) == 0
        segments, _info = model.transcribe(
            samples, beam_size=5, language="en", word_timestamps=want_words
        )
        if want_words:
            hypothesis = _hypothesis_from_segments(segments)
            words = [
                {"w": token.text, "start": round(token.start, 3), "end": round(token.end, 3)}
                for token in hypothesis.tokens
            ]
            text = hypothesis.text
        else:
            words = []
            text = " ".join(seg.text.strip() for seg in segments).strip()
        _model_use_counts[size] = _model_use_counts.get(size, 0) + 1
    elapsed = time.perf_counter() - start

    log.info(
        "Transcribed in %.2fs → %r size=%s cache=%s first_use=%s "
        "tail_padding_ms=%d",
        elapsed,
        text[:100],
        size,
        "hit" if cached_before else "miss",
        first_use,
        padding_ms,
    )
    return {
        "text": text,
        "duration_s": round(duration_s, 2),
        "processing_ms": int(elapsed * 1000),
        **({"words": words} if want_words else {}),
    }


def _stream_or_404(session_id: str) -> LocalAgreementSession:
    try:
        return _sessions.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown stream session") from exc


@app.post("/stream/start")
async def stream_start(request: Request):
    """Create an incremental session pinned to X-Model-Size."""
    session = _sessions.create(_model_size(request))
    return {
        "id": session.session_id,
        "session_id": session.session_id,
        "expires_in_s": STREAM_IDLE_SECONDS,
    }


@app.post("/stream/{session_id}/feed")
async def stream_feed(session_id: str, request: Request):
    """Append raw PCM16 and run at most one due incremental decode."""
    session = _stream_or_404(session_id)
    audio_bytes = await request.body()
    sample_rate = _sample_rate(request)
    try:
        return await asyncio.to_thread(session.feed, audio_bytes, sample_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/stream/{session_id}/finish")
async def stream_finish(session_id: str, request: Request):
    """Decode only the uncommitted tail and return the complete transcript.

    The internal ``X-Keep-Session: 1`` option supports the phone gateway's
    dynamic-endpoint continuation: it may inspect a provisional endpoint and
    keep feeding the same committed-prefix session.  Normal callers omit the
    header, and finish removes the session.
    """
    session = _stream_or_404(session_id)
    keep = request.headers.get("X-Keep-Session", "").lower() in {
        "1",
        "true",
        "yes",
    }
    result = await asyncio.to_thread(session.finish)
    if not keep:
        _sessions.pop(session_id)
    return result


@app.delete("/stream/{session_id}")
async def stream_delete(session_id: str):
    """Best-effort explicit cleanup for a kept dynamic-endpoint session."""
    return {"deleted": _sessions.pop(session_id) is not None}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-14s %(levelname)-5s %(message)s",
    )
    log.info("Starting STT service on port 8200")
    uvicorn.run(app, host="0.0.0.0", port=8200)
