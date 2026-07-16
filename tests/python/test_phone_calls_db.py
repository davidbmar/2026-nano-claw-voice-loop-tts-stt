import os
import tempfile

from voice import metrics_db as m


def _conn():
    return m.init_db(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_call_lifecycle_start_turns_end():
    c = _conn()
    m.record_call_start(c, "cc-1", "+15551234567", "+15123569101", "nano.example.com")
    m.bump_call_turns(c, "cc-1")
    m.bump_call_turns(c, "cc-1")
    m.record_call_end(c, "cc-1")
    calls = m.recent_calls(c)
    assert len(calls) == 1
    call = calls[0]
    assert call["caller"] == "+15551234567"
    assert call["node"] == "nano.example.com"
    assert call["turns"] == 2
    assert call["answered_at"] and call["ended_at"]


def test_duplicate_start_is_ignored_carrier_retry():
    c = _conn()
    m.record_call_start(c, "cc-2", "+15550000001", "+15123569101", "n")
    m.record_call_start(c, "cc-2", "+15550000001", "+15123569101", "n")
    assert len(m.recent_calls(c)) == 1


def test_end_and_bump_unknown_call_never_raise():
    c = _conn()
    m.record_call_end(c, "never-started")
    m.bump_call_turns(c, "never-started")
    assert m.recent_calls(c) == []


def test_writers_are_noops_on_none_conn():
    m.record_call_start(None, "x", "a", "b", "n")
    m.record_call_end(None, "x")
    m.bump_call_turns(None, "x")


def test_recent_calls_newest_first():
    c = _conn()
    m.record_call_start(c, "cc-a", "+1", "+2", "n")
    m.record_call_start(c, "cc-b", "+3", "+2", "n")
    calls = m.recent_calls(c)
    assert [x["call_id"] for x in calls] == ["cc-b", "cc-a"]
