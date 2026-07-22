from voice.text_chunker import TextChunker, clean_for_speech, normalize_for_speech


def test_first_chunk_waits_for_sentence_boundary():
    c = TextChunker()
    assert c.push("one two three four five six ") == []
    assert c.push("words still arriving, with a clause ") == []
    assert c.push("that now ends. More text") == [
        "one two three four five six words still arriving, with a clause that now ends."
    ]
    assert c.flush() == "More text"


def test_later_chunks_only_on_sentence_boundary():
    c = TextChunker()
    got = c.push("Short one. And another one! Third sentence here.")
    joined = " ".join(got)
    # Confirmed boundaries (punctuation + trailing space) flush via push().
    assert "Short one." in joined
    assert "And another one!" in joined
    # The final sentence has no trailing space, so push() must NOT emit it —
    # only flush() reveals it, since more text could still arrive mid-stream.
    assert "Third sentence here." not in joined
    assert c.flush() == "Third sentence here."


def test_decimal_not_split_across_deltas():
    c = TextChunker()
    # No confirmed sentence boundary yet, so nothing should flush even though
    # the buffer ends in ".".
    out = c.push("The value is 3.")
    assert out == []
    out = c.push("5 percent. Done here now.")
    assert out, "expected a chunk once a confirmed boundary arrives"
    assert "3.5 percent." in out[0]
    assert out[0] != "The value is 3."


def test_under_six_words_no_boundary_returns_empty():
    c = TextChunker()
    assert c.push("just four words here") == []


def test_markdown_is_stripped():
    c = TextChunker()
    got = c.push("**Bold** and `code` and a [link](http://x). ")
    joined = " ".join(got) + c.flush()
    assert "*" not in joined
    assert "`" not in joined
    assert "http" not in joined


def test_hash_markers_never_reach_tts():
    spoken = clean_for_speech(
        "  ###Compact heading\n# Normal heading\nUse option #2 [#17]. Discuss #strategy."
    )

    assert "#" not in spoken
    assert "Compact heading" in spoken
    assert "Normal heading" in spoken
    assert "number 2" in spoken
    assert "17" not in spoken
    assert "strategy" in spoken


def test_flush_returns_remainder():
    c = TextChunker()
    c.push("First sentence here now please. ")  # emits first chunk
    c.push("Trailing without terminator")
    assert c.flush().strip() == "Trailing without terminator"
    assert c.flush() == ""  # nothing left after flush


def test_numbered_list_marker_not_spoken():
    c = TextChunker()
    out = []
    out += c.push("1. First item. 2. Second item here now. ")
    out.append(c.flush())
    joined = " ".join(x for x in out if x)
    # The bare list numbers "1." / "2." must not survive as standalone chunks.
    assert "First item." in joined
    assert "Second item here now." in joined
    assert not any(chunk.strip() in ("1.", "2.") for chunk in out)


def test_scheduler_reply_chunks_only_at_sentence_ends_with_punctuation():
    c = TextChunker()
    reply = (
        "I have Monday at 10:00 AM or 12:00 PM — both open for an hour. "
        "Which works?"
    )

    chunks = c.push(reply)
    tail = c.flush()

    assert chunks == [
        "I have Monday at 10:00 AM or 12:00 PM — both open for an hour."
    ]
    assert tail == "Which works?"
    assert chunks[0].endswith(".")
    assert tail.endswith("?")
    assert "PM — both" in chunks[0]


def test_scheduler_speech_normalization():
    reply = (
        "I have Monday at 10:00 AM or 12:00 PM — both open (for an hour).  "
        "Which works?"
    )

    assert normalize_for_speech(reply) == (
        "I have Monday at 10 AM or 12 PM, both open for an hour. Which works?"
    )


def test_speech_normalization_preserves_nonzero_minutes_and_contents():
    assert normalize_for_speech("Try (about) 10:30 am – or 01:00 pm.") == (
        "Try about 10:30 am, or 1 PM."
    )
