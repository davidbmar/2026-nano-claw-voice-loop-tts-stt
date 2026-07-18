"""Conversation capture, owner-scoped API, deletion, and sweep tests."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from voice import server
from voice.webauth.aiohttp_adapter import (
    AUTH_MODE_OPTIONAL,
    PUBLIC_ORIGIN,
    SESSION_COOKIE_NAME,
    AiohttpAuthAdapter,
    WebSocketIdentity,
)
from voice.webauth.policy import SessionPolicy
from voice.webauth.sqlite_store import (
    MAX_CONVERSATION_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    MAX_TURN_PAGE_SIZE,
    MAX_TURN_TEXT_LENGTH,
    SQLiteAuthStore,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


class _MemoryPayload:
    def __init__(self, body: bytes):
        self.body = body

    def set_read_chunk_size(self, _size):
        return None

    async def readany(self):
        body, self.body = self.body, b""
        return body


class InProcessClient:
    """Dispatch through aiohttp's router/middleware without a TCP bind."""

    def __init__(self, app: web.Application):
        self.app = app
        self.cookies: dict[str, str] = {}
        app.freeze()

    def make_request(self, method, path, *, headers=None, json_body=None):
        request_headers = {
            "Host": "nano.chattychapters.com",
            **(headers or {}),
        }
        if self.cookies:
            request_headers.setdefault(
                "Cookie",
                "; ".join(
                    f"{name}={value}" for name, value in self.cookies.items()
                ),
            )
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        transport = mock.Mock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            ("127.0.0.1", 40000) if name == "peername" else default
        )
        return make_mocked_request(
            method,
            path,
            headers=request_headers,
            app=self.app,
            transport=transport,
            payload=_MemoryPayload(body),
        )

    async def request(self, method, path, *, headers=None, json_body=None):
        response = await self.app._handle(
            self.make_request(
                method, path, headers=headers, json_body=json_body
            )
        )
        return response

    async def get(self, path, *, headers=None):
        return await self.request("GET", path, headers=headers)

    async def delete(self, path, *, headers=None):
        return await self.request("DELETE", path, headers=headers)


def mutation_headers():
    return {
        "Origin": PUBLIC_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-NC-Auth": "1",
    }


def payload(response):
    return json.loads(response.text)


def make_store(path: Path, tenant: str = "tenant-a") -> SQLiteAuthStore:
    policy = SessionPolicy(
        random_bytes=secrets.token_bytes,
        clock=lambda: NOW,
    )
    return SQLiteAuthStore(path, tenant_id=tenant, policy=policy)


def add_user(store: SQLiteAuthStore, sub: str) -> str:
    store.upsert_identity(sub, f"{sub}@example.test", sub)
    return store.issue_hashed_session(sub, store.tenant_id, NOW)


def make_client(store: SQLiteAuthStore, raw_token: str) -> InProcessClient:
    adapter = AiohttpAuthAdapter(
        client_id="client.apps.googleusercontent.com",
        mode=AUTH_MODE_OPTIONAL,
        public_https=False,
        store=store,
        verifier=object(),
        clock=lambda: NOW,
    )
    client = InProcessClient(server.create_app(auth_adapter=adapter))
    client.cookies[SESSION_COOKIE_NAME] = raw_token
    return client


class CaptureWebSocket:
    def __init__(self):
        self.closed = False
        self.messages: list[dict] = []

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, *, code=None, message=None):
        self.closed = True
        return True


class CaptureSession:
    def __init__(self, store, runtime, conversation_id="voice-" + "a" * 32):
        self._history_store = store
        self._history_runtime = runtime
        self._history_clock = lambda: NOW
        self._history_started = False
        self._history_warning_sent = False
        self._history_agent_active = False
        self._history_agent_failed = False
        self._history_agent_parts = []
        self._tenant_id = store.tenant_id if store is not None else None
        self._user_sub = "user-a" if store is not None else None
        self.conversation_id = conversation_id
        self._stream_task = None
        self._turn = {}
        self._backoff = SimpleNamespace(reset=lambda: None)
        self.voice_id = "voice"
        self.speed = 1.0
        self.model = ""
        self.stt_size = "base"
        self._scheduler_flow_enabled = False
        self._scheduler_flow_attempted = False
        self._scheduler_flow = None

    def set_stream_task(self, task):
        self._stream_task = task

    def begin_stream(self):
        return None

    async def end_stream(self, _total_bytes):
        return None

    def enqueue_chunk(self, chunk, _voice_id, _speed):
        return len(chunk.encode())

    async def speak_text(self, _text, _voice_id, _speed):
        return None

    def stop_speaking(self):
        return None


class SSE:
    def __init__(self, *frames: tuple[str, dict]):
        self.lines: list[str] = []
        for event, data in frames:
            self.lines.extend(
                [f"event: {event}", f"data: {json.dumps(data)}", ""]
            )

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class BoundWebSocket(dict):
    def __init__(self, events=None):
        super().__init__()
        self.prepared = False
        self.closed = False
        self.events = events

    async def prepare(self, _request):
        self.prepared = True

    async def close(self, *, code=None, message=None):
        if self.events is not None:
            self.events.append("socket_closed")
        self.closed = True
        return True


def read_turns(store, conversation_id, user="user-a"):
    result = store.get_conversation_page(
        conversation_id,
        store.tenant_id,
        user,
        limit=MAX_TURN_PAGE_SIZE + 1,
    )
    assert result is not None
    return result


def test_store_owner_scope_title_bounds_and_atomic_sequence_allocation(tmp_path):
    path = tmp_path / "history.db"
    tenant_a = make_store(path, "tenant-a")
    tenant_b = make_store(path, "tenant-b")
    add_user(tenant_a, "user-a")
    add_user(tenant_a, "user-b")
    add_user(tenant_b, "user-a")

    conversation_id = "voice-" + "1" * 32
    first = "  First\n\tutterance   " + "x" * MAX_TITLE_LENGTH
    assert tenant_a.open_conversation(
        conversation_id, "tenant-a", "user-a", first, NOW
    ) == 0
    metadata, turns = read_turns(tenant_a, conversation_id)
    assert metadata["title"] == " ".join(first.split())[:MAX_TITLE_LENGTH]
    assert len(metadata["title"]) == MAX_TITLE_LENGTH
    assert turns[0]["text"] == first

    def append(index):
        return tenant_a.append_conversation_turn(
            conversation_id,
            "tenant-a",
            "user-a",
            "agent",
            f"reply-{index}",
            NOW + timedelta(seconds=index + 1),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        allocated = sorted(pool.map(append, range(20)))
    assert allocated == list(range(1, 21))
    metadata, turns = read_turns(tenant_a, conversation_id)
    assert metadata["turn_count"] == len(turns) == 21
    assert [turn["seq"] for turn in turns] == list(range(21))

    assert tenant_a.get_conversation_page(
        conversation_id, "tenant-a", "user-b", limit=2
    ) is None
    assert tenant_b.get_conversation_page(
        conversation_id, "tenant-b", "user-a", limit=2
    ) is None
    assert tenant_a.list_conversations(
        "tenant-a", "user-b", limit=2
    ) == []

    with pytest.raises(ValueError, match="at most"):
        tenant_a.append_conversation_turn(
            conversation_id,
            "tenant-a",
            "user-a",
            "agent",
            "x" * (MAX_TURN_TEXT_LENGTH + 1),
            NOW,
        )

    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TRIGGER fail_history_insert
        BEFORE INSERT ON conversation_turns
        WHEN NEW.text = 'forced failure'
        BEGIN SELECT RAISE(ABORT, 'forced failure'); END
        """
    )
    connection.close()
    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        tenant_a.append_conversation_turn(
            conversation_id,
            "tenant-a",
            "user-a",
            "agent",
            "forced failure",
            NOW,
        )
    metadata, turns = read_turns(tenant_a, conversation_id)
    assert metadata["turn_count"] == len(turns) == 21


def test_api_owner_scope_guards_bounds_and_stable_cursor_pagination(tmp_path):
    async def exercise():
        path = tmp_path / "history.db"
        tenant_a = make_store(path, "tenant-a")
        tenant_b = make_store(path, "tenant-b")
        token_a = add_user(tenant_a, "user-a")
        token_other = add_user(tenant_a, "user-b")
        token_tenant_b = add_user(tenant_b, "user-a")
        client_a = make_client(tenant_a, token_a)
        client_other = make_client(tenant_a, token_other)
        client_tenant_b = make_client(tenant_b, token_tenant_b)

        ids = [f"voice-{index:032x}" for index in range(1, 4)]
        for index, conversation_id in enumerate(ids):
            tenant_a.open_conversation(
                conversation_id,
                "tenant-a",
                "user-a",
                f"question {index}",
                NOW + timedelta(seconds=index),
            )
        other_id = "voice-" + "b" * 32
        tenant_a.open_conversation(
            other_id, "tenant-a", "user-b", "private B", NOW
        )
        tenant_b_id = "voice-" + "c" * 32
        tenant_b.open_conversation(
            tenant_b_id, "tenant-b", "user-a", "private tenant B", NOW
        )

        first = await client_a.get("/api/conversations?limit=2")
        assert first.status == 200
        assert first.headers["Cache-Control"] == "no-store"
        first_body = payload(first)
        assert [row["id"] for row in first_body["conversations"]] == [
            ids[2],
            ids[1],
        ]
        assert first_body["nextCursor"]

        newest = "voice-" + "f" * 32
        tenant_a.open_conversation(
            newest,
            "tenant-a",
            "user-a",
            "inserted after page one",
            NOW + timedelta(seconds=30),
        )
        second = await client_a.get(
            "/api/conversations?limit=2&cursor=" + first_body["nextCursor"]
        )
        assert [row["id"] for row in payload(second)["conversations"]] == [
            ids[0]
        ]

        for private_client in (client_other, client_tenant_b):
            hidden = await private_client.get(f"/api/conversations/{ids[0]}")
            missing = await private_client.get("/api/conversations/does-not-exist")
            assert (hidden.status, hidden.text) == (missing.status, missing.text)
            assert hidden.status == 404
            hidden_delete = await private_client.delete(
                f"/api/conversations/{ids[0]}", headers=mutation_headers()
            )
            assert (hidden_delete.status, hidden_delete.text) == (
                missing.status,
                missing.text,
            )

        assert [
            row["id"]
            for row in payload(await client_other.get("/api/conversations"))[
                "conversations"
            ]
        ] == [other_id]
        assert [
            row["id"]
            for row in payload(
                await client_tenant_b.get("/api/conversations")
            )["conversations"]
        ] == [tenant_b_id]

        unguarded = await client_a.delete(f"/api/conversations/{ids[0]}")
        assert unguarded.status == 403
        too_large = await client_a.get(
            f"/api/conversations?limit={MAX_CONVERSATION_PAGE_SIZE + 1}"
        )
        assert too_large.status == 400
        bad_cursor = await client_a.get(
            "/api/conversations?cursor=" + "a" * 1025
        )
        assert bad_cursor.status == 400
        oversized_id = await client_a.get("/api/conversations/" + "x" * 129)
        assert oversized_id.status == 400

        detail = await client_a.get(
            f"/api/conversations/{ids[0]}?limit=1"
        )
        detail_body = payload(detail)
        assert [turn["seq"] for turn in detail_body["turns"]] == [0]
        assert detail_body["nextCursor"] is None
        too_many_turns = await client_a.get(
            f"/api/conversations/{ids[0]}?limit={MAX_TURN_PAGE_SIZE + 1}"
        )
        assert too_many_turns.status == 400

    asyncio.run(exercise())


def test_turn_pagination_remains_sequence_stable_across_appends(tmp_path):
    async def exercise():
        store = make_store(tmp_path / "history.db")
        token = add_user(store, "user-a")
        conversation_id = "voice-" + "d" * 32
        store.open_conversation(
            conversation_id, store.tenant_id, "user-a", "one", NOW
        )
        for index in range(1, 5):
            store.append_conversation_turn(
                conversation_id,
                store.tenant_id,
                "user-a",
                "agent" if index % 2 else "user",
                str(index),
                NOW + timedelta(seconds=index),
            )
        client = make_client(store, token)
        first = payload(
            await client.get(f"/api/conversations/{conversation_id}?limit=2")
        )
        assert [turn["seq"] for turn in first["turns"]] == [0, 1]
        store.append_conversation_turn(
            conversation_id,
            store.tenant_id,
            "user-a",
            "agent",
            "new append",
            NOW + timedelta(seconds=10),
        )
        second = payload(
            await client.get(
                f"/api/conversations/{conversation_id}?limit=2&cursor="
                + first["nextCursor"]
            )
        )
        assert [turn["seq"] for turn in second["turns"]] == [2, 3]
        third = payload(
            await client.get(
                f"/api/conversations/{conversation_id}?limit=2&cursor="
                + second["nextCursor"]
            )
        )
        assert [turn["seq"] for turn in third["turns"]] == [4, 5]
        assert third["nextCursor"] is None

    asyncio.run(exercise())


def test_all_completion_paths_and_partial_error_rules(monkeypatch, tmp_path):
    async def exercise():
        store = make_store(tmp_path / "history.db")
        add_user(store, "user-a")
        runtime = server._HistoryRuntime()
        session = CaptureSession(store, runtime)
        ws = CaptureWebSocket()
        monkeypatch.setattr(server, "_write_turn_metrics", lambda *args: None)
        monkeypatch.setattr(server, "_stash_turn_metrics", lambda *args: None)

        assert await server._capture_user_utterance(ws, session, "stream user")
        server._begin_agent_turn(session)
        await server._consume_sse(
            ws,
            session,
            SSE(
                ("delta", {"text": "Streamed reply."}),
                ("final", {"response": "Streamed reply.", "debug": {}}),
            ),
        )

        assert await server._capture_user_utterance(ws, session, "tool user")
        server._begin_agent_turn(session)
        await server._consume_sse(
            ws,
            session,
            SSE(
                ("delta", {"text": "Checking."}),
                (
                    "tool_pending",
                    {"requestId": "request-1", "tools": [], "debug": {}},
                ),
            ),
        )
        await server._consume_sse(
            ws,
            session,
            SSE(
                ("delta", {"text": "Done."}),
                ("final", {"response": "Done.", "debug": {}}),
            ),
        )

        class Flow:
            slots = {}
            turns_used = 0
            max_turns = 5

            async def reply(self, _text):
                return SimpleNamespace(
                    text="Scheduler reply",
                    outcome=None,
                    slots={},
                    rejected=[],
                    turns_used=1,
                    max_turns=5,
                    supervisor_ms=1,
                    done=False,
                )

        assert await server._capture_user_utterance(ws, session, "scheduler user")
        server._begin_agent_turn(session)
        session._scheduler_flow_enabled = True
        session._scheduler_flow_attempted = True
        session._scheduler_flow = Flow()
        assert await server._handle_scheduler_request(
            ws, session, "scheduler user"
        )

        assert await server._capture_user_utterance(ws, session, "nonstream user")
        server._begin_agent_turn(session)
        await server._process_api_response(
            ws, session, {"type": "final", "response": "Nonstream reply"}
        )

        # A committed barge-in, an API error, and a disconnect leave their
        # completed user turns but never save partial/status agent text.
        assert await server._capture_user_utterance(ws, session, "barge user")
        server._begin_agent_turn(session)
        server._append_agent_delta(session, "barge partial")
        server._abandon_agent_turn(session)

        assert await server._capture_user_utterance(ws, session, "error user")
        server._begin_agent_turn(session)
        await server._process_api_response(ws, session, {"error": "backend"})

        assert await server._capture_user_utterance(ws, session, "disconnect user")
        server._begin_agent_turn(session)
        server._append_agent_delta(session, "disconnect partial")
        server._abandon_agent_turn(session)

        # Completion after an interruption still records normally.
        assert await server._capture_user_utterance(ws, session, "recovery user")
        server._begin_agent_turn(session)
        await server._process_api_response(
            ws, session, {"type": "final", "response": "Recovered reply"}
        )

        metadata, turns = read_turns(store, session.conversation_id)
        assert metadata["turn_count"] == len(turns) == 13
        assert [(turn["role"], turn["text"]) for turn in turns] == [
            ("user", "stream user"),
            ("agent", "Streamed reply."),
            ("user", "tool user"),
            ("agent", "Checking.\n\nDone."),
            ("user", "scheduler user"),
            ("agent", "Scheduler reply"),
            ("user", "nonstream user"),
            ("agent", "Nonstream reply"),
            ("user", "barge user"),
            ("user", "error user"),
            ("user", "disconnect user"),
            ("user", "recovery user"),
            ("agent", "Recovered reply"),
        ]

        anonymous = CaptureSession(None, runtime, "voice-" + "e" * 32)
        assert await server._capture_user_utterance(ws, anonymous, "ephemeral")
        connection = sqlite3.connect(store.db_path)
        assert connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0] == 1
        connection.close()

        oversized = "x" * (MAX_TURN_TEXT_LENGTH + 1)
        assert not await server._capture_user_utterance(ws, session, oversized)
        assert ws.messages[-1] == {
            "type": "input_error",
            "error": "message_too_long",
            "maxLength": MAX_TURN_TEXT_LENGTH,
        }

    asyncio.run(exercise())


def test_write_failure_is_visible_and_counter_stays_atomic(tmp_path):
    async def exercise():
        store = make_store(tmp_path / "history.db")
        token = add_user(store, "user-a")
        client = make_client(store, token)
        runtime = client.app[server.HISTORY_RUNTIME_KEY]
        session = CaptureSession(store, runtime, "voice-" + "9" * 32)
        ws = CaptureWebSocket()
        assert await server._capture_user_utterance(ws, session, "saved user")

        def fail_append(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected history outage")

        store.append_conversation_turn = fail_append
        server._begin_agent_turn(session)
        await server._complete_agent_turn(ws, session, "lost agent reply")

        assert any(
            message.get("error") == "history_write_failed"
            for message in ws.messages
        )
        detail = await client.get(
            f"/api/conversations/{session.conversation_id}"
        )
        assert detail.status == 200
        body = payload(detail)
        assert body["conversation"]["incomplete"] is True
        assert body["conversation"]["turnCount"] == 1
        assert [turn["text"] for turn in body["turns"]] == ["saved user"]

    asyncio.run(exercise())


def test_delete_item_and_all_cascade_memory_and_close_active_first(
    monkeypatch, tmp_path
):
    async def exercise():
        store = make_store(tmp_path / "history.db")
        token = add_user(store, "user-a")
        add_user(store, "user-b")
        item_id = "voice-" + "6" * 32
        all_id = "voice-" + "7" * 32
        other_id = "voice-" + "8" * 32
        for conversation_id, user in (
            (item_id, "user-a"),
            (all_id, "user-a"),
            (other_id, "user-b"),
        ):
            store.open_conversation(
                conversation_id, store.tenant_id, user, conversation_id, NOW
            )
            store.append_conversation_turn(
                conversation_id,
                store.tenant_id,
                user,
                "agent",
                "reply",
                NOW,
            )
            (tmp_path / f"{conversation_id}.json").write_text("memory")

        events: list[str] = []

        class MemoryClient:
            def __init__(self, *args, **kwargs):
                pass

            async def request(self, method, url, **kwargs):
                assert method == "DELETE"
                conversation_id = kwargs["json"]["sessionId"]
                (tmp_path / f"{conversation_id}.json").unlink(missing_ok=True)
                events.append(f"memory:{conversation_id}")
                return SimpleNamespace(status_code=200)

            async def aclose(self):
                return None

        monkeypatch.setattr(server.httpx, "AsyncClient", MemoryClient)
        client = make_client(store, token)
        runtime = client.app[server.HISTORY_RUNTIME_KEY]
        active_ws = BoundWebSocket(events)
        await server._register_history_socket(
            runtime,
            WebSocketIdentity("user-a", store.tenant_id, item_id),
            active_ws,
        )
        original_delete = store.delete_conversation

        def checked_delete(*args):
            assert active_ws.closed is True
            events.append("database:item")
            return original_delete(*args)

        store.delete_conversation = checked_delete
        deleted = await client.delete(
            f"/api/conversations/{item_id}", headers=mutation_headers()
        )
        assert deleted.status == 200
        assert events[:2] == ["socket_closed", "database:item"]
        assert not (tmp_path / f"{item_id}.json").exists()
        assert store.get_conversation_page(
            item_id, store.tenant_id, "user-a", limit=2
        ) is None

        # The tombstone prevents the closed socket from recreating/appending.
        stale_session = CaptureSession(store, runtime, item_id)
        stale_session._history_started = True
        assert not await server._capture_user_utterance(
            CaptureWebSocket(), stale_session, "late append"
        )

        delete_all = await client.delete(
            "/api/conversations", headers=mutation_headers()
        )
        assert delete_all.status == 200
        assert payload(delete_all) == {"ok": True, "deleted": 1}
        assert not (tmp_path / f"{all_id}.json").exists()
        assert (tmp_path / f"{other_id}.json").exists()

        connection = sqlite3.connect(store.db_path)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM conversation_turns AS turns
            JOIN conversations AS conversations
              ON conversations.id=turns.conversation_id
            WHERE conversations.tenant_id=? AND conversations.user_sub=?
            """,
            (store.tenant_id, "user-a"),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_turns"
        ).fetchone()[0] == 2
        connection.close()

    asyncio.run(exercise())


def test_periodic_sweep_removes_expired_session_and_closes_socket(tmp_path):
    async def exercise():
        current = [NOW]
        policy = SessionPolicy(
            random_bytes=lambda size: b"r" * size,
            clock=lambda: current[0],
        )
        store = SQLiteAuthStore(
            tmp_path / "history.db", tenant_id="tenant-a", policy=policy
        )
        raw_token = add_user(store, "user-a")
        adapter = AiohttpAuthAdapter(
            client_id="client.apps.googleusercontent.com",
            mode=AUTH_MODE_OPTIONAL,
            public_https=False,
            store=store,
            verifier=object(),
            clock=lambda: current[0],
        )
        app = server.create_app(auth_adapter=adapter)
        client = InProcessClient(app)
        client.cookies[SESSION_COOKIE_NAME] = raw_token
        ws = BoundWebSocket()
        request = client.make_request(
            "GET", "/ws", headers={"Origin": PUBLIC_ORIGIN}
        )
        await adapter.bind_websocket(
            request, ws, "voice-" + "5" * 32, prepare=True
        )
        assert ws.closed is False

        current[0] = NOW + timedelta(days=8)
        context = server._auth_sweep_context(app)
        await anext(context)
        for _ in range(100):
            if ws.closed:
                break
            await asyncio.sleep(0.01)
        await context.aclose()

        assert ws.closed is True
        connection = sqlite3.connect(store.db_path)
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        connection.close()

    asyncio.run(exercise())
