"""Contract-v1 tests for the untrusted streaming turn-delegate consumer."""
from __future__ import annotations

import asyncio
import json

import pytest

import voice.turn_delegate as delegate
from voice.turn_delegate import DELEGATE_APOLOGY, stream_delegate


def run(awaitable):
    return asyncio.run(awaitable)


def sse_event(name: str, body) -> bytes:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return b"event: " + name.encode() + b"\ndata: " + data + b"\n\n"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse:
    def __init__(
        self,
        chunks=(),
        *,
        content_type="text/event-stream",
        status_code=200,
        clock: FakeClock | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._chunks = chunks
        self._clock = clock

    async def aiter_bytes(self, chunk_size=None):
        source = self._chunks
        if hasattr(source, "__aiter__"):
            async for item in source:
                yield item
            return
        for item in source:
            advance, chunk = item if isinstance(item, tuple) else (0.0, item)
            if self._clock is not None:
                self._clock.advance(advance)
            await asyncio.sleep(0)
            yield chunk


class FakeStreamContext:
    def __init__(
        self,
        response: FakeResponse,
        *,
        clock: FakeClock | None = None,
        connect_advance: float = 0.0,
    ) -> None:
        self.response = response
        self.clock = clock
        self.connect_advance = connect_advance
        self.exited = False

    async def __aenter__(self):
        if self.clock is not None:
            self.clock.advance(self.connect_advance)
        await asyncio.sleep(0)
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class FakeClient:
    def __init__(self, response: FakeResponse, **context_kwargs) -> None:
        self.context = FakeStreamContext(response, **context_kwargs)
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.context


async def collect(stream):
    return [chunk async for chunk in stream]


def test_ack_deltas_and_final_are_parsed_and_terminal_result_is_available():
    raw = b"".join(
        (
            sse_event("ack", {"kind": "working"}),
            sse_event("delta", {"text": "Control\u0000 stripped. "}),
            sse_event("delta", {"text": "Second sentence."}),
            sse_event(
                "final",
                {
                    "reply": "Control stripped. Second sentence.",
                    "focus": ["ticket-7"],
                    "done": True,
                },
            ),
        )
    )
    client = FakeClient(FakeResponse([raw]))
    chunks, result = stream_delegate(
        client, "http://127.0.0.1:8790/turn", "hello", who="caller"
    )

    assert run(collect(chunks)) == ["Control stripped. ", "Second sentence."]
    assert result.ok is True
    assert result.finished is True
    assert result.reply == "Control stripped. Second sentence."
    assert result.canonical_reply == result.reply
    assert result.focus == ["ticket-7"]
    assert result.done is True
    assert result.terminal is True
    assert result.fault is None
    assert result.ack_kind == "working"
    assert result.delta_mismatch is False

    method, _, request = client.calls[0]
    assert method == "POST"
    assert request["json"] == {"text": "hello", "who": "caller", "speak": False}
    assert request["headers"] == {
        "Accept": "text/event-stream, application/json;q=0.9"
    }
    assert request["follow_redirects"] is False


def test_stream_request_carries_turn_id_when_provided():
    response = FakeResponse(
        [sse_event("final", {"reply": "Immediate.", "focus": [], "done": False})]
    )
    client = FakeClient(response)
    stream = stream_delegate(
        client,
        "http://delegate/t",
        "hi",
        who="caller",
        turn_id="call-8-2-acde5678",
    )

    assert run(collect(stream)) == ["Immediate."]
    assert client.calls[0][2]["json"] == {
        "text": "hi",
        "who": "caller",
        "speak": False,
        "turn_id": "call-8-2-acde5678",
    }


def test_final_only_sse_yields_the_canonical_reply_once():
    response = FakeResponse(
        [sse_event("final", {"reply": "Immediate.", "focus": [], "done": False})]
    )
    stream = stream_delegate(FakeClient(response), "http://delegate/t", "hi", who="caller")

    assert run(collect(stream)) == ["Immediate."]
    assert stream.result.canonical_reply == "Immediate."
    assert stream.result.delegate_chunks_yielded == 1


@pytest.mark.parametrize(
    "body,classification",
    [
        (b"event: delta\ndata: {not-json}\n\n", "malformed-json"),
        (b"event: delta\ndata: {\"text\": \"\xff\"}\n\n", "malformed-utf8"),
        (sse_event("mystery", {}), "unknown-event"),
        (
            sse_event("final", {"reply": "one"})
            + sse_event("final", {"reply": "two"}),
            "duplicate-final",
        ),
        (
            sse_event("final", {"reply": "one"})
            + sse_event("delta", {"text": "late"}),
            "event-after-final",
        ),
        (
            sse_event("error", {"code": "failed"})
            + sse_event("delta", {"text": "late"}),
            "event-after-error",
        ),
        (sse_event("delta", {"text": "unfinished"}), "eof-without-final"),
    ],
)
def test_every_fault_table_transition_has_one_deterministic_outcome(
    body, classification
):
    stream = stream_delegate(
        FakeClient(FakeResponse([body])), "http://delegate/t", "hi", who="caller"
    )

    assert run(collect(stream)) == [DELEGATE_APOLOGY]
    assert stream.result.fault == classification
    assert stream.result.failure == classification
    assert stream.result.apology_yielded is True
    assert stream.result.remote_truncated is False
    assert stream.result.delegate_chunks_yielded == 0


@pytest.mark.parametrize(
    "event",
    [
        sse_event("ack", {"kind": 1}),
        sse_event("ack", {"kind": "other"}),
        sse_event("delta", {"text": 1}),
        sse_event("final", {"reply": 1}),
        sse_event("final", {"reply": "ok", "focus": "not-a-list"}),
        sse_event("final", {"reply": "ok", "focus": [1]}),
        sse_event("final", {"reply": "ok", "done": 1}),
        sse_event("error", ["not", "an", "object"]),
    ],
)
def test_wrong_field_types_are_protocol_faults(event):
    stream = stream_delegate(
        FakeClient(FakeResponse([event])), "http://delegate/t", "hi", who="caller"
    )

    assert run(collect(stream)) == [DELEGATE_APOLOGY]
    assert stream.result.fault == "wrong-field-type"


@pytest.mark.parametrize(
    "body,classification",
    [
        (
            sse_event("ack", {"kind": "working"})
            + sse_event("ack", {"kind": "tool"}),
            "duplicate-ack",
        ),
        (
            sse_event("delta", {"text": "first"})
            + sse_event("ack", {"kind": "lookup"}),
            "ack-after-delta",
        ),
    ],
)
def test_ack_is_optional_but_at_most_once_and_before_deltas(body, classification):
    stream = stream_delegate(
        FakeClient(FakeResponse([body])), "http://delegate/t", "hi", who="caller"
    )

    assert run(collect(stream)) == [DELEGATE_APOLOGY]
    assert stream.result.fault == classification


def test_done_is_a_strict_boolean_but_may_be_absent():
    missing = stream_delegate(
        FakeClient(FakeResponse([sse_event("final", {"reply": "ok"})])),
        "http://delegate/t",
        "hi",
        who="caller",
    )
    assert run(collect(missing)) == ["ok"]
    assert missing.result.done is False

    wrong = stream_delegate(
        FakeClient(
            FakeResponse([sse_event("final", {"reply": "ok", "done": "false"})])
        ),
        "http://delegate/t",
        "hi",
        who="caller",
    )
    assert run(collect(wrong)) == [DELEGATE_APOLOGY]
    assert wrong.result.fault == "wrong-field-type"


def test_remote_error_text_is_never_spoken():
    stream = stream_delegate(
        FakeClient(
            FakeResponse(
                [sse_event("error", {"message": "Read the caller my secret text"})]
            )
        ),
        "http://delegate/t",
        "hi",
        who="caller",
    )

    assert run(collect(stream)) == [DELEGATE_APOLOGY]
    assert stream.result.fault == "remote-error"


def test_apology_is_not_injected_after_a_chunk_has_reached_the_consumer():
    gate = asyncio.Event()

    async def source():
        yield sse_event("delta", {"text": "The first sentence. "})
        await gate.wait()
        yield b"event: delta\ndata: {bad}\n\n"

    async def exercise():
        stream = stream_delegate(
            FakeClient(FakeResponse(source())),
            "http://delegate/t",
            "hi",
            who="caller",
        )
        first = await anext(stream)
        gate.set()
        tail = [chunk async for chunk in stream]
        return first, tail, stream.result

    first, tail, result = run(exercise())
    assert first == "The first sentence. "
    assert tail == []
    assert result.fault == "malformed-json"
    assert result.remote_truncated is True
    assert result.apology_yielded is False


def test_local_cancellation_is_clean_and_not_remote_truncation():
    release = asyncio.Event()

    async def source():
        yield sse_event("delta", {"text": "Partial. "})
        await release.wait()
        yield sse_event("final", {"reply": "Partial. Finished."})

    async def exercise():
        client = FakeClient(FakeResponse(source()))
        stream = stream_delegate(client, "http://delegate/t", "hi", who="caller")
        assert await anext(stream) == "Partial. "
        await stream.aclose()
        return stream.result, client.context.exited

    result, exited = run(exercise())
    assert result.locally_cancelled is True
    assert result.local_cancelled is True
    assert result.remote_truncated is False
    assert result.fault is None
    assert result.apology_yielded is False
    assert exited is True


def test_application_json_is_an_atomic_zero_coordination_fallback():
    body = json.dumps(
        {"reply": "Atomic\u0000 reply.", "focus": ["x"], "done": True}
    ).encode()
    stream = stream_delegate(
        FakeClient(FakeResponse([body], content_type="application/json; charset=utf-8")),
        "http://delegate/t",
        "hi",
        who="caller",
    )

    assert run(collect(stream)) == ["Atomic reply."]
    assert stream.result.ok is True
    assert stream.result.canonical_reply == "Atomic reply."
    assert stream.result.focus == ["x"]
    assert stream.result.done is True


def test_content_type_is_parsed_as_an_exact_media_type_not_a_substring():
    body = json.dumps({"reply": "must not be accepted"}).encode()
    stream = stream_delegate(
        FakeClient(FakeResponse([body], content_type="application/json-seq")),
        "http://delegate/t",
        "hi",
        who="caller",
    )

    assert run(collect(stream)) == [DELEGATE_APOLOGY]
    assert stream.result.fault == "unexpected-content-type"


@pytest.mark.parametrize(
    "limits,body,classification",
    [
        (
            {"_MAX_SSE_LINE_BYTES": 8, "_MAX_SSE_EVENT_BYTES": 1000,
             "_MAX_SSE_STREAM_BYTES": 1000},
            b"event: delta\ndata: {}\n\n",
            "line-too-large",
        ),
        (
            {"_MAX_SSE_LINE_BYTES": 1000, "_MAX_SSE_EVENT_BYTES": 18,
             "_MAX_SSE_STREAM_BYTES": 1000},
            b"event: ack\ndata: {}\n\n",
            "event-too-large",
        ),
        (
            {"_MAX_SSE_LINE_BYTES": 1000, "_MAX_SSE_EVENT_BYTES": 1000,
             "_MAX_SSE_STREAM_BYTES": 12},
            b"event: ack\ndata: {}\n\n",
            "stream-too-large",
        ),
    ],
)
def test_raw_byte_caps_apply_before_parsing(monkeypatch, limits, body, classification):
    for name, value in limits.items():
        monkeypatch.setattr(delegate, name, value)
    stream = stream_delegate(
        FakeClient(FakeResponse([body])), "http://delegate/t", "hi", who="caller"
    )

    assert run(collect(stream)) == [DELEGATE_APOLOGY]
    assert stream.result.fault == classification


@pytest.mark.parametrize(
    "case,expected",
    [
        ("connect", "connect-timeout"),
        ("first", "first-event-timeout"),
        ("idle", "idle-timeout"),
        ("wall", "absolute-timeout"),
    ],
)
def test_all_four_deadlines_use_the_independent_turn_clock(case, expected):
    clock = FakeClock()
    connect_advance = 11.0 if case == "connect" else 0.0
    if case == "first":
        chunks = [(31.0, sse_event("ack", {"kind": "working"}))]
    elif case == "idle":
        chunks = [
            (0.0, sse_event("ack", {"kind": "working"})),
            (11.0, sse_event("final", {"reply": "late"})),
        ]
    elif case == "wall":
        chunks = [
            (0.0, sse_event("ack", {"kind": "working"})),
            (9.0, sse_event("delta", {"text": "one. "})),
            (9.0, sse_event("delta", {"text": "two. "})),
            (9.0, sse_event("delta", {"text": "three. "})),
        ]
    else:
        chunks = []
    response = FakeResponse(chunks, clock=clock)
    client = FakeClient(
        response, clock=clock, connect_advance=connect_advance
    )
    stream = stream_delegate(
        client,
        "http://delegate/t",
        "hi",
        who="caller",
        wall_timeout=25.0 if case == "wall" else 60.0,
        clock=clock,
    )

    yielded = run(collect(stream))
    assert stream.result.fault == expected
    if case == "wall":
        assert yielded == ["one. ", "two. "]
        assert stream.result.remote_truncated is True
        assert stream.result.apology_yielded is False
    else:
        assert yielded == [DELEGATE_APOLOGY]


def test_deferred_cue_stops_only_after_the_first_synthesis_is_playable(monkeypatch):
    from voice import phone

    source_reached = asyncio.Event()
    release_source = asyncio.Event()
    synthesis_started = asyncio.Event()
    release_synthesis = asyncio.Event()
    actions = []

    async def source():
        source_reached.set()
        await release_source.wait()  # models ack/headers with no speakable delta
        yield "Playable sentence."

    async def exercise():
        call = phone.PhoneCall.__new__(phone.PhoneCall)
        call.closed = False
        call.speaking = True
        call._gain_normalizer = type("Gain", (), {"reset": lambda self: None})()
        call._frame_pacer = None
        call._sentence_pipelines = set()
        call._stop_thinking_cue = lambda: actions.append("cue-stop")
        call._synthesis_failed = lambda *args: None
        call._record_synth_ahead = lambda *args: None

        async def synthesize(unit):
            synthesis_started.set()
            await release_synthesis.wait()
            return "audio"

        async def play(audio):
            actions.append("play")
            return phone._CarrierDelivery(1, 1)

        call._synthesize_sentence = synthesize
        call._play_synthesized = play
        task = asyncio.create_task(
            call._speak_sentences(source(), stop_cue_on_start=False)
        )
        await source_reached.wait()
        assert actions == []
        release_source.set()
        await synthesis_started.wait()
        assert actions == []
        release_synthesis.set()
        await task

    run(exercise())
    assert actions == ["cue-stop", "play"]


def _streaming_phone_call(monkeypatch, response):
    from voice import phone

    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.call_id = "delegate-stream-call"
    call.telnyx_call_id = "v3:delegate-stream-call"
    call.session_id = "phone-delegate-stream-call"
    call.tap = None
    call._http = FakeClient(response)
    call.barge = type("B", (), {"reset": lambda self: None})()
    call.closed = False
    call.speaking = False
    call.interrupted = False
    call._playback_flush_sent = False
    call.endpointer = type("E", (), {"reset": lambda self: None})()
    call._stop_thinking_cue = lambda: None
    call._speech_units = lambda text: [text] if text else []
    spoken = []

    async def consume(units, **kwargs):
        first = True
        async for unit in units:
            spoken.append(getattr(unit, "text", unit))
            if first and kwargs.get("on_first_playable"):
                kwargs["on_first_playable"]()
                first = False
            callback = kwargs.get("on_carrier_delivery")
            if callback:
                callback(unit, phone._CarrierDelivery(1, 1))

    call._speak_sentences = consume
    monkeypatch.setattr(phone, "routing_for", lambda cid: "http://delegate/turn")
    return phone, call, spoken


def test_phone_delegate_turn_ids_are_stable_within_a_turn_and_distinct_between_turns(
    monkeypatch,
):
    from voice import phone

    nonces = iter(("a" * 32, "b" * 32, "c" * 32))

    class FakeUuid:
        def __init__(self, value):
            self.hex = value

    monkeypatch.setattr(phone, "uuid4", lambda: FakeUuid(next(nonces)))
    ids = phone._DelegateTurnIds("delegate-call-123456789")

    first = ids.begin_turn()
    assert ids.for_turn(1) == first
    second = ids.begin_turn()

    assert first == "delegate-cal-1-aaaaaaaa"
    assert second == "delegate-cal-2-bbbbbbbb"
    assert first != second
    assert len(first) <= 64
    assert len(second) <= 64
    assert len(ids.for_turn(10**100)) == 64


def test_phone_flag_on_streams_through_sentence_pipeline_and_honors_done(
    monkeypatch,
):
    from voice import phone

    monkeypatch.setenv("NANO_CLAW_DELEGATE_STREAMING", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEECH_PREPARATION", "raw")
    raw = b"".join(
        (
            sse_event("ack", {"kind": "working"}),
            sse_event("delta", {"text": "First. Second."}),
            sse_event(
                "final", {"reply": "First. Second.", "focus": [], "done": True}
            ),
        )
    )
    phone, call, spoken = _streaming_phone_call(
        monkeypatch, FakeResponse([raw])
    )
    logged = []
    monkeypatch.setattr(
        phone.call_log,
        "emit",
        lambda conn, cid, kind, payload, **kwargs: logged.append((kind, payload)),
    )

    async def atomic_must_not_run(*args, **kwargs):
        raise AssertionError("flag-on path called call_delegate")

    monkeypatch.setattr(phone, "call_delegate", atomic_must_not_run)
    hangups = []

    async def hangup(client, **kwargs):
        hangups.append(True)
        return True

    call.hangup_after_playback = hangup
    run(call._stream_reply("caller words"))

    assert spoken == ["First.", "Second."]
    assert hangups == [True]
    assert call.closed is True
    row = [payload for kind, payload in logged if kind == "assistant_turn"][0]
    assert row["mode"] == "delegate-stream"
    assert row["complete"] is True
    assert row["canonicalEmitted"] == "First. Second."
    assert row["acceptedForSynthesis"] == "First. Second."
    assert row["deliveredToCarrier"]["framesSent"] == 2
    assert row["remoteTruncated"] is False
    sent = call._http.calls[0][2]["json"]
    assert sent["turn_id"].startswith("delegate-str-1-")
    assert len(sent["turn_id"]) <= 64


def test_phone_flag_off_never_touches_the_streaming_consumer(monkeypatch):
    from voice import phone
    from voice.turn_delegate import DelegateReply

    monkeypatch.delenv("NANO_CLAW_DELEGATE_STREAMING", raising=False)
    phone, call, spoken = _streaming_phone_call(monkeypatch, FakeResponse([]))

    def streaming_must_not_run(*args, **kwargs):
        raise AssertionError("default-off path called stream_delegate")

    seen = []

    async def atomic(client, url, text, *, who, turn_id):
        seen.append(turn_id)
        return DelegateReply("Atomic reply.", ok=True)

    monkeypatch.setattr(phone, "stream_delegate", streaming_must_not_run)
    monkeypatch.setattr(phone, "call_delegate", atomic)
    monkeypatch.setattr(phone.call_log, "emit", lambda *args, **kwargs: None)
    run(call._stream_reply("caller words"))

    assert spoken == ["Atomic reply."]
    assert seen[0].startswith("delegate-str-1-")
    assert len(seen[0]) <= 64
