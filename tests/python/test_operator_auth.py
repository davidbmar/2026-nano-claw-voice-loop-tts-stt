"""Operator-password guard on deployment-wide configuration writes.

These four POST routes change behaviour for EVERY caller on the deployment —
phone-line voice/model/STT, assistant mode, scheduler model — and until this
guard existed they were reachable anonymously from the public internet
(`SENSITIVE_PATH_PREFIXES` covered only the auth/history routes, and the
same-origin check it applies is CSRF mitigation, not authentication).

The regression these tests exist to prevent has two halves:
  1. an anonymous or wrong-password caller must never mutate global config;
  2. the Telnyx webhook (/api/phone/incoming) must never be caught by the
     guard — it carries its own token, cannot present a browser origin or the
     operator secret, and guarding it silently kills every inbound call.
"""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from voice.webauth.aiohttp_adapter import (
    OPERATOR_HEADER,
    OPERATOR_PATHS,
    request_security_middleware,
)

PASSWORD = "test-operator-secret"

# Headers a real same-origin browser POST from the console carries. These
# satisfy the CSRF check on their own — which is exactly why they must NOT be
# sufficient to mutate operator config.
SAME_ORIGIN = {
    "Origin": "http://localhost:9090",
    "Host": "localhost:9090",
    "Sec-Fetch-Site": "same-origin",
    "X-NC-Auth": "1",
}


async def _ok_handler(_request: web.Request) -> web.Response:
    return web.json_response({"reached": True})


def _run(method: str, path: str, headers: dict[str, str], password: str | None):
    """Drive the middleware directly and report whether the handler ran."""

    env = dict(os.environ)
    env.pop("NANO_CLAW_OPERATOR_PASSWORD", None)
    if password is not None:
        env["NANO_CLAW_OPERATOR_PASSWORD"] = password

    async def exercise():
        request = make_mocked_request(method, path, headers=headers)
        return await request_security_middleware(request, _ok_handler)

    with mock.patch.dict(os.environ, env, clear=True):
        return asyncio.run(exercise())


@pytest.mark.parametrize("path", sorted(OPERATOR_PATHS))
def test_anonymous_post_is_refused(path):
    """The pre-fix behaviour: a bare POST from anywhere used to be accepted."""

    response = _run("POST", path, {}, PASSWORD)
    assert response.status == 403


@pytest.mark.parametrize("path", sorted(OPERATOR_PATHS))
def test_same_origin_alone_is_not_enough(path):
    """CSRF headers are forgeable by any non-browser client, so they cannot authorize."""

    response = _run("POST", path, dict(SAME_ORIGIN), PASSWORD)
    assert response.status == 403


@pytest.mark.parametrize("path", sorted(OPERATOR_PATHS))
def test_correct_password_reaches_the_handler(path):
    headers = dict(SAME_ORIGIN, **{OPERATOR_HEADER: PASSWORD})
    response = _run("POST", path, headers, PASSWORD)
    assert response.status == 200


@pytest.mark.parametrize("path", sorted(OPERATOR_PATHS))
def test_wrong_password_is_refused(path):
    headers = dict(SAME_ORIGIN, **{OPERATOR_HEADER: "not-the-password"})
    response = _run("POST", path, headers, PASSWORD)
    assert response.status == 403


@pytest.mark.parametrize("path", sorted(OPERATOR_PATHS))
def test_unset_password_fails_closed(path):
    """An unconfigured deployment refuses operator writes rather than allowing them."""

    headers = dict(SAME_ORIGIN, **{OPERATOR_HEADER: ""})
    response = _run("POST", path, headers, None)
    assert response.status == 403


@pytest.mark.parametrize("path", sorted(OPERATOR_PATHS))
def test_reads_stay_open(path):
    """GET must keep working anonymously — the phone banner reads config unauthenticated."""

    response = _run("GET", path, {}, PASSWORD)
    assert response.status == 200


def test_telnyx_webhook_is_never_guarded():
    """/api/phone/incoming must not be captured by a "/api/phone/" style prefix.

    Telnyx has no browser origin and no operator password. If this 403s, every
    inbound call to the production line breaks.
    """

    response = _run("POST", "/api/phone/incoming", {}, PASSWORD)
    assert response.status == 200
    assert "/api/phone/incoming" not in OPERATOR_PATHS


def test_phone_media_websocket_is_never_guarded():
    response = _run("GET", "/ws/phone-media", {}, PASSWORD)
    assert response.status == 200


# ── Operator-data endpoints ──────────────────────────────────
# /api/metrics and /api/costs were readable by anyone: metrics published live
# session IDs, the serving model, and token counts; costs published real spend
# and customer counts. Both now take the same operator read token as /api/calls.
#
# The session-ID publication mattered beyond the data itself — it is what would
# have made a session-scoped lookup route genuinely reachable rather than
# theoretically guessable.

OPS_DATA_PATHS = ("/api/metrics", "/api/costs")


@pytest.mark.parametrize("path", OPS_DATA_PATHS)
def test_ops_data_requires_token(path):
    from voice import server

    async def exercise():
        request = make_mocked_request("GET", path, headers={})
        handler = (
            server.metrics_handler if path == "/api/metrics" else server.costs_handler
        )
        return await handler(request)

    env = {
        "NANO_CLAW_OPERATOR_READ_TOKEN": "ops-token",
        "NANO_CLAW_PHONE_TOKEN": "phone-token",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        response = asyncio.run(exercise())
    assert response.status == 403


@pytest.mark.parametrize("path", OPS_DATA_PATHS)
def test_ops_data_allows_correct_token(path):
    from voice import server

    async def exercise():
        request = make_mocked_request(
            "GET", path, headers={"X-NC-Operator-Read": "ops-token"}
        )
        handler = (
            server.metrics_handler if path == "/api/metrics" else server.costs_handler
        )
        return await handler(request)

    env = {
        "NANO_CLAW_OPERATOR_READ_TOKEN": "ops-token",
        "NANO_CLAW_PHONE_TOKEN": "phone-token",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        response = asyncio.run(exercise())
    assert response.status == 200
