import asyncio
import json
import re
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web

from voice import server, tts, webrtc
from voice.audio.webrtc_audio_source import WebRTCAudioSource
from voice.phone_audio import resample_48k_to_16k
from voice.ws_audio import (
    FRAME_BYTES,
    WsAudioFormatError,
    WsAudioTransport,
    wire_format,
)


def run(coro):
    return asyncio.run(coro)


class FakeWebSocket:
    def __init__(self):
        self.binary = []
        self.sent = asyncio.Event()

    async def send_bytes(self, data):
        self.binary.append(bytes(data))
        self.sent.set()


class FakeAudioFrame:
    def __init__(self, samples):
        self._array = np.asarray(samples, dtype=np.int16).reshape(1, -1)
        self.samples = self._array.shape[1]
        self.sample_rate = 16000
        self.format = SimpleNamespace(name="s16")

    def to_ndarray(self):
        return self._array


class FakeTrack:
    def __init__(self, frame):
        self.frame = frame

    async def recv(self):
        if self.frame is None:
            raise EOFError
        frame, self.frame = self.frame, None
        return frame


class HandlerWebSocket:
    incoming = []
    last = None

    def __init__(self):
        self.headers = {}
        self.messages = []
        self.binary = []
        self.close_code = None
        self.closed = False
        self._incoming = list(self.incoming)
        type(self).last = self

    async def prepare(self, _request):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send_json(self, message):
        self.messages.append(message)

    async def send_bytes(self, data):
        self.binary.append(bytes(data))

    async def close(self, code=None, message=None):
        self.close_code = code
        self.closed = True


class FakeHandlerHttpClient:
    def __init__(self, **_kwargs):
        pass

    async def request(self, *_args, **_kwargs):
        return SimpleNamespace(status_code=200)

    async def aclose(self):
        return None


def test_ws_mic_uses_the_same_accumulator_as_a_webrtc_track():
    async def exercise():
        samples = np.arange(320, dtype=np.int16) - 160
        pcm = samples.tobytes()
        rtc_session = webrtc.Session()
        socket = FakeWebSocket()
        transport = WsAudioTransport(socket)
        ws_session = webrtc.Session(transport)
        try:
            rtc_session.start_recording()
            await rtc_session._recv_mic_audio(FakeTrack(FakeAudioFrame(samples)))

            transport.start_mic(wire_format()["mic"])
            ws_session.start_recording()
            transport.receive_mic_frame(pcm)

            assert rtc_session._mic_frames == [pcm]
            assert ws_session._mic_frames == rtc_session._mic_frames
        finally:
            await rtc_session.close()
            await ws_session.close()

    run(exercise())


def test_handler_binds_binary_audio_to_its_server_owned_session(monkeypatch):
    base_session = webrtc.Session

    class CaptureSession(base_session):
        last = None

        def __init__(self, audio_transport=None):
            super().__init__(audio_transport)
            type(self).last = self

    mic_start = {
        "type": "mic_audio_start",
        **wire_format()["mic"],
        "conversationId": "client-forced-id",
        "user_sub": "client-forced-user",
    }
    pcm = np.arange(320, dtype=np.int16).tobytes()
    HandlerWebSocket.incoming = [
        SimpleNamespace(type=web.WSMsgType.TEXT, data=json.dumps({"type": "hello"})),
        SimpleNamespace(type=web.WSMsgType.TEXT, data=json.dumps(mic_start)),
        SimpleNamespace(type=web.WSMsgType.TEXT, data=json.dumps({"type": "mic_start"})),
        SimpleNamespace(type=web.WSMsgType.BINARY, data=pcm),
    ]
    monkeypatch.setenv("NANO_CLAW_WS_AUDIO", "1")
    monkeypatch.setattr(webrtc, "Session", CaptureSession)
    monkeypatch.setattr(server.web, "WebSocketResponse", HandlerWebSocket)
    monkeypatch.setattr(server.httpx, "AsyncClient", FakeHandlerHttpClient)

    run(server.websocket_handler(object()))

    socket = HandlerWebSocket.last
    session = CaptureSession.last
    assert socket is not None and session is not None
    ack = next(message for message in socket.messages if message["type"] == "hello_ack")
    assert ack["wsAudio"] is True
    assert ack["wsAudioFormat"] == wire_format()
    assert any(message["type"] == "mic_audio_ready" for message in socket.messages)
    assert session._mic_frames == [pcm]
    assert re.fullmatch(r"voice-[0-9a-f]{32}", session.conversation_id)
    assert session.conversation_id != "client-forced-id"
    assert session.user_sub is None


def test_ws_mic_stt_uses_the_announced_native_rate(monkeypatch):
    captured = {}

    class FakeResponse:
        def json(self):
            return {"text": "native rate", "processing_ms": 7}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(webrtc.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    async def exercise():
        socket = FakeWebSocket()
        transport = WsAudioTransport(socket)
        session = webrtc.Session(transport)
        try:
            transport.start_mic(wire_format()["mic"])
            session.start_recording()
            transport.receive_mic_frame(np.zeros(320, dtype=np.int16).tobytes())
            text, duration, stt_ms = await session.stop_recording()
            assert (text, duration, stt_ms) == ("native rate", 0.02, 7)
            assert captured["headers"]["X-Sample-Rate"] == "16000"
            assert len(captured["content"]) == FRAME_BYTES
        finally:
            await session.close()

    run(exercise())


def test_agent_pcm_is_resampled_and_framed_to_the_socket(monkeypatch):
    pcm_48k = np.arange(960, dtype=np.int16).tobytes()
    monkeypatch.setattr(tts, "synthesize", lambda *_args: pcm_48k)

    async def exercise():
        socket = FakeWebSocket()
        transport = WsAudioTransport(socket)
        session = webrtc.Session(transport)
        expected = resample_48k_to_16k(
            np.frombuffer(pcm_48k, dtype=np.int16)
        ).tobytes()
        try:
            session.begin_stream()
            outbound_bytes = session.enqueue_chunk("test", "voice", 1.0)
            await asyncio.wait_for(socket.sent.wait(), timeout=1)
            assert socket.binary == [expected]
            assert len(socket.binary[0]) == FRAME_BYTES
            assert outbound_bytes == len(expected)
            await session.end_stream(outbound_bytes)
        finally:
            await session.close()

    run(exercise())


def test_mic_format_mismatch_and_invalid_frames_are_rejected():
    socket = FakeWebSocket()
    transport = WsAudioTransport(socket)
    session = webrtc.Session(transport)
    mismatched = wire_format()["mic"]
    mismatched["sampleRate"] = 48000

    with pytest.raises(WsAudioFormatError, match="unsupported mic audio format"):
        transport.start_mic(mismatched)
    with pytest.raises(WsAudioFormatError, match="before mic_audio_start"):
        transport.receive_mic_frame(b"\x00\x00")

    transport.start_mic(wire_format()["mic"])
    with pytest.raises(WsAudioFormatError, match="complete PCM16"):
        transport.receive_mic_frame(b"\x00")
    with pytest.raises(WsAudioFormatError, match="exactly 640 bytes"):
        transport.receive_mic_frame(b"\x00\x00")
    run(session.close())


def test_flag_off_constructs_the_original_webrtc_transport(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_WS_AUDIO", raising=False)
    assert server._ws_audio_enabled() is False

    async def exercise():
        session = webrtc.Session()
        try:
            assert session._audio_transport is None
            assert isinstance(session._audio_source, WebRTCAudioSource)
            assert session._pc is not None
            assert session._mic_sample_rate == webrtc.SAMPLE_RATE
            assert session._playback_sample_rate == webrtc.SAMPLE_RATE
        finally:
            await session.close()

    run(exercise())
