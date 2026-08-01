"""Per-call seam metrics: persist what the inspector already computes.

`audio_inspect` has scored chunk seams (the clicks/pops at LuxTTS chunk joins)
since the audio inspector landed, but only on demand when someone opened one
call in the review panel — so nothing accumulated. There was no way to ask
"are pops getting worse?" or "which voice causes them?".

These tests cover the capture path: the aggregate is persisted as an
`audio_seams` call_event with the pipeline settings that were in force, and a
failure in the analysis can never affect a call.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from voice import phone


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_VOICE", "lux_isabella")
    monkeypatch.setenv("NANO_CLAW_PHONE_MODEL", "ollama/gemma4:e2b")
    monkeypatch.setenv("NANO_CLAW_PHONE_SPEED", "1.0")
    monkeypatch.setenv("NANO_CLAW_PHONE_STT_SIZE", "small")


ANALYSIS = {
    "available": True,
    "sampleRate": 8000,
    "durationS": 42.5,
    "peak": 21000,
    "seams": [
        {"gapStart": 1.0, "gapEnd": 1.2, "fadeIn": 0.30, "fadeOut": 0.02, "harsh": True},
        {"gapStart": 3.0, "gapEnd": 3.1, "fadeIn": 0.01, "fadeOut": 0.01, "harsh": False},
        {"gapStart": 5.0, "gapEnd": 5.4, "fadeIn": 0.02, "fadeOut": 0.02, "harsh": False},
        {"gapStart": 9.0, "gapEnd": 9.2, "fadeIn": 0.40, "fadeOut": 0.03, "harsh": True},
    ],
    "edgeSummary": {
        "fadeIn": {"median": 0.16, "p90": 0.38, "worst": 0.40},
        "fadeOut": {"median": 0.02, "p90": 0.03, "worst": 0.03},
    },
    "harshCount": 2,
}


def _capture(analysis):
    """Run the capture with a stubbed analyzer; return the emitted payload."""

    emitted = {}

    def fake_emit(_conn, call_id, kind, payload=None, **_kwargs):
        emitted.update({"call_id": call_id, "kind": kind, "payload": payload})
        return True

    with mock.patch("voice.audio_inspect.analyze_outbound", return_value=analysis):
        with mock.patch.object(phone.call_log, "emit", fake_emit):
            phone._capture_seam_metrics("call-abc", "/tmp/tap")
    return emitted


def test_capture_persists_the_aggregate_with_pipeline_context():
    emitted = _capture(ANALYSIS)

    assert emitted["call_id"] == "call-abc"
    assert emitted["kind"] == "audio_seams"
    payload = emitted["payload"]

    assert payload["seamCount"] == 4
    assert payload["harshCount"] == 2
    # Rate, not raw count: a long call naturally has more seams.
    assert payload["harshRate"] == 0.5
    assert payload["durationS"] == 42.5
    assert payload["edgeSummary"]["fadeIn"]["worst"] == 0.40

    # Settings in force, so a regression can be attributed to a change.
    assert payload["voice"] == "lux_isabella"
    assert payload["model"] == "ollama/gemma4:e2b"
    assert payload["sttSize"] == "small"
    assert payload["speed"] == 1.0

    # The per-seam list stays on demand from the tap; only the aggregate is stored.
    assert "seams" not in payload
    assert "envelope" not in payload


def test_no_seams_does_not_divide_by_zero():
    payload = _capture({**ANALYSIS, "seams": [], "harshCount": 0})["payload"]
    assert payload["seamCount"] == 0
    assert payload["harshRate"] == 0.0


def test_unavailable_audio_emits_nothing():
    assert _capture({"available": False}) == {}


def test_analysis_failure_never_propagates():
    """A crash in the analyzer must not affect call teardown."""

    with mock.patch(
        "voice.audio_inspect.analyze_outbound", side_effect=RuntimeError("boom")
    ):
        with mock.patch.object(phone.call_log, "emit") as emit:
            phone._capture_seam_metrics("call-abc", "/tmp/tap")  # must not raise
    emit.assert_not_called()


def test_capture_can_be_disabled(monkeypatch):
    monkeypatch.setenv("NANO_CLAW_PHONE_SEAM_METRICS", "0")
    assert phone._seam_capture_enabled() is False
    with mock.patch("voice.audio_inspect.analyze_outbound") as analyze:
        phone._schedule_seam_capture("call-abc", "/tmp/tap")
    analyze.assert_not_called()


def test_schedule_is_a_noop_without_a_tap_directory():
    with mock.patch("voice.audio_inspect.analyze_outbound") as analyze:
        phone._schedule_seam_capture("call-abc", None)
    analyze.assert_not_called()


def test_schedule_runs_off_the_event_loop():
    """Teardown must not wait on numpy; the work goes to an executor."""

    seen = {}

    async def exercise():
        with mock.patch("voice.audio_inspect.analyze_outbound", return_value=ANALYSIS):
            with mock.patch.object(phone.call_log, "emit", lambda *a, **k: True):
                loop = asyncio.get_running_loop()
                real = loop.run_in_executor

                def spy(executor, fn, *args):
                    seen["scheduled"] = True
                    return real(executor, fn, *args)

                with mock.patch.object(loop, "run_in_executor", spy):
                    phone._schedule_seam_capture("call-abc", "/tmp/tap")
                await asyncio.sleep(0.05)

    asyncio.run(exercise())
    assert seen.get("scheduled") is True


# ── Listing merge ────────────────────────────────────────────
# The aggregate is only useful if it surfaces. The calls list is the operator's
# primary view, so the merge is additive and must never break the listing.


class _FakeConn:
    def __init__(self, rows, fail=False):
        self._rows = rows
        self._fail = fail
        self.queries = 0

    def execute(self, _sql, _params):
        self.queries += 1
        if self._fail:
            raise RuntimeError("db exploded")
        return self

    def fetchall(self):
        return self._rows


def test_listing_merges_seam_summary_in_one_query():
    calls = [{"call_id": "a"}, {"call_id": "b"}]
    conn = _FakeConn(
        [
            ("a", '{"seamCount": 4, "harshCount": 2, "harshRate": 0.5, "voice": "lux_isabella"}'),
        ]
    )

    phone._attach_seam_summaries(conn, calls)

    assert conn.queries == 1, "one query for the whole page, not one per call"
    assert calls[0]["seams"]["harshCount"] == 2
    assert calls[0]["seams"]["voice"] == "lux_isabella"
    assert "seams" not in calls[1], "calls without analysis stay unannotated"


def test_listing_survives_a_failed_lookup():
    calls = [{"call_id": "a"}]
    phone._attach_seam_summaries(_FakeConn([], fail=True), calls)
    assert calls == [{"call_id": "a"}]


def test_listing_survives_malformed_payload():
    calls = [{"call_id": "a"}]
    phone._attach_seam_summaries(_FakeConn([("a", "not json{")]), calls)
    assert "seams" not in calls[0]


def test_listing_handles_empty_input():
    phone._attach_seam_summaries(_FakeConn([]), [])  # must not raise
