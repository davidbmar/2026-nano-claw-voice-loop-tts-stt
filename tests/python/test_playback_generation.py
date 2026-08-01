from concurrent.futures import ThreadPoolExecutor
import threading

from voice import tts
from voice.webrtc import Session


class UnitTransport:
    sample_rate = 48_000
    playback_sample_rate = 48_000

    def __init__(self):
        self.generator = None
        self.session = None

    def attach_session(self, session):
        self.session = session

    def set_generator(self, generator):
        self.generator = generator

    def clear_generator(self):
        self.generator = None

    def prepare_tts(self, pcm):
        return pcm


def test_late_synthesis_cannot_refill_a_cancelled_generation(monkeypatch):
    synthesis_started = threading.Event()
    release_synthesis = threading.Event()
    pcm = b"\x01\x02" * 960

    def delayed_synthesis(*_args):
        synthesis_started.set()
        assert release_synthesis.wait(timeout=2)
        return pcm

    monkeypatch.setattr(tts, "synthesize", delayed_synthesis)
    session = Session(UnitTransport())
    token = session.begin_stream()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(session.enqueue_chunk, "late answer", "voice", 1.0)
        assert synthesis_started.wait(timeout=1)
        receipt = session.cancel_stream(reason="manual_stop")
        release_synthesis.set()
        assert pending.result(timeout=1) == 0

    assert receipt["utterance_id"] == token.utterance_id
    assert receipt["status"] == "not_delivered"
    assert session._audio_queue.available == 0
    assert session.late_audio_drops == 1


def test_generations_are_monotonic_and_stale_generators_return_silence():
    transport = UnitTransport()
    session = Session(transport)
    first = session.begin_stream()
    stale_generator = transport.generator
    session.cancel_stream(reason="superseded")
    second = session.begin_stream()

    stale = stale_generator.next_chunk()

    assert second.generation > first.generation
    assert stale.payload_bytes == 0
    assert stale.samples == bytes(len(stale.samples))
    assert session.active_playback == second


def test_receipt_distinguishes_partial_from_not_delivered():
    session = Session(UnitTransport())
    partial_token = session.begin_stream()
    assert session.enqueue_synthesized_chunk(
        partial_token, b"\x01\x02" * 1_000, audio_role="answer"
    ) == 2_000
    _frame, payload_bytes = session.read_playback_frame(partial_token, 960)
    assert session.confirm_playback_bytes(partial_token, payload_bytes)

    partial = session.cancel_stream(reason="confirmed_barge_in")

    assert partial["status"] == "partial"
    assert partial["chunks"][0]["status"] == "partial"
    assert partial["chunks"][0]["played_audio_ms"] > 0

    unheard_token = session.begin_stream()
    session.enqueue_synthesized_chunk(
        unheard_token, b"\x03\x04" * 100, audio_role="answer"
    )
    unheard = session.cancel_stream(reason="manual_stop")

    assert unheard["status"] == "not_delivered"
    assert unheard["chunks"][0]["status"] == "not_delivered"


def test_fully_confirmed_chunk_is_completed_even_if_stop_arrives_afterward():
    session = Session(UnitTransport())
    token = session.begin_stream()
    session.enqueue_synthesized_chunk(token, b"\x01\x02" * 20)
    _frame, payload_bytes = session.read_playback_frame(token, 40)
    session.confirm_playback_bytes(token, payload_bytes)

    receipt = session.stop_speaking(reason="manual_stop")

    assert receipt["status"] == "completed"
    assert receipt["chunks"][0]["status"] == "finished"


def test_requested_chunk_ids_are_unique_within_one_playback():
    session = Session(UnitTransport())
    token = session.begin_stream()

    session.enqueue_synthesized_chunk(token, b"\x01\x02", chunk_id="chunk_0")
    session.enqueue_synthesized_chunk(token, b"\x03\x04", chunk_id="chunk_0")
    receipt = session.cancel_stream(reason="test")

    assert [chunk["chunk_id"] for chunk in receipt["chunks"]] == [
        "chunk_0",
        "chunk_0_1",
    ]
