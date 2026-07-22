import numpy as np

from voice.phone_audio import (
    BargeInDetector,
    UtteranceEndpointer,
    pcm48k_to_l16_frames,
    resample_48k_to_16k,
)


def test_resample_48k_to_16k_length_dtype_and_empty_input():
    source = np.arange(4800, dtype=np.int16)

    output = resample_48k_to_16k(source)

    assert len(output) == 1600
    assert output.dtype == np.int16
    empty = resample_48k_to_16k(np.zeros(0, dtype=np.int16))
    assert len(empty) == 0
    assert empty.dtype == np.int16


def test_pcm48k_to_l16_frames_are_raw_20ms_pcm16():
    source = np.arange(5040, dtype=np.int16)  # 105 ms at 48 kHz
    expected = resample_48k_to_16k(source)

    frames = pcm48k_to_l16_frames(source.tobytes())

    assert frames
    assert all(len(frame) == 640 for frame in frames[:-1])
    assert 0 < len(frames[-1]) <= 640
    decoded = np.frombuffer(b"".join(frames), dtype=np.int16)
    assert np.array_equal(decoded, expected)


def _endpoint_wall_clock_ms(rate_hz: int) -> int:
    endpoint = UtteranceEndpointer(rate_hz=rate_hz)
    frame = np.full(rate_hz * 20 // 1000, 1000, dtype=np.int16)
    silence = np.zeros_like(frame)
    sequence = [(frame, True)] * 15 + [(silence, False)] * 35
    for frame_number, (samples, is_speech) in enumerate(sequence, start=1):
        if endpoint.feed(samples, is_speech=is_speech) is not None:
            return frame_number * 20
    raise AssertionError("speech followed by endpoint silence did not complete")


def test_endpointer_rate_awareness_preserves_wall_clock_timing():
    assert _endpoint_wall_clock_ms(8000) == 1000
    assert _endpoint_wall_clock_ms(16000) == 1000


def _barge_trigger_wall_clock_ms(rate_hz: int) -> int:
    detector = BargeInDetector(rate_hz=rate_hz, trigger_ms=240)
    frame = np.full(rate_hz * 20 // 1000, 1000, dtype=np.int16)
    for frame_number in range(1, 21):
        if detector.feed(frame, is_speech=True):
            return frame_number * 20
    raise AssertionError("sustained speech did not trigger barge-in")


def test_barge_in_rate_awareness_preserves_wall_clock_timing():
    assert _barge_trigger_wall_clock_ms(8000) == 240
    assert _barge_trigger_wall_clock_ms(16000) == 240
