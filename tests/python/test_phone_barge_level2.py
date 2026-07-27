import numpy as np
import pytest

from tools.phone_tap_report import _barge_counts
from voice import phone
from voice.phone_audio import (
    FRAME_SAMPLES,
    BargeInDetector,
    EchoReferenceGate,
    UtteranceEndpointer,
)


def _speech_like(seed: int, *, rate_hz: int = 8_000, duration_ms: int = 1_000):
    """Deterministic carrier with a speech-like, varying 10 ms envelope."""
    rng = np.random.default_rng(seed)
    hop_samples = rate_hz // 100
    hops = duration_ms // 10
    raw_envelope = rng.uniform(0.1, 1.0, hops + 8)
    envelope = np.convolve(raw_envelope, np.ones(9) / 9.0, mode="valid")
    envelope = 0.1 + 0.8 * (
        (envelope - envelope.min()) / (envelope.max() - envelope.min())
    )
    samples = hops * hop_samples
    time_s = np.arange(samples) / rate_hz
    carrier = np.sin(2.0 * np.pi * (180.0 + seed) * time_s)
    return (12_000.0 * np.repeat(envelope, hop_samples) * carrier).astype(
        np.int16
    )


def _frames(samples: np.ndarray):
    return [
        samples[index : index + FRAME_SAMPLES]
        for index in range(0, len(samples), FRAME_SAMPLES)
    ]


def test_correlated_outbound_replay_with_lag_and_gain_is_suppressed():
    outbound = _speech_like(86)
    lag_samples = 8_000 * 80 // 1_000
    inbound = np.concatenate(
        [
            np.zeros(lag_samples, dtype=np.int16),
            (outbound[:-lag_samples].astype(np.float64) * 0.35).astype(np.int16),
        ]
    )
    gate = EchoReferenceGate(rate_hz=8_000)

    gate.feed_outbound(outbound)
    suppressed, correlation = gate.should_suppress(inbound)

    assert suppressed
    assert correlation == pytest.approx(1.0, abs=1e-6)
    assert gate.outbound_samples <= 8_000 * 1.2
    assert gate.inbound_samples <= 8_000 * 1.2


def test_uncorrelated_real_speech_envelope_passes_echo_gate():
    gate = EchoReferenceGate(rate_hz=8_000)
    gate.feed_outbound(_speech_like(86))

    suppressed, correlation = gate.should_suppress(_speech_like(99))

    assert not suppressed
    assert correlation < 0.75


def test_silero_barge_uses_stricter_raw_probability_than_endpointer():
    quiet = np.full(FRAME_SAMPLES, 50, dtype=np.int16)
    endpointer = UtteranceEndpointer()
    detector = BargeInDetector(vad_enter=0.7)

    # Silero's 0.5 enter decision starts the normal accept path immediately.
    assert endpointer.feed(quiet, is_speech=True) is None
    assert endpointer.in_utterance

    # The same hysteresis-positive chunks cannot accumulate barge votes until
    # the separate raw probability reaches the stricter interruption level.
    assert not any(
        detector.feed(quiet, is_speech=True, speech_prob=0.5)
        for _ in range(25)
    )
    assert detector.last_candidate is not None
    assert detector.last_candidate.reason == "low_conf"

    detector.reset()
    assert any(
        detector.feed(quiet, is_speech=True, speech_prob=0.7)
        for _ in range(20)
    )


def test_default_sustain_rejects_300ms_and_commits_400ms_speech():
    detector = BargeInDetector()
    speech = np.full(FRAME_SAMPLES, 2_000, dtype=np.int16)

    assert not any(detector.feed(speech, is_speech=True) for _ in range(15))
    assert any(detector.feed(speech, is_speech=True) for _ in range(5))


def test_echo_vote_is_suppressed_and_candidate_reason_is_exposed():
    detector = BargeInDetector()
    speech = np.full(FRAME_SAMPLES, 2_000, dtype=np.int16)

    assert not any(
        detector.feed(
            speech,
            is_speech=True,
            speech_prob=0.9,
            echo_correlation=0.8,
        )
        for _ in range(30)
    )
    assert detector.last_candidate is not None
    assert detector.last_candidate.reason == "echo"
    assert detector.last_candidate.prob == 0.9
    assert detector.last_candidate.corr == 0.8


def test_barge_environment_values_are_clamped(monkeypatch):
    monkeypatch.setattr(phone, "_overrides", {})
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_VAD_ENTER", "0.1")
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_TRIGGER_MS", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_ECHO_CORR", "-1")

    assert phone.phone_barge_vad_enter() == 0.5
    assert phone.phone_barge_trigger_ms() == 120
    assert phone.phone_echo_correlation() == 0.0

    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_VAD_ENTER", "2")
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_TRIGGER_MS", "9999")
    monkeypatch.setenv("NANO_CLAW_PHONE_ECHO_CORR", "2")

    assert phone.phone_barge_vad_enter() == 0.95
    assert phone.phone_barge_trigger_ms() == 1_500
    assert phone.phone_echo_correlation() == 1.0


def test_echo_gate_defaults_on_with_barge_in_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr(phone, "_overrides", {})
    monkeypatch.delenv("NANO_CLAW_PHONE_ECHO_GATE", raising=False)
    monkeypatch.setenv("NANO_CLAW_PHONE_BARGE_IN", "1")
    assert phone.phone_echo_gate_enabled()

    monkeypatch.setenv("NANO_CLAW_PHONE_ECHO_GATE", "0")
    assert not phone.phone_echo_gate_enabled()


def test_tap_report_counts_candidates_commits_and_suppression_reasons():
    counts = _barge_counts(
        [
            {"event": "barge_candidate", "reason": "low_conf"},
            {"event": "barge_candidate", "reason": "echo"},
            {"event": "barge_candidate", "reason": "short"},
            {"event": "barge_candidate", "reason": "short"},
            {"event": "barge_in"},
        ]
    )

    assert counts == {
        "candidates": 4,
        "commits": 1,
        "suppressions": 4,
        "reasons": {"low_conf": 1, "echo": 1, "short": 2},
    }
