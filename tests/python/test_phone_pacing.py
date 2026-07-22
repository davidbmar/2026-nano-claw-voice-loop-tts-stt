import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from voice import phone


class _SimulatedClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _send_frames(
    pacer: phone.FramePacer,
    clock: _SimulatedClock,
    count: int,
    *,
    slow_after: int | None = None,
    slow_s: float = 0.0,
) -> np.ndarray:
    sent_at = []
    for index in range(count):
        deadline = pacer.next_deadline()
        clock.advance(max(0.0, deadline - clock()))
        sent_at.append(clock())
        if index == slow_after:
            clock.advance(slow_s)
    return np.asarray(sent_at, dtype=np.float64)


def test_five_second_phone_playback_reports_bounded_surplus(monkeypatch) -> None:
    class RecordingTap:
        def __init__(self) -> None:
            self.events = []

        def event(self, name, **fields) -> None:
            self.events.append((name, fields))

        def outbound_frame(self, frame) -> None:
            pass

    class RecordingWebSocket:
        def __init__(self) -> None:
            self.sent_at = []

        async def send_json(self, message) -> None:
            self.sent_at.append(clock())

    clock = _SimulatedClock()
    tap = RecordingTap()
    ws = RecordingWebSocket()
    frames = [bytes(160)] * round(5.0 / phone.FRAME_S)

    async def simulated_sleep(delay: float) -> None:
        clock.advance(delay)

    monkeypatch.setattr(phone, "phone_codec", lambda: "pcmu")
    monkeypatch.setattr(phone, "pcm48k_to_ulaw_frames", lambda pcm: frames)
    monkeypatch.setattr(phone.asyncio, "sleep", simulated_sleep)
    call = object.__new__(phone.PhoneCall)
    call.call_id = "pacing-tap"
    call.ws = ws
    call.closed = False
    call.speaking = True
    call._active_tap_sentence_index = None
    call._gain_normalizer = SimpleNamespace(
        normalize=lambda pcm: SimpleNamespace(
            pcm16=pcm,
            measured_peak_dbfs=-12.0,
            applied_gain_db=0.0,
        )
    )
    speech = phone._SynthesizedSpeech(b"simulated", tap, 1)

    asyncio.run(call._play_synthesized(speech, phone.FramePacer(clock=clock)))

    frames_event = next(fields for name, fields in tap.events if name == "frames_sent")
    assert frames_event["count"] == 250
    assert frames_event["elapsed_s"] == pytest.approx(4.8, abs=1e-8)
    assert frames_event["audio_s"] == pytest.approx(5.0, abs=1e-8)
    assert frames_event["surplus_s"] == pytest.approx(0.2, abs=1e-8)
    assert 19.9 <= frames_event["interval_p95_ms"] <= 20.1


def test_five_second_reply_finishes_audio_duration_minus_prebuffer() -> None:
    clock = _SimulatedClock()
    pacer = phone.FramePacer(clock=clock)
    started_at = clock()
    pacer.reset()

    sent_at = _send_frames(pacer, clock, round(5.0 / phone.FRAME_S))
    intervals_ms = np.diff(sent_at) * 1000.0

    assert sent_at[-1] - started_at == pytest.approx(4.8, abs=1e-8)
    assert 19.9 <= np.percentile(intervals_ms, 95) <= 20.1
    assert 5.0 - (sent_at[-1] - started_at) == pytest.approx(0.2, abs=1e-8)


def test_slow_iteration_is_absorbed_without_cumulative_drift() -> None:
    clock = _SimulatedClock()
    pacer = phone.FramePacer(clock=clock)
    started_at = clock()
    pacer.reset()

    sent_at = _send_frames(
        pacer,
        clock,
        round(5.0 / phone.FRAME_S),
        slow_after=100,
        slow_s=0.075,
    )
    intervals = np.diff(sent_at)

    assert intervals[100] == pytest.approx(0.075)
    assert intervals[101] == pytest.approx(0.0)
    assert intervals[102] == pytest.approx(0.0)
    assert sent_at[-1] - started_at == pytest.approx(4.8, abs=1e-8)


def test_one_deadline_schedule_spans_sentence_boundaries() -> None:
    clock = _SimulatedClock()
    pacer = phone.FramePacer(clock=clock)
    started_at = clock()
    pacer.reset()

    first_sentence = _send_frames(pacer, clock, 50)
    second_sentence = _send_frames(pacer, clock, 50)

    assert np.count_nonzero(np.isclose(first_sentence, started_at)) == 10
    assert second_sentence[0] - first_sentence[-1] == pytest.approx(phone.FRAME_S)
    assert np.allclose(np.diff(second_sentence), phone.FRAME_S)
    assert second_sentence[-1] - started_at == pytest.approx(1.8, abs=1e-8)


def test_environment_factor_and_prebuffer_are_respected(monkeypatch) -> None:
    monkeypatch.setattr(phone, "_overrides", {})
    monkeypatch.setenv("NANO_CLAW_PHONE_PREBUFFER_MS", "400")
    monkeypatch.setenv("NANO_CLAW_PHONE_PACE_FACTOR", "0.8")
    clock = _SimulatedClock()
    pacer = phone._phone_frame_pacer(clock=clock)
    started_at = clock()
    pacer.reset()

    sent_at = _send_frames(pacer, clock, round(5.0 / phone.FRAME_S))
    intervals_ms = np.diff(sent_at) * 1000.0

    assert pacer.prebuffer_ms == 400.0
    assert pacer.pace_factor == 0.8
    assert np.count_nonzero(np.isclose(sent_at, started_at)) == 20
    assert sent_at[-1] - started_at == pytest.approx(3.68, abs=1e-8)
    assert np.percentile(intervals_ms, 95) == pytest.approx(16.0, abs=1e-7)


def test_phone_reply_builds_one_pacer_for_all_sentences(monkeypatch) -> None:
    sentinel_pacer = object()
    factory_calls = []
    played = []

    def make_pacer():
        factory_calls.append(None)
        return sentinel_pacer

    async def synthesize(sentence):
        return sentence

    async def play(speech):
        played.append((speech, call._frame_pacer))

    monkeypatch.setattr(phone, "_phone_frame_pacer", make_pacer)
    call = object.__new__(phone.PhoneCall)
    call.closed = False
    call.speaking = True
    call.tap = None
    call._gain_normalizer = SimpleNamespace(reset=lambda: None)
    call._sentence_pipelines = set()
    call._synthesize_sentence = synthesize
    call._synthesis_failed = lambda sentence, error: None
    call._record_synth_ahead = lambda ready, wait_s: None
    call._play_synthesized = play

    asyncio.run(call._speak_sentences(("one", "two")))

    assert len(factory_calls) == 1
    assert played == [("one", sentinel_pacer), ("two", sentinel_pacer)]
