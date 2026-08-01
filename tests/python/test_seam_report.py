"""Corpus seam report: the real-vs-loopback split is the point.

Loopback exercises the same synthesis and framing with no carrier path, so it
is the control group. A defect present in both is ours; a defect only in real
calls is downstream of our transmit point. The summary must keep those two
populations separate or the comparison is lost.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "seam_report", Path(__file__).resolve().parents[2] / "scripts" / "seam_report.py"
)
seam_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seam_report)


def _row(call, seams, harsh, duration=60.0, fade_in=0.1, synthetic=False):
    return {
        "call": call,
        "durationS": duration,
        "seamCount": seams,
        "harshCount": harsh,
        "harshRate": round(harsh / seams, 4) if seams else 0.0,
        "harshPerMinute": round(harsh / (duration / 60), 3) if duration else 0.0,
        "worstFadeIn": fade_in,
        "worstFadeOut": 0.05,
        "synthetic": synthetic,
    }


def test_real_and_loopback_are_summarized_separately():
    rows = [
        _row("v3:aaa", 100, 3, fade_in=1.0),
        _row("v3:bbb", 50, 1, fade_in=0.2),
        _row("loopback-1", 40, 0, synthetic=True),
        _row("loopback-2", 40, 1, fade_in=0.15, synthetic=True),
    ]

    summary = seam_report.summarize(rows)

    assert summary["analyzed"] == 4
    real = summary["groups"]["real"]
    loop = summary["groups"]["loopback"]

    assert real["calls"] == 2
    assert real["seams"] == 150
    assert real["harsh"] == 4
    # The severity tail is what distinguishes the populations, so the max must
    # survive aggregation rather than being averaged away.
    assert real["worstFadeIn"] == 1.0
    assert loop["calls"] == 2
    assert loop["harsh"] == 1
    assert loop["worstFadeIn"] == 0.15


def test_rate_is_seams_normalized_not_raw_count():
    """A long call has more seams; raw counts would rank by duration."""

    rows = [_row("long", 1000, 5), _row("short", 10, 4)]
    summary = seam_report.summarize(rows)
    assert summary["groups"]["real"]["harshRate"] == round(9 / 1010, 4)


def test_a_corrupt_tap_does_not_abort_the_sweep(tmp_path, monkeypatch):
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir()
    bad.mkdir()

    def fake_analyze(d):
        if d.name == "bad":
            raise ValueError("truncated wav")
        return {
            "available": True,
            "durationS": 30.0,
            "seams": [{"harsh": True}],
            "harshCount": 1,
            "edgeSummary": {"fadeIn": {"worst": 0.5}, "fadeOut": {"worst": 0.1}},
        }

    monkeypatch.setattr(seam_report.audio_inspect, "analyze_outbound", fake_analyze)
    rows = seam_report.scan(tmp_path)

    assert len(rows) == 2
    assert any("error" in r for r in rows)
    # The failure is reported but excluded from the statistics.
    assert seam_report.summarize(rows)["analyzed"] == 1


def test_missing_root_is_not_an_exception():
    assert seam_report.scan(Path("/nonexistent/taps")) == []


def test_zero_seam_call_does_not_divide_by_zero():
    summary = seam_report.summarize([_row("silent", 0, 0)])
    assert summary["groups"]["real"]["harshRate"] == 0.0


@pytest.mark.parametrize("name,expected", [("loopback-123", True), ("v3:abc", False)])
def test_loopback_detection(name, expected):
    assert _row(name, 1, 0, synthetic=name.startswith("loopback"))["synthetic"] is expected
