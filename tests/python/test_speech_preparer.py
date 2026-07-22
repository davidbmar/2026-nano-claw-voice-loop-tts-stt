from voice.speech_preparer import (
    NORMALIZER_VERSION,
    SPEECH_COMPILER_VERSION,
    FINAL_TAIL_PAD_MS,
    compile_speech,
    normalize_spoken_forms,
)


def test_strategy_markdown_becomes_short_ordered_spoken_chunks():
    source = """## Biggest weaknesses

1. **Fragile SEO foundation**
The strategy depends heavily on Google's unpredictable organic rankings, which can change before the sites recover their acquisition costs.

2. **Slow revenue ramp**
It requires months of sales work before recurring revenue becomes meaningful.

What should you address first?
"""

    plan = compile_speech(source, max_words_per_chunk=18)

    assert plan.compiler_version == SPEECH_COMPILER_VERSION
    assert plan.normalizer_version == NORMALIZER_VERSION
    assert plan.guarantee_level == "text_structural"
    assert "#" not in plan.spoken_text
    assert "*" not in plan.spoken_text
    assert any(
        chunk.text.startswith("First, Fragile search engine optimization foundation:")
        for chunk in plan.chunks
    )
    assert any(chunk.text.startswith("Second, Slow revenue ramp:") for chunk in plan.chunks)
    assert plan.chunks[-1].kind == "question"
    assert plan.chunks[-1].pause_after_ms == FINAL_TAIL_PAD_MS
    assert all(chunk.sequence == index for index, chunk in enumerate(plan.chunks))
    assert [chunk.is_final for chunk in plan.chunks].count(True) == 1
    assert plan.chunks[-1].is_final


def test_high_value_values_receive_deterministic_spoken_renderings():
    text = (
        "Call (512) 555-0184. The appointment is 07/24/2026 at 3:30 PM. "
        "The price is $1,250.50, with a 2-4 hour window and 3.5% fee."
    )

    spoken, records = normalize_spoken_forms(text)

    assert "five one two, five five five, zero one eight four" in spoken
    assert "July twenty fourth, twenty twenty six" in spoken
    assert "three thirty P M" in spoken
    assert "one thousand two hundred fifty dollars and fifty cents" in spoken
    assert "two to four hour window" in spoken
    assert "three point five percent" in spoken
    assert {record.kind for record in records} >= {
        "phone",
        "date",
        "time",
        "currency",
        "range",
        "percentage",
    }


def test_hashes_citations_and_acronyms_never_reach_tts_as_visual_syntax():
    plan = compile_speech(
        "### SEO risk\nUse option #2 [#17]. The LLM should compare ROI vs. CAC."
    )

    assert "#" not in plan.spoken_text
    assert "17" not in plan.spoken_text
    assert "number two" in plan.spoken_text
    assert "search engine optimization" in plan.spoken_text
    assert "language model" in plan.spoken_text
    assert "return on investment" in plan.spoken_text
    assert "versus" in plan.spoken_text
    assert "customer acquisition cost" in plan.spoken_text


def test_acronyms_expand_only_when_the_source_is_actually_uppercase():
    spoken, _records = normalize_spoken_forms(
        "AI can help, but a person named Ai should keep her name."
    )

    assert spoken == (
        "artificial intelligence can help, but a person named Ai should keep her name."
    )


def test_long_sentence_splits_only_at_clause_boundaries():
    plan = compile_speech(
        "The plan depends on organic search rankings, which can change without warning, "
        "and it requires a long sales cycle before revenue becomes predictable.",
        max_words_per_chunk=12,
    )

    assert len(plan.chunks) >= 2
    joined = " ".join(chunk.text for chunk in plan.chunks)
    assert "organic search rankings" in joined
    assert "change without warning" in joined
    assert "long sales cycle" in joined
    assert "revenue becomes predictable" in joined
    assert all(chunk.text.strip() for chunk in plan.chunks)


def test_public_metadata_contains_no_source_or_spoken_text():
    plan = compile_speech("The answer is $125. What would you like to examine next?")

    metadata = plan.public_metadata()

    assert metadata["compilerVersion"] == SPEECH_COMPILER_VERSION
    assert metadata["chunkCount"] == len(plan.chunks)
    assert metadata["normalizationCount"] == 1
    assert "source_text" not in metadata
    assert "spoken_text" not in metadata
    assert "text" not in metadata


def test_empty_text_produces_a_complete_empty_plan():
    plan = compile_speech("   ")

    assert plan.chunks == ()
    assert plan.spoken_text == ""
    assert plan.public_metadata()["chunkCount"] == 0
