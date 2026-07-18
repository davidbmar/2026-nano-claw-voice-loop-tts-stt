import numpy as np
import pytest

from voice.phone import PhoneCall
from voice.phone_audio import NoiseFloorEstimator, UtteranceEndpointer


RATE_HZ = 16_000
FRAME_SAMPLES = RATE_HZ * 20 // 1000


def frame(level: int) -> np.ndarray:
    return np.full(FRAME_SAMPLES, level, dtype=np.int16)


def test_high_constant_l16_noise_does_not_start_an_utterance():
    endpoint = UtteranceEndpointer(codec="l16", rate_hz=RATE_HZ)

    # Establish a noisy line floor, then raise it above the fixed L16 minimum.
    # The adaptive boundary rises first, so sustained background stays noise.
    for _ in range(20):
        assert endpoint.feed(frame(100)) is None
    for _ in range(100):
        assert endpoint.feed(frame(250)) is None

    assert not endpoint.in_utterance
    assert endpoint.noise_floor > 240.0
    assert endpoint.effective_threshold > 720.0


def test_quiet_l16_line_detects_soft_speech():
    endpoint = UtteranceEndpointer(codec="l16", rate_hz=RATE_HZ)
    for _ in range(20):
        endpoint.feed(frame(10))

    for _ in range(15):
        endpoint.feed(frame(180))
    completed = [endpoint.feed(frame(0)) for _ in range(35)]

    assert sum(result is not None for result in completed) == 1
    assert endpoint.current_rms == 0.0


def test_floor_tracks_changed_line_conditions_and_freezes_on_speech():
    floor = NoiseFloorEstimator(min_threshold=120.0, ratio=3.0)
    for _ in range(20):
        assert not floor.classify(10.0)
    quiet_floor = floor.floor

    for _ in range(80):
        assert not floor.classify(100.0)
    noisy_floor = floor.floor
    assert noisy_floor > quiet_floor

    assert floor.classify(500.0)
    assert floor.floor == noisy_floor

    for _ in range(60):
        assert not floor.classify(10.0)
    assert floor.floor < noisy_floor


def test_separate_calls_learn_their_own_line_conditions():
    quiet_call = UtteranceEndpointer(codec="l16", rate_hz=RATE_HZ)
    noisy_call = UtteranceEndpointer(codec="l16", rate_hz=RATE_HZ)

    for _ in range(40):
        quiet_call.feed(frame(10))
        noisy_call.feed(frame(100))

    assert quiet_call.noise_floor == pytest.approx(10.0)
    assert noisy_call.noise_floor == pytest.approx(100.0)
    assert quiet_call.effective_threshold == 120.0
    assert noisy_call.effective_threshold == pytest.approx(300.0)


def test_pcmu_default_keeps_the_fixed_350_rms_boundary(monkeypatch):
    monkeypatch.delenv("NANO_CLAW_PHONE_RMS_MIN", raising=False)
    monkeypatch.delenv("NANO_CLAW_PHONE_RMS_RATIO", raising=False)
    endpoint = UtteranceEndpointer(codec="pcmu", rate_hz=8_000)
    endpoint.feed(np.full(160, 2_000, dtype=np.int16), is_speech=False)

    assert endpoint.rms_threshold == 350.0
    assert endpoint.effective_threshold == 350.0
    endpoint.feed(np.full(160, 349, dtype=np.int16))
    assert not endpoint.in_utterance
    endpoint.feed(np.full(160, 350, dtype=np.int16))
    assert endpoint.in_utterance


def test_rms_environment_overrides_are_respected(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_RMS_MIN", "75")
    monkeypatch.setenv("NANO_CLAW_PHONE_RMS_RATIO", "4.5")

    endpoint = UtteranceEndpointer(codec="l16", rate_hz=RATE_HZ)
    endpoint.feed(frame(20))

    assert endpoint.rms_min == 75.0
    assert endpoint.rms_ratio == 4.5
    assert endpoint.noise_floor == 20.0
    assert endpoint.effective_threshold == 90.0


class RecordingTap:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


def test_tap_endpoint_events_include_current_rms_and_floor():
    call = PhoneCall.__new__(PhoneCall)
    call.tap = RecordingTap()
    call.endpointer = UtteranceEndpointer(codec="l16", rate_hz=RATE_HZ)

    for _ in range(10):
        call._feed_endpointer(frame(10), None)
    for _ in range(15):
        call._feed_endpointer(frame(180), None)
    for _ in range(35):
        call._feed_endpointer(frame(0), None)

    boundaries = [
        fields
        for name, fields in call.tap.events
        if name in ("utterance_start", "utterance_end")
    ]
    assert len(boundaries) == 2
    assert all({"rms", "floor"} <= fields.keys() for fields in boundaries)
    assert boundaries[0]["rms"] == 180.0
    assert boundaries[0]["floor"] == 10.0
    assert boundaries[1]["rms"] == 0.0
