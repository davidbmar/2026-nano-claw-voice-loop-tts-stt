#!/usr/bin/env python3
"""Drive a WHOLE scripted call through the real audio path — no PSTN, no cost.

`phone_loopback_test.py` proves one utterance gets an answer. That is not enough
to claim parity with the Gemini path this gateway replaced, because every bug
found on 2026-07-31 lived in a layer that text-level tests cannot reach:

  * DTMF was not handled at all — a digit never touches VAD, STT or the
    endpointer, so a delegate-contract test passes forever without it.
  * The endpointer cut callers off between clauses; the caller heard the
    thinking cue while still speaking.
  * The greeting came from the wrong place when a race was lost.

All three are audible and none are visible over HTTP. So this drives the media
WebSocket exactly as the carrier does, for a whole conversation, and transcribes
what the caller WOULD HAVE HEARD back through Whisper — the same
"what did the caller actually hear" discipline riff's call review uses.

Usage:
    phone_call_harness.py                       # the default parity script
    phone_call_harness.py --did +15102160079    # mint a delegate route first
    phone_call_harness.py --script my.json      # [{"say": "..."}, {"press": "1"}]

A delegated line needs a routing entry, which is normally minted by the
`call.initiated` webhook. --did posts that webhook first so a delegated line can
be exercised without a carrier.

WHAT THIS CANNOT MEASURE, and has twice claimed to:

1. TURN LATENCY. End-of-reply detection here is a silence window, and it is not
   reliable: replies that begin before the window opens, or that the pump misses
   the tail of, leave it waiting until the node's own 30s idle prompt. It then
   reports "34s" and an empty transcript for a turn the gateway logged as
   ok=True in 6s. Take timings from the gateway log
   (`delegate turn ok=True (Ns)`), which is the real clock.

2. RECOGNITION QUALITY. `synthesize_caller_audio` is TTS, and the node runs
   neural Silero VAD, which is entitled to doubt a synthetic caller. Use --wav
   with real speech before believing anything this says about STT.

It IS trustworthy for what it was built for: whether DTMF arrives, whether menus
route, whether the greeting is the right business, and whether a call reaches
the state it should. Those are the defects it found, and they are real.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phone_loopback_test import (  # noqa: E402
    FRAME_SAMPLES, WS_BASE, _encode, env_codec, env_token, synthesize_caller_audio,
)

# 20 ms per frame. ~400 ms of room tone before each utterance so VAD has
# something to open on before the first syllable arrives.
_LEAD_IN_FRAMES = 35
STT_URL = os.environ.get("STT_SERVICE_URL_HOST", "http://127.0.0.1:8200")
HTTP_BASE = WS_BASE.replace("wss://", "https://").replace("ws://", "http://")

# The parity script: an identified caller reporting a new issue, answering the
# numbered gates by keypad, through to the pre-file readback. Modelled on the
# last known-good Gemini call (session v3:12f7_afn..., 2026-07-22).
DEFAULT_SCRIPT = [
    {"press": "1"},                       # yes, that unit is right
    {"press": "0"},                       # a new ticket, not the open one
    {"say": "the garage door spring is broken and it opens very slowly"},
    {"say": "the weights seem off and it is the garage door"},
    {"press": "2"},                       # urgency: today
    {"press": "1"},                       # permission to enter: yes
    {"say": "you can come in when I am not home"},
]


def transcribe(pcm16: bytes, rate: int) -> str:
    """What the caller would have heard, per Whisper."""
    if len(pcm16) < rate:  # under ~0.5s of audio is not worth a model pass
        return ""
    req = urllib.request.Request(
        f"{STT_URL}/transcribe", data=pcm16, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Sample-Rate": str(rate), "X-Model-Size": "base"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return (json.loads(resp.read().decode()).get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001 — a transcript is diagnostics
        return f"(transcription failed: {type(exc).__name__})"


def mint_route(did: str, caller: str, call_id: str) -> None:
    """Post the call.initiated webhook so a delegated line has a route.

    The Telnyx `answer` this triggers will fail for a synthetic call id, which is
    fine and is exactly why the route is now staked BEFORE that round trip.
    """
    body = json.dumps({"data": {
        "event_type": "call.initiated",
        "payload": {"call_control_id": call_id, "from": caller, "to": did},
    }}).encode()
    req = urllib.request.Request(
        f"{HTTP_BASE}/api/phone/incoming?token={env_token()}", data=body,
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[harness] minted route for {did} -> HTTP {resp.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"[harness] route mint returned {type(exc).__name__} "
              f"(expected: synthetic call ids cannot be answered at Telnyx)")


async def run(script: list[dict], did: str | None, caller: str) -> int:
    token, codec = env_token(), env_codec()
    rate = 8000 if codec == "pcmu" else 16000
    frame_bytes = FRAME_SAMPLES if codec == "pcmu" else FRAME_SAMPLES * 2 * 2
    silence_samples = FRAME_SAMPLES if codec == "pcmu" else FRAME_SAMPLES * 2
    silence = _encode(np.zeros(silence_samples, dtype=np.int16), codec)
    # A real phone line is never digitally silent, and energy VAD tuned for a
    # line (NANO_CLAW_PHONE_RMS_MIN=70) needs something to sit above. Feeding
    # exact zeros and then a full-amplitude first syllable is a step function
    # no caller produces, and the onset was being clipped: "the smoke
    # detector in the hallway" arrived as "With a snooker catcher". Room tone
    # is what the carrier would have sent.
    _rng = np.random.default_rng(1729)
    room_tone = _encode(
        (_rng.normal(0, 40, silence_samples)).astype(np.int16), codec)
    call_id = os.environ.get("LOOPBACK_CALL_ID", f"harness-{int(time.time())}")

    if did:
        mint_route(did, caller, call_id)

    heard: list[bytes] = []          # agent audio for the CURRENT segment
    last_audio_at = [0.0]
    apologies = 0

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{WS_BASE}/ws/phone-media?token={token}") as ws:
            await ws.send_json({"event": "start", "stream_id": "harness",
                                "start": {"call_control_id": call_id}})

            async def pump():
                """Collect agent audio until the socket closes."""
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("event") == "media":
                        heard.append(base64.b64decode(
                            (data.get("media") or {}).get("payload", "")))
                        last_audio_at[0] = time.monotonic()

            async def quiet_for(seconds: float, timeout: float = 45.0,
                                since: float = 0.0) -> None:
                """Wait until the agent has been silent for `seconds`.

                `since` is when the caller finished, so a reply that BEGINS
                before we start waiting still counts. The earlier version reset
                last_audio_at to 0 after sending and then required fresh audio,
                so a fast reply was invisible and the harness sat until the
                node's 30s idle prompt — reporting empty replies and 34s turns
                for a call the gateway logged as ok=True in 6s. The instrument
                was the defect, twice over.
                """
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    heard_at = last_audio_at[0]
                    if heard_at > since and time.monotonic() - heard_at >= seconds:
                        return
                    await ws.send_json({"event": "media", "media": {
                        "payload": base64.b64encode(silence).decode()}})
                    await asyncio.sleep(0.02)

            def take() -> bytes:
                pcm, heard[:] = b"".join(heard), []
                return pcm

            pump_task = asyncio.create_task(pump())
            try:
                await quiet_for(1.2)
                print(f"AGENT (opening): {transcribe(take(), rate)!r}\n")

                for step in script:
                    t0 = time.monotonic()
                    if "press" in step:
                        print(f"CALLER presses {step['press']}")
                        await ws.send_json({"event": "dtmf",
                                            "dtmf": {"digit": step["press"]}})
                    else:
                        print(f"CALLER says {step['say']!r}")
                        # LEAD-IN SILENCE, or the harness lies to you. Sending
                        # speech on the first frame after the agent stops gives
                        # VAD nothing to latch onto, and the opening words are
                        # swallowed: "the smoke detector in the hallway keeps
                        # chirping at three in the morning" reached STT as "Keep
                        # searching at 3-in-1", and the agent was then blamed for
                        # not understanding a sentence it never received. A real
                        # caller is always preceded by line noise; this is the
                        # cheapest way to stop measuring an artefact of the test.
                        for _ in range(_LEAD_IN_FRAMES):
                            await ws.send_json({"event": "media", "media": {
                                "payload": base64.b64encode(room_tone).decode()}})
                            await asyncio.sleep(0.02)
                        audio = synthesize_caller_audio(step["say"], codec)
                        for i in range(0, len(audio), frame_bytes):
                            await ws.send_json({"event": "media", "media": {
                                "payload": base64.b64encode(
                                    audio[i:i + frame_bytes]).decode()}})
                            await asyncio.sleep(0.02)
                    spoke_at = time.monotonic()
                    await quiet_for(1.5, since=spoke_at)
                    text = transcribe(take(), rate)
                    if "say that again" in text.lower() or "didn't catch" in text.lower():
                        apologies += 1
                    print(f"AGENT [{time.monotonic()-t0:5.1f}s]: {text!r}\n")

                await ws.send_json({"event": "stop"})
            finally:
                pump_task.cancel()

    print(f"--- apologies={apologies} over {len(script)} caller turns")
    return apologies


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--did", default=None, help="mint a delegate route for this DID")
    ap.add_argument("--caller", default="+15124317196")
    ap.add_argument("--script", default=None, help="JSON file of steps")
    args = ap.parse_args()
    steps = (json.loads(Path(args.script).read_text())
             if args.script else DEFAULT_SCRIPT)
    sys.exit(0 if asyncio.run(run(steps, args.did, args.caller)) == 0 else 1)
