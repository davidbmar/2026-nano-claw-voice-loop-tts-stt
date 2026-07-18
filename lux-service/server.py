"""Standalone LuxTTS service — 48kHz voice-cloning TTS on Mac (MPS→CPU fallback).

Accepts JSON {text, voice, speed}, returns raw int16 PCM at 48kHz. Called from
the Docker container via HTTP. Mirrors tts-service/server.py (FastAPI + uvicorn,
lazy model load). Voices are reference wav files in voices/; each is encoded
once (Whisper transcription + feature extraction) and cached.

Supply-chain pins (see .gstack/security-reports/2026-07-17-luxtts-supply-chain.json):
- LuxTTS repo cloned at a pinned commit by setup.sh
- model weights pinned to HF_REVISION and pickle-scanned by setup.sh before
  this server will load them (.verified marker gate)
"""

import logging
import os
import sys
import time
from pathlib import Path

# Enable CPU fallback for any MPS-unsupported op BEFORE torch is imported.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

# No network at runtime: everything (model snapshot, whisper transcriber) is
# prefetched at pinned revisions by setup.sh. A cache miss should fail loudly
# ("run setup.sh"), never silently fetch latest. Export HF_HUB_OFFLINE=0 to
# override for debugging.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("lux-service")

app = FastAPI()

LUX_RATE = 48000  # LuxTTS vocoder output rate

SERVICE_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVICE_DIR / "LuxTTS"          # pinned clone (setup.sh)
VOICES_DIR = SERVICE_DIR / "voices"        # reference wav per voice id
VERIFIED_MARKER = SERVICE_DIR / ".verified"

# Pinned HF snapshot (audited 2026-07-17). load_models_cpu() in upstream
# ignores model_path and re-downloads unpinned, so we always resolve the
# snapshot ourselves and use the GPU-path loader (works on cpu/mps too).
HF_REPO = "YatharthS/LuxTTS"
HF_REVISION = "527f245a276a0eb42ea103a7a512bcfd771eb9b6"

# Make the pinned clone importable even without `pip install ./LuxTTS`.
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

_models = None      # (model, feature_extractor, vocos, tokenizer, transcriber)
_prompts: dict = {}  # voice_id -> encoded prompt tuple
_device: str | None = None


def _pick_device() -> str:
    global _device
    if _device is not None:
        return _device
    import torch

    if os.environ.get("LUX_DEVICE"):
        _device = os.environ["LUX_DEVICE"]
    elif torch.backends.mps.is_available():
        _device = "mps"
    else:
        _device = "cpu"
    log.info("LuxTTS device: %s", _device)
    return _device


def _snapshot_path() -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(HF_REPO, revision=HF_REVISION)


def _get_models():
    global _models
    if _models is not None:
        return _models

    if not VERIFIED_MARKER.exists():
        raise RuntimeError(
            "lux-service/.verified missing — run lux-service/setup.sh first "
            "(it pickle-scans the pinned model weights before first load)"
        )

    from zipvoice.modeling_utils import load_models_gpu

    device = _pick_device()
    model_path = _snapshot_path()
    log.info("Loading LuxTTS models (device=%s, snapshot=%s) ...", device, model_path)
    start = time.time()
    model, feature_extractor, vocos, tokenizer, transcriber = load_models_gpu(
        model_path=model_path, device=device
    )
    # Match the upstream LuxTTS wrapper's vocoder settings (luxvoice.py).
    vocos.freq_range = 12000
    vocos.return_48k = True
    _models = (model, feature_extractor, vocos, tokenizer, transcriber)
    log.info("LuxTTS ready in %.1fs", time.time() - start)
    return _models


def _list_voices() -> list[dict]:
    voices = []
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        vid = wav.stem
        voices.append({
            "id": vid,
            "name": vid.removeprefix("lux_").replace("_", " ").title(),
            "group": "LuxTTS — cloned 48k",
        })
    return voices


def _get_prompt(voice_id: str):
    """Encode a reference wav once (Whisper + fbank) and cache the result."""
    if voice_id in _prompts:
        return _prompts[voice_id]

    wav_path = VOICES_DIR / f"{voice_id}.wav"
    if not wav_path.exists():
        raise KeyError(voice_id)

    from zipvoice.modeling_utils import process_audio

    model, feature_extractor, vocos, tokenizer, transcriber = _get_models()
    log.info("Encoding reference prompt for %s ...", voice_id)
    prompt = process_audio(
        str(wav_path), transcriber, tokenizer, feature_extractor,
        _pick_device(), target_rms=0.01, duration=5,
    )
    _prompts[voice_id] = prompt
    return prompt


def _prewarm() -> None:
    """Load the model and encode every reference prompt in the background so
    the first synthesis of any voice doesn't pay model-load + Whisper latency
    (the phone pipeline's worst dead-air class). Failures are non-fatal —
    synthesis falls back to lazy loading."""
    try:
        start = time.time()
        for v in _list_voices():
            _get_prompt(v["id"])
        log.info("Prewarmed %d voices in %.1fs", len(_list_voices()), time.time() - start)
    except Exception as exc:
        log.warning("Prewarm failed (lazy load still available): %s", exc)


@app.on_event("startup")
async def _startup():
    if os.environ.get("LUX_PREWARM", "1") != "0":
        import threading

        threading.Thread(target=_prewarm, daemon=True, name="lux-prewarm").start()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/voices")
async def list_voices():
    return {"voices": _list_voices()}


def _synthesize_pcm(text: str, voice_id: str, speed: float) -> bytes:
    """Blocking LuxTTS synthesis; runs on a threadpool worker so /health and
    concurrent requests aren't blocked by the GPU/CPU-bound generate call."""
    from zipvoice.modeling_utils import generate

    prompt_tokens, prompt_features_lens, prompt_features, prompt_rms = _get_prompt(voice_id)
    model, feature_extractor, vocos, tokenizer, transcriber = _get_models()

    try:
        wav = generate(
            prompt_tokens, prompt_features_lens, prompt_features, prompt_rms,
            text, model, vocos, tokenizer,
            num_step=4, guidance_scale=3.0, speed=speed, t_shift=0.5, target_rms=0.01,
        )
    except RuntimeError as exc:
        # Borderline duration prediction on tiny chunks can yield fewer frames
        # than the vocoder's conv kernel ("padded input size (N) < kernel
        # size"). Lower speed stretches the predicted duration; retry once.
        if "Kernel size" not in str(exc) and "padded input" not in str(exc):
            raise
        log.warning("Short-chunk frame underrun for %r; retrying at half speed", text[:40])
        wav = generate(
            prompt_tokens, prompt_features_lens, prompt_features, prompt_rms,
            text, model, vocos, tokenizer,
            num_step=4, guidance_scale=3.0, speed=speed * 0.5, t_shift=0.5, target_rms=0.01,
        )
    audio = wav.cpu().numpy().squeeze().astype(np.float32)
    return np.clip(audio * 32767.0, -32768, 32767).astype(np.int16).tobytes()


@app.post("/synthesize")
async def synthesize(request: Request):
    """Synthesize text → raw int16 PCM (48kHz). Body: {text, voice, speed}."""
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    voice = payload.get("voice") or "lux_heart"
    speed = float(payload.get("speed") or 1.0)

    if not text:
        return Response(content=b"", headers={"X-Sample-Rate": str(LUX_RATE)})

    if not (VOICES_DIR / f"{voice}.wav").exists():
        return Response(status_code=400, content=b"unsupported voice")

    start = time.time()
    try:
        pcm = await run_in_threadpool(_synthesize_pcm, text, voice, speed)
    except RuntimeError as exc:
        log.error("Synthesis unavailable: %s", exc)
        return Response(status_code=503, content=str(exc).encode())

    if not pcm:
        return Response(content=b"", headers={"X-Sample-Rate": str(LUX_RATE)})

    log.info("Synth voice=%s %d chars -> %.2fs in %.2fs",
             voice, len(text), len(pcm) / (LUX_RATE * 2), time.time() - start)
    return Response(content=pcm, headers={"X-Sample-Rate": str(LUX_RATE)})


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-14s %(levelname)-5s %(message)s",
    )
    port = int(os.environ.get("LUX_PORT", "8301"))
    log.info("Starting LuxTTS service on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
