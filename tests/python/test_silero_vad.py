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


def test_rebuffering_needs_full_chunk(monkeypatch):
    # 160-sample frames: first frame (<256) can't run inference yet
    vad = silero_vad.SileroVAD()
    if not silero_vad.available():
        pytest.skip("model unavailable")
    vad.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert len(vad._buf) == FRAME_SAMPLES  # buffered, one chunk short
    vad.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert len(vad._buf) == 2 * FRAME_SAMPLES - silero_vad.CHUNK_8K


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
