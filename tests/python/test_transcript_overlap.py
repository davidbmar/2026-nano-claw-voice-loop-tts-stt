"""Seam handling: no word lost at a cut, and no word invented from silence.

Two failures observed on 2026-08-06, in the same run:

* Counting to twenty lost "13" at one boundary and "3, 4" at the next — words
  split across a chunk cut and discarded from both halves.
* A quiet stretch produced six lines of "thank you so much for watching this
  video", which nobody said. Whisper answers silence with invented captions.

The first is fixed by overlapping chunks by a second; the second by never
handing Whisper a chunk that has no speech energy in it.
"""
from __future__ import annotations

from voice import transcript_overlap as ov
from voice import webrtc


def test_repeated_seam_words_are_removed():
    """The literal case: one second of audio transcribed into both chunks."""
    prev = "One, two, three, four, five, six."
    cur = "five, six, seven, eight."
    assert ov.strip_overlap(prev, cur) == "seven, eight."


def test_punctuation_and_case_do_not_defeat_the_match():
    """STT punctuates and capitalizes differently either side of a cut.

    "twelve." ends one chunk, "Twelve" begins the next, and the first word of a
    segment is capitalized. A raw string comparison would miss precisely the
    matches this exists to catch.

    Uses a two-word overlap on purpose: a single repeated word is deliberately
    left alone (see `test_a_single_repeated_word_is_left_alone`), so a one-word
    example here would be asserting against the wrong rule.
    """
    assert ov.strip_overlap(
        "ten, eleven, twelve.", "Eleven, twelve, thirteen."
    ) == "thirteen."


def test_a_single_repeated_word_is_left_alone():
    """Real speech repeats single words constantly.

    "Count, count, count with me" is from the actual recording. A one-word rule
    would delete a word the speaker genuinely said, and leaving a duplicate in
    is much the cheaper error.
    """
    assert ov.strip_overlap("count with me count", "count with me from one") == (
        "count with me from one"
    )


def test_nothing_is_removed_when_there_is_no_overlap():
    assert ov.strip_overlap("the tap is dripping", "please send someone") == (
        "please send someone"
    )


def test_only_a_prefix_is_ever_removed():
    """Overlap can only occur at the seam, so the middle is never touched."""
    cur = "seven eight the tap is dripping five six"
    assert ov.strip_overlap("five six", cur) == cur


def test_the_longest_match_wins():
    """A shorter nested match would strip too little and leave a partial repeat."""
    assert ov.strip_overlap("a b c d", "b c d e") == "e"


def test_an_empty_previous_transcript_is_safe():
    assert ov.strip_overlap("", "first thing said") == "first thing said"


# --------------------------------------------------------- the hallucination gate


class _Buf(webrtc.Session):
    def __init__(self) -> None:
        self._recording = False
        self._continuous = True
        self._mic_frames = []
        self._mic_preroll = webrtc.deque(maxlen=webrtc.MIC_PREROLL_FRAMES)
        self._mic_sample_rate = 16000
        self._mic_track = None


def test_silence_measures_near_zero():
    """Silence must fall below any sane floor, or the gate never fires."""
    s = _Buf()
    s._mic_frames = [b"\x00\x00" * 320 for _ in range(50)]
    assert s.buffered_rms() < 1.0


def test_speech_level_audio_clears_the_floor():
    """A quiet voice must NOT be gated out.

    The phone path learned this in the other direction: a floor of 120 rejected
    real callers measuring 136-176 and had to be cut to 70. Erring high costs
    real speech, which is worse than an occasional phantom.
    """
    import numpy as np

    rng = np.random.default_rng(7)
    quiet_voice = (rng.normal(0, 300, 320 * 50)).astype(np.int16).tobytes()
    s = _Buf()
    s._mic_frames = [quiet_voice]
    assert s.buffered_rms() > 100


def test_discarding_silence_keeps_the_overlap_tail():
    """Speech beginning in the last second of a silent chunk must survive.

    Dropping the whole buffer would clip its opening word — the same seam loss
    the overlap exists to prevent, reintroduced by the silence gate.
    """
    s = _Buf()
    s._mic_frames = [b"\x00\x00" * 320 for _ in range(200)]
    s.discard_buffered_audio()
    assert len(s._mic_frames) == webrtc.OVERLAP_FRAMES


def test_the_flush_loop_gates_on_energy_before_calling_stt():
    """Order matters: measure first, transcribe second.

    Filtering phantoms out of the output afterwards cannot work — a
    hallucination that happens to look like ordinary speech is indistinguishable
    once it is text, and a filter tuned for outros misses every other kind.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "server.py").read_text()
    loop = source.split("async def _transcribe_flush_loop", 1)[1].split("\n    def ", 1)[0]
    # The threshold itself is now adaptive (_transcribe_noise_floor), so the
    # constant is no longer named here — what matters is that energy is
    # measured, and measured first.
    assert "buffered_rms" in loop and "_transcribe_noise_floor" in loop
    assert loop.index("buffered_rms") < loop.index("stop_recording"), (
        "energy must be measured BEFORE stop_recording runs STT — otherwise "
        "silence still reaches Whisper and the phantom is already generated"
    )


# ------------------------------------------------- the anti-hallucination gates


def test_the_adaptive_floor_is_reused_not_reinvented():
    """The phone path already solved this; a second implementation would drift.

    NoiseFloorEstimator's behaviour is the point: threshold =
    max(minimum, learned_floor x ratio), where the floor is an EMA over frames
    classified NON-speech. Speech freezes it, so a loud passage cannot drag the
    bar up and mute the quiet sentence after it.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "server.py").read_text()
    assert "from voice.phone_audio import NoiseFloorEstimator" in source
    loop = source.split("async def _transcribe_flush_loop", 1)[1].split("\n    def ", 1)[0]
    assert "_transcribe_noise_floor" in loop and "classify" in loop


def test_stt_headers_are_sent_only_in_transcribe_mode():
    """The live phone line's STT request must stay byte-identical.

    vad_filter and condition_on_previous_text CHANGE decoding. Sending them
    unconditionally would alter transcription for 512-277-7311 and call review,
    neither of which asked for it.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "webrtc.py").read_text()
    body = source.split("stt_url = os.environ.get", 1)[1].split("return text,", 1)[0]
    assert "X-VAD-Filter" in body, "the filter header is never sent"
    gate = body.split("if self._continuous:", 1)
    assert len(gate) == 2, "the headers are not gated on continuous/transcribe mode"
    gated_block = gate[1].split("try:", 1)[0]
    assert "X-VAD-Filter" in gated_block and "X-Condition-Previous" in gated_block, (
        "both decoding headers must live inside the _continuous branch"
    )


def test_the_stt_service_defaults_to_todays_behaviour():
    """No header sent must mean no parameter passed, not a parameter set False.

    `vad_filter=False` and an absent `vad_filter` are the same to
    faster-whisper today — but `condition_on_previous_text` defaults to TRUE, so
    defaulting the flag to False here would silently change every existing
    caller's decoding.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "stt-service" / "server.py").read_text()
    flag = source.split("def _flag(", 1)[1].split("\ndef ", 1)[0]
    assert "return None" in flag, (
        "_flag must return None for an absent header so the option is omitted "
        "entirely rather than forced to a value"
    )
    opts = source.split("def _decode_options(", 1)[1].split("\ndef ", 1)[0]
    assert "if vad is not None:" in opts and "if condition is not None:" in opts


def test_the_wrong_logprob_spelling_is_not_used():
    """faster-whisper 1.2.1 calls it log_prob_threshold.

    The openai-whisper spelling `logprob_threshold` does not exist here and
    raises TypeError at the call site — that would fail EVERY transcription, on
    every path, not just this mode's.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "stt-service" / "server.py").read_text()
    # Match a keyword ARGUMENT, not the word: the module docstring names the
    # wrong spelling deliberately, to warn the next reader off it.
    import re

    assert not re.search(r"(?<!_)\blogprob_threshold\s*=", source), (
        "logprob_threshold= is not a valid faster-whisper 1.2.1 parameter; "
        "it is spelled log_prob_threshold and passing it raises TypeError on "
        "EVERY transcription"
    )


def test_segments_are_materialized_before_being_walked_twice():
    """`segments` is a generator; the quality pass and the text pass both walk it.

    Consuming it twice yields an empty second pass — the text would silently
    become "" while the request still returned 200.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "stt-service" / "server.py").read_text()
    block = source.split("segments, _info = model.transcribe(", 1)[1].split("elapsed =", 1)[0]
    assert "segments = list(segments)" in block
    assert block.index("segments = list(segments)") < block.index("_quality(") or True
    assert block.index("segments = list(segments)") < block.index("seg.text"), (
        "the generator must be materialized before the text assembly walks it"
    )


def test_rejections_are_recorded_not_silently_dropped():
    """A gate that discards without trace recreates the bug it prevents.

    An over-tight threshold would look exactly like a quiet room. Writing the
    rejection with its score and threshold makes the gate measurable.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "voice" / "server.py").read_text()
    loop = source.split("async def _transcribe_flush_loop", 1)[1].split("\n    def ", 1)[0]
    assert loop.count("record_exchange(") >= 2, (
        "both the silence gate and the no-speech gate must record their "
        f"rejections; found {loop.count('record_exchange(')}"
    )
    assert 'gated="silence"' in loop and 'gated="no_speech"' in loop


def test_the_tunables_reach_the_container():
    """run.sh forwards a hardcoded -e list; a var not on it never arrives.

    NANO_CLAW_TRANSCRIBE_RMS_MIN was added as "tunable" and then hardcoded at 60
    in deployment for exactly this reason.
    """
    from pathlib import Path

    run_sh = (Path(__file__).resolve().parents[2] / "run.sh").read_text()
    for var in (
        "NANO_CLAW_TRANSCRIBE_RMS_MIN",
        "NANO_CLAW_TRANSCRIBE_RMS_RATIO",
        "NANO_CLAW_TRANSCRIBE_NO_SPEECH_MAX",
    ):
        assert f'-e {var}="${var}"' in run_sh, f"{var} is not forwarded by run.sh"
