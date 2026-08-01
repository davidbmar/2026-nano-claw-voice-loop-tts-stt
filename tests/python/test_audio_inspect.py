"""Seam analysis for the audio inspector.

The inspector exists to locate the LuxTTS chunk-edge clicks David reported.
These tests build audio whose seams are known by construction, so a clean
fade and a hard step can be told apart, and pin the timebase rule that a
previous manual analysis got wrong: seams are DETECTED from the waveform,
never reconstructed by summing per-sentence durations.
"""

import json
import wave

import numpy as np
import pytest

from voice import audio_inspect

RATE = 16000


def _tone(ms: int, amp: int = 8000, fade_ms: int = 0) -> np.ndarray:
    n = RATE * ms // 1000
    t = np.arange(n) / RATE
    wave_ = (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    if fade_ms:
        f = RATE * fade_ms // 1000
        wave_[:f] *= np.linspace(0, 1, f)
        wave_[-f:] *= np.linspace(1, 0, f)
    return wave_


def _silence(ms: int) -> np.ndarray:
    return np.zeros(RATE * ms // 1000, dtype=np.float32)


def _write(tmp_path, chunks, name="outbound.wav"):
    pcm = np.concatenate(chunks).astype(np.int16)
    path = tmp_path / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())
    return tmp_path


def test_clean_fades_score_low_and_are_not_harsh(tmp_path):
    _write(tmp_path, [_tone(400, fade_ms=20), _silence(200), _tone(400, fade_ms=20)])
    result = audio_inspect.analyze_outbound(tmp_path)

    assert result["available"] and len(result["seams"]) == 1
    seam = result["seams"][0]
    assert seam["fadeOut"] < 0.15 and seam["fadeIn"] < 0.15
    assert seam["harsh"] is False
    assert result["harshCount"] == 0


def test_hard_step_edges_are_flagged_harsh(tmp_path):
    # No fades: audio stops and restarts at full amplitude — an audible tick.
    _write(tmp_path, [_tone(400), _silence(200), _tone(400)])
    result = audio_inspect.analyze_outbound(tmp_path)

    seam = result["seams"][0]
    assert seam["fadeOut"] > 0.5, "abrupt stop must score high"
    assert seam["fadeIn"] > 0.5, "abrupt start must score high"
    assert seam["harsh"] is True
    assert result["harshCount"] == 1


def test_asymmetric_edges_are_reported_separately(tmp_path):
    # Clean tail, hard onset — the real LuxTTS signature (onset is the weak side).
    _write(tmp_path, [_tone(400, fade_ms=20), _silence(200), _tone(400)])
    seam = audio_inspect.analyze_outbound(tmp_path)["seams"][0]

    assert seam["fadeOut"] < 0.15
    assert seam["fadeIn"] > 0.5
    assert seam["harsh"] is True


def test_gap_timings_and_duration_are_in_audio_time(tmp_path):
    _write(tmp_path, [_tone(500, fade_ms=20), _silence(300), _tone(500, fade_ms=20)])
    result = audio_inspect.analyze_outbound(tmp_path)

    seam = result["seams"][0]
    assert seam["gapStart"] == pytest.approx(0.5, abs=0.02)
    assert seam["gapMs"] == pytest.approx(300, abs=25)
    assert result["durationS"] == pytest.approx(1.3, abs=0.02)


def test_short_gaps_are_not_seams(tmp_path):
    # A 10ms dip inside speech is not a chunk boundary.
    _write(tmp_path, [_tone(300, fade_ms=20), _silence(10), _tone(300, fade_ms=20)])
    assert audio_inspect.analyze_outbound(tmp_path)["seams"] == []


def test_envelope_tracks_loud_and_quiet_regions(tmp_path):
    _write(tmp_path, [_tone(200, amp=12000), _silence(200), _tone(200, amp=3000)])
    result = audio_inspect.analyze_outbound(tmp_path)

    env = result["envelope"]
    assert len(env) == pytest.approx(60, abs=3)  # 600ms at 10ms hops
    assert max(env[:15]) > max(env[-15:]) > 0  # loud first, quieter last
    assert min(env[22:38]) < 100  # the silence in the middle


def test_synth_markers_are_advisory_not_authoritative(tmp_path):
    _write(tmp_path, [_tone(400, fade_ms=20)])
    (tmp_path / "timings.jsonl").write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {"event": "frames_sent", "sentence_index": 1, "audio_s": 0.4,
                 "last_frame_audio_ms": 20.0},
                {"event": "frames_sent", "sentence_index": 2, "audio_s": 0.6,
                 "last_frame_audio_ms": 20.0},
                {"event": "synth_done", "sentence_index": 1, "ms": 300},
            )
        ),
        encoding="utf-8",
    )
    markers = audio_inspect.synth_markers(tmp_path)

    assert [m["sentenceIndex"] for m in markers] == [1, 2]
    # Declared positions accumulate; they are labels, not detected seams.
    assert markers[1]["declaredStart"] == pytest.approx(0.4)
    assert markers[1]["declaredEnd"] == pytest.approx(1.0)


def test_missing_or_empty_audio_degrades_quietly(tmp_path):
    assert audio_inspect.analyze_outbound(tmp_path) == {"available": False}
    assert audio_inspect.summarize(tmp_path) == {"available": False}
    assert audio_inspect.synth_markers(tmp_path) == []
    (tmp_path / "outbound.wav").write_bytes(b"")
    assert audio_inspect.analyze_outbound(tmp_path) == {"available": False}


def test_unfinalized_wav_header_still_reads(tmp_path):
    # A call that never closed cleanly leaves a zero-length header; the tap is
    # still full of audio and must remain inspectable.
    _write(tmp_path, [_tone(300, fade_ms=20)])
    data = bytearray((tmp_path / "outbound.wav").read_bytes())
    data[40:44] = (0).to_bytes(4, "little")  # zero the data-chunk size
    (tmp_path / "outbound.wav").write_bytes(bytes(data))

    result = audio_inspect.analyze_outbound(tmp_path)
    assert result["available"] and result["durationS"] > 0.25


def test_band_energy_reports_spectral_balance(tmp_path):
    _write(tmp_path, [_tone(2000, amp=10000)])  # 220Hz tone => sub-1k dominant
    bands = audio_inspect.band_energy(tmp_path)

    assert bands["sub1k"] > 0.8
    assert sum(bands.values()) <= 1.0001


def test_stt_payload_is_pcm16_at_the_wav_rate(tmp_path):
    _write(tmp_path, [_tone(500, fade_ms=20)])
    payload, rate = audio_inspect.audio_bytes_for_stt(tmp_path)

    assert rate == RATE
    assert len(payload) == 2 * (RATE * 500 // 1000)
    assert np.frombuffer(payload, dtype=np.int16).dtype == np.int16
