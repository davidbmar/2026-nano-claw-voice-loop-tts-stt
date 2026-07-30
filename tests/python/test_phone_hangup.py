"""Ending a call without cutting off the words we stayed on the line to say.

The gateway could not hang up. It issues `answer`; `call.hangup` is an INBOUND
notification, and `PhoneCall.close` is local teardown — dropping the media
WebSocket leaves the carrier holding a half-played buffer. That is why the
conversation-start design listed "speak the apology and end the call" as real
work rather than a line (MEDIUM-2 of the 2026-07-30 Codex review).

The wait is computed rather than guessed, and these tests are what "computed"
means.
"""
from __future__ import annotations

import asyncio

import pytest

from voice.phone import FramePacer, PhoneCall


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ── the pacer knows exactly how far ahead it sent ────────────────────────────

def test_nothing_is_buffered_before_a_reply_starts():
    assert FramePacer(clock=FakeClock()).buffered_s == 0.0


def test_a_fresh_reply_is_ahead_by_its_prebuffer():
    """`reset` anchors the deadline in the PAST by the prebuffer, so the first
    frames go out ahead of wall clock. That headroom is audio the caller has not
    heard yet, and hanging up during it truncates."""
    clock = FakeClock()
    pacer = FramePacer(prebuffer_ms=200.0, pace_factor=1.0, clock=clock)
    pacer.reset()

    # Immediately after reset the schedule is 200ms behind now, so nothing is
    # owed yet — the surplus appears as frames are sent ahead of their deadline.
    assert pacer.buffered_s == 0.0

    for _ in range(20):          # 20 frames × 20ms = 400ms of audio sent
        pacer.next_deadline()
    assert pacer.buffered_s == pytest.approx(0.2, abs=0.02), (
        "after sending 400ms of audio with 200ms of headroom, 200ms is owed")


def test_the_surplus_drains_as_the_clock_advances():
    clock = FakeClock()
    pacer = FramePacer(prebuffer_ms=200.0, pace_factor=1.0, clock=clock)
    pacer.reset()
    for _ in range(20):
        pacer.next_deadline()

    before = pacer.buffered_s
    clock.advance(0.1)
    assert pacer.buffered_s == pytest.approx(before - 0.1, abs=1e-6)

    clock.advance(10.0)
    assert pacer.buffered_s == 0.0, "never negative — nothing is owed backwards"


def test_reading_the_surplus_does_not_move_the_schedule():
    """`next_deadline` advances the deadline. If the surplus were read through
    it, asking how far ahead we are would push us further ahead."""
    clock = FakeClock()
    pacer = FramePacer(prebuffer_ms=200.0, pace_factor=1.0, clock=clock)
    pacer.reset()
    # Build a REAL surplus first. Reading straight after reset would sit in the
    # clamped-to-zero region, where a mutating implementation looks identical to
    # a read-only one — this test passed against a broken version until the
    # vacuity check caught it.
    for _ in range(20):
        pacer.next_deadline()

    first = pacer.buffered_s
    assert first > 0.1, "the surplus must be well clear of the zero clamp"
    for _ in range(5):
        pacer.buffered_s
    assert pacer.buffered_s == first


# ── the hangup itself ────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, status=200):
        self.status = status
        self.posts = []

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})

        class R:
            status_code = self.status

            def raise_for_status(_self):
                if self.status >= 400:
                    raise RuntimeError(f"HTTP {self.status}")
        return R()


def make_call(monkeypatch, buffered=0.0):
    """A PhoneCall with only the two attributes the hangup path reads."""
    call = object.__new__(PhoneCall)
    call.call_id = "safe_v3_abc"
    call.telnyx_call_id = "v3:raw-id-with-colon"
    call._frame_pacer = type("P", (), {"buffered_s": buffered})()
    return call


def test_it_waits_for_the_buffer_plus_a_margin(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def exercise():
        call = make_call(monkeypatch, buffered=0.4)
        await call.hangup_after_playback(FakeClient(), margin_s=0.35)

    asyncio.run(exercise())
    assert slept == [pytest.approx(0.75)], (
        "must wait the measured surplus AND the unobservable margin")


def test_it_still_waits_the_margin_with_nothing_buffered(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def exercise():
        await make_call(monkeypatch, buffered=0.0).hangup_after_playback(
            FakeClient(), margin_s=0.35)

    asyncio.run(exercise())
    assert slept == [pytest.approx(0.35)]


def test_it_hangs_up_with_the_raw_carrier_id_not_the_sanitized_one(monkeypatch):
    """`call_id` is sanitized for tap paths and logs — Telnyx ids carry a "v3:"
    prefix whose colon is stripped. Sending that to the carrier addresses no
    call, so the hangup would silently do nothing."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = FakeClient()

    async def exercise():
        await make_call(monkeypatch).hangup_after_playback(client)

    asyncio.run(exercise())

    assert len(client.posts) == 1
    url = client.posts[0]["url"]
    assert "v3:raw-id-with-colon" in url, url
    assert "safe_v3_abc" not in url, "the sanitized id would address no call"
    assert url.endswith("/actions/hangup")


def test_a_carrier_failure_is_reported_not_raised(monkeypatch):
    """This runs while a call is ending. Raising here would replace a tidy
    hangup with a traceback and an open line."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def exercise():
        return await make_call(monkeypatch).hangup_after_playback(
            FakeClient(status=500))

    assert asyncio.run(exercise()) is False


def test_a_call_with_no_pacer_yet_still_hangs_up(monkeypatch):
    """A call that failed before speaking has no pacer. It must still end."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = FakeClient()

    async def exercise():
        call = make_call(monkeypatch)
        call._frame_pacer = None
        return await call.hangup_after_playback(client)

    assert asyncio.run(exercise()) is True
    assert len(client.posts) == 1


# ── every hangup that follows speech must wait for it ────────────────────────

def _calls_in(func_name: str) -> set:
    """Attribute calls inside a function, read from the file on disk.

    From disk, not `inspect.getsource`: `cost_ledger.install_phone_tracking`
    wraps methods, and getsource then returns the wrapper — that has produced
    tests here which pass alone and fail in the full suite.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[2] / "voice" / "phone.py").read_text())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == func_name)
    return {n.func.attr for n in ast.walk(func)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def test_the_idle_goodbye_waits_before_hanging_up():
    """"It sounds like you've stepped away. Thanks for calling — goodbye!" then
    an immediate carrier hangup clips the goodbye by the pacer's surplus. This
    was the shape at three sites before `hangup_after_playback` was wired in —
    which also corrects a claim in the conversation-start design that no
    gateway-controlled hangup existed at all. Three did."""
    assert "hangup_after_playback" in _calls_in("_idle_watchdog"), (
        "the idle goodbye hangs up without waiting for playback to drain")


def test_a_finished_flow_waits_before_hanging_up():
    """Same shape at the other site: a scheduling flow that reaches a terminal
    outcome speaks its closing line and ends the call."""
    assert "hangup_after_playback" in _calls_in("_run_turn")


def test_the_pre_answer_gate_still_uses_the_id_it_was_given():
    """`_apologize_and_hangup` takes `telnyx_call_id` as a PARAMETER, and its
    docstring says media-only calls have no Telnyx leg — so what arrives is not
    always a full PhoneCall. Routing it through `hangup_after_playback` would
    ignore the id it was handed and assume a method the object may not have. I
    made that change and reverted it; this keeps it reverted."""
    calls = _calls_in("_apologize_and_hangup")

    assert "hangup_after_playback" not in calls
    assert "speak" in calls
