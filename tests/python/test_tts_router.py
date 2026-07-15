import numpy as np

from voice import tts, kokoro_client


def _pcm(n, rate):
    # n samples of silence-ish int16 at the given rate
    return np.zeros(n, dtype=np.int16).tobytes()


def test_kokoro_voice_resampled_to_48k(monkeypatch):
    # 2400 samples @ 24k (0.1s) should become ~4800 samples @ 48k
    monkeypatch.setattr(
        kokoro_client, "synthesize", lambda text, voice, speed: (_pcm(2400, 24000), 24000)
    )
    out = tts.synthesize("hola", "ef_dora", 1.0)
    assert len(out) // 2 == 4800  # 2 bytes per int16 sample


def test_kokoro_failure_falls_back_to_piper(monkeypatch):
    def _boom(text, voice, speed):
        raise kokoro_client.KokoroUnavailable("down")

    monkeypatch.setattr(kokoro_client, "synthesize", _boom)

    called = {}

    def _fake_piper(text, voice_id):
        called["voice_id"] = voice_id
        return _pcm(4800, 48000)  # pretend Piper already returns 48k

    monkeypatch.setattr(tts, "_synthesize_piper", _fake_piper)
    out = tts.synthesize("hello", "af_heart", 1.0)
    assert called["voice_id"] == tts.DEFAULT_VOICE
    assert len(out) // 2 == 4800
