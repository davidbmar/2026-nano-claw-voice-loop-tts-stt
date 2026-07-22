import asyncio
import json
from types import SimpleNamespace

import numpy as np

from voice import phone, server
from voice.processing_audio import CHIME_SECONDS, PEAK_AMPLITUDE, SAMPLE_RATE, processing_chime


def run(coro):
    return asyncio.run(coro)


def test_processing_chime_is_quiet_click_free_pcm16():
    pcm = processing_chime()
    samples = np.frombuffer(pcm, dtype=np.int16)

    assert len(samples) == int(SAMPLE_RATE * CHIME_SECONDS)
    assert samples[0] == 0
    assert np.max(np.abs(samples.astype(np.int32))) <= PEAK_AMPLITUDE
    assert processing_chime() is pcm


def test_phone_processing_marker_bypasses_tts():
    call = phone.PhoneCall.__new__(phone.PhoneCall)
    call.tap = None

    speech = run(call._synthesize_sentence(phone.PROCESSING_CUE_SENTINEL))

    assert speech.pcm48k == processing_chime()
    assert speech.sentence_index is None


class FakeResponse:
    def __init__(self, events):
        self.events = events

    async def aiter_lines(self):
        for event, payload in self.events:
            yield f"event: {event}"
            yield f"data: {json.dumps(payload)}"
            yield ""


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.closed = False

    async def send_json(self, message):
        self.messages.append(message)


class FakeSession:
    def __init__(self):
        self.voice_id = "test"
        self.speed = 1.0
        self.chunks = []
        self.pcm = []
        self.total_bytes = None
        self._history_agent_active = True
        self._history_agent_failed = False
        self._history_agent_parts = []
        self._turn = {}
        self._backoff = SimpleNamespace(reset=lambda: None)

    def set_stream_task(self, _task):
        return None

    def begin_stream(self):
        return None

    def enqueue_chunk(self, text, _voice, _speed):
        self.chunks.append(text)
        return 100

    def enqueue_pcm(self, pcm):
        self.pcm.append(pcm)
        return len(pcm)

    async def end_stream(self, total_bytes):
        self.total_bytes = total_bytes

    def stop_speaking(self):
        return None


def test_browser_voice_speaks_acknowledgement_and_plays_progress_cue(monkeypatch):
    monkeypatch.setattr(server, "DEEP_PROCESSING_CUE_INTERVAL_S", 0.0)
    response = FakeResponse(
        [
            (
                "deep_started",
                {
                    "acknowledgement": "Let me think deeply about this.",
                    "score": 6,
                    "reasons": ["cross_evidence_synthesis"],
                },
            ),
            (
                "deep_progress",
                {
                    "phase": "reasoning",
                    "message": "Evaluating evidence pass 1.",
                    "completedSteps": 0,
                    "maxSteps": 6,
                    "retrievalQueries": 1,
                },
            ),
            ("delta", {"text": "The two phases form a sequence."}),
            (
                "final",
                {"response": "The two phases form a sequence.", "debug": {}},
            ),
        ]
    )
    websocket = FakeWebSocket()
    session = FakeSession()

    run(server._consume_sse(websocket, session, response, req_start=0.0))

    assert session.chunks == [
        "Let me think deeply about this.",
        "The two phases form a sequence.",
    ]
    assert session.pcm == [processing_chime()]
    assert session.total_bytes == 200 + len(processing_chime())
    assert any(message["type"] == "deep_thinking" for message in websocket.messages)
    assert any(message["type"] == "deep_progress" for message in websocket.messages)
