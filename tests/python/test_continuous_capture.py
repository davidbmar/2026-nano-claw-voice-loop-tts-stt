"""Audio between turns must survive in transcribe mode.

Reproduces the reported failure exactly. A user counted from one to twenty; the
capture began at eleven. The cause is not VAD tuning and not STT:

    if self._recording:
        self._mic_frames.append(frame)
    else:
        self._mic_preroll.append(frame)     # deque(maxlen=30) == 600ms

Between turns, frames land in a bounded ring holding 600ms. Speaking for twelve
seconds while no turn is open leaves 600ms of it; the other 11.4s is overwritten
and gone. That is correct for normal turn-taking — the gap there is the
assistant speaking, and recording through it would feed playback back into STT.
It is precisely wrong for a mode whose only job is to miss nothing.

These tests drive frames directly rather than synthesizing audio, so the loss is
counted rather than listened for.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voice import webrtc


class FrameSink(webrtc.Session):
    """A session with the transport stripped out, so frames can be fed by hand.

    Subclasses rather than mocks the buffer logic, because the buffer logic IS
    what is under test — a double would assert against a reimplementation of the
    bug.
    """

    def __init__(self) -> None:  # noqa: D107 - deliberately skips real __init__
        self._recording = False
        self._continuous = False
        self._mic_frames = []
        self._mic_preroll = webrtc.deque(maxlen=webrtc.MIC_PREROLL_FRAMES)
        self._mic_sample_rate = 16000
        self._mic_track = None

    def feed(self, count: int, marker: bytes = b"\x01\x02") -> None:
        """Push `count` frames, each 20ms of PCM16 at 16kHz (640 bytes)."""
        for _ in range(count):
            self.receive_mic_pcm(marker * 320)

    @property
    def buffered_frames(self) -> int:
        return len(self._mic_frames)


def _session_with_pcm_hook() -> FrameSink:
    """Fail loudly if the frame handler is renamed, rather than passing vacuously."""
    if not hasattr(webrtc.Session, "receive_mic_pcm"):
        pytest.fail("voice.webrtc.Session.receive_mic_pcm was renamed; this "
                    "test drives it directly and must be updated")
    return FrameSink()


# ------------------------------------------------------------------- the bug


def test_normal_mode_still_discards_between_turns():
    """The existing behaviour must not change for speaking modes.

    Recording through the gap there would capture the assistant's own playback
    and feed it back into STT — the reason the ring is bounded in the first
    place.
    """
    s = _session_with_pcm_hook()
    s._continuous = False
    s._recording = False

    s.feed(200)  # 4 seconds while no turn is open

    assert len(s._mic_preroll) == webrtc.MIC_PREROLL_FRAMES, (
        "the pre-roll ring must stay bounded in normal mode"
    )
    assert s.buffered_frames == 0


def test_continuous_mode_keeps_everything_said_between_turns():
    """The reported bug: counting between turns must not vanish.

    600ms survived before; this asserts all twelve seconds do.
    """
    s = _session_with_pcm_hook()
    s.set_continuous_capture(True)
    s._recording = False

    s.feed(600)  # 12 seconds — the counting window

    assert s.buffered_frames == 600, (
        f"continuous capture kept only {s.buffered_frames}/600 frames — audio "
        "between turns is still being discarded"
    )


def test_the_gap_during_transcription_is_covered():
    """Frames arriving WHILE stt runs belong to the next turn, not the void.

    `stop_recording` sets `_recording = False` and then transcribes, which takes
    seconds. Without continuous mode every frame in that window falls into the
    600ms ring — this is the second, subtler half of the same loss.
    """
    s = _session_with_pcm_hook()
    s.set_continuous_capture(True)
    s._recording = True
    s.feed(50)

    # Simulate what stop_recording does to the flags, then keep speaking.
    s._recording = False
    s._mic_frames.clear()
    s.feed(150)  # 3 seconds of speech during transcription

    assert s.buffered_frames == 150, (
        f"only {s.buffered_frames}/150 frames survived the transcription "
        "window — the STT gap is still deaf"
    )


def test_starting_a_turn_does_not_wipe_the_buffer():
    """`start_recording` resets `_mic_frames` — which would undo all of this.

    This is the trap in the fix rather than in the original bug: continuous
    capture accumulates between turns, and the very next `mic_start` would throw
    it away, restoring the reported symptom while every other test still passed.
    """
    s = _session_with_pcm_hook()
    s.set_continuous_capture(True)
    s._recording = False
    s.feed(300)  # 6 seconds captured between turns

    s.start_recording()

    assert s.buffered_frames == 300, (
        f"start_recording discarded the backlog ({s.buffered_frames}/300 left) "
        "— audio captured between turns must carry into the turn"
    )


def test_enabling_mid_session_adopts_the_preroll():
    """Switching the dropdown must not begin with a hole."""
    s = _session_with_pcm_hook()
    s._continuous = False
    s._recording = False
    s.feed(10)  # sitting in the pre-roll ring

    s.set_continuous_capture(True)

    assert s.buffered_frames == 10
    assert not s._mic_preroll, "the pre-roll should be handed over, not copied"


# ---------------------------------------------------------------- the backstop


def test_a_monologue_cannot_exhaust_memory():
    """Bounded even if VAD stops segmenting entirely.

    Drops the OLDEST audio so the most recent speech survives, and warns —
    silent truncation here would be the same bug at a larger timescale.
    """
    s = _session_with_pcm_hook()
    s.set_continuous_capture(True)
    s._recording = True

    seconds = webrtc.MAX_CONTINUOUS_SECONDS + 30
    s.feed(seconds * 50)  # 50 frames/sec at 20ms

    held_bytes = sum(len(f) for f in s._mic_frames)
    cap = 16000 * 2 * webrtc.MAX_CONTINUOUS_SECONDS
    assert held_bytes <= cap, (
        f"buffer grew to {held_bytes} bytes, past the {cap}-byte backstop"
    )


def test_the_backstop_is_generous_enough_to_never_fire_normally():
    """A minute of uninterrupted speech must not trip it.

    If the cap were tight it would become the new source of loss, and the
    warning would be routine enough to ignore.
    """
    s = _session_with_pcm_hook()
    s.set_continuous_capture(True)
    s._recording = True

    s.feed(60 * 50)  # a full minute

    assert s.buffered_frames == 60 * 50


# ------------------------------------------------------------------ the wiring


def test_the_server_enables_it_for_transcribe_mode_only():
    """Ungated, this would record the assistant's own voice in every mode."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "server.py").read_text()
    call = source.split("set_continuous_capture", 1)
    assert len(call) > 1, "the server never enables continuous capture"
    following = call[1][:200]
    assert "is_transcribe_mode()" in following, (
        "continuous capture must be enabled with is_transcribe_mode(), not "
        "unconditionally — every speaking mode would record its own playback"
    )


# ------------------------------------------------- server-clocked segmentation


def test_transcribe_mode_segments_on_a_server_clock():
    """Browser VAD runs on requestAnimationFrame, which stops in a hidden tab.

    That is why switching windows to play audio at the page captured nothing:
    no turn ever opened, while the mic kept streaming from the audio thread.
    A server-side timer has no opinion about which window is in front.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "server.py").read_text()
    assert "TRANSCRIBE_FLUSH_SECONDS" in source
    assert "_transcribe_flush_loop" in source
    loop = source.split("async def _transcribe_flush_loop", 1)[1].split("\n    def ", 1)[0]
    assert "is_transcribe_mode()" in loop, (
        "the flush loop must no-op outside transcribe mode; unguarded it would "
        "cut turns underneath every speaking mode's own VAD"
    )
    assert "stop_recording" in loop


def test_the_browser_disables_vad_only_for_transcribe():
    """Every other mode must keep VAD — it ends a turn when the human stops.

    A fixed interval cannot know that, so applying the lock everywhere would
    make normal conversation cut mid-sentence every 8 seconds.
    """
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[2] / "voice" / "web" / "app.js").read_text()
    start = app_js.split("async function startPhoneMode()", 1)[1].split("\n}", 1)[0]
    assert "flowSelect.value === 'transcribe'" in start, (
        "the mic lock must be gated on transcribe mode"
    )
    lock_branch = start.split("flowSelect.value === 'transcribe'", 1)[1].split("return;", 1)[0]
    assert "requestAnimationFrame" not in lock_branch, (
        "the transcribe branch must not start the rAF VAD loop — that loop is "
        "exactly what stops firing in a hidden tab"
    )
    assert "requestAnimationFrame(monitorPhoneAudio)" in start, (
        "the normal VAD path must still exist for every other mode"
    )


def test_stopping_the_mic_flushes_rather_than_discards():
    """`mic_cancel` DISCARDS the buffer — up to 8s of untranscribed speech.

    Stopping the microphone must not be the one action that loses audio.
    """
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[2] / "voice" / "web" / "app.js").read_text()
    stop = app_js.split("function stopPhoneMode(", 1)[1].split("\n}", 1)[0]
    lock_path = stop.split("if (transcribeLock)", 1)
    assert len(lock_path) == 2, "stopPhoneMode has no transcribe-lock branch"
    branch = lock_path[1].split("} else", 1)[0]
    # Match the CALL, not the word — the branch's comment names mic_cancel to
    # explain why it is wrong, and a substring check would trip on the prose.
    assert "sendMsg('mic_stop')" in branch, (
        "the locked path must flush the buffer with mic_stop"
    )
    assert "sendMsg('mic_cancel')" not in branch, (
        "the locked path must never discard the buffer with mic_cancel"
    )
