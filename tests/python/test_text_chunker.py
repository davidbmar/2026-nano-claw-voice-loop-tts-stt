from voice.text_chunker import TextChunker


def test_first_chunk_flushes_after_six_words_without_a_boundary():
    c = TextChunker()
    out = []
    for word in "one two three four five six".split():
        out += c.push(word + " ")
    # First chunk emitted exactly once >=6 words accumulate, even mid-sentence.
    assert len(out) == 1, f"expected exactly one early first chunk, got {out}"
    assert len(out[0].split()) >= 6


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
    # No confirmed sentence boundary yet and under FIRST_CHUNK_WORDS words,
    # so nothing should flush — even though the buffer ends in ".".
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
