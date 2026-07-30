#!/usr/bin/env python3
"""Preflight the turn-delegate chain before a real phone call touches it.

Delegating a line spans two repos and two hand-written JSON env vars, and the
failure modes are quiet by design: a delegate that cannot be reached makes the
gateway speak a fixed apology, and a line that is not configured simply behaves
as it always did. Both are correct behaviours and both look like nothing
happening. This exercises the chain deliberately so the silence is explained
before a caller hears it.

    cd ~/src/nano-claw
    set -a; source .env; set +a
    .venv-test/bin/python scripts/check_delegate_setup.py
    .venv-test/bin/python scripts/check_delegate_setup.py --did +15125550100

It calls the SAME functions the live path calls — `validate_delegate_url`,
`start_conversation`, `call_delegate` — rather than reimplementing them. A
preflight that passes against its own copy of the logic proves nothing.

Nothing here dials a carrier, answers a call, or writes to the routing map.
Exit status is 0 only if every configured line completed a round trip.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from voice.phone import (  # noqa: E402
    conversation_key_for,
    delegate_starts,
)
from voice.turn_delegate import (  # noqa: E402
    DelegateUrlRefused,
    call_delegate,
    resolve_returned_url,
    safe_url_for_log,
    start_conversation,
    validate_delegate_url,
)
from voice.flow_session import delegate_allowed_hosts  # noqa: E402

OK = "  ok   "
BAD = " FAIL  "
NOTE = "  ..   "


def line(status: str, text: str) -> None:
    print(f"[{status}] {text}")


async def check_line(did: str, profile, probe_text: str, raw: dict | None = None) -> bool:
    print(f"\n─── {did} ───")

    # A key nobody recognises is dropped in silence, which looks identical to a
    # field that was never set. The log says so; an operator running a preflight
    # is not reading the log.
    if isinstance(raw, dict):
        from voice.phone import _PROFILE_KEYS
        for key in sorted(set(raw) - _PROFILE_KEYS):
            import difflib
            close = difflib.get_close_matches(key, sorted(_PROFILE_KEYS), n=1)
            line(BAD, f"unknown profile key {key!r}"
                      + (f" — did you mean {close[0]!r}?" if close else "")
                      + " — it is ignored")

    try:
        validate_delegate_url(
            profile.start_url, allowed_hosts=delegate_allowed_hosts())
        line(OK, f"start url allowed: {safe_url_for_log(profile.start_url)}")
    except DelegateUrlRefused as exc:
        line(BAD, f"start url refused: {exc}")
        line(NOTE, "loopback is always allowed; anything else needs "
                   "NANO_CLAW_DELEGATE_HOSTS")
        return False

    # This script runs on the HOST. The gateway may not.
    #
    # nano-claw is deployed in a container — every other host service it uses is
    # reached at `host.docker.internal` — and inside a container `127.0.0.1` is
    # the container itself. A loopback start URL therefore passes here and fails
    # in production, where the failure is silent: the start fails, the gateway
    # falls OPEN, and calls are answered undelegated with nothing to see.
    #
    # This cannot be tested from here, so it is said rather than checked.
    if urlparse(profile.start_url).hostname in {"127.0.0.1", "localhost", "::1"}:
        line(NOTE, "loopback URL — fine if the gateway runs on this host. If it "
                   "runs in a container, use host.docker.internal and add it to "
                   "NANO_CLAW_DELEGATE_HOSTS; this check cannot tell.")

    # A key that is stable per call, so this probe does not mint a fresh
    # conversation every time it runs against a delegate that deduplicates.
    key = conversation_key_for(f"preflight:{did}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        t0 = time.monotonic()
        started = await start_conversation(
            client, profile.start_url, conversation_key=key,
            channel="phone", to=did)
        start_ms = (time.monotonic() - t0) * 1000
        if not started.ok:
            # `failure` carries the app's own explanation when it gave one. A
            # ceiling, a misconfigured line and a crash are all "start failed"
            # without it, and because the gateway fails OPEN the only symptom in
            # production is calls quietly ceasing to be delegated.
            line(BAD, f"conversation start failed: {started.failure}")
            if "already open" in (started.failure or "") or "503" in (started.failure or ""):
                # The app states the situation without naming a config variable,
                # because its HTTP details are owner-facing. This output is not:
                # only an operator runs a preflight, so the variable belongs here.
                line(NOTE, "at the app's conversation ceiling — raise "
                           "RB_DELEGATE_MAX_LIVE on the riff-builder side")
            line(NOTE, "the gateway would answer the call and handle it "
                       "undelegated — start failures fail OPEN")
            return False
        line(OK, f"conversation start {start_ms:.0f}ms → "
                 f"{safe_url_for_log(started.delegate_url)}")

        # Already enforced inside start_conversation; restated here because a
        # human reading this output should see the guarantee named, not implied.
        try:
            resolve_returned_url(profile.start_url, started.delegate_url)
            line(OK, "returned url is same-origin with the start url")
        except DelegateUrlRefused as exc:
            line(BAD, f"returned url refused: {exc}")
            return False

        # Idempotency: the same key must not mint a second conversation.
        repeat = await start_conversation(
            client, profile.start_url, conversation_key=key,
            channel="phone", to=did)
        if repeat.ok and repeat.delegate_url == started.delegate_url:
            line(OK, "a repeated conversation_key returns the same conversation")
        else:
            line(BAD, "a repeated conversation_key minted a DIFFERENT "
                      "conversation — a redelivered webhook would split one "
                      "caller across two")
            return False

        t0 = time.monotonic()
        reply = await call_delegate(
            client, started.delegate_url, probe_text, who="caller")
        turn_ms = (time.monotonic() - t0) * 1000
        if not reply.ok:
            line(BAD, f"turn failed: {reply.failure}")
            return False
        line(OK, f"turn {turn_ms:.0f}ms → {reply.text[:60]!r}")
        if turn_ms > 2000:
            line(NOTE, f"{turn_ms:.0f}ms is past the ~2s the contract says to "
                       "fill; the phone thinking cue covers it")

    if profile.greeting:
        line(OK, f"greeting: {profile.greeting[:60]!r}")
    else:
        line(NOTE, "no greeting set — this line will answer naming nobody")
    if profile.voice:
        line(OK, f"voice: {profile.voice}")
    if profile.speed:
        line(OK, f"speed: {profile.speed}")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--did", help="check only this line")
    parser.add_argument("--say", default="we do emergency plumbing repairs",
                        help="what the probe 'caller' says")
    args = parser.parse_args()

    lines = delegate_starts()
    if not lines:
        line(BAD, "no delegated lines: NANO_CLAW_DELEGATE_STARTS is unset, "
                  "empty, or not valid JSON")
        line(NOTE, 'e.g. {"+15125550100": {"start": "http://127.0.0.1:8790'
                   '/api/delegate/start", "greeting": "Thanks for calling."}}')
        return 1

    if args.did:
        if args.did not in lines:
            line(BAD, f"{args.did} is not a delegated line")
            line(NOTE, f"configured: {', '.join(sorted(lines))}")
            return 1
        lines = {args.did: lines[args.did]}

    # The raw JSON too, so unrecognised keys can be named — `delegate_starts()`
    # has already dropped them by the time it returns profiles.
    try:
        raw_table = json.loads(os.environ.get("NANO_CLAW_DELEGATE_STARTS", "") or "{}")
    except json.JSONDecodeError:
        raw_table = {}
    results = [
        await check_line(did, profile, args.say,
                         raw_table.get(did) if isinstance(raw_table.get(did), dict) else None)
        for did, profile in sorted(lines.items())
    ]

    print()
    passed = sum(1 for r in results if r)
    if passed == len(results):
        line(OK, f"{passed}/{len(results)} lines ready")
        return 0
    line(BAD, f"{passed}/{len(results)} lines ready")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
