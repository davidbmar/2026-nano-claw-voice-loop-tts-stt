"""Piper TTS wrapper — text to 48kHz PCM with resampling."""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

import numpy as np
from scipy.signal import resample

log = logging.getLogger("tts")

TARGET_RATE = 48000  # WebRTC Opus expects 48kHz

MODEL_DIR = Path(__file__).resolve().parent / "models"

# Voice catalog — each entry maps to a HuggingFace Piper voice model
VOICE_CATALOG = [
    {"id": "en_US-lessac-medium", "name": "Lessac (US)", "lang": "en", "locale": "en_US", "voice_name": "lessac", "quality": "medium"},
    {"id": "en_US-hfc_female-medium", "name": "HFC Female (US)", "lang": "en", "locale": "en_US", "voice_name": "hfc_female", "quality": "medium"},
    {"id": "en_US-hfc_male-medium", "name": "HFC Male (US)", "lang": "en", "locale": "en_US", "voice_name": "hfc_male", "quality": "medium"},
]

DEFAULT_VOICE = "en_US-lessac-medium"
_CATALOG_BY_ID = {v["id"]: v for v in VOICE_CATALOG}
_voice_cache: dict = {}


def _model_url(voice_id: str) -> tuple[str, str]:
    """Build HuggingFace download URLs for a voice's .onnx and .onnx.json."""
    entry = _CATALOG_BY_ID[voice_id]
    base = (
        f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
        f"{entry['lang']}/{entry['locale']}/{entry['voice_name']}/{entry['quality']}/{voice_id}"
    )
    return f"{base}.onnx", f"{base}.onnx.json"


def _download_model(voice_id: str) -> Path:
    """Download the Piper ONNX model + config if not already on disk."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    onnx_path = MODEL_DIR / f"{voice_id}.onnx"
    config_path = MODEL_DIR / f"{voice_id}.onnx.json"

    onnx_url, config_url = _model_url(voice_id)

    if not onnx_path.exists():
        log.info("Downloading voice model: %s ...", voice_id)
        urllib.request.urlretrieve(onnx_url, onnx_path)
        log.info("Model downloaded: %s", onnx_path)

    if not config_path.exists():
        log.info("Downloading voice config: %s ...", voice_id)
        urllib.request.urlretrieve(config_url, config_path)
        log.info("Config downloaded: %s", config_path)

    return onnx_path


def _get_voice(voice_id: str = ""):
    """Load a Piper voice model, using the cache for repeated calls."""
    voice_id = voice_id or DEFAULT_VOICE
    if voice_id in _voice_cache:
        return _voice_cache[voice_id]

    if voice_id not in _CATALOG_BY_ID:
        log.warning("Unknown voice %r, falling back to default", voice_id)
        voice_id = DEFAULT_VOICE

    from piper import PiperVoice

    model_path = _download_model(voice_id)
    log.info("Loading Piper TTS voice: %s", model_path)
    voice = PiperVoice.load(str(model_path))
    log.info("Piper voice loaded: %s (native rate: %d Hz)", voice_id, voice.config.sample_rate)
    _voice_cache[voice_id] = voice
    return voice


def _resample_to_48k(pcm: bytes, native_rate: int) -> bytes:
    """Resample int16 PCM from native_rate to 48kHz (WebRTC Opus)."""
    if not pcm:
        return b""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if native_rate == TARGET_RATE:
        return samples.astype(np.int16).tobytes()
    num_output_samples = int(len(samples) * TARGET_RATE / native_rate)
    resampled = resample(samples, num_output_samples)
    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
    return resampled.tobytes()


def _synthesize_piper(text: str, voice_id: str) -> bytes:
    """Piper path: text → 48kHz int16 PCM (the original fast engine)."""
    voice = _get_voice(voice_id)
    native_rate = voice.config.sample_rate
    raw_parts = [chunk.audio_int16_bytes for chunk in voice.synthesize(text)]
    if not raw_parts:
        log.warning("Piper produced no audio for: %r", text[:50])
        return b""
    return _resample_to_48k(b"".join(raw_parts), native_rate)


def _synthesize_kokoro(text: str, voice_id: str, speed: float) -> bytes:
    """Kokoro path: fetch from the native service, resample to 48kHz.

    Falls back to the Piper default voice if the service is unavailable so the
    voice loop is never silent (degraded-mode convention).
    """
    from voice import kokoro_client

    try:
        pcm, rate = kokoro_client.synthesize(text, voice_id, speed)
        return _resample_to_48k(pcm, rate)
    except kokoro_client.KokoroUnavailable:
        log.warning("Kokoro unavailable for %r; falling back to Piper %s",
                    voice_id, DEFAULT_VOICE)
        return _synthesize_piper(text, DEFAULT_VOICE)


def synthesize(text: str, voice_id: str = "", speed: float = 1.0) -> bytes:
    """Route to the right engine and return 48kHz mono int16 PCM.

    - Kokoro voices → native TTS service (uses `speed`).
    - Piper voices  → local Piper (ignores `speed`; it is the fast option).
    - Unknown id    → Piper default.
    """
    from voice import voice_catalog

    entry = voice_catalog.lookup(voice_id) if voice_id else None
    if entry and entry["engine"] == "kokoro":
        return _synthesize_kokoro(text, voice_id, speed)

    piper_id = voice_id if (entry and entry["engine"] == "piper") else DEFAULT_VOICE
    return _synthesize_piper(text, piper_id)
