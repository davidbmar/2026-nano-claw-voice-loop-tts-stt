import asyncio
import base64
import json
import logging
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from voice import phone
from voice.phone_audio import ulaw_encode
from voice.phone_tap import CallTap


def _tone(freq_hz: float, duration_s: float, rate: int, amplitude: int = 9000) -> np.ndarray:
    times = np.arange(round(rate * duration_s), dtype=np.float64) / rate
    return (amplitude * np.sin(2.0 * np.pi * freq_hz * times)).astype(np.int16)


def _wav_params(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as source:
        return (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
            source.getnframes(),
        )


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.astype("<i2").tobytes())


def test_pcmu_frames_write_decoded_wavs_with_correct_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tmp_path))
    pcm8k = _tone(440.0, 0.1, 8_000)
    pcm48k = _tone(440.0, 0.1, 48_000)

    tap = CallTap.create("call-pcmu", "pcmu", 8_000, 8_000)

    assert tap is not None
    tap.inbound_frame(ulaw_encode(pcm8k))
    tap.tts_pcm48k(pcm48k.tobytes())
    tap.outbound_frame(ulaw_encode(pcm8k))
    tap.event("marker", t=-1.0, milliseconds=12.5)
    tap.close()

    call_dir = tmp_path / "call-pcmu"
    assert _wav_params(call_dir / "inbound.wav") == (1, 2, 8_000, 800)
    assert _wav_params(call_dir / "tts_48k.wav") == (1, 2, 48_000, 4_800)
    assert _wav_params(call_dir / "outbound.wav") == (1, 2, 8_000, 800)
    event = json.loads((call_dir / "timings.jsonl").read_text().strip())
    assert event["event"] == "marker"
    assert event["milliseconds"] == 12.5
    assert event["t"] >= 0.0


def test_l16_frames_keep_supplied_rates_and_pcm_width(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tmp_path))
    pcm16k = _tone(1_000.0, 0.04, 16_000)

    tap = CallTap.create("call-l16", "l16", 16_000, 16_000)

    assert tap is not None
    tap.inbound_frame(pcm16k.tobytes())
    tap.outbound_frame(pcm16k.tobytes())
    tap.close()
    call_dir = tmp_path / "call-l16"
    assert _wav_params(call_dir / "inbound.wav") == (1, 2, 16_000, 640)
    assert _wav_params(call_dir / "outbound.wav") == (1, 2, 16_000, 640)


def test_disabled_mode_returns_none_and_creates_nothing(tmp_path, monkeypatch):
    output_root = tmp_path / "disabled"
    monkeypatch.delenv("NANO_CLAW_PHONE_TAP", raising=False)
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(output_root))

    tap = CallTap.create("never-created", "pcmu", 8_000, 8_000)

    assert tap is None
    assert not output_root.exists()


def test_unwritable_output_disables_without_raising(tmp_path, monkeypatch, caplog):
    output_root = tmp_path / "not-a-directory"
    output_root.write_text("a file cannot contain a call directory")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(output_root))
    caplog.set_level(logging.WARNING, logger="nano-claw.phone_tap")

    tap = CallTap.create("blocked", "pcmu", 8_000, 8_000)

    assert tap is None
    warnings = [record for record in caplog.records if "call tap disabled" in record.message]
    assert len(warnings) == 1


def test_runtime_failure_warns_once_and_all_public_methods_stay_safe(
    tmp_path, monkeypatch, caplog
):
    class BrokenWav:
        def writeframesraw(self, raw):
            raise OSError("disk disappeared")

        def close(self):
            raise OSError("still gone")

    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "1")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tmp_path))
    caplog.set_level(logging.WARNING, logger="nano-claw.phone_tap")
    tap = CallTap.create("runtime-failure", "l16", 16_000, 16_000)
    assert tap is not None
    original_inbound = tap._inbound
    tap._inbound = BrokenWav()

    tap.inbound_frame(b"\x00\x00")
    tap.tts_pcm48k(b"\x00\x00")
    tap.outbound_frame(b"\x00\x00")
    tap.event("ignored")
    tap.close()
    if original_inbound is not None:
        original_inbound.close()

    warnings = [record for record in caplog.records if "call tap disabled" in record.message]
    assert len(warnings) == 1


def test_report_runs_on_synthetic_fixture_and_prints_expected_sections(tmp_path):
    fixture = tmp_path / "synthetic-call"
    fixture.mkdir()
    _write_wav(fixture / "inbound.wav", _tone(6_000.0, 0.2, 16_000), 16_000)
    _write_wav(fixture / "tts_48k.wav", _tone(440.0, 0.2, 48_000), 48_000)
    _write_wav(fixture / "outbound.wav", _tone(6_000.0, 0.2, 16_000), 16_000)
    events = [
        {
            "event": "frames_sent",
            "t": 1.2,
            "sentence_index": 1,
            "count": 5,
            "first_frame_t": 1.0,
            "last_frame_t": 1.08,
            "last_frame_audio_ms": 20.0,
            "interval_p50_ms": 19.0,
            "interval_p95_ms": 21.0,
            "interval_max_ms": 22.0,
            "audio_s": 0.1,
            "elapsed_s": 0.09,
            "surplus_s": 0.01,
        },
        {"event": "barge_in", "t": 2.05, "sentence_index": 2},
        {
            "event": "frames_sent",
            "t": 2.1,
            "sentence_index": 2,
            "count": 4,
            "first_frame_t": 2.0,
            "last_frame_t": 2.06,
            "last_frame_audio_ms": 20.0,
            "interval_p50_ms": 20.0,
            "interval_p95_ms": 23.0,
            "interval_max_ms": 24.0,
            "audio_s": 0.08,
            "elapsed_s": 0.07,
            "surplus_s": 0.01,
        },
    ]
    (fixture / "timings.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )
    script = Path(__file__).parents[2] / "tools" / "phone_tap_report.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(fixture)],
        check=True,
        capture_output=True,
        text=True,
    )

    for expected in (
        "WAV summary",
        "Spectral energy above 4 kHz",
        "Inter-sentence gaps",
        "Pacing",
        "Barge-in to last outbound frame",
    ):
        assert expected in completed.stdout
    assert "inbound.wav" in completed.stdout
    assert "outbound.wav" in completed.stdout


class _RecordingTap:
    def __init__(self):
        self.events = []
        self.inbound = []
        self.tts = []
        self.outbound = []
        self.closed = False

    def event(self, name, **fields):
        self.events.append((name, fields))

    def inbound_frame(self, raw):
        self.inbound.append(raw)

    def tts_pcm48k(self, pcm):
        self.tts.append(pcm)

    def outbound_frame(self, raw):
        self.outbound.append(raw)

    def close(self):
        self.closed = True


class _FakeWs:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


def _install_recording_tap(monkeypatch, tap):
    class Factory:
        @staticmethod
        def create(call_id, codec, inbound_rate, outbound_rate):
            return tap

    monkeypatch.setattr(phone, "CallTap", Factory)
    monkeypatch.setattr(phone, "phone_codec", lambda: "pcmu")
    monkeypatch.setattr(phone, "get_vad_mode", lambda: "energy")
    monkeypatch.setattr(phone, "get_flow_mode", lambda: "off")


def test_phone_call_records_lifecycle_inbound_and_endpoint_events(monkeypatch):
    tap = _RecordingTap()
    _install_recording_tap(monkeypatch, tap)

    async def exercise():
        call = phone.PhoneCall(_FakeWs(), "integration-call")
        completed = []
        call._start_turn = completed.append
        tone = np.full(160, 2_000, dtype=np.int16)
        silence = np.zeros(160, dtype=np.int16)
        try:
            for frame in [tone] * 15 + [silence] * 35:
                encoded = ulaw_encode(frame)
                call.feed_media(base64.b64encode(encoded).decode())
            assert completed
        finally:
            await call.close()

    asyncio.run(exercise())
    event_names = [name for name, _ in tap.events]
    assert event_names[0] == "call_start"
    assert "utterance_start" in event_names
    assert "utterance_end" in event_names
    assert event_names[-1] == "call_end"
    assert len(tap.inbound) == 50
    assert tap.closed


def test_phone_call_records_stt_agent_synthesis_and_pacing(monkeypatch):
    tap = _RecordingTap()
    _install_recording_tap(monkeypatch, tap)
    pcm48k = _tone(440.0, 0.04, 48_000).tobytes()
    monkeypatch.setattr(phone, "tts_synthesize", lambda sentence, voice, speed: pcm48k)
    monkeypatch.setattr(phone.metrics_db, "bump_call_turns", lambda *args: None)

    class SttResponse:
        def json(self):
            return {"text": "hello"}

    class FakeHttp:
        async def post(self, *args, **kwargs):
            return SttResponse()

        async def aclose(self):
            return None

    class Reply:
        text = "agent reply"
        outcome = ""
        slots = {}
        done = False

    class FakeFlow:
        async def reply(self, text):
            return Reply()

    async def exercise():
        ws = _FakeWs()
        call = phone.PhoneCall(ws, "timed-call")
        await call._http.aclose()
        call._http = FakeHttp()
        assert await call._transcribe(b"\x00\x00") == "hello"
        monkeypatch.setattr(phone, "get_flow_mode", lambda: "scheduler")
        call.flow = FakeFlow()

        async def no_speak(text):
            return None

        call.speak = no_speak
        await call._run_turn(b"\x00\x00")
        call.speaking = True
        await call._speak_chunk("one sentence")
        call.speaking = False
        await call.close()
        return ws

    ws = asyncio.run(exercise())
    event_names = [name for name, _ in tap.events]
    for expected in ("stt_done", "agent_done", "synth_start", "synth_done", "frames_sent"):
        assert expected in event_names
    frames_event = next(fields for name, fields in tap.events if name == "frames_sent")
    synth_event = next(fields for name, fields in tap.events if name == "synth_done")
    assert frames_event["count"] == len(ws.messages) == len(tap.outbound) == 2
    assert {"interval_p50_ms", "interval_p95_ms", "interval_max_ms", "surplus_s"} <= frames_event.keys()
    assert synth_event["samples"] == len(pcm48k) // 2
    assert tap.tts == [pcm48k]
