"""MCP stdio server: nano-claw's local voice services behind one standard door.

Spec: ~/.claude/plans/nano-claw-mcp-voice.md (v0.3, Codex-reviewed). The
directive behind it is Gemini-minimization — Kokoro (tts-service), LuxTTS
(lux-service) and Whisper (stt-service) become the voice door harnesses and
agents reach over MCP, beside (never instead of) their existing HTTP.

One thin shim, services unchanged. Zero Python dependencies; the only
external binary is ffmpeg, used solely for `target_rate` resampling,
discovered at call time. Transport is the decision-core sidecar's: JSON-RPC
2.0 over newline-delimited stdio with the classic initialize handshake —
plus the max-line-size guard that server still needs backported.

Statelessness, stated honestly: batch tools are stateless; streaming STT is
an explicit stateful resource protocol. The backend holds real per-stream
state (60s idle expiry, per-stream rate); the handles here make that state
visible and client-threaded — they mirror backend lifetime semantics, never
reinvent them.

Run:  python3 mcp-voice/server.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from typing import Any, Callable, Optional

SCHEMA_VERSION = "1.0"
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "nano-claw-voice", "version": "0.1.0"}

TTS_URL = os.environ.get("NANO_CLAW_TTS_URL", "http://127.0.0.1:8300")
LUX_URL = os.environ.get("NANO_CLAW_LUX_URL", "http://127.0.0.1:8301")
STT_URL = os.environ.get("NANO_CLAW_STT_URL", "http://127.0.0.1:8200")

# ── bounds (spec "Binary contract", exact) ───────────────────────────────────
MAX_LINE_BYTES = 64 * 1024 * 1024          # JSON-RPC line, rejected before decode
INLINE_WAV_BOUND = 1_048_576               # decoded WAV bytes (44B header + PCM)
MAX_BACKEND_READ = 64 * 1024 * 1024        # streamed backend read cap
MAX_WAV_DURATION_S = 120
MAX_TEXT_CHARS = 5_000
MAX_STT_DECODED = 32 * 1024 * 1024
CONNECT_TIMEOUT_S = 2.0
TTS_DEADLINE_S = 60.0
STT_DEADLINE_S = 120.0
STREAM_DEADLINE_S = 30.0
RESAMPLE_TIMEOUT_S = 30.0
SPEED_MIN, SPEED_MAX = 0.5, 2.0
TARGET_RATE_MIN, TARGET_RATE_MAX = 8000, 48000

# ── streaming caps (mirrors of backend semantics, not inventions) ────────────
STREAM_IDLE_EXPIRY_S = 60                  # the backend's, surfaced not invented
MAX_STREAMS = 2                            # backend warns above 2
STREAM_LIFETIME_S = 180
STREAM_TOTAL_BYTES = 32 * 1024 * 1024
TOMBSTONE_TTL_S = 120
TOMBSTONE_MAX = 32

OUT_ROOT = os.environ.get("MCP_VOICE_OUT_ROOT",
                          os.path.join(tempfile.gettempdir(), "mcp-voice"))

_ENGINES = {"kokoro": TTS_URL, "luxtts": LUX_URL}


class VoiceError(Exception):
    """A tool error with a contract code. Message is for humans; the code is
    the contract — every one is named in the spec and reachable by a test."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── backend HTTP (stdlib, streamed, size-capped, two-phase timeout) ──────────

def _http(method: str, url: str, body: Optional[bytes] = None,
          headers: Optional[dict[str, str]] = None,
          deadline_s: float = 30.0) -> tuple[int, bytes, dict[str, str]]:
    """One backend request: connect 2s, then the op deadline for everything else.

    Two-phase on purpose — urlopen's single timeout also governs the wait for
    response HEADERS, so a model legitimately thinking for 10s would read as
    "timed out connecting" (hit on the first real Kokoro round-trip). Deadline
    exceeded → backend_timeout; unreachable → service_unavailable; the body is
    read in chunks against MAX_BACKEND_READ so a misbehaving backend cannot
    balloon this process.
    """
    import http.client
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    start = time.monotonic()

    def remaining() -> float:
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            raise VoiceError("backend_timeout",
                             f"{url} exceeded {deadline_s:.0f}s deadline")
        return left

    conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_S)
    try:
        try:
            conn.connect()
        except (socket.timeout, TimeoutError):
            raise VoiceError("backend_timeout", f"{url} timed out connecting")
        except OSError as exc:
            raise VoiceError("service_unavailable", f"{url} unreachable: {exc}")
        # One monotonic deadline: every socket op gets only what is LEFT of
        # the budget, never the full budget again — otherwise a slow chunked
        # response extends the 30/60/120s contract indefinitely (Codex).
        try:
            conn.sock.settimeout(remaining())
            conn.request(method, path, body=body, headers=headers or {})
            conn.sock.settimeout(remaining())
            resp = conn.getresponse()
        except VoiceError:
            raise
        except (socket.timeout, TimeoutError):
            raise VoiceError("backend_timeout",
                             f"{url} exceeded {deadline_s:.0f}s waiting for a response")
        except (http.client.HTTPException, OSError) as exc:
            raise VoiceError("service_unavailable", f"{url} dropped mid-request: {exc}")

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                conn.sock.settimeout(remaining())
                chunk = resp.read(65536)
            except VoiceError:
                raise
            except (socket.timeout, TimeoutError):
                raise VoiceError("backend_timeout",
                                 f"{url} exceeded {deadline_s:.0f}s mid-read")
            except (http.client.IncompleteRead, http.client.HTTPException) as exc:
                raise VoiceError("service_unavailable",
                                 f"{url} truncated its response: {exc}")
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BACKEND_READ:
                raise VoiceError("response_too_large",
                                 f"{url} response exceeded {MAX_BACKEND_READ} bytes")
            chunks.append(chunk)
        return (resp.status, b"".join(chunks),
                {k.lower(): v for k, v in resp.getheaders()})
    finally:
        conn.close()


# ── WAV boundary (audio is WAV at the MCP boundary, both directions) ─────────

def wav_wrap(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm)
    return buf.getvalue()


def wav_parse(data: bytes) -> tuple[bytes, int]:
    """Validate and unwrap: PCM16, mono, sane rate, complete data chunk.

    Deliberately built on the stdlib wave module, with its leniencies
    accepted and stated: exotic RIFF malformations wave tolerates (bogus
    block_align, trailing chunks) pass through, because what leaves here is
    always even-length PCM at a validated rate — exactly what the localhost
    backends consume — and the trust boundary is a local stdio client. A
    hand-rolled RIFF parser would triple this function to defend a boundary
    that doesn't exist here; revisit if an HTTP transport ever lands.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as fh:
            if fh.getcomptype() != "NONE":
                raise VoiceError("malformed_wav", "compressed WAV not accepted")
            if fh.getsampwidth() != 2:
                raise VoiceError("malformed_wav",
                                 f"sample width {fh.getsampwidth() * 8}-bit; PCM16 required")
            if fh.getnchannels() != 1:
                raise VoiceError("malformed_wav",
                                 f"{fh.getnchannels()} channels; mono required")
            rate = fh.getframerate()
            if not (8000 <= rate <= 384_000):
                raise VoiceError("malformed_wav", f"implausible sample rate {rate}")
            declared_frames = fh.getnframes()
            frames = fh.readframes(declared_frames)
    except VoiceError:
        raise
    except (wave.Error, EOFError, struct.error) as exc:
        raise VoiceError("malformed_wav", f"not a readable WAV: {exc}")
    # wave returns PARTIAL frames without raising when the data chunk is cut
    # short — the header's frame count is the claim the bytes must honor.
    if len(frames) != declared_frames * 2:
        raise VoiceError("malformed_wav", "data chunk truncated")
    if len(frames) / 2 / rate > MAX_WAV_DURATION_S:
        raise VoiceError("too_large",
                         f"audio exceeds the {MAX_WAV_DURATION_S}s duration cap")
    return frames, rate


# ── ffmpeg resampling (target_rate only; discovered at call time) ────────────

def _resample(pcm: bytes, rate: int, target: int) -> tuple[bytes, dict[str, str]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VoiceError("resampler_unavailable",
                         "ffmpeg not on PATH; install it or drop target_rate")
    try:
        version = subprocess.run([ffmpeg, "-version"], capture_output=True,
                                 text=True, timeout=5).stdout.splitlines()[0]
    except Exception:
        version = "unknown"
    cmd = [ffmpeg, "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
           "-f", "s16le", "-ar", str(target), "-ac", "1", "pipe:1"]
    try:
        proc = subprocess.run(cmd, input=pcm, capture_output=True,
                              timeout=RESAMPLE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise VoiceError("resample_failed",
                         f"ffmpeg exceeded {RESAMPLE_TIMEOUT_S:.0f}s")
    if proc.returncode != 0 or not proc.stdout:
        raise VoiceError("resample_failed",
                         f"ffmpeg exit {proc.returncode}: "
                         f"{proc.stderr[-200:].decode(errors='replace')}")
    return proc.stdout, {"name": "ffmpeg", "version": version}


# ── output delivery (inline / file / auto) ───────────────────────────────────

def _deliver(wav: bytes, response_mode: str, out_dir: Optional[str]) -> dict[str, Any]:
    if response_mode not in ("inline", "file", "auto"):
        raise VoiceError("invalid_argument",
                         "response_mode must be inline, file or auto")
    inline_ok = len(wav) <= INLINE_WAV_BOUND
    if response_mode == "inline" or (response_mode == "auto" and inline_ok):
        if not inline_ok:
            raise VoiceError("too_large",
                             f"decoded WAV is {len(wav)} bytes (bound "
                             f"{INLINE_WAV_BOUND}); use response_mode=file with out_dir")
        return {"audio_base64": base64.b64encode(wav).decode()}
    # file path — required out_dir, contained under OUT_ROOT
    if not out_dir:
        if response_mode == "file":
            raise VoiceError("missing_out_dir", "response_mode=file requires out_dir")
        raise VoiceError("too_large",
                         f"decoded WAV is {len(wav)} bytes (bound {INLINE_WAV_BOUND}); "
                         "pass out_dir to receive a file instead")
    root = os.path.realpath(OUT_ROOT)
    os.makedirs(root, exist_ok=True)
    resolved = os.path.realpath(out_dir)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise VoiceError("unsafe_out_dir",
                         f"out_dir must resolve under {root}")
    os.makedirs(resolved, exist_ok=True)
    # Server-generated name, O_EXCL temp, atomic rename — never caller-named.
    # All three ops go through a directory fd so a symlink swapped in after
    # the containment check cannot redirect the write (TOCTOU, Codex): the fd
    # pins the actual directory that passed the check.
    name = f"tts-{uuid.uuid4().hex}.wav"
    dirfd = os.open(resolved, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fd = os.open(name + ".part", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
                     dir_fd=dirfd)
        try:
            view = memoryview(wav)
            while view:
                written = os.write(fd, view)   # os.write may short-write
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(name + ".part", dir_fd=dirfd)
            except OSError:
                pass
            raise
        os.close(fd)
        os.rename(name + ".part", name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    finally:
        os.close(dirfd)
    return {"file_path": os.path.join(resolved, name)}


# ── validators ───────────────────────────────────────────────────────────────

def _require_text(args: dict[str, Any]) -> str:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("empty_text", "text is required and must be non-empty")
    if len(text) > MAX_TEXT_CHARS:
        raise VoiceError("invalid_argument",
                         f"text is {len(text)} chars (cap {MAX_TEXT_CHARS})")
    return text


def _require_engine(args: dict[str, Any]) -> str:
    engine = args.get("engine", "kokoro")
    if engine not in _ENGINES:
        raise VoiceError("invalid_engine",
                         f"engine must be one of {sorted(_ENGINES)}")
    return engine


def _require_speed(args: dict[str, Any]) -> float:
    speed = args.get("speed", 1.0)
    # Validated BEFORE dispatch: "finite > 0" would admit 1e-9 and burn
    # backend GPU past the client timeout (spec, verbatim rationale).
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) \
            or not (SPEED_MIN <= float(speed) <= SPEED_MAX):
        raise VoiceError("invalid_argument",
                         f"speed must be in [{SPEED_MIN}, {SPEED_MAX}]")
    return float(speed)


def _require_target_rate(args: dict[str, Any]) -> Optional[int]:
    rate = args.get("target_rate")
    if rate is None:
        return None
    if not isinstance(rate, int) or isinstance(rate, bool) \
            or not (TARGET_RATE_MIN <= rate <= TARGET_RATE_MAX):
        raise VoiceError("invalid_argument",
                         f"target_rate must be an integer in "
                         f"[{TARGET_RATE_MIN}, {TARGET_RATE_MAX}]")
    return rate


def _stt_input_wav(args: dict[str, Any]) -> bytes:
    b64, path = args.get("audio_base64"), args.get("file_path")
    if b64 and path:
        raise VoiceError("audio_source_conflict",
                         "pass audio_base64 OR file_path, not both")
    if b64:
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            raise VoiceError("invalid_argument", "audio_base64 is not valid base64")
    elif path:
        if not isinstance(path, str) or not os.path.isfile(path):
            raise VoiceError("invalid_argument", f"file_path not readable: {path}")
        if os.path.getsize(path) > MAX_STT_DECODED:
            raise VoiceError("too_large",
                             f"file exceeds {MAX_STT_DECODED} bytes")
        with open(path, "rb") as fh:
            data = fh.read(MAX_STT_DECODED + 1)
    else:
        raise VoiceError("invalid_argument",
                         "one of audio_base64 or file_path is required")
    if len(data) > MAX_STT_DECODED:
        raise VoiceError("too_large",
                         f"decoded audio exceeds {MAX_STT_DECODED} bytes")
    return data


# ── tools ────────────────────────────────────────────────────────────────────

def tts_synthesize(args: dict[str, Any]) -> dict[str, Any]:
    text = _require_text(args)
    engine = _require_engine(args)
    speed = _require_speed(args)
    target_rate = _require_target_rate(args)
    payload: dict[str, Any] = {"text": text, "speed": speed}
    if args.get("voice"):
        payload["voice"] = args["voice"]

    status, body, headers = _http(
        "POST", f"{_ENGINES[engine]}/synthesize",
        body=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        deadline_s=TTS_DEADLINE_S)
    if status == 400 and b"voice" in body.lower():
        raise VoiceError("unsupported_voice", body.decode(errors="replace")[:200])
    if status != 200:
        raise VoiceError("service_unavailable",
                         f"{engine} synthesize returned {status}: "
                         f"{body[:200].decode(errors='replace')}")
    declared = headers.get("x-sample-rate")
    if not declared or not declared.isdigit() \
            or not (8000 <= int(declared) <= 384_000):
        raise VoiceError("service_unavailable",
                         f"{engine} declared no plausible X-Sample-Rate ({declared!r})")
    rate = int(declared)
    pcm = body
    if not pcm or len(pcm) % 2:
        raise VoiceError("service_unavailable",
                         f"{engine} returned empty or odd-length PCM ({len(pcm)} bytes)")
    result: dict[str, Any] = {"engine": engine}
    if headers.get("x-engine-version"):
        result["engine_version"] = headers["x-engine-version"]
    # Duration cap BEFORE resampling: once the source passes, the resampled
    # output is bounded (≤120s × 48kHz × 2B ≈ 11.5 MiB), so ffmpeg's buffered
    # stdout cannot balloon past the cap it was about to be checked against.
    if len(pcm) / 2 / rate > MAX_WAV_DURATION_S:
        raise VoiceError("too_large",
                         f"synthesis exceeds the {MAX_WAV_DURATION_S}s duration cap")
    if target_rate and target_rate != rate:
        pcm, resampler = _resample(pcm, rate, target_rate)
        rate = target_rate
        result["resampler"] = resampler
    wav = wav_wrap(pcm, rate)
    result.update({
        "mime": "audio/wav", "sample_rate": rate, "channels": 1,
        "duration_ms": int(len(pcm) / 2 / rate * 1000),
    })
    result.update(_deliver(wav, args.get("response_mode", "auto"), args.get("out_dir")))
    return result


def tts_voices(args: dict[str, Any]) -> dict[str, Any]:
    wanted = [args["engine"]] if args.get("engine") else sorted(_ENGINES)
    if any(e not in _ENGINES for e in wanted):
        raise VoiceError("invalid_engine", f"engine must be one of {sorted(_ENGINES)}")
    engines: dict[str, Any] = {}
    for engine in wanted:
        try:
            status, body, _ = _http("GET", f"{_ENGINES[engine]}/voices",
                                    deadline_s=STREAM_DEADLINE_S)
            voices = json.loads(body).get("voices", []) if status == 200 else []
            engines[engine] = {"reachable": status == 200, "voices": voices}
        except VoiceError:
            engines[engine] = {"reachable": False, "voices": []}
    return {"engines": engines}


def stt_transcribe(args: dict[str, Any]) -> dict[str, Any]:
    data = _stt_input_wav(args)
    pcm, rate = wav_parse(data)
    status, body, _ = _http(
        "POST", f"{STT_URL}/transcribe", body=pcm,
        headers={"X-Sample-Rate": str(rate),
                 "Content-Type": "application/octet-stream"},
        deadline_s=STT_DEADLINE_S)
    if status != 200:
        raise VoiceError("service_unavailable",
                         f"stt returned {status}: {body[:200].decode(errors='replace')}")
    text = json.loads(body).get("text", "")
    return {"text": text, "duration_ms": int(len(pcm) / 2 / rate * 1000)}


# ── streaming STT: stateful resource protocol, backend semantics mirrored ────

_streams: dict[str, dict[str, Any]] = {}
_tombstones: dict[str, tuple[str, float]] = {}   # stream_id -> (reason, ts)


def _bury(stream_id: str, reason: str) -> None:
    while len(_tombstones) >= TOMBSTONE_MAX:
        _tombstones.pop(next(iter(_tombstones)))
    _tombstones[stream_id] = (reason, time.monotonic())


def _reap_streams() -> None:
    """Lazily drop handles the backend has already evicted (60s idle) or that
    exceeded the absolute lifetime — a dead backend session must never occupy
    MCP stream capacity."""
    now = time.monotonic()
    for sid, entry in list(_streams.items()):
        if now - entry["last_used"] > STREAM_IDLE_EXPIRY_S:
            _streams.pop(sid)
            _bury(sid, "expired")
        elif now - entry["created"] > STREAM_LIFETIME_S:
            try:
                # Best-effort with a SHORT deadline: cleanup runs inside some
                # OTHER call's budget and must not spend it (Codex — two slow
                # DELETEs here once meant 60s+ stream calls).
                _http("DELETE", f"{STT_URL}/stream/{entry['backend_sid']}",
                      deadline_s=2.0)
            except VoiceError:
                pass
            _streams.pop(sid)
            _bury(sid, "lifetime exceeded")
    for sid, (_, ts) in list(_tombstones.items()):
        if now - ts > TOMBSTONE_TTL_S:
            _tombstones.pop(sid)


def _stream_or_gone(stream_id: str) -> dict[str, Any]:
    _reap_streams()
    entry = _streams.get(stream_id)
    if entry is None:
        reason, _ = _tombstones.get(stream_id, ("unknown or expired", 0.0))
        # unknown-vs-expired is ONE code: after registry eviction the backend
        # cannot distinguish them either, and pretending otherwise would lie.
        raise VoiceError("stream_gone", f"stream {stream_id}: {reason}")
    return entry


def stt_stream_start(args: dict[str, Any]) -> dict[str, Any]:
    rate = args.get("sample_rate")
    if not isinstance(rate, int) or isinstance(rate, bool) \
            or not (8000 <= rate <= 384_000):
        raise VoiceError("invalid_argument", "sample_rate must be a plausible integer")
    _reap_streams()
    if len(_streams) >= MAX_STREAMS:
        raise VoiceError("stream_limit",
                         f"{MAX_STREAMS} concurrent streams (backend warning "
                         "threshold); finish or cancel one first")
    status, body, _ = _http("POST", f"{STT_URL}/stream/start", body=b"{}",
                            headers={"Content-Type": "application/json"},
                            deadline_s=STREAM_DEADLINE_S)
    if status != 200:
        raise VoiceError("service_unavailable",
                         f"stt stream/start returned {status}")
    parsed_start = json.loads(body)
    # The service names it session_id/id (server.py:641-644); accept the
    # stream_id spelling too so a stubbed or future backend also works.
    backend_sid = (parsed_start.get("session_id") or parsed_start.get("id")
                   or parsed_start.get("stream_id"))
    if not backend_sid:
        raise VoiceError("service_unavailable", "stt stream/start returned no id")
    stream_id = f"vstream-{uuid.uuid4().hex[:12]}"
    now = time.monotonic()
    _streams[stream_id] = {"backend_sid": backend_sid, "rate": rate,
                           "created": now, "last_used": now, "bytes": 0}
    return {"stream_id": stream_id, "idle_expiry_s": STREAM_IDLE_EXPIRY_S}


def stt_stream_feed(args: dict[str, Any]) -> dict[str, Any]:
    entry = _stream_or_gone(str(args.get("stream_id")))
    b64 = args.get("audio_base64")
    if not b64:
        raise VoiceError("invalid_argument", "audio_base64 is required")
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise VoiceError("invalid_argument", "audio_base64 is not valid base64")
    if len(data) > INLINE_WAV_BOUND:
        raise VoiceError("too_large",
                         f"per-feed decoded WAV bound is {INLINE_WAV_BOUND} bytes")
    pcm, rate = wav_parse(data)
    if rate != entry["rate"]:
        raise VoiceError("stream_rate_change",
                         f"stream opened at {entry['rate']} Hz; feed is {rate} Hz "
                         "(per-stream rate is immutable, matching the backend)")
    if entry["bytes"] + len(pcm) > STREAM_TOTAL_BYTES:
        raise VoiceError("too_large",
                         f"stream exceeded {STREAM_TOTAL_BYTES} total decoded bytes")
    stream_id = str(args.get("stream_id"))
    try:
        status, body, _ = _http(
            "POST", f"{STT_URL}/stream/{entry['backend_sid']}/feed", body=pcm,
            headers={"X-Sample-Rate": str(entry["rate"]),
                     "Content-Type": "application/octet-stream"},
            deadline_s=STREAM_DEADLINE_S)
    except VoiceError as exc:
        if exc.code == "backend_timeout":
            # Ambiguous: the backend may or may not have consumed this audio.
            # A retry against a live handle could DUPLICATE it in the
            # transcript, so the handle dies with the ambiguity (Codex).
            _streams.pop(stream_id, None)
            _bury(stream_id, "invalidated after an ambiguous feed timeout")
        raise
    if status == 404:
        _streams.pop(stream_id, None)
        _bury(stream_id, "expired")
        raise VoiceError("stream_gone", "backend expired the stream (60s idle)")
    if status != 200:
        raise VoiceError("service_unavailable", f"stt feed returned {status}")
    # Accounting only counts audio the backend ACCEPTED — a failed request
    # must not consume the budget.
    entry["bytes"] += len(pcm)
    entry["last_used"] = time.monotonic()
    try:
        parsed = json.loads(body)
    except ValueError:
        raise VoiceError("service_unavailable", "stt feed returned unparseable JSON")
    return {"ok": True,
            # The backend's stable agreed prefix, passed through untranslated.
            "committed_text": parsed.get("committed_text", ""),
            "pass_ran": bool(parsed.get("pass_ran", parsed.get("passes", 0)))}


def stt_stream_finish(args: dict[str, Any]) -> dict[str, Any]:
    stream_id = str(args.get("stream_id"))
    entry = _stream_or_gone(stream_id)
    status, body, _ = _http(
        "POST", f"{STT_URL}/stream/{entry['backend_sid']}/finish",
        body=b"", deadline_s=STREAM_DEADLINE_S)
    _streams.pop(stream_id, None)
    # Tombstone AFTER the status is known — 'finished' on a backend error
    # would be a false record the next stream_gone message repeats (Codex).
    if status == 404:
        _bury(stream_id, "expired")
        raise VoiceError("stream_gone", "backend expired the stream (60s idle)")
    if status != 200:
        _bury(stream_id, f"finish failed (backend {status})")
        raise VoiceError("service_unavailable", f"stt finish returned {status}")
    _bury(stream_id, "finished")
    try:
        return {"text": json.loads(body).get("text", "")}
    except ValueError:
        raise VoiceError("service_unavailable", "stt finish returned unparseable JSON")


def stt_stream_cancel(args: dict[str, Any]) -> dict[str, Any]:
    stream_id = str(args.get("stream_id"))
    entry = _stream_or_gone(stream_id)
    _streams.pop(stream_id, None)
    try:
        status, _, _ = _http("DELETE", f"{STT_URL}/stream/{entry['backend_sid']}",
                             deadline_s=STREAM_DEADLINE_S)
    except VoiceError as exc:
        _bury(stream_id, "cancelled (backend unreachable)")
        raise VoiceError("service_unavailable",
                         f"cancel could not reach the backend: {exc}")
    if status not in (200, 404):
        _bury(stream_id, f"cancel failed (backend {status})")
        raise VoiceError("service_unavailable", f"stt cancel returned {status}")
    _bury(stream_id, "cancelled")
    return {"ok": True}


def voice_health(args: dict[str, Any]) -> dict[str, Any]:
    """Reachability, deliberately — /health answers before models load, and
    Lux's post-start .verified gate can fail after /health says ok. probe:true
    adds a bounded readiness check."""
    services = {"kokoro": _ENGINES["kokoro"], "luxtts": _ENGINES["luxtts"],
                "stt": STT_URL}
    out: dict[str, Any] = {}
    for name, base in services.items():
        try:
            status, body, _ = _http("GET", f"{base}/health", deadline_s=5.0)
            out[name] = {"reachable": status == 200,
                         "detail": body[:200].decode(errors="replace")}
        except VoiceError as exc:
            out[name] = {"reachable": False, "detail": str(exc)}
    if args.get("probe"):
        for name in ("kokoro", "luxtts"):
            if not out[name]["reachable"]:
                out[name]["ready"] = False
                continue
            try:
                tts_synthesize({"text": "ready check", "engine": name})
                out[name]["ready"] = True
            except VoiceError as exc:
                out[name]["ready"] = False
                out[name]["detail"] = f"{exc.code}: {exc}"
        if out["stt"]["reachable"]:
            try:
                probe = wav_wrap(b"\x00\x00" * 1600, 16000)
                stt_transcribe({"audio_base64": base64.b64encode(probe).decode()})
                out["stt"]["ready"] = True
            except VoiceError as exc:
                out["stt"]["ready"] = False
                out["stt"]["detail"] = f"{exc.code}: {exc}"
        else:
            out["stt"]["ready"] = False
    return {"services": out}


_TOOL_FNS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "tts_synthesize": tts_synthesize,
    "tts_voices": tts_voices,
    "stt_transcribe": stt_transcribe,
    "stt_stream_start": stt_stream_start,
    "stt_stream_feed": stt_stream_feed,
    "stt_stream_finish": stt_stream_finish,
    "stt_stream_cancel": stt_stream_cancel,
    "voice_health": voice_health,
}

TOOLS = [
    {"name": "tts_synthesize",
     "description": "Synthesize speech via nano-claw's local Kokoro or LuxTTS. "
                    "Returns WAV inline (base64, ≤1 MiB decoded) or as a file "
                    "under MCP_VOICE_OUT_ROOT.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"},
         "engine": {"type": "string", "enum": ["kokoro", "luxtts"]},
         "voice": {"type": "string"},
         "speed": {"type": "number", "minimum": SPEED_MIN, "maximum": SPEED_MAX},
         "response_mode": {"type": "string", "enum": ["inline", "file", "auto"]},
         "out_dir": {"type": "string"},
         "target_rate": {"type": "integer", "minimum": TARGET_RATE_MIN,
                         "maximum": TARGET_RATE_MAX}},
         "required": ["text"]}},
    {"name": "tts_voices",
     "description": "List available voices per engine, with reachability.",
     "inputSchema": {"type": "object", "properties": {
         "engine": {"type": "string", "enum": ["kokoro", "luxtts"]}}}},
    {"name": "stt_transcribe",
     "description": "Transcribe one WAV (PCM16 mono) via the local Whisper "
                    "service. English only — the service hardcodes it.",
     "inputSchema": {"type": "object", "properties": {
         "audio_base64": {"type": "string"},
         "file_path": {"type": "string"}}}},
    {"name": "stt_stream_start",
     "description": "Open a streaming STT session (stateful resource protocol; "
                    "60s idle expiry mirrored from the backend).",
     "inputSchema": {"type": "object", "properties": {
         "sample_rate": {"type": "integer"}}, "required": ["sample_rate"]}},
    {"name": "stt_stream_feed",
     "description": "Feed one WAV chunk; returns the backend's committed_text "
                    "(stable agreed prefix).",
     "inputSchema": {"type": "object", "properties": {
         "stream_id": {"type": "string"}, "audio_base64": {"type": "string"}},
         "required": ["stream_id", "audio_base64"]}},
    {"name": "stt_stream_finish",
     "description": "Finish a stream and return the final transcript.",
     "inputSchema": {"type": "object", "properties": {
         "stream_id": {"type": "string"}}, "required": ["stream_id"]}},
    {"name": "stt_stream_cancel",
     "description": "Cancel a stream (proxies DELETE) so abandoned streams "
                    "don't wait out expiry.",
     "inputSchema": {"type": "object", "properties": {
         "stream_id": {"type": "string"}}, "required": ["stream_id"]}},
    {"name": "voice_health",
     "description": "Reachability of the three services; probe:true adds a "
                    "bounded readiness check.",
     "inputSchema": {"type": "object", "properties": {
         "probe": {"type": "boolean"}}}},
]


# ── transport (decision-core pattern + the line-size guard) ──────────────────

def _tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    payload = dict(payload)
    payload["schema_version"] = SCHEMA_VERSION
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fn = _TOOL_FNS.get(name)
    if fn is None:
        return _tool_result({"error": {"code": "invalid_argument",
                                       "message": f"unknown tool: {name}"}},
                            is_error=True)
    try:
        return _tool_result(fn(arguments))
    except VoiceError as exc:
        return _tool_result({"error": {"code": exc.code, "message": str(exc)}},
                            is_error=True)
    except Exception as exc:  # genuinely unexpected only
        return _tool_result({"error": {"code": "internal",
                                       "message": f"{type(exc).__name__}: {exc}"}},
                            is_error=True)


def _handle(request: Any) -> Optional[dict[str, Any]]:
    # Shape-check FIRST: a list request or non-object params reaching .get()
    # would kill the serve loop — the opposite of "never crashes" (Codex).
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "request must be an object"}}
    method = request.get("method", "")
    request_id = request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        result: Any = {"protocolVersion": PROTOCOL_VERSION,
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": SERVER_INFO}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        name = params.get("name")
        result = _call_tool(name if isinstance(name, str) else "", arguments)
    else:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _read_bounded_lines(raw, limit: int):
    """Yield (line_bytes, oversized) without ever holding more than ~limit.

    `for line in raw` allocates the WHOLE line before any length check runs —
    a huge unterminated line OOMs the process before the guard sees it
    (Codex). readline(limit+1) bounds the allocation; an over-limit read is
    drained to the next newline in bounded chunks and reported, not stored.
    """
    while True:
        line = raw.readline(limit + 1)
        if not line:
            return
        if len(line) > limit:
            while not line.endswith(b"\n"):
                line = raw.readline(limit)
                if not line:
                    break
            yield b"", True
            continue
        yield line, False


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    raw = getattr(stdin, "buffer", stdin)
    for line, oversized in _read_bounded_lines(raw, MAX_LINE_BYTES):
        if oversized:
            stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600,
                          "message": f"line exceeds {MAX_LINE_BYTES} bytes"}},
                sort_keys=True) + "\n")
            stdout.flush()
            continue
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            response: Optional[dict[str, Any]] = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                response = _handle(request)
            except Exception as exc:  # the loop survives anything a request does
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32603,
                                      "message": f"{type(exc).__name__}: {exc}"}}
        if response is not None:
            stdout.write(json.dumps(response, sort_keys=True) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
