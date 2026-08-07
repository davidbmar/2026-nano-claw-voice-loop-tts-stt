"""Offline stitching turns an arrival-ordered capture into session prose."""

from __future__ import annotations

import json

from scripts import stitch_transcript as stitch


def test_groups_by_session_sorts_seq_uses_raw_and_reports_integrity():
    entries = [
        {
            "session_id": "alpha",
            "seq": 4,
            "transcript_raw": "after the gap",
            "transcript": "WRONG CLEANED TEXT",
        },
        {
            "session_id": "beta",
            "seq": 2,
            "transcript_raw": "blue green yellow done",
            "transcript": "WRONG CLEANED TEXT",
        },
        {
            "session_id": "alpha",
            "seq": 1,
            "transcript_raw": "start eleven twelve 13 fourteen fifteen",
            "transcript": "WRONG CLEANED TEXT",
        },
        {
            "session_id": "beta",
            "seq": 1,
            "transcript_raw": "begin blue green yellow",
            "transcript": "WRONG CLEANED TEXT",
        },
        {
            "session_id": "alpha",
            "seq": 2,
            "transcript_raw": (
                "eleven twelve thirteen fourteen fifteen continues"
            ),
            "transcript": "WRONG CLEANED TEXT",
        },
        {
            "session_id": "beta",
            "seq": 3,
            "transcript_raw": "this rejected text must disappear",
            "transcript": "WRONG CLEANED TEXT",
            "gated": "silence",
        },
    ]

    report = stitch.stitch_entries(entries)
    sessions = {session.session_id: session for session in report.sessions}

    assert sessions["alpha"].transcript == (
        "start eleven twelve 13 fourteen fifteen continues after the gap"
    )
    assert sessions["beta"].transcript == "begin blue green yellow done"
    assert "WRONG" not in sessions["alpha"].transcript
    assert sessions["alpha"].gaps == ((3, 3),)
    assert sessions["beta"].gaps == ()
    assert report.entries == 6
    assert report.gated_counts == {"silence": 1}
    assert report.seams_joined == 2
    assert report.words_removed == 8


def test_legacy_cli_splits_on_seq_reset_warns_and_prints_every_seam(
    tmp_path, capsys
):
    capture = tmp_path / "legacy.jsonl"
    rows = [
        {"seq": 1, "transcript": "one two three"},
        {"seq": 2, "transcript": "one two three four"},
        {"seq": 1, "transcript": "a separate session"},
        {"seq": 2, "transcript": "without an overlap"},
    ]
    capture.write_text("".join(json.dumps(row) + "\n" for row in rows))

    assert stitch.main([str(capture), "--verbose"]) == 0
    output = capsys.readouterr().out

    assert "!!! WARNING:" in output
    assert "BEST-EFFORT guesses" in output
    assert "Concurrent legacy sessions cannot be recovered reliably" in output
    assert "SESSION legacy-001 [BEST-EFFORT LEGACY]" in output
    assert "SESSION legacy-002 [BEST-EFFORT LEGACY]" in output
    assert 'legacy-001 seq 1 -> 2: removed 3 word(s) "one two three"' in output
    assert 'legacy-002 seq 1 -> 2: removed 0 word(s) ""' in output
    assert "  entries: 4" in output
    assert "  sessions: 2" in output
    assert "  seams joined: 1" in output
    assert "  words removed: 3" in output
    assert "  seq gaps:\n    none" in output


def test_known_session_ids_are_a_lookup_not_a_seq_reset_inference():
    entries = [
        {"session_id": "a", "seq": 1, "transcript_raw": "a one"},
        {"session_id": "b", "seq": 1, "transcript_raw": "b one"},
        {"session_id": "a", "seq": 2, "transcript_raw": "a two"},
        {"session_id": "b", "seq": 2, "transcript_raw": "b two"},
    ]

    report = stitch.stitch_entries(entries)

    assert report.legacy_entries == 0
    assert [session.session_id for session in report.sessions] == ["a", "b"]
    assert [session.entries for session in report.sessions] == [2, 2]
