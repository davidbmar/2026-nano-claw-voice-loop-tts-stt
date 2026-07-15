"""Sync HTTP client to the native Kokoro TTS service.

Called from within voice/tts.py's synthesize(), which itself runs inside a
thread-pool executor (see webrtc.speak_text), so a synchronous httpx client is
correct here.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("kokoro-client")

TTS_SERVICE_URL = os.environ.get("TTS_SERVICE_URL", "http://host.docker.internal:8300")
KOKORO_RATE = 24000


class KokoroUnavailable(Exception):
    """The native TTS service could not be reached or returned an error."""


def synthesize(text: str, voice: str, speed: float) -> tuple[bytes, int]:
    """POST text to the TTS service; return (int16_pcm, sample_rate)."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{TTS_SERVICE_URL}/synthesize",
                json={"text": text, "voice": voice, "speed": speed},
            )
            resp.raise_for_status()
            rate = int(resp.headers.get("X-Sample-Rate", KOKORO_RATE))
            return resp.content, rate
    except Exception as exc:  # transport error, timeout, non-2xx
        log.warning("Kokoro TTS service unavailable: %s", exc)
        raise KokoroUnavailable(str(exc)) from exc


def is_healthy() -> bool:
    """Cheap readiness probe used when the user selects a Kokoro voice."""
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{TTS_SERVICE_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False
