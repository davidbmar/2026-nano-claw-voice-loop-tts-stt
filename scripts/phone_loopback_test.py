#!/usr/bin/env python3
"""Loopback test for the phone gateway — a fake Telnyx caller, no PSTN needed.

Connects to /ws/phone-media exactly like Telnyx would, "speaks" a question
(synthesized by the local TTS service, resampled to μ-law 8k), goes silent,
and measures what a real caller would feel:

    t_endpoint   end of caller audio → (silence window closes)
    t_first_ms   end of caller audio → FIRST agent audio frame back
    t_total_ms   end of caller audio → last agent audio frame

Run against a live node (default localhost:9090; token read from .env):
    .venv-test/bin/python scripts/phone_loopback_test.py "What is the next rocket launch?"
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice.phone_audio import FRAME_SAMPLES, resample_48k_to_8k, ulaw_encode  # noqa: E402

TTS_URL = "http://localhost:8300/synthesize"
WS_BASE = "ws://localhost:9090"


def env_token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("NANO_CLAW_PHONE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("NANO_CLAW_PHONE_TOKEN not found in .env")


def synthesize_caller_audio(text: str) -> bytes:
    """Question audio as μ-law 8k — the local TTS service plays the caller."""
    body = json.dumps({"text": text, "voice": "af_heart", "speed": 1.0}).encode()
    req = urllib.request.Request(TTS_URL, data=body, headers={"Content-Type": "application/json"})
    pcm48 = urllib.request.urlopen(req, timeout=60).read()
    return ulaw_encode(resample_48k_to_8k(np.frombuffer(pcm48, dtype=np.int16)))


async def run(question: str) -> None:
    token = env_token()
    caller_ulaw = synthesize_caller_audio(question)
    frames = [caller_ulaw[i : i + FRAME_SAMPLES] for i in range(0, len(caller_ulaw), FRAME_SAMPLES)]
    silence = ulaw_encode(np.zeros(FRAME_SAMPLES, dtype=np.int16))

    first_reply_at: float | None = None
    last_reply_at: float | None = None
    reply_frames = 0
    speech_end_at: float | None = None

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{WS_BASE}/ws/phone-media?token={token}") as ws:
            await ws.send_json({
                "event": "start",
                "stream_id": "loopback",
                "start": {"call_control_id": f"loopback-{int(time.time())}"},
            })

            async def sender():
                nonlocal speech_end_at
                # Let the greeting finish first so half/duplex gating doesn't
                # swallow the question: wait for first greeting audio, then
                # for a 1.5s gap in agent audio.
                while first_reply_at is None:
                    await asyncio.sleep(0.1)
                while time.monotonic() - (last_reply_at or 0) < 1.5:
                    await asyncio.sleep(0.1)
                print(f"greeting done ({reply_frames} frames) — asking: {question!r}")
                for f in frames:
                    await ws.send_json({"event": "media",
                                        "media": {"payload": base64.b64encode(f).decode()}})
                    await asyncio.sleep(0.02)
                speech_end_at = time.monotonic()
                # ~12s of silence → endpointer fires, turn runs, reply streams
                for _ in range(600):
                    await ws.send_json({"event": "media",
                                        "media": {"payload": base64.b64encode(silence).decode()}})
                    await asyncio.sleep(0.02)
                await ws.send_json({"event": "stop"})

            send_task = asyncio.create_task(sender())
            answer_first: float | None = None
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("event") == "media":
                        now = time.monotonic()
                        reply_frames += 1
                        if first_reply_at is None:
                            first_reply_at = now
                        last_reply_at = now
                        if speech_end_at and answer_first is None and now > speech_end_at:
                            answer_first = now
                            print(f"FIRST ANSWER AUDIO: {(now - speech_end_at)*1000:.0f} ms after caller stopped")
                    if send_task.done() and last_reply_at and time.monotonic() - last_reply_at > 3:
                        break
            finally:
                send_task.cancel()

            if speech_end_at and answer_first:
                print(f"answer audio finished: {(last_reply_at - speech_end_at):.1f}s after caller stopped"
                      f" ({reply_frames} total frames incl. greeting)")
            else:
                print("NO ANSWER AUDIO RECEIVED — check container logs")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "What is the next rocket launch?"))
