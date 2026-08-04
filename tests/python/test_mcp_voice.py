"""mcp-voice/server.py — the validator matrix, binary contract, and stream
lifecycle from the spec (~/.claude/plans/nano-claw-mcp-voice.md v0.3).

Every error code is reachable by a test here and none is spurious; backends
are stubbed by monkeypatching server._http, so nothing needs a model or a
socket. The inline/file/auto boundary is tested at decoded WAV sizes
1,048,576 and 1,048,578 — the nearest valid PCM16 step across the bound.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import sys
import wave
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("mcp_voice_server",
                                              _ROOT / "mcp-voice" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def wav_bytes(pcm: bytes, rate: int = 16000, channels: int = 1,
              width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(channels)
        fh.setsampwidth(width)
        fh.setframerate(rate)
        fh.writeframes(pcm)
    return buf.getvalue()


def b64wav(pcm: bytes, rate: int = 16000, **kw) -> str:
    return base64.b64encode(wav_bytes(pcm, rate, **kw)).decode()


class Backend:
    """Scripted _http replacement recording every request."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def __call__(self, method, url, body=None, headers=None, deadline_s=30.0):
        self.calls.append({"method": method, "url": url, "body": body,
                           "headers": headers or {}})
        for needle, resp in self.responses.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return 200, b"{}", {}


@pytest.fixture()
def backend(monkeypatch):
    be = Backend()
    monkeypatch.setattr(server, "_http", be)
    server._streams.clear()
    server._tombstones.clear()
    return be


def err(fn, args):
    with pytest.raises(server.VoiceError) as exc:
        fn(args)
    return exc.value.code


# ── validators ───────────────────────────────────────────────────────────────

class TestValidators:
    def test_empty_text(self, backend):
        assert err(server.tts_synthesize, {"text": "  "}) == "empty_text"
        assert err(server.tts_synthesize, {}) == "empty_text"

    def test_text_length_cap(self, backend):
        assert err(server.tts_synthesize,
                   {"text": "x" * (server.MAX_TEXT_CHARS + 1)}) == "invalid_argument"

    def test_invalid_engine(self, backend):
        assert err(server.tts_synthesize,
                   {"text": "hi", "engine": "gemini"}) == "invalid_engine"
        assert err(server.tts_voices, {"engine": "gemini"}) == "invalid_engine"

    @pytest.mark.parametrize("speed", [0.4, 2.1, 0, -1, "1", True, float("nan")])
    def test_speed_bounds(self, backend, speed):
        assert err(server.tts_synthesize,
                   {"text": "hi", "speed": speed}) == "invalid_argument"

    @pytest.mark.parametrize("rate", [7999, 48001, 1.5, "16000", True])
    def test_target_rate_bounds(self, backend, rate):
        assert err(server.tts_synthesize,
                   {"text": "hi", "target_rate": rate}) == "invalid_argument"

    def test_stt_source_conflict_and_absence(self, backend):
        assert err(server.stt_transcribe,
                   {"audio_base64": "aGk=", "file_path": "/x"}) == "audio_source_conflict"
        assert err(server.stt_transcribe, {}) == "invalid_argument"
        assert err(server.stt_transcribe,
                   {"audio_base64": "not-base64!!"}) == "invalid_argument"


# ── WAV boundary ─────────────────────────────────────────────────────────────

class TestWavContract:
    def test_wrap_parse_round_trip_24k_48k(self):
        for rate in (24000, 48000):
            pcm = b"\x01\x00" * 480
            frames, got = server.wav_parse(server.wav_wrap(pcm, rate))
            assert frames == pcm and got == rate

    @pytest.mark.parametrize("kw,label", [
        ({"channels": 2}, "stereo"),
        ({"width": 1}, "8-bit"),
    ])
    def test_malformed_wav_matrix(self, backend, kw, label):
        audio = base64.b64encode(wav_bytes(b"\x00\x00" * 100, **kw)).decode()
        assert err(server.stt_transcribe, {"audio_base64": audio}) == "malformed_wav", label

    def test_garbage_and_truncated(self, backend):
        garbage = base64.b64encode(b"RIFFnope").decode()
        assert err(server.stt_transcribe, {"audio_base64": garbage}) == "malformed_wav"
        whole = wav_bytes(b"\x00\x00" * 1000)
        cut = base64.b64encode(whole[: len(whole) - 300]).decode()
        assert err(server.stt_transcribe, {"audio_base64": cut}) == "malformed_wav"

    def test_duration_cap(self, backend):
        pcm = b"\x00\x00" * (8000 * (server.MAX_WAV_DURATION_S + 5))
        audio = base64.b64encode(wav_bytes(pcm, 8000)).decode()
        assert err(server.stt_transcribe, {"audio_base64": audio}) == "too_large"


# ── inline / file / auto boundary ────────────────────────────────────────────

def _synth_backend(be, pcm, rate=24000):
    be.responses["/synthesize"] = (200, pcm, {"x-sample-rate": str(rate)})


class TestDelivery:
    # 44-byte header: PCM of 1,048,532 sits exactly ON the bound; +2 crosses.
    AT = server.INLINE_WAV_BOUND - 44
    OVER = AT + 2

    def test_inline_exactly_at_bound(self, backend):
        _synth_backend(backend, b"\x00" * self.AT)
        out = server.tts_synthesize({"text": "hi"})
        assert "audio_base64" in out
        assert len(base64.b64decode(out["audio_base64"])) == server.INLINE_WAV_BOUND

    def test_auto_over_bound_without_out_dir_names_the_fix(self, backend):
        _synth_backend(backend, b"\x00" * self.OVER)
        with pytest.raises(server.VoiceError) as exc:
            server.tts_synthesize({"text": "hi"})
        assert exc.value.code == "too_large"
        assert "out_dir" in str(exc.value)

    def test_inline_mode_over_bound_is_too_large(self, backend):
        _synth_backend(backend, b"\x00" * self.OVER)
        assert err(server.tts_synthesize,
                   {"text": "hi", "response_mode": "inline"}) == "too_large"

    def test_file_mode_requires_out_dir(self, backend):
        _synth_backend(backend, b"\x00" * 2000)
        assert err(server.tts_synthesize,
                   {"text": "hi", "response_mode": "file"}) == "missing_out_dir"

    def test_auto_over_bound_with_out_dir_writes_a_file(self, backend, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(server, "OUT_ROOT", str(tmp_path))
        _synth_backend(backend, b"\x00" * self.OVER)
        out = server.tts_synthesize({"text": "hi", "out_dir": str(tmp_path / "runs")})
        path = Path(out["file_path"])
        assert path.exists() and path.name.startswith("tts-") and path.suffix == ".wav"
        assert path.stat().st_size == server.INLINE_WAV_BOUND + 2

    def test_out_dir_traversal_refused(self, backend, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "OUT_ROOT", str(tmp_path / "root"))
        _synth_backend(backend, b"\x00" * 2000)
        assert err(server.tts_synthesize,
                   {"text": "hi", "response_mode": "file",
                    "out_dir": str(tmp_path / "root" / ".." / "escape")}) == "unsafe_out_dir"

    def test_out_dir_symlink_escape_refused(self, backend, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        monkeypatch.setattr(server, "OUT_ROOT", str(root))
        _synth_backend(backend, b"\x00" * 2000)
        assert err(server.tts_synthesize,
                   {"text": "hi", "response_mode": "file",
                    "out_dir": str(root / "link")}) == "unsafe_out_dir"


# ── tts backend mapping ──────────────────────────────────────────────────────

class TestTtsBackend:
    def test_wraps_declared_rate_never_assumed(self, backend):
        _synth_backend(backend, b"\x01\x00" * 4800, rate=48000)
        out = server.tts_synthesize({"text": "hi", "engine": "luxtts"})
        assert out["sample_rate"] == 48000
        assert out["duration_ms"] == 100

    def test_missing_declared_rate_is_a_backend_fault(self, backend):
        backend.responses["/synthesize"] = (200, b"\x00\x00", {})
        assert err(server.tts_synthesize, {"text": "hi"}) == "service_unavailable"

    def test_odd_length_pcm_is_a_backend_fault(self, backend):
        backend.responses["/synthesize"] = (200, b"\x00\x00\x00",
                                            {"x-sample-rate": "24000"})
        assert err(server.tts_synthesize, {"text": "hi"}) == "service_unavailable"

    def test_400_naming_voice_maps_to_unsupported_voice(self, backend):
        backend.responses["/synthesize"] = (400, b'{"detail": "unknown voice zz"}', {})
        assert err(server.tts_synthesize,
                   {"text": "hi", "voice": "zz"}) == "unsupported_voice"

    def test_timeout_and_unreachable_map_distinctly(self, backend):
        backend.responses["/synthesize"] = server.VoiceError(
            "backend_timeout", "deadline")
        assert err(server.tts_synthesize, {"text": "hi"}) == "backend_timeout"
        backend.responses["/synthesize"] = server.VoiceError(
            "service_unavailable", "refused")
        assert err(server.tts_synthesize, {"text": "hi"}) == "service_unavailable"

    def test_resampler_absent(self, backend, monkeypatch):
        _synth_backend(backend, b"\x00\x00" * 100, rate=24000)
        monkeypatch.setattr(server.shutil, "which", lambda _: None)
        assert err(server.tts_synthesize,
                   {"text": "hi", "target_rate": 16000}) == "resampler_unavailable"


# ── streaming lifecycle ──────────────────────────────────────────────────────

def _stream_backend(be):
    be.responses["/stream/start"] = (200, b'{"stream_id": "b-1"}', {})
    be.responses["/feed"] = (200, b'{"committed_text": "hello", "pass_ran": true}', {})
    be.responses["/finish"] = (200, b'{"text": "hello world"}', {})


class TestStreams:
    def test_lifecycle_start_feed_finish(self, backend):
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        assert h["idle_expiry_s"] == 60
        fed = server.stt_stream_feed({"stream_id": h["stream_id"],
                                      "audio_base64": b64wav(b"\x00\x00" * 800)})
        assert fed["committed_text"] == "hello" and fed["pass_ran"] is True
        done = server.stt_stream_finish({"stream_id": h["stream_id"]})
        assert done["text"] == "hello world"
        assert err(server.stt_stream_feed,
                   {"stream_id": h["stream_id"], "audio_base64": b64wav(b"\x00\x00")}
                   ) == "stream_gone"

    def test_rate_is_immutable_per_stream(self, backend):
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        assert err(server.stt_stream_feed,
                   {"stream_id": h["stream_id"],
                    "audio_base64": b64wav(b"\x00\x00" * 100, rate=24000)}
                   ) == "stream_rate_change"

    def test_third_stream_hits_the_limit(self, backend):
        _stream_backend(backend)
        server.stt_stream_start({"sample_rate": 16000})
        server.stt_stream_start({"sample_rate": 16000})
        assert err(server.stt_stream_start,
                   {"sample_rate": 16000}) == "stream_limit"

    def test_lazy_reap_frees_capacity(self, backend):
        _stream_backend(backend)
        a = server.stt_stream_start({"sample_rate": 16000})
        server.stt_stream_start({"sample_rate": 16000})
        # Backend evicted `a` (60s idle) — the shim must not let the dead
        # handle occupy MCP capacity.
        server._streams[a["stream_id"]]["last_used"] -= server.STREAM_IDLE_EXPIRY_S + 1
        h = server.stt_stream_start({"sample_rate": 16000})
        assert h["stream_id"]
        assert err(server.stt_stream_feed,
                   {"stream_id": a["stream_id"], "audio_base64": b64wav(b"\x00\x00")}
                   ) == "stream_gone"

    def test_backend_404_is_stream_gone_with_tombstone(self, backend):
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        backend.responses["/feed"] = (404, b"", {})
        assert err(server.stt_stream_feed,
                   {"stream_id": h["stream_id"],
                    "audio_base64": b64wav(b"\x00\x00" * 100)}) == "stream_gone"
        # And the tombstone remembers why on the NEXT call too.
        with pytest.raises(server.VoiceError) as exc:
            server.stt_stream_feed({"stream_id": h["stream_id"],
                                    "audio_base64": b64wav(b"\x00\x00")})
        assert "expired" in str(exc.value)

    def test_cancel_proxies_delete(self, backend):
        _stream_backend(backend)
        backend.responses["/stream/b-1"] = (200, b"{}", {})
        h = server.stt_stream_start({"sample_rate": 16000})
        assert server.stt_stream_cancel({"stream_id": h["stream_id"]}) == {"ok": True}
        assert any(c["method"] == "DELETE" for c in backend.calls)
        assert err(server.stt_stream_cancel,
                   {"stream_id": h["stream_id"]}) == "stream_gone"

    def test_per_feed_and_total_caps(self, backend):
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        big = base64.b64encode(b"\x00" * (server.INLINE_WAV_BOUND + 10)).decode()
        assert err(server.stt_stream_feed,
                   {"stream_id": h["stream_id"], "audio_base64": big}) == "too_large"
        server._streams[h["stream_id"]]["bytes"] = server.STREAM_TOTAL_BYTES
        assert err(server.stt_stream_feed,
                   {"stream_id": h["stream_id"],
                    "audio_base64": b64wav(b"\x00\x00" * 100)}) == "too_large"


# ── transport ────────────────────────────────────────────────────────────────

def _rpc(lines: list[str]) -> list[dict]:
    stdin = io.BytesIO(("\n".join(lines) + "\n").encode())
    stdin_wrapper = type("W", (), {"buffer": stdin})()
    out = io.StringIO()
    server.serve(stdin=stdin_wrapper, stdout=out)
    return [json.loads(l) for l in out.getvalue().splitlines()]


class TestTransport:
    def test_initialize_and_tools_list(self):
        got = _rpc([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})])
        assert got[0]["result"]["serverInfo"]["name"] == "nano-claw-voice"
        names = {t["name"] for t in got[1]["result"]["tools"]}
        assert {"tts_synthesize", "stt_transcribe", "stt_stream_start",
                "voice_health"} <= names

    def test_line_size_cap_rejected_before_decode(self, monkeypatch):
        monkeypatch.setattr(server, "MAX_LINE_BYTES", 128)
        payload = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping",
                              "pad": "x" * 500})
        got = _rpc([payload])
        assert got[0]["error"]["code"] == -32600
        assert "exceeds" in got[0]["error"]["message"]

    def test_parse_error_and_unknown_tool(self, backend):
        got = _rpc(["{nope",
                    json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "no_such_tool", "arguments": {}}})])
        assert got[0]["error"]["code"] == -32700
        payload = json.loads(got[1]["result"]["content"][0]["text"])
        assert payload["error"]["code"] == "invalid_argument"

    def test_tool_error_carries_code_and_schema_version(self, backend):
        got = _rpc([json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                "params": {"name": "tts_synthesize",
                                           "arguments": {"text": ""}}})])
        result = got[0]["result"]
        assert result["isError"] is True
        payload = result["structuredContent"]
        assert payload["error"]["code"] == "empty_text"
        assert payload["schema_version"] == "1.0"


# ── health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_reachability_not_readiness(self, backend):
        backend.responses["/health"] = (200, b'{"status": "ok"}', {})
        out = server.voice_health({})
        assert all(s["reachable"] for s in out["services"].values())

    def test_unreachable_is_truthful(self, backend):
        backend.responses["/health"] = server.VoiceError(
            "service_unavailable", "refused")
        out = server.voice_health({})
        assert not any(s["reachable"] for s in out["services"].values())


# ── codex round 1 folds (2026-08-04) ─────────────────────────────────────────


class TestTransportSurvival:
    def test_malformed_shapes_do_not_kill_the_loop(self, backend):
        """A list request, list params, or list arguments must produce an
        error response — not an AttributeError that ends serve() (Codex F3)."""
        got = _rpc([
            json.dumps([1, 2, 3]),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": ["not", "a", "dict"]}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "voice_health", "arguments": [1]}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        ])
        assert got[0]["error"]["code"] == -32600
        assert got[-1]["result"] == {}, "the loop must survive to serve later requests"

    def test_unterminated_giant_line_is_bounded_not_allocated(self, monkeypatch):
        """readline(limit+1) bounds the allocation; `for line in raw` would
        have materialized the whole line before any check (Codex F2)."""
        monkeypatch.setattr(server, "MAX_LINE_BYTES", 64)
        stdin = type("W", (), {"buffer": io.BytesIO(
            b"x" * 500 + b"\n" + json.dumps(
                {"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode() + b"\n")})()
        out = io.StringIO()
        server.serve(stdin=stdin, stdout=out)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        assert lines[0]["error"]["code"] == -32600
        assert lines[1]["result"] == {}


class TestStreamTruthfulness:
    def test_failed_feed_does_not_consume_the_budget(self, backend):
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        backend.responses["/feed"] = (500, b"", {})
        with pytest.raises(server.VoiceError):
            server.stt_stream_feed({"stream_id": h["stream_id"],
                                    "audio_base64": b64wav(b"\x00\x00" * 800)})
        assert server._streams[h["stream_id"]]["bytes"] == 0, \
            "audio the backend never accepted must not count (Codex F6)"

    def test_ambiguous_feed_timeout_invalidates_the_handle(self, backend):
        """The backend may or may not have consumed the audio; a retry could
        duplicate it in the transcript, so the handle dies with the ambiguity."""
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        backend.responses["/feed"] = server.VoiceError("backend_timeout", "slow")
        with pytest.raises(server.VoiceError):
            server.stt_stream_feed({"stream_id": h["stream_id"],
                                    "audio_base64": b64wav(b"\x00\x00" * 800)})
        with pytest.raises(server.VoiceError) as exc:
            server.stt_stream_feed({"stream_id": h["stream_id"],
                                    "audio_base64": b64wav(b"\x00\x00")})
        assert exc.value.code == "stream_gone"
        assert "ambiguous" in str(exc.value)

    def test_finish_backend_error_is_not_recorded_as_finished(self, backend):
        _stream_backend(backend)
        h = server.stt_stream_start({"sample_rate": 16000})
        backend.responses["/finish"] = (500, b"", {})
        with pytest.raises(server.VoiceError) as exc:
            server.stt_stream_finish({"stream_id": h["stream_id"]})
        assert exc.value.code == "service_unavailable"
        with pytest.raises(server.VoiceError) as gone:
            server.stt_stream_finish({"stream_id": h["stream_id"]})
        assert "finish failed" in str(gone.value), \
            "the tombstone must say what happened, not claim success (Codex F7)"

    def test_cancel_reports_backend_refusal(self, backend):
        _stream_backend(backend)
        backend.responses["/stream/b-1"] = (500, b"", {})
        h = server.stt_stream_start({"sample_rate": 16000})
        with pytest.raises(server.VoiceError) as exc:
            server.stt_stream_cancel({"stream_id": h["stream_id"]})
        assert exc.value.code == "service_unavailable"

    def test_lifetime_reap_uses_a_short_cleanup_deadline(self, backend):
        """Cleanup runs inside some OTHER call's budget: its DELETE gets 2s,
        never the full stream deadline (Codex F8)."""
        _stream_backend(backend)
        backend.responses["/stream/b-1"] = (200, b"{}", {})
        h = server.stt_stream_start({"sample_rate": 16000})
        server._streams[h["stream_id"]]["created"] -= server.STREAM_LIFETIME_S + 1

        deadlines = []
        original = backend.__call__

        def spy(method, url, body=None, headers=None, deadline_s=30.0):
            if method == "DELETE":
                deadlines.append(deadline_s)
            return original(method, url, body=body, headers=headers,
                            deadline_s=deadline_s)

        server._http = spy
        server._reap_streams()
        assert deadlines == [2.0]

    def test_tombstones_are_bounded(self, backend):
        for i in range(server.TOMBSTONE_MAX + 8):
            server._bury(f"s-{i}", "test")
        assert len(server._tombstones) <= server.TOMBSTONE_MAX


class TestBackendProtocol:
    def test_implausible_declared_rate_is_a_backend_fault(self, backend):
        backend.responses["/synthesize"] = (200, b"\x00\x00", {"x-sample-rate": "0"})
        assert err(server.tts_synthesize, {"text": "hi"}) == "service_unavailable"

    def test_empty_pcm_is_a_backend_fault(self, backend):
        backend.responses["/synthesize"] = (200, b"", {"x-sample-rate": "24000"})
        assert err(server.tts_synthesize, {"text": "hi"}) == "service_unavailable"

    def test_duration_cap_applies_before_resampling(self, backend, monkeypatch):
        """The cap must reject on the SOURCE audio so ffmpeg never buffers an
        unbounded expansion (Codex F10)."""
        called = []
        monkeypatch.setattr(server, "_resample",
                            lambda *a: called.append(1) or (b"", {}))
        pcm = b"\x00\x00" * (24000 * (server.MAX_WAV_DURATION_S + 5))
        _synth_backend(backend, pcm, rate=24000)
        assert err(server.tts_synthesize,
                   {"text": "hi", "target_rate": 16000}) == "too_large"
        assert not called


class TestRealSocketTimeouts:
    def test_slow_headers_map_to_backend_timeout_within_deadline(self, monkeypatch):
        """A real socket pin: the deadline governs the wait for HEADERS too,
        and one slow response cannot stretch past the overall budget (Codex F1).
        The unit stubs can't see this — this test owns it."""
        import http.server
        import threading
        import time as _time

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                _time.sleep(3)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Slow)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            start = __import__("time").monotonic()
            with pytest.raises(server.VoiceError) as exc:
                server._http("GET",
                             f"http://127.0.0.1:{httpd.server_port}/x",
                             deadline_s=0.5)
            elapsed = __import__("time").monotonic() - start
            assert exc.value.code == "backend_timeout"
            assert elapsed < 2.5, "the deadline must bound the header wait"
        finally:
            httpd.shutdown()
