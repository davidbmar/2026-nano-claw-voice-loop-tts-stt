"""Privacy, bounds, correlation, and resilience tests for client telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from voice import server
from voice.webauth.aiohttp_adapter import (
    AUTH_MODE_OFF,
    PUBLIC_ORIGIN,
    AiohttpAuthAdapter,
)


class _MemoryPayload:
    def __init__(self, body: bytes):
        self.body = body

    def set_read_chunk_size(self, _size):
        return None

    async def readany(self):
        body, self.body = self.body, b""
        return body


class InProcessClient:
    """Dispatch through aiohttp's real router and middleware without a port."""

    def __init__(self, app: web.Application):
        self.app = app
        app.freeze()

    def make_request(
        self,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        remote: str = "127.0.0.1",
    ) -> web.Request:
        request_headers = {
            "Host": "nano.chattychapters.com",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
        transport = mock.Mock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            (remote, 40000) if name == "peername" else default
        )
        return make_mocked_request(
            "POST",
            "/api/client-log",
            headers=request_headers,
            app=self.app,
            transport=transport,
            payload=_MemoryPayload(body),
        )

    async def post_raw(
        self,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        remote: str = "127.0.0.1",
    ) -> web.StreamResponse:
        return await self.app._handle(
            self.make_request(body, headers=headers, remote=remote)
        )

    async def post_json(
        self,
        body: object,
        *,
        headers: dict[str, str] | None = None,
        remote: str = "127.0.0.1",
    ) -> web.StreamResponse:
        return await self.post_raw(
            json.dumps(body).encode("utf-8"),
            headers=headers,
            remote=remote,
        )


def mutation_headers() -> dict[str, str]:
    return {
        "Origin": PUBLIC_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-NC-Auth": "1",
    }


def make_client() -> InProcessClient:
    adapter = AiohttpAuthAdapter(
        client_id=None,
        mode=AUTH_MODE_OFF,
        public_https=False,
        store=None,
        verifier=object(),
    )
    return InProcessClient(server.create_app(auth_adapter=adapter))


def event(message: str = "WS OPEN gen=1") -> dict[str, object]:
    return {"t": "2026-07-18T12:00:00.000Z", "tag": "ws", "msg": message}


def run(coro):
    return asyncio.run(coro)


def test_client_log_accepts_batch_and_uses_server_owned_conversation(
    caplog: pytest.LogCaptureFixture,
):
    async def exercise():
        client = make_client()
        runtime = client.app[server.CLIENT_TELEMETRY_RUNTIME_KEY]
        server_conversation = "voice-" + "a" * 32
        runtime.register_socket(server_conversation, "127.0.0.1")

        with caplog.at_level(logging.INFO, logger="client"):
            response = await client.post_json(
                {
                    "events": [event()],
                    "conv": server_conversation,
                    "ua": "Telemetry Test Browser",
                },
                headers=mutation_headers(),
            )
            forged = await client.post_json(
                {
                    "events": [event("WS OPEN forged")],
                    "conv": "client-forged-conversation",
                    "ua": "Telemetry Test Browser",
                },
                headers=mutation_headers(),
            )

        assert response.status == 204
        assert forged.status == 204
        records = [record for record in caplog.records if record.name == "client"]
        assert len(records) == 2
        logged = json.loads(records[0].getMessage())
        assert logged == {
            "ip": "127.0.0.1",
            "conv": server_conversation,
            "ua": "Telemetry Test Browser",
            "t": "2026-07-18T12:00:00.000Z",
            "tag": "ws",
            "msg": "WS OPEN gen=1",
        }
        forged_log = json.loads(records[1].getMessage())
        assert forged_log["conv"] is None
        assert "client-forged-conversation" not in records[1].getMessage()

    run(exercise())


def test_client_log_rejects_oversized_body_and_batch():
    async def exercise():
        client = make_client()
        too_large = await client.post_raw(
            b"x" * (server.CLIENT_LOG_MAX_BODY_BYTES + 1),
            headers=mutation_headers(),
        )
        too_many = await client.post_json(
            {
                "events": [event(str(index)) for index in range(51)],
                "conv": None,
                "ua": "test",
            },
            headers=mutation_headers(),
        )

        assert too_large.status == 413
        assert too_many.status == 413

    run(exercise())


def test_client_log_truncates_long_messages(caplog: pytest.LogCaptureFixture):
    async def exercise():
        client = make_client()
        with caplog.at_level(logging.INFO, logger="client"):
            response = await client.post_json(
                {"events": [event("x" * 700)], "conv": None, "ua": "test"},
                headers=mutation_headers(),
            )

        assert response.status == 204
        record = next(record for record in caplog.records if record.name == "client")
        assert json.loads(record.getMessage())["msg"] == "x" * 500

    run(exercise())


def test_client_log_rate_limits_each_socket_or_ip():
    async def exercise():
        client = make_client()
        runtime = client.app[server.CLIENT_TELEMETRY_RUNTIME_KEY]
        runtime.rate_limiter = server._ClientLogRateLimiter(
            capacity=2,
            refill_per_second=0,
            clock=lambda: 1.0,
        )
        statuses = []
        for _ in range(3):
            response = await client.post_json(
                {"events": [event()], "conv": None, "ua": "test"},
                headers=mutation_headers(),
            )
            statuses.append(response.status)

        assert statuses == [204, 204, 429]

    run(exercise())


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b'{"events":"not-a-list"}',
        b'{"events":[{"t":null,"tag":"ws","msg":"open"}]}',
        b'{"events":[{"t":1,"tag":false,"msg":"open"}]}',
    ],
)
def test_client_log_bad_input_never_returns_500(body: bytes):
    async def exercise():
        client = make_client()
        response = await client.post_raw(body, headers=mutation_headers())
        assert 400 <= response.status < 500

        healthy = await client.post_json(
            {"events": [event()], "conv": None, "ua": "test"},
            headers=mutation_headers(),
        )
        assert healthy.status == 204

    run(exercise())


def test_client_log_requires_existing_same_origin_mutation_headers():
    async def exercise():
        client = make_client()
        body = {"events": [event()], "conv": None, "ua": "test"}
        missing = await client.post_json(body)
        cross_origin = await client.post_json(
            body,
            headers={
                **mutation_headers(),
                "Origin": "https://attacker.example",
            },
        )

        assert missing.status == 403
        assert cross_origin.status == 403

    run(exercise())


def test_client_log_logging_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
):
    async def exercise():
        client = make_client()
        monkeypatch.setattr(
            server.client_log,
            "info",
            mock.Mock(side_effect=RuntimeError("logging unavailable")),
        )
        response = await client.post_json(
            {"events": [event()], "conv": None, "ua": "test"},
            headers=mutation_headers(),
        )
        assert response.status == 204

    run(exercise())
