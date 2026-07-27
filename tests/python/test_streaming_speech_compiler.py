"""StreamingSpeechCompiler: sentence-level streaming ≡ batch compilation.

The invariant that makes streaming prepared speech safe: for ANY partitioning
of a reply into deltas, feed*()+finish(None) must produce chunks identical to
compile_speech(whole) — same texts, kinds, pauses, sequences. The stability
frontier (hold the open tail's last sentence, short labels awaiting a merge,
unclosed markup) is what guarantees it; these tests are the enforcement net
that must evolve with any normalizer change.
"""

import random

import pytest

import voice.speech_preparer as sp
from voice.speech_preparer import (
    FINAL_TAIL_PAD_MS,
    SpeechChunk,
    StreamingSpeechCompiler,
    compile_speech,
)


CORPUS = [
    "Hello there.",
    "The next launch is on Friday. Come out early. Parking fills fast.",
    "Is it going to rain? I hope not. Bring a jacket anyway.",
    "The strategy is risky, the timeline is tight, and the budget is thin. "
    "We ship anyway; the numbers look good.",
    "It is risky — really risky — but worth it. Trust the process.",
    "## Next steps\n1. Call (512) 555-0184.\n2. Meet at 3:30 PM.\n3. Bring $12.50.",
    "- First point here.\n- Second point there.\n- Third point everywhere.",
    "## Overview\nThe plan depends on three things. Each one matters. None are optional.",
    "See No. 5 for details. The rest is in the appendix.",
    "We use A.I. every day. It works well, e.g. in search.",
    "First paragraph runs here.\n\nSecond paragraph starts fresh. It has two sentences.",
    "Some `inline code` and **bold words. across a boundary** survive cleanup.",
    "Line one continues\nonto line two here. Then a second sentence lands.",
    "Windows line endings arrive.\r\nThey still split correctly. Even now.",
    "A reply with no trailing space after the last sentence.",
    "Prices run 10-20 percent higher on 12/25/2026 at 4:15 pm. Plan for it.",
]


def _partitions(text: str, seed: int) -> list[str]:
    rng = random.Random(seed)
    parts: list[str] = []
    index = 0
    while index < len(text):
        step = rng.randint(1, 9)
        parts.append(text[index : index + step])
        index += step
    return parts


def _stream(text: str, parts: list[str]) -> list[SpeechChunk]:
    compiler = StreamingSpeechCompiler()
    chunks: list[SpeechChunk] = []
    for part in parts:
        chunks.extend(compiler.feed(part))
    chunks.extend(compiler.finish(None))
    return chunks


def _fields(chunks) -> list[tuple]:
    return [
        (c.chunk_id, c.sequence, c.text, c.kind, c.estimated_duration_ms,
         c.pause_after_ms, c.is_final)
        for c in chunks
    ]


@pytest.mark.parametrize("text", CORPUS)
def test_streaming_equals_batch_for_any_partition(monkeypatch, text):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    batch = _fields(compile_speech(text).chunks)

    strategies: list[list[str]] = [
        list(text),                              # char by char
        [text],                                  # whole at once
        text.replace(" ", " \0").split("\0"),    # token-ish splits
    ]
    strategies += [[text[:i], text[i:]] for i in range(1, min(len(text), 40))]
    strategies += [_partitions(text, seed) for seed in range(20)]

    for parts in strategies:
        assert _fields(_stream(text, parts)) == batch, f"partition {parts!r}"


def test_seeded_jitter_draw_order_matches_batch(monkeypatch):
    # Emission is strictly in order with one jitter draw per non-final chunk,
    # so even the random pause wobble reproduces batch exactly when seeded.
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0.15")
    text = CORPUS[3]
    random.seed(42)
    batch = _fields(compile_speech(text).chunks)
    random.seed(42)
    streamed = _fields(_stream(text, _partitions(text, seed=7)))
    assert streamed == batch


def test_feed_chunks_never_carry_the_final_tail_pad(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    compiler = StreamingSpeechCompiler()
    fed: list[SpeechChunk] = []
    for part in _partitions(CORPUS[1], seed=3):
        fed.extend(compiler.feed(part))
    assert fed, "multi-sentence text must stream before finish"
    assert all(c.pause_after_ms != FINAL_TAIL_PAD_MS for c in fed)
    assert all(not c.is_final for c in fed)
    tail = compiler.finish(None)
    assert tail[-1].pause_after_ms == FINAL_TAIL_PAD_MS
    assert tail[-1].is_final


def test_every_unit_ends_in_boundary_punctuation():
    # Tripwire for the pause-independence assumption: _boundary_pause's
    # next_kind branch stays unreachable only while _ensure_terminal
    # guarantees terminal punctuation on every unit.
    for text in CORPUS:
        for chunk in compile_speech(text).chunks:
            assert chunk.text.rstrip()[-1:] in {".", "!", "?", ",", ":", ";"}


def test_first_chunk_streams_once_second_sentence_starts(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    compiler = StreamingSpeechCompiler()
    assert compiler.feed("The launch is Friday. Gates open at") != []


def test_label_segment_is_held_until_its_follower_arrives(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    compiler = StreamingSpeechCompiler()
    # A closed short heading may still merge with the next statement.
    assert compiler.feed("## Overview\n") == []
    chunks = compiler.feed("The plan has three parts. Each part matters. More below. ")
    assert chunks, "merge decided once the follower exists"
    assert chunks[0].text.startswith("Overview:")


def test_open_markup_holds_the_tail(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    compiler = StreamingSpeechCompiler()
    assert compiler.feed("Here is code ```first part. Second part. ") == []
    # Closing the fence releases the (cleaned) remainder at finish.
    chunks = compiler.feed("more``` and prose. Another sentence lands. ")
    total = chunks + compiler.finish(None)
    assert _fields(total) == _fields(
        compile_speech(
            "Here is code ```first part. Second part. more``` and prose. "
            "Another sentence lands. "
        ).chunks
    )


def test_feed_after_finish_is_a_noop(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    compiler = StreamingSpeechCompiler()
    compiler.feed("One. Two. ")
    compiler.finish(None)
    assert compiler.feed("Three. ") == []
    assert compiler.finish(None) == []


def test_finish_with_extended_final_text(monkeypatch):
    # final.response can extend the streamed deltas (server-side additions);
    # finish(final_text) must speak only the remainder.
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    fed = "The launch is Friday. Gates open early. "
    full = fed + "Bring water."
    compiler = StreamingSpeechCompiler()
    streamed = list(compiler.feed(fed))
    streamed.extend(compiler.finish(full))
    assert _fields(streamed) == _fields(compile_speech(full).chunks)


def test_finish_never_respeaks_on_divergence(monkeypatch, caplog):
    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    compiler = StreamingSpeechCompiler()
    emitted = compiler.feed("Alpha beta gamma. Delta epsilon zeta. ")
    assert emitted
    with caplog.at_level("WARNING", logger="nano-claw.speech"):
        tail = compiler.finish("Completely different text. With two sentences.")
    spoken = [c.text for c in emitted + tail]
    # Never re-speak: already-emitted units stay; only units BEYOND the
    # emitted count from the recompute are added.
    assert spoken[0] == "Alpha beta gamma."
    assert len(spoken) == len(set(spoken))
    assert any("divergence" in r.message for r in caplog.records)


def test_empty_and_whitespace_inputs():
    compiler = StreamingSpeechCompiler()
    assert compiler.feed("") == []
    assert compiler.finish(None) == []
    compiler2 = StreamingSpeechCompiler()
    assert compiler2.feed("   \n  ") == []
    assert compiler2.finish(None) == []
