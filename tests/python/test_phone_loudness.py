import asyncio

import numpy as np
import pytest

from voice import phone
from voice.phone_audio import SentencePeakNormalizer


def _sine(amplitude: int, samples: int = 4_800) -> bytes:
    phase = np.arange(samples, dtype=np.float64) * (2.0 * np.pi / 48.0)
    return (amplitude * np.sin(phase)).astype(np.int16).tobytes()


def _peak_dbfs(pcm16: bytes) -> float:
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    return float(20.0 * np.log10(np.max(np.abs(samples)) / 32768.0))


def test_quiet_sine_is_boosted_toward_target_but_capped_at_twelve_db():
    normalizer = SentencePeakNormalizer(target_dbfs=-3.0)
    source = _sine(500)

    result = normalizer.normalize(source)

    assert result.applied_gain_db == pytest.approx(12.0)
    assert _peak_dbfs(result.pcm16) == pytest.approx(
        result.measured_peak_dbfs + 12.0, abs=0.02
    )
    assert _peak_dbfs(result.pcm16) < -3.0


def test_hot_sine_is_attenuated_to_target_without_clipped_samples():
    normalizer = SentencePeakNormalizer(target_dbfs=-3.0)

    result = normalizer.normalize(_sine(30_000))
    samples = np.frombuffer(result.pcm16, dtype=np.int16)

    assert result.applied_gain_db < 0.0
    assert _peak_dbfs(result.pcm16) == pytest.approx(-3.0, abs=0.01)
    assert not np.any(samples == np.iinfo(np.int16).min)
    assert not np.any(samples == np.iinfo(np.int16).max)


def test_alternating_quiet_and_loud_sentences_move_at_most_three_db():
    normalizer = SentencePeakNormalizer(target_dbfs=-3.0)

    gains = [
        normalizer.normalize(_sine(amplitude)).applied_gain_db
        for amplitude in (400, 30_000, 400, 30_000)
    ]

    assert gains == pytest.approx([12.0, 9.0, 12.0, 9.0])
    assert np.all(np.abs(np.diff(gains)) <= 3.0)


def test_off_mode_is_byte_identical():
    source = np.array(
        [-32768, -12345, -1, 0, 1, 12345, 32767], dtype=np.int16
    ).tobytes()
    normalizer = SentencePeakNormalizer(target_dbfs=-3.0, enabled=False)

    result = normalizer.normalize(source)

    assert result.pcm16 == source
    assert result.applied_gain_db == 0.0


def test_phone_playback_taps_gain_decision_before_resampling(monkeypatch):
    class RecordingTap:
        def __init__(self):
            self.events = []

        def event(self, name, **fields):
            self.events.append((name, fields))

        def outbound_frame(self, frame):
            pass

    class RecordingWebSocket:
        async def send_json(self, message):
            pass

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(phone, "phone_codec", lambda: "pcmu")
    monkeypatch.setattr(phone.asyncio, "sleep", no_sleep)
    tap = RecordingTap()
    call = object.__new__(phone.PhoneCall)
    call.call_id = "gain-tap"
    call.ws = RecordingWebSocket()
    call.closed = False
    call.speaking = True
    call._active_tap_sentence_index = None
    call._gain_normalizer = SentencePeakNormalizer(target_dbfs=-3.0)
    speech = phone._SynthesizedSpeech(_sine(500, samples=960), tap, 7)

    asyncio.run(call._play_synthesized(speech))

    event = next(fields for name, fields in tap.events if name == "gain_applied")
    assert event["sentence_index"] == 7
    assert event["measured_peak_dbfs"] == pytest.approx(
        SentencePeakNormalizer.measure(speech.pcm48k)
    )
    assert event["applied_gain_db"] == pytest.approx(12.0)
