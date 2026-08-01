import asyncio
from types import SimpleNamespace

import pytest

from voice import call_log, metrics_db, server


@pytest.fixture
def conn(tmp_path, monkeypatch):
    conn = metrics_db.init_db(str(tmp_path / "metrics.db"))
    assert conn is not None
    assert call_log.ensure_schema(conn)
    monkeypatch.setattr(server, "METRICS", conn)
    call_log._seq.clear()
    return conn


def _session(cid="voice-abc123"):
    return SimpleNamespace(conversation_id=cid, _history_store=None)


def _ws():
    async def send_json(_msg):
        return None

    return SimpleNamespace(closed=False, send_json=send_json)


def test_web_session_recorded_on_first_accepted_utterance(conn):
    session = _session()
    assert asyncio.run(server._capture_user_utterance(_ws(), session, "hello there"))
    assert asyncio.run(server._capture_user_utterance(_ws(), session, "second turn"))

    calls = metrics_db.recent_calls(conn)
    assert len(calls) == 1
    assert calls[0]["call_id"] == "voice-abc123"
    assert calls[0]["caller"] == "webUI"
    assert calls[0]["turns"] == 2
    events = call_log.read_timeline(conn, "voice-abc123")
    assert [e["kind"] for e in events] == ["call_start"]
    assert events[0]["payload"]["mode"] == "web"
    assert events[0]["payload"]["sessionId"] == "voice-abc123"


def test_invalid_utterance_records_nothing(conn):
    session = _session("voice-invalid")
    assert not asyncio.run(server._capture_user_utterance(_ws(), session, 123))
    assert metrics_db.recent_calls(conn) == []
    assert call_log.read_timeline(conn, "voice-invalid") == []


def test_finalize_web_call_sets_end_once(conn):
    session = _session("voice-final")
    asyncio.run(server._capture_user_utterance(_ws(), session, "hello"))
    server._finalize_web_call(session)
    server._finalize_web_call(session)

    calls = metrics_db.recent_calls(conn)
    assert calls[0]["ended_at"]
    events = call_log.read_timeline(conn, "voice-final")
    assert [e["kind"] for e in events] == ["call_start", "call_end"]


def test_finalize_without_recorded_call_is_noop(conn):
    server._finalize_web_call(_session("voice-quiet"))
    assert metrics_db.recent_calls(conn) == []
    assert call_log.read_timeline(conn, "voice-quiet") == []
