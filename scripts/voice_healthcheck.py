#!/usr/bin/env python3
"""Synthetic end-to-end health check for the browser voice path.

Drives the real /ws WebSocket exactly as a browser would: streams a short
spoken phrase as PCM16/16k frames, then asserts the full round-trip —
  1. WS connects and selects WS-audio
  2. mic -> STT produces a non-empty transcript
  3. the agent replies AND
  4. agent audio frames come back at the expected 48 kHz (1920-byte frames)

Prints one JSON verdict line and exits 0 (healthy) or non-zero (which stage
failed, so the watchdog can remediate the right component):
  exit 2 = WS/link, 3 = STT, 4 = agent/LLM, 5 = agent-audio, 6 = format.

Usage: voice_healthcheck.py [wss://host/ws] [origin]
The speech WAV is generated once with macOS `say` if absent.
"""
from __future__ import annotations
import asyncio, json, os, subprocess, sys, wave

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:9090/ws"
ORIGIN = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:9090"
WAV = "/tmp/voice_healthcheck_speech.wav"
PHRASE = "This is an automated health check. Please confirm you can hear me."
MIC_FRAME = 640            # 320 samples * 2 = 20ms @ 16kHz
AGENT_FRAME_BYTES = 1920   # 960 samples * 2 = 20ms @ 48kHz (the 052 contract)


def ensure_speech() -> bytes:
    if not os.path.exists(WAV):
        subprocess.run(
            ["say", "-o", WAV, "--data-format=LEI16@16000", PHRASE],
            check=True, capture_output=True,
        )
    w = wave.open(WAV)
    pcm = w.readframes(w.getnframes())
    if len(pcm) % MIC_FRAME:
        pcm += b"\x00" * (MIC_FRAME - len(pcm) % MIC_FRAME)
    return pcm


def verdict(stage: str, ok: bool, code: int, **extra) -> "tuple[str,int]":
    print(json.dumps({"health": "ok" if ok else "fail", "stage": stage, **extra}))
    return (stage, 0 if ok else code)


async def run() -> int:
    import aiohttp
    pcm = ensure_speech()
    session = aiohttp.ClientSession()
    try:
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(URL, headers={"Origin": ORIGIN}), timeout=15)
        except Exception as exc:
            return verdict("ws_connect", False, 2, error=str(exc)[:120])[1]

        async def wait(pred, to):
            try:
                async with asyncio.timeout(to):
                    while True:
                        m = await ws.receive()
                        if m.type == aiohttp.WSMsgType.TEXT:
                            if pred("text", json.loads(m.data)): return ("text", json.loads(m.data))
                        elif m.type == aiohttp.WSMsgType.BINARY:
                            if pred("bin", m.data): return ("bin", m.data)
                        elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            return None
            except TimeoutError:
                return None

        await ws.send_json({"type": "hello"})
        ack = await wait(lambda t, d: t == "text" and d.get("type") == "hello_ack", 15)
        if not ack or not ack[1].get("wsAudio"):
            return verdict("ws_audio_select", False, 2)[1]
        fmt = ack[1]["wsAudioFormat"]

        await ws.send_json({"type": "mic_audio_start", **fmt["mic"]})
        if not await wait(lambda t, d: t == "text" and d.get("type") == "mic_audio_ready", 10):
            return verdict("mic_ready", False, 2)[1]

        await ws.send_json({"type": "mic_start"})
        for i in range(0, len(pcm), MIC_FRAME):
            await ws.send_bytes(pcm[i:i + MIC_FRAME])
        await ws.send_json({"type": "mic_stop"})

        tr = await wait(lambda t, d: t == "text" and d.get("type") == "transcription" and d.get("text"), 40)
        if not tr:
            return verdict("stt", False, 3)[1]  # no transcript -> STT down

        # agent reply + audio frames
        agent_bytes = agent_frames = 0
        got_reply = False
        try:
            async with asyncio.timeout(45):
                while True:
                    m = await ws.receive()
                    if m.type == aiohttp.WSMsgType.BINARY:
                        agent_bytes += len(m.data); agent_frames += 1
                    elif m.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(m.data)
                        if d.get("type") in ("agent_reply", "agent_reply_delta", "agent_reply_done"):
                            got_reply = True
                        if d.get("type") == "agent_audio_end":
                            break
                    elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                        break
        except TimeoutError:
            pass

        if agent_frames == 0:
            return verdict("agent_audio", False, 5, transcript=tr[1]["text"][:60], got_reply=got_reply)[1]
        # confirm the 48kHz contract (a frame that isn't 1920 bytes => wrong rate/regression)
        avg = agent_bytes // agent_frames if agent_frames else 0
        if avg != AGENT_FRAME_BYTES:
            return verdict("agent_audio_format", False, 6, avg_frame=avg, expected=AGENT_FRAME_BYTES)[1]

        return verdict("full_roundtrip", True, 0,
                       transcript=tr[1]["text"][:60], agent_frames=agent_frames,
                       agent_bytes=agent_bytes)[1]
    finally:
        try: await ws.close()
        except Exception: pass
        await session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
