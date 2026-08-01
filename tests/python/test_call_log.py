import os
import sqlite3
import time

import pytest

from voice import call_log, metrics_db


@pytest.fixture(autouse=True)
def _clean_module_state():
    call_log._seq.clear()
    call_log._subscribers.clear()
    yield
    call_log._seq.clear()
    call_log._subscribers.clear()


def _conn(tmp_path):
    conn = metrics_db.init_db(str(tmp_path / "metrics.db"))
    assert conn is not None
    assert call_log.ensure_schema(conn)
    return conn


def test_schema_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    assert call_log.ensure_schema(conn)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(call_events)")]
    assert cols == ["id", "call_id", "ts", "seq", "kind", "payload"]


def test_emit_and_read_timeline_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert call_log.emit(conn, "call-1", "call_start", {"codec": "l16"}, ts=100.0)
    assert call_log.emit(conn, "call-1", "user_turn", {"text": "hi there"}, ts=101.0)
    assert call_log.emit(conn, "call-1", "assistant_turn", {"text": "hello"}, ts=102.0)

    events = call_log.read_timeline(conn, "call-1")
    assert [e["kind"] for e in events] == ["call_start", "user_turn", "assistant_turn"]
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert events[0]["ts"] == 100.0
    assert events[1]["payload"] == {"text": "hi there"}


def test_emit_without_payload_reads_back_as_empty_dict(tmp_path):
    conn = _conn(tmp_path)
    assert call_log.emit(conn, "call-1", "barge_in")
    events = call_log.read_timeline(conn, "call-1")
    assert events[0]["payload"] == {}
    assert events[0]["ts"] == pytest.approx(time.time(), abs=5.0)


def test_seq_is_independent_across_interleaved_calls(tmp_path):
    conn = _conn(tmp_path)
    call_log.emit(conn, "call-a", "call_start")
    call_log.emit(conn, "call-b", "call_start")
    call_log.emit(conn, "call-a", "user_turn", {"text": "a1"})
    call_log.emit(conn, "call-b", "user_turn", {"text": "b1"})
    call_log.emit(conn, "call-a", "call_end")

    assert [e["seq"] for e in call_log.read_timeline(conn, "call-a")] == [0, 1, 2]
    assert [e["seq"] for e in call_log.read_timeline(conn, "call-b")] == [0, 1]


def test_call_end_releases_seq_state(tmp_path):
    conn = _conn(tmp_path)
    call_log.emit(conn, "call-1", "call_start")
    call_log.emit(conn, "call-1", "call_end")
    assert "call-1" not in call_log._seq


def test_seq_state_is_bounded(tmp_path):
    conn = _conn(tmp_path)
    for i in range(call_log.MAX_TRACKED_CALLS + 10):
        call_log.emit(conn, f"call-{i}", "call_start")
    assert len(call_log._seq) <= call_log.MAX_TRACKED_CALLS


def test_emit_is_noop_on_none_conn():
    assert call_log.emit(None, "call-1", "user_turn", {"text": "x"}) is False


def test_emit_rejects_blank_ids_and_kinds(tmp_path):
    conn = _conn(tmp_path)
    assert call_log.emit(conn, "", "user_turn") is False
    assert call_log.emit(conn, "call-1", "") is False
    assert call_log.read_timeline(conn, "call-1") == []


def test_emit_never_raises_on_closed_conn(tmp_path):
    conn = _conn(tmp_path)
    conn.close()
    assert call_log.emit(conn, "call-1", "user_turn", {"text": "x"}) is False


def test_read_timeline_returns_empty_on_broken_db(tmp_path):
    conn = _conn(tmp_path)
    conn.close()
    assert call_log.read_timeline(conn, "call-1") == []


def test_subscribers_receive_events(tmp_path):
    conn = _conn(tmp_path)
    seen = []
    call_log.subscribe(seen.append)
    call_log.emit(conn, "call-1", "user_turn", {"text": "hi"}, ts=50.0)

    assert len(seen) == 1
    event = seen[0]
    assert event["call_id"] == "call-1"
    assert event["kind"] == "user_turn"
    assert event["payload"] == {"text": "hi"}
    assert event["ts"] == 50.0
    assert event["seq"] == 0


def test_raising_subscriber_does_not_break_emit_or_other_subscribers(tmp_path):
    conn = _conn(tmp_path)
    seen = []

    def bad(_event):
        raise RuntimeError("boom")

    call_log.subscribe(bad)
    call_log.subscribe(seen.append)
    assert call_log.emit(conn, "call-1", "user_turn", {"text": "hi"})
    assert len(seen) == 1
    assert call_log.read_timeline(conn, "call-1")[0]["kind"] == "user_turn"


def test_unsubscribe_removes_subscriber(tmp_path):
    conn = _conn(tmp_path)
    seen = []
    call_log.subscribe(seen.append)
    call_log.unsubscribe(seen.append)
    call_log.emit(conn, "call-1", "user_turn")
    assert seen == []


def test_sweep_deletes_old_events_and_tap_dirs_keeps_young(tmp_path):
    conn = _conn(tmp_path)
    now = time.time()
    old_ts = now - 40 * 86400
    call_log.emit(conn, "old-call", "call_start", ts=old_ts)
    call_log.emit(conn, "young-call", "call_start", ts=now)
    metrics_db.record_call_start(conn, "old-call", "+15550001111", "+15550002222", "n")

    tap_root = tmp_path / "taps"
    old_dir = tap_root / "old-call"
    young_dir = tap_root / "young-call"
    for d in (old_dir, young_dir):
        d.mkdir(parents=True)
        (d / "inbound.wav").write_bytes(b"RIFF")
    os.utime(old_dir, (old_ts, old_ts))

    call_log.sweep(conn, tap_root, older_than_days=30)

    assert call_log.read_timeline(conn, "old-call") == []
    assert [e["kind"] for e in call_log.read_timeline(conn, "young-call")] == ["call_start"]
    assert not old_dir.exists()
    assert young_dir.exists()
    # Call metadata survives the sweep: phone_calls is the failover call record.
    assert any(c["call_id"] == "old-call" for c in metrics_db.recent_calls(conn))


def test_sweep_disabled_when_days_not_positive(tmp_path):
    conn = _conn(tmp_path)
    call_log.emit(conn, "old-call", "call_start", ts=time.time() - 400 * 86400)
    call_log.sweep(conn, tmp_path / "taps", older_than_days=0)
    assert len(call_log.read_timeline(conn, "old-call")) == 1


def test_sweep_never_raises(tmp_path):
    conn = _conn(tmp_path)
    conn.close()
    call_log.sweep(conn, tmp_path / "missing-root", older_than_days=30)
    call_log.sweep(None, tmp_path / "missing-root", older_than_days=30)
