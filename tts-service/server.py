"""Standalone TTS service — runs Kokoro-82M natively on Mac (MPS→CPU fallback).

Accepts JSON {text, voice, speed}, returns raw int16 PCM at 24kHz. Called from
the Docker container via HTTP. Mirrors stt-service/server.py (FastAPI + uvicorn,
lazy model load).
"""

import logging
import os
import time

# Enable CPU fallback for any MPS-unsupported op BEFORE torch is imported.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, Response

import voices as voice_catalog

log = logging.getLogger("tts-service")

app = FastAPI()

KOKORO_RATE = 24000  # Kokoro native sample rate

# One KPipeline per lang_code, loaded lazily and cached.
_pipelines: dict = {}
_device: str | None = None


def _pick_device() -> str:
    global _device
    if _device is not None:
        return _device
    import torch

    if os.environ.get("TTS_DEVICE"):
        _device = os.environ["TTS_DEVICE"]
    elif torch.backends.mps.is_available():
        _device = "mps"
    else:
        _device = "cpu"
    log.info("Kokoro device: %s", _device)
    return _device


def _setup_espeak() -> None:
    """Point phonemizer at a bundled espeak-ng lib (needed for Spanish, OOV EN)."""
    try:
        import espeakng_loader
        from phonemizer.backend.espeak.wrapper import EspeakWrapper

        EspeakWrapper.set_library(espeakng_loader.get_library_path())
        log.info("espeak-ng library configured via espeakng-loader")
    except Exception:
        log.warning("espeakng-loader setup skipped; relying on system espeak-ng")


def _get_pipeline(lang_code: str):
    if lang_code in _pipelines:
        return _pipelines[lang_code]

    _setup_espeak()
    from kokoro import KPipeline

    log.info("Loading Kokoro KPipeline (lang=%s) ...", lang_code)
    pipeline = KPipeline(lang_code=lang_code, device=_pick_device())
    _pipelines[lang_code] = pipeline
    log.info("Kokoro pipeline ready (lang=%s)", lang_code)
    return pipeline


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/voices")
async def list_voices():
    return {"voices": voice_catalog.KOKORO_VOICES}


@app.post("/synthesize")
async def synthesize(request: Request):
    """Synthesize text → raw int16 PCM (24kHz). Body: {text, voice, speed}."""
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    voice = payload.get("voice") or "af_heart"
    speed = float(payload.get("speed") or 1.0)

    if not text:
        return Response(content=b"", headers={"X-Sample-Rate": str(KOKORO_RATE)})

    try:
        lang_code = voice_catalog.lang_code_for(voice)
    except KeyError:
        return Response(status_code=400, content=b"unsupported voice")

    start = time.time()
    pipeline = _get_pipeline(lang_code)

    chunks = []
    for _gs, _ps, audio in pipeline(text, voice=voice, speed=speed):
        arr = np.asarray(audio, dtype=np.float32)  # torch tensor / ndarray
        chunks.append(arr)

    if not chunks:
        return Response(content=b"", headers={"X-Sample-Rate": str(KOKORO_RATE)})

    audio = np.concatenate(chunks)
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16).tobytes()

    log.info("Synth voice=%s %d chars -> %.2fs in %.2fs",
             voice, len(text), len(pcm) / (KOKORO_RATE * 2), time.time() - start)
    return Response(content=pcm, headers={"X-Sample-Rate": str(KOKORO_RATE)})


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-14s %(levelname)-5s %(message)s",
    )
    port = int(os.environ.get("TTS_PORT", "8300"))
    log.info("Starting TTS service on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
