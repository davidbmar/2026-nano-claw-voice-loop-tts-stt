import asyncio
import logging

import numpy as np

from voice import cost_ledger, phone
from voice.streaming_stt import (
    StreamingSTTResult,
    StreamingSTTSession,
)
from tools.phone_tap_report import _print_stt_timeline


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, one_shot_text="fallback words"):
        self.one_shot_text = one_shot_text
        self.posts = []
        self.closed = False

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse({"text": self.one_shot_text})

    async def aclose(self):
        self.closed = True


def _configure_phone(monkeypatch, *, dynamic=False):
    monkeypatch.setenv("NANO_CLAW_PHONE_STT_STREAM", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_DYNAMIC_ENDPOINT", "1" if dynamic else "0")
    monkeypatch.setenv("NANO_CLAW_PHONE_THINKING_CUE", "off")
    monkeypatch.setenv("NANO_CLAW_PHONE_CODEC", "pcmu")
    monkeypatch.setenv("NANO_CLAW_PHONE_VAD", "energy")
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_IN", "0")
    monkeypatch.setattr(phone, "_vad_mode", None)


def _fake_stream_class(results=(), *, finish_error=None):
    class FakeStream:
        instances = []

        def __init__(self, http, service_url, **kwargs):
            self.http = http
            self.service_url = service_url
            self.kwargs = kwargs
            self.feeds = []
            self.finishes = []
            self.closed = False
            self.results = list(results)
            type(self).instances.append(self)

        def feed_nowait(self, pcm):
            self.feeds.append(bytes(pcm))

        async def finish(self, *, keep_session=False):
            self.finishes.append(keep_session)
            if finish_error is not None:
                raise finish_error
            text = self.results.pop(0)
            return StreamingSTTResult(
                text=text,
                committed_chars=max(0, len(text) - 4),
                finish_ms=23.5,
                pass_count=2,
                duration_s=1.0,
            )

        async def close(self):
            self.closed = True

    return FakeStream


def test_reusable_client_batches_serially_and_reports_passes():
    class Service:
        def __init__(self):
            self.calls = []
            self.feed_sizes = []

        async def post(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("/stream/start"):
                return FakeResponse({"session_id": "stream-1"})
            if url.endswith("/feed"):
                self.feed_sizes.append(len(kwargs["content"]))
                number = len(self.feed_sizes)
                return FakeResponse(
                    {
                        "passes": [
                            {
                                "pass_count": number,
                                "window_ms": 700,
                                "ms": 10,
                            }
                        ]
                    }
                )
            return FakeResponse(
                {
                    "text": "hello",
                    "committed_chars": 3,
                    "finish_ms": 12,
                    "pass_count": 4,
                    "duration_s": 1.2,
                    "passes": [],
                }
            )

        async def delete(self, url):
            self.calls.append(url)
            return FakeResponse({"deleted": True})

    async def exercise():
        service = Service()
        passes = []
        session = StreamingSTTSession(
            service,
            "http://stt",
            sample_rate=1000,
            model_size="small",
            batch_ms=500,
            on_pass=passes.append,
        )
        session.feed_nowait(np.ones(1200, dtype=np.int16).tobytes())
        result = await session.finish()
        return service, passes, result

    service, passes, result = asyncio.run(exercise())
    assert service.feed_sizes == [1000, 1000, 400]
    assert [item["pass_count"] for item in passes] == [1, 2, 3]
    assert result.text == "hello"
    assert service.calls[0].endswith("/stream/start")
    assert service.calls[-1].endswith("/finish")


def test_flag_off_keeps_original_one_shot_request(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_STT_STREAM", "0")
    monkeypatch.setenv("NANO_CLAW_PHONE_CODEC", "l16")
    monkeypatch.setenv("NANO_CLAW_PHONE_STT_SIZE", "small")
    monkeypatch.setenv("STT_SERVICE_URL", "http://stt.test")
    pcm = b"\x01\x02\x03\x04"
    http = FakeHttp("one shot")
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.call_id = "flag-off"
    call.tap = None
    call._http = http

    assert asyncio.run(call._transcribe(pcm)) == "one shot"
    assert http.posts == [
        (
            "http://stt.test/transcribe",
            {
                "content": pcm,
                "headers": {
                    "Content-Type": "application/octet-stream",
                    "X-Sample-Rate": "16000",
                    "X-Model-Size": "small",
                },
            },
        )
    ]


def test_phone_tees_exact_endpointer_utterance_into_stream(
    monkeypatch,
):
    _configure_phone(monkeypatch)
    FakeStream = _fake_stream_class(["hello"])
    monkeypatch.setattr(phone, "StreamingSTTSession", FakeStream)

    async def exercise():
        call = phone.PhoneCall(
            object(), "cc-stream-tee", _flow=None, _flow_domain_id=None
        )
        tone = np.full(160, 2000, dtype=np.int16)
        silence = np.zeros(160, dtype=np.int16)
        utterance = None
        try:
            for frame in [tone] * 15 + [silence] * 35:
                utterance = call._feed_endpointer(frame, None) or utterance
            stream = FakeStream.instances[0]
            assert utterance is not None
            assert b"".join(stream.feeds) == utterance
        finally:
            await call.close()

    asyncio.run(exercise())


def test_stream_finish_text_reaches_the_turn_and_response_is_spoken(
    monkeypatch,
):
    _configure_phone(monkeypatch)
    FakeStream = _fake_stream_class(["caller words"])
    monkeypatch.setattr(phone, "StreamingSTTSession", FakeStream)
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)
    received = []
    spoken = []

    class Flow:
        greeting = "Hi."

        async def reply(self, text):
            received.append(text)
            return type(
                "Reply",
                (),
                {"text": "spoken answer", "outcome": "", "slots": {}, "done": False},
            )()

    async def exercise():
        call = phone.PhoneCall(
            object(), "cc-stream-turn", _flow=Flow(), _flow_domain_id=None
        )

        async def no_sync():
            return None

        async def capture_speech(text):
            spoken.append(text)

        call._sync_flow_mode_async = no_sync
        call.speak = capture_speech
        call._begin_stt_stream(np.ones(8000, dtype=np.int16).tobytes())
        try:
            await call._run_turn(np.ones(8000, dtype=np.int16).tobytes())
        finally:
            await call.close()

    asyncio.run(exercise())
    assert received == ["caller words"]
    assert spoken == ["spoken answer"]
    assert FakeStream.instances[0].finishes == [False]


def test_streamed_stt_tap_fields_include_pass_and_finish_telemetry(
    monkeypatch,
):
    _configure_phone(monkeypatch)
    FakeStream = _fake_stream_class(["caller words"])
    events = []

    class Tap:
        def event(self, name, **fields):
            events.append((name, fields))

    async def exercise():
        call = phone.PhoneCall.__new__(phone.PhoneCall)
        call.call_id = "cc-tap"
        call.tap = Tap()
        call.dynamic = False
        call._stt_stream_failed = False
        call._stt_stream = FakeStream(
            FakeHttp(), "http://stt", sample_rate=8000, model_size="small"
        )
        call._record_stt_pass(
            {"pass_count": 2, "window_ms": 1200, "ms": 44}
        )
        return await call._transcribe(b"\x00\x00")

    assert asyncio.run(exercise()) == "caller words"
    assert events[0] == (
        "stt_pass",
        {"pass_count": 2, "window_ms": 1200.0, "ms": 44.0},
    )
    name, fields = events[1]
    assert name == "stt_done"
    assert fields["streamed"] is True
    assert fields["committed_chars"] == len("caller words") - 4
    assert fields["finish_ms"] == 23.5


def test_stream_error_falls_back_once_and_turn_survives(
    monkeypatch,
    caplog,
):
    _configure_phone(monkeypatch)
    FakeStream = _fake_stream_class(finish_error=OSError("feed failed"))
    monkeypatch.setattr(phone, "StreamingSTTSession", FakeStream)
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)
    received = []
    spoken = []

    class Flow:
        greeting = "Hi."

        async def reply(self, text):
            received.append(text)
            return type(
                "Reply",
                (),
                {"text": "still answered", "outcome": "", "slots": {}, "done": False},
            )()

    async def exercise():
        call = phone.PhoneCall(
            object(), "cc-stream-fail", _flow=Flow(), _flow_domain_id=None
        )
        await call._http.aclose()
        http = FakeHttp("one-shot rescue")
        call._http = http

        async def no_sync():
            return None

        async def capture_speech(text):
            spoken.append(text)

        call._sync_flow_mode_async = no_sync
        call.speak = capture_speech
        call._begin_stt_stream(np.ones(8000, dtype=np.int16).tobytes())
        try:
            await call._run_turn(np.ones(8000, dtype=np.int16).tobytes())
        finally:
            await call.close()
        return http

    with caplog.at_level(logging.WARNING, logger="nano-claw.phone"):
        http = asyncio.run(exercise())
    assert received == ["one-shot rescue"]
    assert spoken == ["still answered"]
    assert sum(url.endswith("/transcribe") for url, _ in http.posts) == 1
    assert caplog.text.count("falling back to one-shot") == 1


def test_dynamic_continuation_reuses_same_stream_session(monkeypatch):
    _configure_phone(monkeypatch, dynamic=True)
    FakeStream = _fake_stream_class(
        ["tell me about", "tell me about Mars"]
    )
    monkeypatch.setattr(phone, "StreamingSTTSession", FakeStream)
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)
    received = []

    class Flow:
        greeting = "Hi."

        async def reply(self, text):
            received.append(text)
            return type(
                "Reply",
                (),
                {"text": "Mars answer", "outcome": "", "slots": {}, "done": False},
            )()

    async def exercise():
        call = phone.PhoneCall(
            object(), "cc-stream-continue", _flow=Flow(), _flow_domain_id=None
        )

        async def no_sync():
            return None

        async def no_speak(_text):
            return None

        call._sync_flow_mode_async = no_sync
        call.speak = no_speak
        initial = np.ones(8000, dtype=np.int16).tobytes()
        continuation = np.ones(5600, dtype=np.int16).tobytes()
        call._begin_stt_stream(initial)
        try:
            await call._run_turn(initial)
            assert call._stt_stream is FakeStream.instances[0]
            call._feed_stt_stream(continuation)
            await call._run_turn(initial + continuation)
        finally:
            await call.close()

    asyncio.run(exercise())
    assert len(FakeStream.instances) == 1
    stream = FakeStream.instances[0]
    assert stream.finishes == [True, True]
    assert stream.feeds == [
        np.ones(8000, dtype=np.int16).tobytes(),
        np.ones(5600, dtype=np.int16).tobytes(),
    ]
    assert stream.closed
    assert received == ["tell me about Mars"]


def test_continuation_audio_is_billed_once(monkeypatch):
    billed = []
    monkeypatch.setattr(
        cost_ledger,
        "add_units",
        lambda call_id, component, units, kind, model="": billed.append(
            (component, units, kind, model)
        ),
    )

    class Base:
        async def _transcribe(self, pcm):
            return "ok"

        async def _run_turn(self, pcm):
            return None

    fake_phone = type(
        "FakePhone",
        (),
        {
            "PhoneCall": Base,
            "phone_rate": staticmethod(lambda: 8000),
            "PROCESSING_CUE_SENTINEL": "\0cue\0",
            "_cfg": staticmethod(
                lambda name, default="": (
                    "small" if name == "NANO_CLAW_PHONE_STT_SIZE" else default
                )
            ),
        },
    )
    cost_ledger.install_phone_tracking(fake_phone, lambda: None)
    call = fake_phone.PhoneCall.__new__(fake_phone.PhoneCall)
    call.call_id = "bill-stream"
    call.flow = None
    call._tail_extensions = 0

    async def exercise():
        initial = np.ones(8000, dtype=np.int16).tobytes()  # 1.0 s
        await call._run_turn(initial)
        call._tail_extensions = 1
        continuation = np.ones(4000, dtype=np.int16).tobytes()  # +0.5 s
        await call._run_turn(initial + continuation)

    asyncio.run(exercise())
    stt_units = [item[1] for item in billed if item[0] == cost_ledger.STT]
    assert sum(stt_units) == 1.5
    assert all(item[3] == "whisper/small" for item in billed)


def test_tap_report_prints_incremental_stt_timeline(capsys):
    _print_stt_timeline(
        [
            {
                "event": "stt_pass",
                "pass_count": 2,
                "window_ms": 1250,
                "ms": 340,
            },
            {
                "event": "stt_done",
                "streamed": True,
                "committed_chars": 18,
                "finish_ms": 220,
                "ms": 225,
            },
        ]
    )
    output = capsys.readouterr().out
    assert "pass 2: window=1250 ms decode=340.0 ms" in output
    assert "committed=18 chars finish=220.0 ms" in output
