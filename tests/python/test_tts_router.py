import numpy as np

from voice import tts, kokoro_client, lux_client


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


def test_kokoro_malformed_response_falls_back_to_piper(monkeypatch):
    # Odd-length byte string: np.frombuffer(..., dtype=np.int16) raises ValueError.
    monkeypatch.setattr(
        kokoro_client, "synthesize", lambda text, voice, speed: (b"\x01\x02\x03", 24000)
    )

    called = {}

    def _fake_piper(text, voice_id):
        called["voice_id"] = voice_id
        return _pcm(4800, 48000)

    monkeypatch.setattr(tts, "_synthesize_piper", _fake_piper)
    out = tts.synthesize("hola", "ef_dora", 1.0)
    assert called["voice_id"] == tts.DEFAULT_VOICE
    assert len(out) // 2 == 4800


def test_unknown_voice_uses_piper_default(monkeypatch):
    called = {}

    def _fake_piper(text, voice_id):
        called["voice_id"] = voice_id
        return _pcm(4800, 48000)

    monkeypatch.setattr(tts, "_synthesize_piper", _fake_piper)
    tts.synthesize("hi", "totally-unknown-voice", 1.0)
    assert called["voice_id"] == tts.DEFAULT_VOICE


def test_lux_voice_48k_passes_through(monkeypatch):
    # LuxTTS already returns 48kHz — no resampling, byte length preserved.
    monkeypatch.setattr(
        lux_client, "synthesize", lambda text, voice, speed: (_pcm(4800, 48000), 48000)
    )
    out = tts.synthesize("hello", "lux_heart", 1.0)
    assert len(out) // 2 == 4800


def test_lux_failure_falls_back_to_piper(monkeypatch):
    def _boom(text, voice, speed):
        raise lux_client.LuxUnavailable("down")

    monkeypatch.setattr(lux_client, "synthesize", _boom)

    called = {}

    def _fake_piper(text, voice_id):
        called["voice_id"] = voice_id
        return _pcm(4800, 48000)

    monkeypatch.setattr(tts, "_synthesize_piper", _fake_piper)
    out = tts.synthesize("hello", "lux_heart", 1.0)
    assert called["voice_id"] == tts.DEFAULT_VOICE
    assert len(out) // 2 == 4800


def test_piper_voice_ignores_speed(monkeypatch):
    called = {}

    def _fake_piper(*args):
        called["args"] = args
        return _pcm(4800, 48000)

    monkeypatch.setattr(tts, "_synthesize_piper", _fake_piper)
    tts.synthesize("hi", "en_US-lessac-medium", 1.7)
    assert called["args"] == ("hi", "en_US-lessac-medium")


def test_kokoro_receives_normalized_scheduler_text(monkeypatch):
    called = {}

    def _fake_kokoro(text, voice, speed):
        called["args"] = (text, voice, speed)
        return _pcm(2400, 24000), 24000

    monkeypatch.setattr(kokoro_client, "synthesize", _fake_kokoro)

    tts.synthesize(
        "Monday at 10:00 AM — open (for an hour).",
        "af_heart",
        1.0,
    )

    assert called["args"] == (
        "Monday at 10 AM, open for an hour.",
        "af_heart",
        1.0,
    )


def test_sentence_final_chunk_has_configured_silence_gap(monkeypatch):
    speech = np.ones(4800, dtype=np.int16).tobytes()
    monkeypatch.setattr(tts, "_synthesize_piper", lambda text, voice_id: speech)

    out = tts.synthesize("A complete sentence.", "en_US-lessac-medium", 1.0)
    gap_bytes = tts.TARGET_RATE * tts.SENTENCE_GAP_MS // 1000 * 2

    # The speech portion keeps its length (edges are declicked, not trimmed) and
    # the configured silence gap is appended as pure zeros.
    assert len(out) == len(speech) + gap_bytes
    assert out[-gap_bytes:] == bytes(gap_bytes)


def test_unpunctuated_final_fragment_has_no_extra_gap(monkeypatch):
    speech = np.ones(4800, dtype=np.int16).tobytes()
    monkeypatch.setattr(tts, "_synthesize_piper", lambda text, voice_id: speech)

    out = tts.synthesize("final fragment", "en_US-lessac-medium", 1.0)
    # No sentence gap is appended, so the length is unchanged; the edges are
    # now declicked, so assert length rather than byte-identity.
    assert len(out) == len(speech)


def test_compiler_pause_overrides_legacy_sentence_gap(monkeypatch):
    speech = np.ones(4800, dtype=np.int16).tobytes()
    monkeypatch.setattr(tts, "_synthesize_piper", lambda text, voice_id: speech)

    zero_pause = tts.synthesize(
        "A final question?", "en_US-lessac-medium", 1.0, pause_after_ms=0
    )
    assert len(zero_pause) == len(speech)

    out = tts.synthesize(
        "A continuing phrase", "en_US-lessac-medium", 1.0, pause_after_ms=120
    )
    gap_bytes = tts.TARGET_RATE * 120 // 1000 * 2
    assert len(out) == len(speech) + gap_bytes
    assert out[-gap_bytes:] == bytes(gap_bytes)


def _max_abs_step(pcm: bytes) -> int:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
    if samples.size < 2:
        return 0
    return int(np.abs(np.diff(samples)).max())


def test_declick_ramps_chunk_edges_to_zero():
    # A loud tone that neither starts nor ends near a zero sample: butting it
    # against a silence gap would otherwise be a step of ~full scale.
    n = tts.TARGET_RATE // 10  # 100 ms
    tone = (np.full(n, 12000, dtype=np.int16)).tobytes()
    declicked = tts._declick_edges(tone)
    samples = np.frombuffer(declicked, dtype=np.int16)
    assert abs(int(samples[0])) < 400, "leading edge must ramp up from ~0"
    assert abs(int(samples[-1])) < 400, "trailing edge must ramp down to ~0"
    # The interior is untouched.
    assert int(samples[n // 2]) == 12000


def test_sentence_gap_seam_has_no_step_discontinuity():
    n = tts.TARGET_RATE // 10
    tone = (np.full(n, 12000, dtype=np.int16)).tobytes()
    with_gap = tts._with_sentence_gap("A full sentence.", tone)
    # The speech->silence seam and the ramp itself must never jump by more than
    # a small fraction of full scale; without declicking the seam step is 12000.
    assert _max_abs_step(with_gap) < 800
    # The gap really was appended (output longer than the speech alone).
    assert len(with_gap) > len(tone)


def test_declick_leaves_short_pcm_untouched():
    tiny = np.array([5000, -5000], dtype=np.int16).tobytes()
    assert tts._declick_edges(tiny) == tiny


def test_declick_fades_both_edges_from_and_to_zero():
    # Both edges ramp between zero and full scale; the fade-out is at least as
    # long as the fade-in (it also masks Lux's abrupt truncation).
    assert tts._DECLICK_OUT_SAMPLES >= tts._DECLICK_IN_SAMPLES
    n = tts.TARGET_RATE // 5  # 200 ms
    tone = np.full(n, 10000, dtype=np.int16).tobytes()
    s = np.frombuffer(tts._declick_edges(tone), dtype=np.int16).astype(np.int32)
    assert abs(int(s[0])) < 200, "onset starts at ~zero"
    assert abs(int(s[-1])) < 200, "release ends at ~zero"
    # Interior (well past both fades) is untouched full scale.
    assert int(s[n // 2]) == 10000
    # The onset ramp is gradual enough to smooth a hard engine onset (Lux):
    # no single step in the fade-in window exceeds a small fraction of scale.
    fin = tts._DECLICK_IN_SAMPLES
    assert int(np.abs(np.diff(s[:fin])).max()) < 60, "onset ramp has no hard step"
