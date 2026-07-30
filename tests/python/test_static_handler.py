"""The console's static file route.

The route pattern was widened from ``/{filename}`` to ``/{filename:.+}`` so that
assets in subdirectories (``vendor/mascot/...``) resolve at all — aiohttp's
default pattern matches a single path segment, so every nested asset 404'd and
the mascot renderer failed to import.

Widening a static route is exactly the change that needs a traversal test. The
guard is on the RESOLVED path rather than on the pattern, so it should hold
regardless of how many segments the request has — these tests are what makes
that claim checkable rather than merely asserted in a comment.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from voice import server


def _get(path: str):
    """Dispatch through the real handler without binding a socket.

    ``match_info`` is normally filled in by the router, and `make_mocked_request`
    leaves it empty — so it is supplied here exactly as `/{filename:.+}` would
    capture it: everything after the leading slash, slashes included.
    """

    filename = path.lstrip("/")
    request = make_mocked_request("GET", path, match_info={"filename": filename})
    return asyncio.run(server.static_handler(request))


def test_serves_a_top_level_asset():
    response = _get("/app.js")
    assert isinstance(response, web.FileResponse)


def test_serves_a_nested_asset():
    """The regression this route change exists to fix."""

    response = _get("/vendor/mascot/character-rig.js")
    assert isinstance(response, web.FileResponse)


def test_serves_a_deeply_nested_binary_asset():
    response = _get("/vendor/mascot/public/layers/body.webp")
    assert isinstance(response, web.FileResponse)


@pytest.mark.parametrize(
    "path",
    [
        "/../server.py",
        "/../../etc/passwd",
        "/vendor/../../server.py",
        "/vendor/mascot/../../../voice/server.py",
        "/....//server.py",
    ],
)
def test_refuses_to_escape_the_web_root(path):
    """Containment is enforced on the resolved path, so extra segments do not
    help an attacker — the whole point of widening the pattern safely."""

    with pytest.raises(web.HTTPNotFound):
        _get(path)


def test_refuses_a_directory():
    """A directory resolves inside the root but is not a file; serving it would
    leak a listing or raise deep in aiohttp."""

    with pytest.raises(web.HTTPNotFound):
        _get("/vendor/mascot")


def test_refuses_a_file_that_does_not_exist():
    with pytest.raises(web.HTTPNotFound):
        _get("/vendor/mascot/nope.js")
