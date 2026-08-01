import numpy as np
import pytest

from voice import silero_vad
from voice.phone_audio import FRAME_SAMPLES, BargeInDetector, UtteranceEndpointer

needs_model = pytest.mark.skipif(
    not silero_vad.available(), reason="silero model/onnxruntime unavailable"
)


@needs_model
def test_silence_scores_low():
    vad = silero_vad.SileroVAD()
    for _ in range(50):  # 1s of silence
        vad.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert vad.prob < 0.2


@needs_model
def test_prob_is_bounded_and_stateful():
    vad = silero_vad.SileroVAD()
    rng = np.random.default_rng(0)
    for _ in range(50):
        noise = (rng.standard_normal(FRAME_SAMPLES) * 3000).astype(np.int16)
        p = vad.feed(noise)
        assert 0.0 <= p <= 1.0


def test_rebuffering_needs_full_chunk():
    if not silero_vad.available():
        pytest.skip("model unavailable")
    # Default = raw 8k path (upsample is opt-in and currently broken):
    # 160-sample frames vs the 256-sample chunk.
    raw = silero_vad.SileroVAD()
    raw.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert len(raw._buf) == FRAME_SAMPLES
    raw.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert len(raw._buf) == 2 * FRAME_SAMPLES - silero_vad.CHUNK_8K
    # Opt-in upsample path: 160 → 320 samples at 16k against a 512 chunk
    up = silero_vad.SileroVAD(upsample_phone_audio=True)
    up.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert len(up._buf) == 2 * FRAME_SAMPLES
    up.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert len(up._buf) == 4 * FRAME_SAMPLES - 512


def test_endpointer_honors_external_speech_flag():
    # Quiet frames (below RMS threshold) but externally flagged as speech:
    # the utterance must still form and complete — proving injection wins.
    ep = UtteranceEndpointer()
    quiet = np.full(FRAME_SAMPLES, 50, dtype=np.int16)
    for _ in range(30):  # 600ms "speech"
        assert ep.feed(quiet, is_speech=True) is None
    result = None
    for _ in range(40):  # 800ms silence
        result = result or ep.feed(quiet, is_speech=False)
    assert result is not None


def test_barge_honors_external_speech_flag():
    det = BargeInDetector()
    loud = np.full(FRAME_SAMPLES, 20000, dtype=np.int16)
    # Loud frames flagged as NOT speech (e.g. TTS echo): never fires
    assert not any(det.feed(loud, is_speech=False) for _ in range(60))
    det.reset()
    quiet = np.full(FRAME_SAMPLES, 50, dtype=np.int16)
    # Quiet frames flagged as speech: fires after sustain
    assert any(det.feed(quiet, is_speech=True) for _ in range(60))


def test_chunks_carry_silero_v5_context_prefix(monkeypatch):
    # Silero v5's ONNX expects each chunk prefixed with the tail of the
    # previous one (64 samples @16k, 32 @8k). Bare chunks score ~0 on real
    # speech — measured on a live tap: bare-512 max prob 0.002 vs 0.999
    # with the prefix (and 0.410 vs 0.993 at 8k).
    captured = []

    class FakeSession:
        def run(self, _out, feeds):
            captured.append(np.array(feeds["input"][0]))
            return np.array([[0.7]], dtype=np.float32), feeds["state"]

    monkeypatch.setattr(silero_vad, "_get_session", lambda: FakeSession())

    vad16 = silero_vad.SileroVAD(sample_rate=16000)
    frame16 = (np.arange(320) % 100).astype(np.int16)
    for _ in range(4):
        vad16.feed(frame16)
    assert captured, "no inference ran"
    assert all(len(x) == 512 + 64 for x in captured)
    # First chunk: zero context. Later chunks: context == previous chunk tail.
    assert np.all(captured[0][:64] == 0.0)
    assert np.allclose(captured[1][:64], captured[0][64:][-64:])

    captured.clear()
    vad8 = silero_vad.SileroVAD(sample_rate=8000)
    frame8 = (np.arange(160) % 100).astype(np.int16)
    for _ in range(4):
        vad8.feed(frame8)
    assert all(len(x) == 256 + 32 for x in captured)
    assert np.allclose(captured[1][:32], captured[0][32:][-32:])
