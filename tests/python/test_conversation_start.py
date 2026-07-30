"""A response must not be able to introduce a destination config did not allow.

The conversation-start exchange exists so each phone call gets its own delegate
URL — a DID is dialled by several people at once, while the contract pairs one
URL with one conversation.

That means the gateway now takes a URL *from a response body*, which is a weaker
position than taking one from config. These tests are the rule that makes it
safe: same origin as the start URL, always.

Design and review: docs/design/2026-07-30-conversation-start-seam.md
"""
from __future__ import annotations

import asyncio

import pytest

from voice.turn_delegate import (
    DelegateUrlRefused,
    resolve_returned_url,
    start_conversation,
)

START = "http://127.0.0.1:8790/api/delegate/start"


class FakeResponse:
    def __init__(self, status_code=200, body=None, raises=None, content=None):
        self.status_code = status_code
        self._body = body
        self._raises = raises
        self.content = content

    def json(self):
        if self._raises:
            raise self._raises
        return self._body


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def post(self, url, *, json, timeout, follow_redirects=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout,
                           "follow_redirects": follow_redirects})
        if self.error:
            raise self.error
        return self.response


# ── the same-origin rule ─────────────────────────────────────────────────────

def test_a_relative_url_resolves_against_the_start_url():
    """riff-builder returns `/api/session/<id>/turn`. The first draft of the
    design would have REJECTED that, because validate_delegate_url requires a
    scheme and host."""
    assert resolve_returned_url(START, "/api/session/abc/turn") == (
        "http://127.0.0.1:8790/api/session/abc/turn")


def test_an_absolute_same_origin_url_is_allowed():
    url = "http://127.0.0.1:8790/api/session/abc/turn"
    assert resolve_returned_url(START, url) == url


@pytest.mark.parametrize("returned,why", [
    ("http://127.0.0.1:3001/api/chat", "the Node agent API — no auth at all"),
    ("http://127.0.0.1:8000/v1/query", "the platform — reads permissions from the body"),
    ("http://127.0.0.1:2375/containers/json", "the Docker daemon"),
    ("https://127.0.0.1:8790/t", "scheme differs"),
    ("http://localhost:8790/t", "same machine, different host string"),
    ("http://evil.example.com/collect", "somewhere else entirely"),
    ("http://127.0.0.1:8790.evil.com/t", "prefix-matching would have accepted this"),
])
def test_a_different_origin_is_refused(returned, why):
    """The finding that changed the design. An allowlist admits ANY port on an
    allowed host and ALL of loopback, so it would have let an allowlisted app
    redirect every caller utterance to any local service."""
    with pytest.raises(DelegateUrlRefused):
        resolve_returned_url(START, returned)


def test_credentials_in_a_returned_url_are_refused():
    """Same-origin says nothing about userinfo, and this string is
    attacker-influenced in a way a config value is not."""
    with pytest.raises(DelegateUrlRefused, match="credentials"):
        resolve_returned_url(START, "http://user:pass@127.0.0.1:8790/t")


@pytest.mark.parametrize("returned", ["", "   ", None, 42, {"url": "x"}])
def test_a_missing_or_non_string_url_is_refused(returned):
    with pytest.raises(DelegateUrlRefused):
        resolve_returned_url(START, returned)


# ── the exchange ─────────────────────────────────────────────────────────────

def test_a_good_start_returns_the_conversation_url():
    async def exercise():
        client = FakeClient(FakeResponse(
            body={"delegate_url": "/api/session/fresh/turn"}))
        result = await start_conversation(
            client, START, conversation_key="call-1", from_="+15125550101",
            to="+15123569101")

        assert result.ok is True
        assert result.delegate_url == "http://127.0.0.1:8790/api/session/fresh/turn"

    asyncio.run(exercise())


def test_the_request_carries_the_idempotency_key_and_bounded_controls():
    async def exercise():
        client = FakeClient(FakeResponse(body={"delegate_url": "/t"}))
        await start_conversation(client, START, conversation_key="call-1",
                                 from_="+15125550101", to="+15123569101")

        sent = client.calls[0]
        assert sent["json"]["conversation_key"] == "call-1", (
            "without this the delegate cannot deduplicate, and Telnyx redelivers "
            "webhooks while media streams reconnect")
        assert sent["json"]["who"] == "caller"
        assert sent["json"]["channel"] == "phone"
        assert sent["json"]["to"] == "+15123569101"
        assert sent["follow_redirects"] is False, (
            "this request carries caller PII; a followed redirect leaks it")
        assert sent["timeout"] == 10.0

    asyncio.run(exercise())


def test_a_withheld_caller_id_is_omitted_rather_than_null():
    """The contract says the app must tolerate a missing caller id. An explicit
    null invites a delegate to treat "withheld" as a routing case of its own."""
    async def exercise():
        client = FakeClient(FakeResponse(body={"delegate_url": "/t"}))
        await start_conversation(client, START, conversation_key="k", from_=None)

        assert "from" not in client.calls[0]["json"]

    asyncio.run(exercise())


@pytest.mark.parametrize("response,why", [
    (FakeResponse(status_code=500, body={"delegate_url": "/t"}), "non-200"),
    (FakeResponse(body={"error": "nope"}), "no delegate_url"),
    (FakeResponse(body=["not", "a", "dict"]), "not an object"),
    (FakeResponse(raises=ValueError("not json")), "unparseable"),
    (FakeResponse(body={"delegate_url": "http://evil.example.com/t"}), "other origin"),
])
def test_every_bad_start_fails_closed(response, why):
    async def exercise():
        result = await start_conversation(
            FakeClient(response), START, conversation_key="k")

        assert result.ok is False, why
        assert result.delegate_url == "", (
            "a failed start must not leave a dialable URL behind")

    asyncio.run(exercise())


def test_an_unreachable_start_does_not_raise():
    """It runs inside call setup. Raising there abandons the call."""
    async def exercise():
        result = await start_conversation(
            FakeClient(error=ConnectionRefusedError("nothing there")),
            START, conversation_key="k")

        assert result.ok is False
        assert result.failure and "ConnectionRefused" in result.failure

    asyncio.run(exercise())


def test_an_oversized_body_is_refused_before_parsing():
    async def exercise():
        huge = b"x" * (64 * 1024 + 1)
        result = await start_conversation(
            FakeClient(FakeResponse(body={"delegate_url": "/t"}, content=huge)),
            START, conversation_key="k")

        assert result.ok is False
        assert result.failure == "body too large"

    asyncio.run(exercise())
