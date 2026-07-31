"""A keypress is a caller turn.

This module had no DTMF handling of any kind — `dtmf` did not appear in
voice/phone.py — while delegated lines answer with greetings like "Press one for
a new maintenance request". A real call on 2026-07-31 pressed 1 and got silence;
speech on the same call worked, which is the signature of a missing input path
rather than a routing bug.

The carrier sends keypresses down the media WebSocket beside the audio, which is
where riff's live path has always read them from (riff/phone/server.py).
"""
from __future__ import annotations

import asyncio

from voice.phone import PhoneCall


class _Tap:
    def __init__(self):
        self.events = []

    def event(self, name, **kw):
        self.events.append((name, kw))


def _bare_call(*, speaking: bool = False, closed: bool = False) -> PhoneCall:
    """A PhoneCall with only the attributes the DTMF path reads."""
    call = object.__new__(PhoneCall)
    call.call_id = "testcall01234567"
    call.closed = closed
    call.speaking = speaking
    call.interrupted = False
    call.tap = _Tap()
    call._turn_task = None
    call.spoken = []
    call.flushed = 0
    call.activity = 0
    call.cues_stopped = 0

    async def _stream_reply(text):
        call.spoken.append(text)

    async def _flush_playback():
        call.flushed += 1

    call._stream_reply = _stream_reply
    call._flush_playback = _flush_playback
    call._mark_activity = lambda: setattr(call, "activity", call.activity + 1)
    call._stop_thinking_cue = lambda: setattr(
        call, "cues_stopped", call.cues_stopped + 1)
    call._turn_finished = lambda task: None
    return call


def _press(call: PhoneCall, digit) -> None:
    """Press a key and let the turn it starts finish."""

    async def exercise():
        await call.inject_dtmf(digit)
        if call._turn_task is not None:
            await call._turn_task

    asyncio.run(exercise())


def test_a_digit_becomes_a_caller_turn():
    call = _bare_call()

    _press(call, "1")

    assert call.spoken == ["1"], "the digit must reach the turn path verbatim"


def test_a_digit_during_speech_is_a_barge_in():
    """Standing operator ruling: a caller who presses a key while the menu is
    playing has heard enough. Queueing behind the readback would make the key
    feel dead — the very complaint that found this."""
    call = _bare_call(speaking=True)

    _press(call, "2")

    assert call.speaking is False
    assert call.interrupted is True
    assert call.flushed == 1, "the buffered reply must be dropped, not played out"
    assert call.spoken == ["2"]


def test_a_quiet_line_is_not_flushed_for_no_reason():
    call = _bare_call(speaking=False)

    _press(call, "1")

    assert call.flushed == 0
    assert call.interrupted is False


def test_a_digit_on_a_closed_call_does_nothing():
    call = _bare_call(closed=True)

    _press(call, "3")

    assert call.spoken == []


def test_an_empty_digit_is_not_a_turn():
    """Telnyx sends the key in `dtmf.digit`; a frame without one must not open a
    turn with an empty utterance, which downstream reads as 'the caller said
    nothing' and answers a question nobody asked."""
    call = _bare_call()

    _press(call, "")
    _press(call, None)

    assert call.spoken == []


def test_the_keypress_is_recorded_for_review():
    call = _bare_call()

    _press(call, "4")

    assert ("dtmf", {"digit": "4"}) in call.tap.events
