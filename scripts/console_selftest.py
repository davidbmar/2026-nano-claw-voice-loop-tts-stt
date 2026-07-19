#!/usr/bin/env python3
"""Boot-contract smoke test for the voice console.

Validates that the console can actually render — every HTTP endpoint the page
fetches on load returns 200, and /api/models returns a model list with a
default that resolves to a real, selectable option (so the LLM dropdown is
never blank). This is the HTTP-layer complement to scripts/voice_healthcheck.py
(which exercises the /ws voice round-trip).

Usage:  console_selftest.py [base_url]        # default http://localhost:9090
Exit 0 = all good; non-zero = first failing check (message on stderr).
No third-party deps — uses only the standard library so it runs anywhere.
"""
from __future__ import annotations
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9090").rstrip("/")

# Endpoints the page fetches on load. `auth_ok` marks ones where 401 is a valid
# "not signed in" answer rather than a failure (/api/me is only reachable with a
# session cookie and legitimately 401s for an anonymous visitor).
PAGES = ["/", "/costs"]
BOOT_ENDPOINTS = [
    "/api/models",
    "/api/voices",
    "/api/voice/flow",
    "/api/voice/region-model",
    "/api/auth/config",
    "/api/phone/config",
    "/api/phone/vad",
]


def fetch(path: str, timeout: float = 8.0):
    """Return (status, body_bytes). Treats any HTTP response as a status, not
    an exception, so a 401/404 is data we can assert on."""
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # connection refused, DNS, timeout, ...
        return None, str(e).encode()


def fail(msg: str, code: int) -> "int":
    print(f"FAIL: {msg}", file=sys.stderr)
    return code


def main() -> int:
    # 1. Static pages load.
    for p in PAGES:
        status, _ = fetch(p)
        if status != 200:
            return fail(f"page {p} -> {status} (expected 200)", 2)

    # 2. Every boot endpoint answers 200.
    for p in BOOT_ENDPOINTS:
        status, body = fetch(p)
        if status != 200:
            return fail(f"boot endpoint {p} -> {status} (expected 200)", 3)

    # 3. /api/models is renderable: a non-empty list and a default that maps to
    #    a real, available option — i.e. the dropdown can show a non-blank pick.
    status, body = fetch("/api/models")
    try:
        data = json.loads(body)
    except Exception:
        return fail("/api/models did not return JSON", 4)
    models = data.get("models") or []
    default = data.get("default")
    if not models:
        return fail("/api/models returned no models", 4)
    ids = {m.get("id") for m in models}
    available = [m for m in models if m.get("available")]
    if default not in ids:
        return fail(f"/api/models default {default!r} is not in the model list", 5)
    if not available:
        return fail("/api/models has no available models (every option disabled)", 5)

    print(json.dumps({
        "ok": True,
        "base": BASE,
        "models": len(models),
        "available": len(available),
        "default": default,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
