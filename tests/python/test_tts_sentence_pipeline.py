import asyncio
import time

from voice import phone
from voice.phone import PhoneCall
from voice.sentence_pipeline import SentencePipeline
from voice.speech_preparer import FINAL_TAIL_PAD_MS, SpeechChunk


def run(coro):
    return asyncio.run(coro)


def test_next_synthesis_overlaps_playback_with_bounded_ordered_lookahead():
    synth_delay = 0.08
    playback_time = 0.14

    async def exercise():
        synth_started = {}
        playback_started = {}
        playback_ended = {}
        played = []
        ahead_events = []

        async def sentences():
            for sentence in ("one", "two", "three"):
                yield sentence

        async def synthesize(sentence):
            synth_started[sentence] = time.monotonic()
            await asyncio.sleep(synth_delay)
            return f"audio:{sentence}"

        def record_ahead(ready, wait_s):
            ahead_events.append(
                ("synth_ahead_hit" if ready else "synth_ahead_miss", wait_s)
            )

        pipeline = SentencePipeline(
            sentences(), synthesize, on_ahead=record_ahead
        )
        async with pipeline:
            async for item in pipeline:
                played.append((item.text, item.audio))
                playback_started[item.text] = time.monotonic()
                await asyncio.sleep(playback_time)
                playback_ended[item.text] = time.monotonic()

        return (
            synth_started,
            playback_started,
            playback_ended,
            played,
            ahead_events,
        )

    synth_started, playback_started, playback_ended, played, events = run(
        exercise()
    )

    assert played == [
        ("one", "audio:one"),
        ("two", "audio:two"),
        ("three", "audio:three"),
    ]
    assert synth_started["two"] < playback_ended["one"]
    assert playback_started["two"] - playback_ended["one"] < synth_delay / 3
    # Sentence three cannot start synthesizing while sentence one is playing:
    # there is exactly one item of synthesis look-ahead.
    assert synth_started["three"] >= playback_ended["one"]
    assert [name for name, _ in events] == [
        "synth_ahead_hit",
        "synth_ahead_hit",
    ]


def test_barge_in_cleanup_cancels_and_awaits_pending_synthesis():
    async def exercise():
        second_started = asyncio.Event()
        second_cancelled = asyncio.Event()
        never_finish = asyncio.Event()
        loop_errors = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def synthesize(sentence):
            if sentence == "one":
                return "audio:one"
            second_started.set()
            try:
                await never_finish.wait()
            except asyncio.CancelledError:
                second_cancelled.set()
                raise

        try:
            pipeline = SentencePipeline(("one", "two"), synthesize)
            async with pipeline:
                async for item in pipeline:
                    assert item.text == "one"
                    await second_started.wait()
                    break  # simulated barge-in: stop consuming immediately
            await asyncio.sleep(0)
            pending_prefetches = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name() == "sentence-synthesis-prefetch"
                and not task.done()
            ]
            return second_cancelled.is_set(), pending_prefetches, loop_errors
        finally:
            loop.set_exception_handler(previous_handler)

    cancelled, pending, loop_errors = run(exercise())
    assert cancelled
    assert pending == []
    assert loop_errors == []


def test_failed_prefetch_does_not_interrupt_current_or_later_playback():
    async def exercise():
        failed_at = None
        first_playback_ended = None
        played = []
        errors = []
        ahead = []

        async def synthesize(sentence):
            nonlocal failed_at
            if sentence == "two":
                await asyncio.sleep(0.02)
                failed_at = time.monotonic()
                raise RuntimeError("fake synthesis failure")
            await asyncio.sleep(0)
            return f"audio:{sentence}"

        pipeline = SentencePipeline(
            ("one", "two", "three"),
            synthesize,
            on_error=lambda sentence, error: errors.append((sentence, str(error))),
            on_ahead=lambda ready, wait_s: ahead.append((ready, wait_s)),
        )
        async with pipeline:
            async for item in pipeline:
                played.append(item.text)
                if item.text == "one":
                    await asyncio.sleep(0.06)
                    first_playback_ended = time.monotonic()

        return failed_at, first_playback_ended, played, errors, ahead

    failed_at, first_ended, played, errors, ahead = run(exercise())
    assert failed_at is not None and first_ended is not None
    assert failed_at < first_ended
    assert played == ["one", "three"]
    assert errors == [("two", "fake synthesis failure")]
    assert [ready for ready, _ in ahead] == [True, False]


def test_phone_tap_records_synthesis_ahead_hit_and_miss():
    class RecordingTap:
        def __init__(self):
            self.events = []

        def event(self, name, **fields):
            self.events.append((name, fields))

    tap = RecordingTap()
    call = object.__new__(PhoneCall)
    call.tap = tap
    call._tap_sentence_index = 7

    call._record_synth_ahead(True, 0.0)
    call._record_synth_ahead(False, 0.025)

    assert [name for name, _ in tap.events] == [
        "synth_ahead_hit",
        "synth_ahead_miss",
    ]
    assert tap.events[1][1] == {"sentence_index": 7, "wait_ms": 25.0}


def test_complete_and_streaming_phone_paths_share_the_sentence_pipeline(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEECH_PREPARATION", "raw")
    class FakeResponse:
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            for line in (
                "event: delta",
                'data: {"text": "Three. Four. "}',
                "",
                "event: final",
                'data: {"text": ""}',
                "",
            ):
                yield line

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class FakeHttp:
        def stream(self, *args, **kwargs):
            return FakeStream()

        async def aclose(self):
            return None

    async def exercise():
        call = PhoneCall(object(), "pipeline-paths")
        await call._http.aclose()
        call._http = FakeHttp()
        synthesized = []
        played = []

        async def synthesize(sentence):
            synthesized.append(sentence)
            await asyncio.sleep(0)
            return f"audio:{sentence}"

        async def play(audio):
            played.append(audio)
            await asyncio.sleep(0)

        call._synthesize_sentence = synthesize
        call._play_synthesized = play
        try:
            await call.speak("One. Two.")
            complete = (synthesized[:], played[:])
            synthesized.clear()
            played.clear()
            await call._stream_reply("caller text")
            streaming = (synthesized[:], played[:])
            return complete, streaming
        finally:
            await call.close()

    complete, streaming = run(exercise())
    assert complete == (
        ["One.", "Two."],
        ["audio:One.", "audio:Two."],
    )
    assert streaming == (
        ["Three.", "Four."],
        ["audio:Three.", "audio:Four."],
    )


def test_prepared_phone_units_carry_normalized_text_and_declared_pauses(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEECH_PREPARATION", "1")
    units = PhoneCall._speech_units(
        "## Next steps\n1. Call (512) 555-0184.\n2. Meet at 3:30 PM."
    )

    assert units
    assert all(isinstance(unit, SpeechChunk) for unit in units)
    assert "#" not in " ".join(unit.text for unit in units)
    assert "five one two" in " ".join(unit.text for unit in units)
    assert units[-1].pause_after_ms == FINAL_TAIL_PAD_MS

    captured = []

    def fake_synthesize(text, voice, speed, pause_after_ms):
        captured.append((text, voice, speed, pause_after_ms))
        return b"\x01\x02"

    monkeypatch.setattr(phone, "tts_synthesize", fake_synthesize)
    call = PhoneCall.__new__(PhoneCall)
    call.tap = None
    speech = run(call._synthesize_sentence(units[0]))

    assert speech.pcm48k == b"\x01\x02"
    assert captured == [
        (units[0].text, "af_heart", 1.0, units[0].pause_after_ms)
    ]


def test_phone_hangup_cancels_active_pipeline_synthesis():
    class ClosedWebSocket:
        closed = True

    async def exercise():
        call = PhoneCall(ClosedWebSocket(), "pipeline-hangup")
        synthesis_started = asyncio.Event()
        synthesis_cancelled = asyncio.Event()
        never_finish = asyncio.Event()

        async def synthesize(sentence):
            synthesis_started.set()
            try:
                await never_finish.wait()
            except asyncio.CancelledError:
                synthesis_cancelled.set()
                raise

        call._synthesize_sentence = synthesize
        speaking = asyncio.create_task(call.speak("One."))
        await synthesis_started.wait()
        await call.close()
        try:
            await speaking
        except asyncio.CancelledError:
            pass
        return synthesis_cancelled.is_set(), call._sentence_pipelines

    cancelled, pipelines = run(exercise())
    assert cancelled
    assert pipelines == set()
