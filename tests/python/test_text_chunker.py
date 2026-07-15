from voice.text_chunker import TextChunker


def test_first_chunk_flushes_after_six_words_without_a_boundary():
    c = TextChunker()
    out = []
    for word in "one two three four five six seven".split():
        out += c.push(word + " ")
    # First chunk emitted once >=6 words accumulate, even mid-sentence.
    assert out, "expected an early first chunk"
    assert len(out[0].split()) >= 6


def test_later_chunks_only_on_sentence_boundary():
    c = TextChunker()
    c.push("This is the first sentence that is quite long already. ")
    # consume whatever the first-chunk rule emitted
    c2 = TextChunker()
    got = c2.push("Short one. And another one! Third?")
    joined = " ".join(got)
    assert "Short one." in joined
    assert "And another one!" in joined
    # "Third?" has no trailing space/flush yet unless boundary seen; it ends with ?
    assert "Third?" in joined


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
