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


# Pause/clause knobs are read per call (no importlib.reload — reloading used
# to rebind the SpeechChunk class and order-poison other test modules).


def test_pause_jitter_disabled_matches_table_and_never_touches_final_pad(monkeypatch):
    import voice.speech_preparer as sp

    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    table = sp._pause_table()
    # Jitter off → boundary pauses equal the cadence table exactly.
    assert sp._jitter_pause(450) == 450
    assert sp._boundary_pause("A full stop.", "statement", None) == table["period"]
    assert sp._boundary_pause("a clause,", "statement", None) == table["comma"]
    # The final transport tail is a fixed guard — never jittered.
    assert sp._pause_after("last words", "statement", None, None) == sp.FINAL_TAIL_PAD_MS


def test_pause_jitter_varies_within_bounds(monkeypatch):
    import voice.speech_preparer as sp

    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0.15")
    base = sp._pause_table()["period"]
    vals = [sp._jitter_pause(base) for _ in range(300)]
    # Stays within ±15% (allow 1ms rounding slack) and actually varies.
    assert all(base * 0.85 - 1 <= v <= base * 1.15 + 1 for v in vals)
    assert len(set(vals)) > 5
    # Final pad is still exempt even with jitter enabled.
    assert sp._pause_after("last", "statement", None, None) == sp.FINAL_TAIL_PAD_MS


def test_commas_split_into_paused_clauses_without_changing_words(monkeypatch):
    # Jitter off so the comma pause equals the table value exactly.
    import voice.speech_preparer as sp

    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    src = "The strategy is risky, the timeline is tight, and the budget is thin."
    plan = sp.compile_speech(src)
    # Three clauses; the first two end on a comma and carry the comma pause.
    assert len(plan.chunks) == 3
    assert plan.chunks[0].text.endswith(",")
    assert plan.chunks[1].text.endswith(",")
    assert plan.chunks[0].pause_after_ms == sp._pause_table()["comma"]
    assert plan.chunks[1].pause_after_ms == sp._pause_table()["comma"]
    # Splitting changes pause placement, never the words.
    assert plan.spoken_text == src


def test_short_appositive_and_word_lists_stay_whole():
    # Guard: clauses shorter than the minimum don't fragment into micro-pauses.
    assert len(compile_speech("Well, sure.").chunks) == 1
    assert len(compile_speech("Pick red, green, or blue.").chunks) == 1


def test_dashes_become_commas_and_semicolons_keep_their_pause(monkeypatch):
    import voice.speech_preparer as sp

    monkeypatch.setenv("NANO_CLAW_PAUSE_JITTER", "0")
    # Models use em-dashes as light connectors, so they normalize to commas
    # (no separate dash pause) — and no "—" survives into the chunks.
    assert "dash" not in sp._pause_table()
    dash = sp.compile_speech("It is risky — really risky — but worth it.")
    assert all("—" not in c.text for c in dash.chunks)
    # The comma list-guard absorbs the short connector fragments instead of
    # chopping ("It is risky" and "really risky" are each under the guard).
    assert dash.spoken_text == "It is risky, really risky, but worth it."
    # A spaced connector hyphen also becomes a comma; intra-word hyphens don't.
    assert "," in sp.compile_speech("Here is the plan - we ship Friday.").spoken_text
    assert len(sp.compile_speech("That is a well-known trade-off.").chunks) == 1
    # Semicolon → semicolon pause, no stray period appended.
    semi = sp.compile_speech("We shipped it; the numbers look good.")
    assert semi.chunks[0].text == "We shipped it;"
    assert semi.chunks[0].pause_after_ms == sp._pause_table()["semicolon"]


# ── an address has to survive a phone speaker ────────────────────────────────
# Captured through scripts/phone_call_harness.py on 2026-07-31: the line
# "I've got you in Unit B at 14723 Martell Ave" reached the caller, and Whisper
# heard it back as "14723 Martell OVE". A house number read as one cardinal and
# a two-letter suffix read as a word are both wrong for the ONE line a caller is
# asked to confirm.

def test_a_house_number_is_spoken_as_digits():
    out, records = normalize_spoken_forms("I've got you at 14723 Martell Ave.")
    assert "1 4 7 2 3" in out
    assert any(r.kind == "address" for r in records)


def test_a_street_suffix_becomes_a_word():
    out, _ = normalize_spoken_forms("14723 Martell Ave")
    assert "Avenue" in out and "Ave " not in out


def test_the_street_name_is_left_alone():
    """Pronunciation of a proper noun belongs to the voice, not to us."""
    out, _ = normalize_spoken_forms("14723 Martell Ave")
    assert "Martell" in out


def test_a_menu_digit_is_not_an_address():
    """The anchor is the SUFFIX. Without it a bare number in a sentence must be
    left alone — these lines are read on every call."""
    out, records = normalize_spoken_forms("Press 1 for status, or 2 to add an update.")
    assert out == "Press 1 for status, or 2 to add an update."
    assert not [r for r in records if r.kind == "address"]


def test_a_duration_is_not_an_address():
    out, records = normalize_spoken_forms("It will take 3 days.")
    assert not [r for r in records if r.kind == "address"]


def test_a_readback_line_carrying_an_address_is_normalized():
    """The pre-file readback is the other line that must be checkable, and it
    gets this for free by living at the speech boundary."""
    out, _ = normalize_spoken_forms(
        "Request for David Mar at 14723 Martell Ave, San Leandro, CA 94578.")
    assert "1 4 7 2 3 Martell Avenue" in out
