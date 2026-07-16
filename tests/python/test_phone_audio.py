import numpy as np

from voice.phone_audio import (
    FRAME_SAMPLES,
    UtteranceEndpointer,
    pcm48k_to_ulaw_frames,
    resample_48k_to_8k,
    ulaw_decode,
    ulaw_encode,
)


def tone(freq_hz: float, ms: int, rate: int = 8000, amp: int = 8000) -> np.ndarray:
    t = np.arange(rate * ms // 1000) / rate
    return (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)


def silence(ms: int, rate: int = 8000) -> np.ndarray:
    return np.zeros(rate * ms // 1000, dtype=np.int16)


def test_ulaw_roundtrip_preserves_speech_band_signal():
    original = tone(440, 100)
    decoded = ulaw_decode(ulaw_encode(original))
    assert len(decoded) == len(original)
    # μ-law is lossy; correlation with the original must stay very high.
    corr = np.corrcoef(original.astype(float), decoded.astype(float))[0, 1]
    assert corr > 0.99


def test_ulaw_silence_stays_quiet():
    decoded = ulaw_decode(ulaw_encode(silence(50)))
    assert np.abs(decoded).max() < 20


def test_resample_48k_to_8k_length_and_tone():
    src = tone(440, 200, rate=48000)
    out = resample_48k_to_8k(src)
    assert len(out) == len(src) // 6
    # The 440 Hz tone is far below the 3.4 kHz cutoff: energy survives.
    assert np.abs(out.astype(np.int32)).max() > 4000


def test_pcm48k_to_ulaw_frames_shapes():
    pcm = tone(300, 100, rate=48000).tobytes()
    frames = pcm48k_to_ulaw_frames(pcm)
    assert frames, "expected at least one frame"
    assert all(len(f) <= FRAME_SAMPLES for f in frames)
    assert all(len(f) == FRAME_SAMPLES for f in frames[:-1])


class TestUtteranceEndpointer:
    def frames(self, pcm: np.ndarray):
        return [pcm[i : i + FRAME_SAMPLES] for i in range(0, len(pcm), FRAME_SAMPLES)]

    def feed_all(self, ep: UtteranceEndpointer, pcm: np.ndarray):
        results = [ep.feed(f) for f in self.frames(pcm)]
        return [r for r in results if r is not None]

    def test_speech_then_silence_yields_one_utterance(self):
        ep = UtteranceEndpointer()
        pcm = np.concatenate([silence(300), tone(300, 600), silence(900)])
        utterances = self.feed_all(ep, pcm)
        assert len(utterances) == 1
        # The utterance should contain roughly the speech duration.
        assert len(utterances[0]) >= 8000 * 2 * 0.5  # ≥ 500ms of int16 @ 8k

    def test_pure_silence_yields_nothing(self):
        ep = UtteranceEndpointer()
        assert self.feed_all(ep, silence(3000)) == []

    def test_short_blip_is_discarded(self):
        ep = UtteranceEndpointer(min_speech_ms=250)
        pcm = np.concatenate([silence(200), tone(300, 60), silence(1000)])
        assert self.feed_all(ep, pcm) == []

    def test_max_utterance_cap_forces_flush(self):
        ep = UtteranceEndpointer(max_utterance_ms=1000)
        utterances = self.feed_all(ep, tone(300, 2500))
        assert len(utterances) >= 2  # monologue split at the cap

    def test_two_utterances_separated(self):
        ep = UtteranceEndpointer()
        pcm = np.concatenate(
            [tone(300, 500), silence(900), tone(300, 500), silence(900)]
        )
        assert len(self.feed_all(ep, pcm)) == 2
