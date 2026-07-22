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
        self._deep_projection_pending = False
        self._paused = False
        self.resumed = 0

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

    def is_paused(self):
        return self._paused

    def resume_speaking(self):
        self._paused = False
        self.resumed += 1


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
                    "currentPass": 1,
                    "completedPasses": 0,
                    "maxPasses": 6,
                    "retrievalPlanned": 5,
                    "retrievalCompleted": 5,
                    "evidenceItems": 19,
                    "model": {
                        "provider": "deepseek",
                        "name": "deepseek-v4-pro",
                        "thinking": "enabled",
                        "effort": "high",
                    },
                    "artifactStatus": "not_applicable",
                    "phaseStartedAt": "2026-07-22T01:00:54Z",
                    "heartbeatAt": "2026-07-22T01:01:34Z",
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
    progress = next(
        message for message in websocket.messages if message["type"] == "deep_progress"
    )
    assert progress["currentPass"] == 1
    assert progress["retrievalCompleted"] == 5
    assert progress["evidenceItems"] == 19
    assert progress["model"]["name"] == "deepseek-v4-pro"
    assert progress["heartbeatAt"] == "2026-07-22T01:01:34Z"
    assert any(
        message["type"] == "deep_projection_ready"
        for message in websocket.messages
    )
    assert session._deep_projection_pending is False


def test_deep_projection_barge_in_is_suppressed_until_answer_audio():
    websocket = FakeWebSocket()
    session = FakeSession()
    session._deep_projection_pending = True
    session._paused = True

    suppressed = run(
        server._suppress_deep_projection_barge_in(websocket, session)
    )

    assert suppressed is True
    assert session.resumed == 1
    assert websocket.messages == [
        {
            "type": "barge_in_suppressed",
            "reason": "deep_projection_pending",
        }
    ]

    session._deep_projection_pending = False
    assert (
        run(server._suppress_deep_projection_barge_in(websocket, session))
        is False
    )
