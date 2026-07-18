import asyncio
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from voice import metrics_db, server


def run(coro):
    return asyncio.run(coro)


def test_metrics_writer_and_public_api_never_expose_transcripts(monkeypatch, tmp_path):
    conn = metrics_db.init_db(str(tmp_path / "metrics.db"))
    assert conn is not None
    marker_asked = "private asked marker"
    marker_said = "private said marker"
    start = time.monotonic()
    session = SimpleNamespace(
        _agent_session_id="voice-" + "a" * 32,
        _turn={
            "t0": start,
            "asked": marker_asked,
            "stt_ms": 12,
            "stt_size": "base",
            "voice_id": "test-voice",
            "model": "openai/gpt-4o-mini",
        },
    )
    monkeypatch.setattr(server, "METRICS", conn)

    server._write_turn_metrics(
        session,
        start + 0.01,
        start + 0.02,
        start + 0.03,
        {
            "model": "openai/gpt-4o-mini",
            "durationMs": 20,
            "firstTokenMs": 5,
            "tokenUsage": {"prompt": 10, "completion": 5},
        },
    )
    metrics_db.record_turn(
        conn,
        {
            "ts": "2026-07-18T12:01:00",
            "asked_text": marker_asked,
            "said_text": marker_said,
        },
    )

    stored = conn.execute(
        "SELECT asked_text, said_text, tokens_in, tokens_out FROM turns ORDER BY id"
    ).fetchall()
    assert dict(stored[0]) == {
        "asked_text": None,
        "said_text": None,
        "tokens_in": 10,
        "tokens_out": 5,
    }
    assert dict(stored[1]) == {
        "asked_text": None,
        "said_text": None,
        "tokens_in": None,
        "tokens_out": None,
    }

    response = run(server.metrics_handler(None))
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["recent"]
    assert "asked_text" not in payload["recent"][0]
    assert "said_text" not in payload["recent"][0]
    assert marker_asked not in json.dumps(payload)
    assert marker_said not in json.dumps(payload)
    conn.close()


def test_metrics_init_securely_scrubs_preexisting_transcripts_once(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "legacy-metrics.db"
    marker_asked = b"LEGACY_ASKED_TRANSCRIPT_MARKER_039"
    marker_said = b"LEGACY_SAID_TRANSCRIPT_MARKER_039"
    legacy = metrics_db.connect(str(db_path))
    legacy.executescript(metrics_db._SCHEMA)
    legacy.execute(
        "INSERT INTO turns(ts, asked_text, said_text) VALUES(?, ?, ?)",
        ("2026-07-18T12:00:00", marker_asked.decode(), marker_said.decode()),
    )
    legacy.commit()
    legacy.close()
    assert marker_asked in db_path.read_bytes()
    assert marker_said in db_path.read_bytes()

    migrated = metrics_db.init_db(str(db_path))
    assert migrated is not None
    row = migrated.execute("SELECT asked_text, said_text FROM turns").fetchone()
    assert tuple(row) == (None, None)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 1
    migrated.close()

    database_bytes = db_path.read_bytes()
    assert marker_asked not in database_bytes
    assert marker_said not in database_bytes

    traced_statements = []
    original_connect = metrics_db.connect

    def traced_connect(path=None):
        connection = original_connect(path)
        connection.set_trace_callback(traced_statements.append)
        return connection

    monkeypatch.setattr(metrics_db, "connect", traced_connect)
    second = metrics_db.init_db(str(db_path))
    assert second is not None
    assert second.execute("PRAGMA user_version").fetchone()[0] == 1
    assert second.total_changes == 0
    second.close()
    normalized_trace = [statement.upper() for statement in traced_statements]
    assert not any("UPDATE TURNS SET" in statement for statement in normalized_trace)
    assert not any("PRAGMA SECURE_DELETE" in statement for statement in normalized_trace)


class _FakeWebRtcSession:
    def __init__(self):
        self._stream_task = None
        self._turn = None
        self.model = ""
        self.stt_size = "base"
        self.voice_id = "test-voice"
        self.speed = 1.0
        self.closed = False

    async def handle_offer(self, sdp):
        return f"answer-for-{sdp}"

    def set_stream_task(self, task):
        self._stream_task = task

    def set_voice(self, voice_id, speed):
        self.voice_id = voice_id
        self.speed = speed

    async def speak_text(self, text, voice_id, speed):
        return None

    async def close(self):
        self.closed = True


class _AgentBackend:
    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.histories = defaultdict(list)
        self.persisted_snapshots = {}
        self.requests_entered = 0
        self.both_requests_entered = asyncio.Event()
        self.deleted = []
        self.both_deleted = asyncio.Event()

    async def reply(self, body):
        session_id = body["sessionId"]
        marker = body["message"]
        self.histories[session_id].append(marker)
        persisted = json.dumps(self.histories[session_id])
        (self.memory_dir / f"{session_id}.json").write_text(persisted)
        self.persisted_snapshots[session_id] = persisted
        self.requests_entered += 1
        if self.requests_entered == 2:
            self.both_requests_entered.set()
        await asyncio.wait_for(self.both_requests_entered.wait(), timeout=2)
        history = "|".join(self.histories[session_id])
        return {"type": "final", "response": f"reply:{history}"}

    async def delete(self, session_id):
        self.deleted.append(session_id)
        (self.memory_dir / f"{session_id}.json").unlink(missing_ok=True)
        if len(self.deleted) == 2:
            self.both_deleted.set()


class _FakeResponse:
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self.payload = payload

    async def aread(self):
        return json.dumps(self.payload).encode()


class _FakeStreamContext:
    def __init__(self, backend, body):
        self.backend = backend
        self.body = body

    async def __aenter__(self):
        return _FakeResponse(await self.backend.reply(self.body))

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeHttpClient:
    backend = None

    def __init__(self, *args, **kwargs):
        assert self.backend is not None

    def stream(self, method, url, **kwargs):
        assert method == "POST"
        return _FakeStreamContext(self.backend, kwargs["json"])

    async def request(self, method, url, **kwargs):
        assert method == "DELETE"
        await self.backend.delete(kwargs["json"]["sessionId"])
        return SimpleNamespace(status_code=200)

    async def aclose(self):
        return None


class _InProcessWebSocket:
    scripts = []
    created = []

    def __init__(self):
        self.incoming = list(self.scripts.pop(0))
        self.messages = []
        self.closed = False
        self.turn_finished = asyncio.Event()
        self.created.append(self)

    async def prepare(self, request):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.incoming:
            message = self.incoming.pop(0)
            return SimpleNamespace(
                type=web.WSMsgType.TEXT,
                data=json.dumps(message),
            )
        await asyncio.wait_for(self.turn_finished.wait(), timeout=2)
        self.closed = True
        raise StopAsyncIteration

    async def send_json(self, message):
        self.messages.append(message)
        if message.get("type") == "agent_audio_end":
            self.turn_finished.set()


def test_two_websockets_isolate_markers_and_delete_anonymous_memory(
    monkeypatch, tmp_path
):
    from voice import webrtc

    async def exercise():
        backend = _AgentBackend(tmp_path)
        _FakeHttpClient.backend = backend
        monkeypatch.setattr(server.httpx, "AsyncClient", _FakeHttpClient)
        monkeypatch.setattr(webrtc, "Session", _FakeWebRtcSession)
        monkeypatch.setattr(server, "get_flow_mode", lambda: "off")
        monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
        monkeypatch.setattr(server.web, "WebSocketResponse", _InProcessWebSocket)

        first_marker = "marker-only-for-alpha"
        second_marker = "marker-only-for-bravo"
        _InProcessWebSocket.created = []
        _InProcessWebSocket.scripts = [
            [
                {"type": "hello"},
                {"type": "webrtc_offer", "sdp": "fake-offer-alpha"},
                {
                    "type": "text_message",
                    "text": first_marker,
                    "sessionId": "client-forced-shared-id",
                },
            ],
            [
                {"type": "hello"},
                {"type": "webrtc_offer", "sdp": "fake-offer-bravo"},
                {
                    "type": "text_message",
                    "text": second_marker,
                    "sessionId": "client-forced-shared-id",
                },
            ],
        ]

        await asyncio.gather(
            server.websocket_handler(object()),
            server.websocket_handler(object()),
        )

        first, second = _InProcessWebSocket.created
        first_reply = next(
            message["text"] for message in first.messages if message["type"] == "agent_reply"
        )
        second_reply = next(
            message["text"] for message in second.messages if message["type"] == "agent_reply"
        )
        assert first_reply == f"reply:{first_marker}"
        assert second_reply == f"reply:{second_marker}"
        assert len(backend.histories) == 2
        session_ids = list(backend.histories)
        assert all(re.fullmatch(r"voice-[0-9a-f]{32}", value) for value in session_ids)
        assert "client-forced-shared-id" not in backend.histories
        assert sorted(backend.histories.values()) == sorted(
            [[first_marker], [second_marker]]
        )
        for session_id, history in backend.histories.items():
            persisted = backend.persisted_snapshots[session_id]
            assert history[0] in persisted
            other = second_marker if history[0] == first_marker else first_marker
            assert other not in persisted

        await asyncio.wait_for(backend.both_deleted.wait(), timeout=2)
        assert set(backend.deleted) == set(session_ids)
        assert not any((tmp_path / f"{value}.json").exists() for value in session_ids)

    run(exercise())
