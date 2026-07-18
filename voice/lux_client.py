"""Sync HTTP client to the native LuxTTS voice-cloning service.

Mirrors kokoro_client.py: called from voice/tts.py's synthesize(), which runs
inside a thread-pool executor (see webrtc.speak_text), so a synchronous httpx
client is correct here.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("lux-client")

LUX_SERVICE_URL = os.environ.get("LUX_SERVICE_URL", "http://host.docker.internal:8301")
LUX_RATE = 48000


class LuxUnavailable(Exception):
    """The native LuxTTS service could not be reached or returned an error."""


def synthesize(text: str, voice: str, speed: float) -> tuple[bytes, int]:
    """POST text to the LuxTTS service; return (int16_pcm, sample_rate)."""
    try:
        # First synth after start encodes the reference prompt (Whisper pass),
        # so the timeout is a bit above kokoro_client's 30s.
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{LUX_SERVICE_URL}/synthesize",
                json={"text": text, "voice": voice, "speed": speed},
            )
            resp.raise_for_status()
            rate = int(resp.headers.get("X-Sample-Rate", LUX_RATE))
            return resp.content, rate
    except Exception as exc:  # transport error, timeout, non-2xx
        log.warning("LuxTTS service unavailable: %s", exc)
        raise LuxUnavailable(str(exc)) from exc


def is_healthy() -> bool:
    """Cheap readiness probe used when the user selects a LuxTTS voice."""
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{LUX_SERVICE_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False
