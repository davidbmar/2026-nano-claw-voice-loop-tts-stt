# Kokoro-82M TTS with voice selection — Design

**Date:** 2026-07-14
**Status:** Approved (design), pending implementation plan

## Problem

The voice loop currently speaks with **Piper TTS** running inside the Docker
container (CPU-only — Docker Desktop on Mac cannot reach Metal, the same
constraint that pushed STT into a native service). `voice/tts.py` already has a
3-voice `VOICE_CATALOG` and both `synthesize(text, voice_id)` and
`speak_text(text, voice_id="")` thread a `voice_id` through — **but nothing ever
sets it.** The browser has no voice picker and the WebSocket protocol has no
"set voice" message, so the plumbing dead-ends at the browser.

We want to:

1. Add **Kokoro-82M** as a higher-quality TTS engine.
2. Let the user **choose any voice** from a picker (English + Spanish for now).
3. Keep the **original Piper voice as the fast / low-latency option**.

## Scope decisions (agreed)

| Decision | Choice |
| --- | --- |
| Engine strategy | Add Kokoro **alongside** Piper — pick a Kokoro voice for quality OR Piper for speed |
| Where Kokoro runs | **Native Mac service** (like STT), can use Apple MPS; Docker posts text → gets audio |
| Voice scope | **English + Spanish only** (American `a`, British `b`, Spanish `e` — ~31 voices). No other languages for now (Spanish is for testing Spanish speaking). |
| Default voice on load | **Kokoro `af_heart`** (quality) |
| Extras | Speed slider, voice preview button, quality-grade labels — all included |
| Persistence | Selected voice + speed persisted in `localStorage` |

Non-English languages beyond Spanish (Japanese, Mandarin, French, etc.) are
explicitly **out of scope** — they require heavier `misaki` phonemizer backends.
English uses `misaki[en]`; Spanish (`lang_code='e'`) routes through `espeak-ng`,
which the service already installs, so Spanish is nearly free.

## Architecture

```
┌─ Your Mac (native) ─────────────────────────────────────────┐
│  STT service  :8200   faster-whisper (Metal)     ← exists    │
│  TTS service  :8300   Kokoro-82M (PyTorch MPS)   ← NEW        │
│     GET /health   GET /voices   POST /synthesize            │
└──────────────▲──────────────────────────────────────────────┘
               │ HTTP (host.docker.internal:8300)
┌─ Docker container ──────────┴───────────────────────────────┐
│  voice/server.py                                            │
│    GET /api/voices   POST /api/preview   ws:set_voice ← NEW │
│  voice/tts.py  → engine router:                            │
│      piper  → local synth (fast path, unchanged)           │
│      kokoro → POST to :8300 → resample 24k→48k             │
│  voice/webrtc.py → speak_text(text, voice_id, speed)       │
└─────────────────────────────────────────────────────────────┘
```

Kokoro is natively 24 kHz; the existing pipeline already resamples any native
rate up to 48 kHz for WebRTC Opus, so no WebRTC changes are needed.

### Why native (even without a guaranteed MPS win)

Docker on Mac runs a Linux VM, so its "CPU" is virtualized and cache-cold; a
native process gets Apple's Accelerate/vecLib BLAS directly. Even if Kokoro's
MPS path hits an unsupported op and falls back to CPU, native-CPU is far faster
than Docker-CPU. The service sets `PYTORCH_ENABLE_MPS_FALLBACK=1` and exposes a
configurable device (`TTS_DEVICE`, default auto: MPS → CPU) so we can benchmark
honestly.

## Components

### New — `tts-service/` (native Mac, sibling to `stt-service/`)

Same shape as `stt-service/`: `.venv`, `server.py`, `run.sh`, `requirements.txt`.

- Loads Kokoro via the `kokoro` PyTorch package. Device auto-selects MPS with
  CPU fallback. Caches **one `KPipeline` per language code** (`a`, `b`, `e`) —
  the voice prefix selects the pipeline. Downloads the ~310 MB model on first
  run (persisted in the venv/HF cache).
- Endpoints:
  - `GET /health` — readiness (mirrors STT).
  - `GET /voices` — the Kokoro EN+ES catalog with A–F grades.
  - `POST /synthesize {text, voice, speed}` → raw **int16 PCM** body with an
    `X-Sample-Rate: 24000` response header (mirrors STT's raw-PCM convention in
    reverse). Empty body if synthesis produced no audio.
- Built on **FastAPI + uvicorn** (same stack as `stt-service/server.py`):
  lazy model load on first request, `/health` route, `uvicorn.run(..., port=8300)`
  under `if __name__ == "__main__"`.
- `requirements.txt`: `kokoro`, `torch`, `numpy`, `soundfile`, `fastapi`,
  `uvicorn`, `scipy`, and `espeakng-loader` (bundled espeak-ng so no Homebrew
  dependency; covers Spanish).
- `run.sh`: `pip install -r requirements.txt` then `python server.py`
  (mirrors `stt-service/run.sh`).

### New — `voice/kokoro_client.py` (Docker side)

Thin `httpx` client to `:8300`: `synthesize(text, voice, speed) -> (pcm_bytes,
sample_rate)`, plus a health probe. Raises/returns a sentinel on failure so the
router can fall back.

### New — `voice/voice_catalog.py` (Docker side)

Single source of truth for the picker: the Piper voices (from the existing
`VOICE_CATALOG`) plus the Kokoro EN/ES voices, each as
`{id, name, engine, lang, grade}`. Static so the picker renders even while the
service is warming. Provides `lookup(voice_id) -> {engine, lang, ...}`.

### Modified

- **`voice/tts.py` → engine router.** `synthesize(text, voice_id, speed)`
  resolves the voice's engine via `voice_catalog`:
  - `piper` → existing local Piper synth (the fast path, unchanged).
  - `kokoro` → `kokoro_client.synthesize()` returns 24 kHz PCM → the existing
    resample-to-48 kHz runs.
  Adds a `speed` argument. Kokoro uses it; Piper ignores it (Piper is already
  the fast option).
- **`voice/server.py`**
  - `GET /api/voices` → combined catalog JSON, grouped for the picker.
  - `POST /api/preview {voiceId}` → synth a **language-appropriate sample
    sentence** (English sample for `a`/`b`, Spanish for `e`) and return a **WAV**
    the browser plays with a plain `<audio>` element. Preview does **not** route
    through the live WebRTC/mic path, so it never disturbs the conversation's
    mic gate.
  - WebSocket `set_voice {voiceId, speed}` → store `voice_id` and `speed` on the
    session; passed into `_speak_with_events` → `speak_text`.
- **`voice/webrtc.py`** — `speak_text(text, voice_id, speed)` threads both
  through to `synthesize`. Session holds the current `voice_id`/`speed`
  (default `af_heart`).
- **`voice/web/*`** (`app.js`, `index.html`, `styles.css`)
  - Grouped voice `<select>`: **American**, **British**, **Spanish**, and
    **Piper — fast**, populated from `/api/voices`.
  - Quality grade (A–F) shown next to each Kokoro voice name.
  - ▶ **Preview** button → `POST /api/preview` → play returned WAV.
  - **Speed slider** (0.5–2×), shown for Kokoro voices.
  - Persist `{voiceId, speed}` in `localStorage`; default `af_heart` on first
    load; on change send `set_voice` over the WebSocket.

### Modified — root `run.sh`

Auto-start the TTS service exactly like the existing STT block: health-check
`:8300`, else create the venv, install requirements, launch, wait for `/health`
with a **generous first-run timeout** (model download), add to the cleanup trap,
and pass `TTS_SERVICE_URL` (default `http://host.docker.internal:8300`) into the
container env.

## Error handling (degraded mode)

Per the project's degraded-mode convention: if a Kokoro voice is requested but
`:8300` is unreachable, the router **falls back to the Piper default for that
utterance**, logs a warning, and the UI shows "Kokoro unavailable — using fast
voice." The loop is never left silent. Because the default voice is `af_heart`,
this fallback also covers the first reply arriving before the model finishes
downloading. `/api/voices` still lists Kokoro voices from the static catalog
even when the service is down; they simply fall back on use.

## Testing

- **Unit:** `voice_catalog.lookup` maps voice → engine/lang correctly for a
  Piper, an English Kokoro, and a Spanish Kokoro voice; `kokoro_client` parses
  PCM bytes + sample rate; resample output length matches the 48 kHz target.
- **Integration:** TTS `/synthesize` returns non-empty PCM for one English and
  one Spanish voice; `/voices` returns EN+ES voices only (no JA/ZH/FR/etc.).
- **Manual / Playwright:** picker shows grouped voices with grades; `af_heart`
  speaks (Kokoro); switching to Piper is fast; the preview button plays a
  sample; the speed slider changes tempo; a Spanish voice reads a Spanish
  sample; with the TTS service stopped, a Kokoro selection falls back to Piper
  with the UI notice.

## Out of scope

- Languages beyond English + Spanish.
- Piper speed control (Piper stays the fast default; the slider is Kokoro-only).
- Streaming/partial-sentence Kokoro synthesis (current sentence-by-sentence
  enqueue is reused).
