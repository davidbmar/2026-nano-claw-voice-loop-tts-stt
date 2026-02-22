"""Standalone STT service — runs natively on Mac for Metal GPU acceleration.

Accepts raw PCM int16 audio bytes, transcribes with faster-whisper, returns text.
Designed to be called from the Docker container via HTTP.
"""

import logging
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, Response
from scipy.signal import resample

log = logging.getLogger("stt-service")

app = FastAPI()

MODEL_SIZE = "base"  # ~75MB, good accuracy for short utterances
WHISPER_RATE = 16000  # faster-whisper expects 16kHz input

# Lazy-loaded Whisper model
_model = None


def _get_model():
    """Load the faster-whisper model on first use (auto-downloads ~75MB)."""
    global _model
    if _model is not None:
        return _model

    from faster_whisper import WhisperModel

    log.info("Loading faster-whisper model: %s (first run downloads ~75MB)...", MODEL_SIZE)
    _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    log.info("Whisper model loaded: %s", MODEL_SIZE)
    return _model


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe(request: Request):
    """Transcribe raw PCM int16 audio bytes to text.

    Headers:
        X-Sample-Rate: sample rate of the audio (default 48000)

    Body:
        Raw PCM int16 mono audio bytes (application/octet-stream)

    Returns:
        JSON: { "text": "...", "duration_s": 4.56 }
    """
    audio_bytes = await request.body()
    sample_rate = int(request.headers.get("X-Sample-Rate", "48000"))

    if not audio_bytes:
        return {"text": "", "duration_s": 0.0}

    start = time.time()
    model = _get_model()

    # Convert int16 PCM to float32 normalized [-1.0, 1.0]
    samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    duration_s = len(samples) / sample_rate

    log.info("Received %.2fs audio (%d bytes, %dHz)", duration_s, len(audio_bytes), sample_rate)

    # Resample to 16kHz if needed
    if sample_rate != WHISPER_RATE:
        num_output = int(len(samples) * WHISPER_RATE / sample_rate)
        samples = resample(samples, num_output).astype(np.float32)

    segments, _info = model.transcribe(samples, beam_size=5, language="en")

    text = " ".join(seg.text.strip() for seg in segments).strip()
    elapsed = time.time() - start

    log.info("Transcribed in %.2fs → %r", elapsed, text[:100])

    return {"text": text, "duration_s": round(duration_s, 2)}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-14s %(levelname)-5s %(message)s",
    )
    log.info("Starting STT service on port 8200")
    uvicorn.run(app, host="0.0.0.0", port=8200)
