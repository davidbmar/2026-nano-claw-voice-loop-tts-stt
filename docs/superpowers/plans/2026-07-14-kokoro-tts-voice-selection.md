# Kokoro-82M TTS with Voice Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Kokoro-82M as a native-Mac TTS service alongside the existing Piper engine, with a browser voice picker (English + Spanish), quality-grade labels, a preview button, and a speed slider — Piper stays as the fast/low-latency option.

**Architecture:** A new native `tts-service/` (FastAPI + uvicorn, port 8300) runs Kokoro via PyTorch (MPS→CPU fallback), mirroring the existing native `stt-service/`. The Docker voice server gains an engine **router** in `voice/tts.py`: Piper voices synth locally (unchanged fast path); Kokoro voices are fetched over HTTP from the TTS service and resampled 24k→48k through the existing pipeline. If the TTS service is down, the router silently falls back to Piper so the voice loop is never silent.

**Tech Stack:** Python 3.12, FastAPI + uvicorn (services), aiohttp (Docker voice server), `kokoro` (PyTorch), numpy/scipy (resample), httpx (service client), vanilla JS frontend, vitest (JS tests), pytest (Python tests).

## Global Constraints

- **Voice scope:** English (American `a`, British `b`) + Spanish (`e`) ONLY. No Japanese/Mandarin/French/etc.
- **Default voice on first load:** `af_heart` (Kokoro).
- **TTS service port:** `8300`. Client URL env `TTS_SERVICE_URL`, default `http://host.docker.internal:8300` (Docker) / `http://localhost:8300` (host health checks).
- **Kokoro native sample rate:** 24000 Hz. Final WebRTC rate: 48000 Hz (`voice/tts.py` already resamples).
- **Raw-PCM HTTP convention** (mirror STT): body is int16 mono PCM; sample rate carried in `X-Sample-Rate` header.
- **Speed slider affects Kokoro only.** Piper ignores `speed` (it is already the fast option).
- **Degraded mode:** Kokoro voice requested but service unreachable → fall back to Piper default (`DEFAULT_VOICE` in `voice/tts.py`), log a warning, never raise to the caller.
- Follow existing style: IIFE-to-global pattern for browser JS modules (see `voice/web/phone-vad.js`), FastAPI lazy model load (see `stt-service/server.py`).

---

## File Structure

**New files**
- `tts-service/voices.py` — Kokoro EN+ES voice catalog + `lang_code` mapping (pure).
- `tts-service/server.py` — FastAPI service: `/health`, `/voices`, `/synthesize`.
- `tts-service/requirements.txt` — kokoro, torch, fastapi, uvicorn, numpy, scipy, espeakng-loader.
- `tts-service/run.sh` — install + launch (mirrors `stt-service/run.sh`).
- `voice/voice_catalog.py` — combined Piper+Kokoro catalog for the picker (Docker side, static).
- `voice/kokoro_client.py` — sync httpx client to the TTS service.
- `voice/wav.py` — wrap int16 PCM into a WAV byte string (for preview).
- `voice/web/voice-ui.js` — pure helpers (`groupVoices`, `sampleTextForLang`) via IIFE-to-global.
- `pytest.ini` — repo-root pytest config (`pythonpath = .`).
- `tests/python/test_tts_service_voices.py`, `tests/python/test_voice_catalog.py`, `tests/python/test_kokoro_client.py`, `tests/python/test_tts_router.py`, `tests/python/test_wav.py`
- `tests/voice-ui.test.mjs`

**Modified files**
- `voice/tts.py` — becomes the engine router; add `speed`; Kokoro dispatch + Piper fallback.
- `voice/webrtc.py` — `speak_text(text, voice_id, speed)`; session holds `voice_id`/`speed`; `set_voice()`.
- `voice/server.py` — `GET /api/voices`, `POST /api/preview`, `set_voice` WS handler, thread voice/speed into speech, health-check-on-select `voice_notice`.
- `voice/web/index.html` — picker markup + `voice-ui.js` script tag.
- `voice/web/app.js` — fetch `/api/voices`, render picker, persist, preview, speed, `voice_notice`.
- `voice/web/styles.css` — picker styles.
- `run.sh` — auto-start TTS service block; pass `TTS_SERVICE_URL` into the container.
- `README.md`, `CHANGELOG.md` — document the feature.

---

## Task 1: TTS service — Kokoro catalog + synthesis

**Files:**
- Create: `tts-service/voices.py`
- Create: `tts-service/server.py`
- Create: `tts-service/requirements.txt`
- Create: `tts-service/run.sh`
- Create: `pytest.ini`
- Create: `tests/python/test_tts_service_voices.py`
- Test venv: repo-root `.venv-test`

**Interfaces:**
- Produces (`tts-service/voices.py`):
  - `KOKORO_VOICES: list[dict]` — each `{"id","name","group","lang","grade"}` where `group ∈ {"American English","British English","Spanish"}` and `lang ∈ {"a","b","e"}`.
  - `LANG_BY_PREFIX: dict[str,str]` mapping `"a"→"a","b"→"b","e"→"e"`.
  - `lang_code_for(voice_id: str) -> str` — returns the KPipeline lang_code from the voice's first char; raises `KeyError` for unsupported prefixes.
- Produces (HTTP): `GET /health`→`{"status":"ok"}`; `GET /voices`→`{"voices": KOKORO_VOICES}`; `POST /synthesize` (JSON `{"text","voice","speed"}`) → int16 PCM body + `X-Sample-Rate: 24000`.

- [ ] **Step 1: Create the repo-root pytest config**

Create `pytest.ini`:

```ini
[pytest]
pythonpath = .
testpaths = tests/python
```

- [ ] **Step 2: Create a test venv with the pure-logic test deps**

Run:

```bash
cd /Users/davidmar/src/nano-claw
python3 -m venv .venv-test
.venv-test/bin/pip install -q pytest numpy scipy httpx
```

Expected: installs cleanly (no torch/kokoro needed for pure-logic tests).

- [ ] **Step 3: Write the failing test for the Kokoro catalog + lang mapping**

Create `tests/python/test_tts_service_voices.py`:

```python
import importlib.util
from pathlib import Path

# Load tts-service/voices.py by path (tts-service is not a package)
_spec = importlib.util.spec_from_file_location(
    "tts_voices", Path(__file__).resolve().parents[2] / "tts-service" / "voices.py"
)
voices = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voices)


def test_only_english_and_spanish():
    langs = {v["lang"] for v in voices.KOKORO_VOICES}
    assert langs == {"a", "b", "e"}, f"unexpected langs: {langs}"


def test_default_voice_present():
    ids = {v["id"] for v in voices.KOKORO_VOICES}
    assert "af_heart" in ids
    assert "ef_dora" in ids  # a Spanish voice for testing


def test_lang_code_for_maps_by_prefix():
    assert voices.lang_code_for("af_heart") == "a"
    assert voices.lang_code_for("bf_emma") == "b"
    assert voices.lang_code_for("ef_dora") == "e"


def test_lang_code_for_rejects_unsupported():
    import pytest
    with pytest.raises(KeyError):
        voices.lang_code_for("jf_alpha")  # Japanese — out of scope
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_tts_service_voices.py -v`
Expected: FAIL — `tts-service/voices.py` does not exist yet.

- [ ] **Step 5: Create `tts-service/voices.py`**

```python
"""Kokoro-82M voice catalog — English (American/British) + Spanish only.

Grades are from the Kokoro-82M v1.0 release notes (A best … F worst); they help
the picker be honest about weaker voices. Spanish voices have no published
letter grade, so they are marked "—".
"""

# lang_code (KPipeline): "a"=American English, "b"=British English, "e"=Spanish
LANG_BY_PREFIX = {"a": "a", "b": "b", "e": "e"}

KOKORO_VOICES = [
    # American English
    {"id": "af_heart",   "name": "Heart",   "group": "American English", "lang": "a", "grade": "A"},
    {"id": "af_bella",   "name": "Bella",   "group": "American English", "lang": "a", "grade": "A-"},
    {"id": "af_nicole",  "name": "Nicole",  "group": "American English", "lang": "a", "grade": "B-"},
    {"id": "af_aoede",   "name": "Aoede",   "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "af_kore",    "name": "Kore",    "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "af_sarah",   "name": "Sarah",   "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "af_nova",    "name": "Nova",    "group": "American English", "lang": "a", "grade": "C"},
    {"id": "af_sky",     "name": "Sky",     "group": "American English", "lang": "a", "grade": "C-"},
    {"id": "am_fenrir",  "name": "Fenrir",  "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "am_michael", "name": "Michael", "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "am_puck",    "name": "Puck",    "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "am_echo",    "name": "Echo",    "group": "American English", "lang": "a", "grade": "D"},
    # British English
    {"id": "bf_emma",     "name": "Emma",     "group": "British English", "lang": "b", "grade": "B-"},
    {"id": "bf_isabella", "name": "Isabella", "group": "British English", "lang": "b", "grade": "C"},
    {"id": "bm_george",   "name": "George",   "group": "British English", "lang": "b", "grade": "C"},
    {"id": "bm_fable",    "name": "Fable",    "group": "British English", "lang": "b", "grade": "C"},
    {"id": "bm_lewis",    "name": "Lewis",    "group": "British English", "lang": "b", "grade": "D+"},
    # Spanish (for testing Spanish speaking)
    {"id": "ef_dora",  "name": "Dora",  "group": "Spanish", "lang": "e", "grade": "—"},
    {"id": "em_alex",  "name": "Alex",  "group": "Spanish", "lang": "e", "grade": "—"},
    {"id": "em_santa", "name": "Santa", "group": "Spanish", "lang": "e", "grade": "—"},
]


def lang_code_for(voice_id: str) -> str:
    """Return the KPipeline lang_code for a voice id, by first-char prefix."""
    return LANG_BY_PREFIX[voice_id[0]]
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_tts_service_voices.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Create `tts-service/requirements.txt`**

```text
fastapi>=0.110
uvicorn>=0.27
kokoro>=0.9
torch>=2.2
numpy>=1.24
scipy>=1.11
soundfile>=0.12
espeakng-loader>=0.2
```

- [ ] **Step 8: Create `tts-service/server.py`**

```python
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
```

> Note: `import voices as voice_catalog` works because `run.sh` launches `python server.py` from inside `tts-service/`, putting that dir on `sys.path`.

- [ ] **Step 9: Create `tts-service/run.sh` (mirrors `stt-service/run.sh`)**

```bash
#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Installing TTS service dependencies..."
pip install -r requirements.txt

echo ""
echo "Starting TTS service on http://0.0.0.0:8300"
echo "The Docker container will call this for Kokoro text-to-speech."
echo ""
python server.py
```

Make it executable:

```bash
chmod +x tts-service/run.sh
```

- [ ] **Step 10: Manually verify the service synthesizes English and Spanish**

This is the model-download + phonemizer smoke test (no automated test — mirrors STT, which also has no model test). First run downloads ~310MB.

Run (in one terminal):

```bash
cd /Users/davidmar/src/nano-claw/tts-service
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
PYTORCH_ENABLE_MPS_FALLBACK=1 ./.venv/bin/python server.py
```

Then in another terminal:

```bash
curl -s localhost:8300/health
curl -s localhost:8300/voices | head -c 300
# English → WAV via a quick python one-liner that adds a header, then play:
curl -s -X POST localhost:8300/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello, this is the Heart voice.","voice":"af_heart","speed":1.0}' \
  --output /tmp/en.pcm
ls -l /tmp/en.pcm   # must be > 0 bytes
# Spanish:
curl -s -X POST localhost:8300/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hola, así es como sueno.","voice":"ef_dora","speed":1.0}' \
  --output /tmp/es.pcm
ls -l /tmp/es.pcm   # must be > 0 bytes (verifies Spanish phonemizer works)
```

Expected: `/health` returns `{"status":"ok"}`; both `.pcm` files are non-empty. If the Spanish call errors on phonemization, install espeak-ng system-wide (`brew install espeak-ng`) and retry.

- [ ] **Step 11: Commit**

```bash
git add tts-service/ pytest.ini tests/python/test_tts_service_voices.py
git commit -m "feat(tts): native Kokoro-82M service (EN+ES) on port 8300"
```

---

## Task 2: Docker-side combined catalog + Kokoro HTTP client

**Files:**
- Create: `voice/voice_catalog.py`
- Create: `voice/kokoro_client.py`
- Test: `tests/python/test_voice_catalog.py`, `tests/python/test_kokoro_client.py`

**Interfaces:**
- Consumes: `voice/tts.py::VOICE_CATALOG` (existing Piper list).
- Produces (`voice/voice_catalog.py`):
  - `combined_catalog() -> list[dict]` — every voice as `{"id","name","engine","lang","grade","group"}` (`engine ∈ {"kokoro","piper"}`).
  - `grouped_for_ui() -> dict` — `{"groups":[{"label","voices":[...]}], "default":"af_heart"}` in order: American, British, Spanish, `Piper — fast`.
  - `lookup(voice_id: str) -> dict | None` — the catalog entry, or `None` if unknown.
- Produces (`voice/kokoro_client.py`):
  - `synthesize(text: str, voice: str, speed: float) -> tuple[bytes, int]` — `(int16_pcm, sample_rate)`; raises `KokoroUnavailable` on any transport/HTTP error.
  - `class KokoroUnavailable(Exception)`.

- [ ] **Step 1: Write the failing test for the combined catalog**

Create `tests/python/test_voice_catalog.py`:

```python
from voice import voice_catalog


def test_lookup_identifies_engine():
    assert voice_catalog.lookup("af_heart")["engine"] == "kokoro"
    assert voice_catalog.lookup("en_US-lessac-medium")["engine"] == "piper"
    assert voice_catalog.lookup("does-not-exist") is None


def test_grouped_for_ui_order_and_default():
    ui = voice_catalog.grouped_for_ui()
    labels = [g["label"] for g in ui["groups"]]
    assert labels == ["American English", "British English", "Spanish", "Piper — fast"]
    assert ui["default"] == "af_heart"


def test_spanish_group_populated():
    ui = voice_catalog.grouped_for_ui()
    spanish = next(g for g in ui["groups"] if g["label"] == "Spanish")
    assert any(v["id"] == "ef_dora" for v in spanish["voices"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_voice_catalog.py -v`
Expected: FAIL — `voice/voice_catalog.py` does not exist. (Note: `voice/tts.py` imports numpy/scipy at module top — both are in `.venv-test`. It imports `piper` only lazily inside functions, so importing the module is safe.)

- [ ] **Step 3: Create `voice/voice_catalog.py`**

```python
"""Combined voice catalog for the picker — Kokoro (EN+ES) + Piper (fast).

Static so the browser picker renders even while the native TTS service warms.
The Kokoro list is kept in sync with tts-service/voices.py by hand (small,
rarely changes). Engine is inferred here so the router in tts.py can dispatch.
"""

from voice.tts import VOICE_CATALOG as PIPER_CATALOG

# Mirror of tts-service/voices.py KOKORO_VOICES (kept in sync manually).
_KOKORO = [
    ("af_heart", "Heart", "American English", "a", "A"),
    ("af_bella", "Bella", "American English", "a", "A-"),
    ("af_nicole", "Nicole", "American English", "a", "B-"),
    ("af_aoede", "Aoede", "American English", "a", "C+"),
    ("af_kore", "Kore", "American English", "a", "C+"),
    ("af_sarah", "Sarah", "American English", "a", "C+"),
    ("af_nova", "Nova", "American English", "a", "C"),
    ("af_sky", "Sky", "American English", "a", "C-"),
    ("am_fenrir", "Fenrir", "American English", "a", "C+"),
    ("am_michael", "Michael", "American English", "a", "C+"),
    ("am_puck", "Puck", "American English", "a", "C+"),
    ("am_echo", "Echo", "American English", "a", "D"),
    ("bf_emma", "Emma", "British English", "b", "B-"),
    ("bf_isabella", "Isabella", "British English", "b", "C"),
    ("bm_george", "George", "British English", "b", "C"),
    ("bm_fable", "Fable", "British English", "b", "C"),
    ("bm_lewis", "Lewis", "British English", "b", "D+"),
    ("ef_dora", "Dora", "Spanish", "e", "—"),
    ("em_alex", "Alex", "Spanish", "e", "—"),
    ("em_santa", "Santa", "Spanish", "e", "—"),
]

_KOKORO_ENTRIES = [
    {"id": vid, "name": name, "engine": "kokoro", "lang": lang, "grade": grade, "group": group}
    for (vid, name, group, lang, grade) in _KOKORO
]

_PIPER_ENTRIES = [
    {"id": v["id"], "name": v["name"], "engine": "piper", "lang": v["lang"],
     "grade": None, "group": "Piper — fast"}
    for v in PIPER_CATALOG
]

_ALL = _KOKORO_ENTRIES + _PIPER_ENTRIES
_BY_ID = {v["id"]: v for v in _ALL}

DEFAULT_VOICE = "af_heart"


def combined_catalog() -> list[dict]:
    return list(_ALL)


def lookup(voice_id: str) -> dict | None:
    return _BY_ID.get(voice_id)


def grouped_for_ui() -> dict:
    order = ["American English", "British English", "Spanish", "Piper — fast"]
    groups = []
    for label in order:
        voices = [v for v in _ALL if v["group"] == label]
        if voices:
            groups.append({"label": label, "voices": voices})
    return {"groups": groups, "default": DEFAULT_VOICE}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_voice_catalog.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Write the failing test for the Kokoro HTTP client**

Create `tests/python/test_kokoro_client.py`:

```python
import pytest

from voice import kokoro_client


class _FakeResponse:
    def __init__(self, content, headers, status=200):
        self.content = content
        self.headers = headers
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        if self._exc:
            raise self._exc
        return self._response


def test_synthesize_parses_pcm_and_rate(monkeypatch):
    pcm = b"\x01\x00\x02\x00"
    resp = _FakeResponse(pcm, {"X-Sample-Rate": "24000"})
    monkeypatch.setattr(kokoro_client.httpx, "Client", lambda *a, **k: _FakeClient(response=resp))
    out, rate = kokoro_client.synthesize("hi", "af_heart", 1.0)
    assert out == pcm
    assert rate == 24000


def test_synthesize_raises_kokoro_unavailable_on_error(monkeypatch):
    monkeypatch.setattr(
        kokoro_client.httpx, "Client",
        lambda *a, **k: _FakeClient(exc=Exception("connection refused")),
    )
    with pytest.raises(kokoro_client.KokoroUnavailable):
        kokoro_client.synthesize("hi", "af_heart", 1.0)
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_kokoro_client.py -v`
Expected: FAIL — `voice/kokoro_client.py` does not exist.

- [ ] **Step 7: Create `voice/kokoro_client.py`**

```python
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
```

- [ ] **Step 8: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_kokoro_client.py -v`
Expected: PASS (2 passed).

- [ ] **Step 9: Commit**

```bash
git add voice/voice_catalog.py voice/kokoro_client.py tests/python/test_voice_catalog.py tests/python/test_kokoro_client.py
git commit -m "feat(tts): combined voice catalog + Kokoro HTTP client (Docker side)"
```

---

## Task 3: Engine router in `voice/tts.py`

**Files:**
- Modify: `voice/tts.py`
- Test: `tests/python/test_tts_router.py`

**Interfaces:**
- Consumes: `voice.voice_catalog.lookup`, `voice.kokoro_client.synthesize`, `voice.kokoro_client.KokoroUnavailable`.
- Produces: `synthesize(text: str, voice_id: str = "", speed: float = 1.0) -> bytes` — 48kHz int16 mono PCM. Kokoro voices route to the service then resample; unknown/failed Kokoro → Piper `DEFAULT_VOICE`. Piper voices unchanged (ignore `speed`).
- Also produces module-level helper `_resample_to_48k(pcm: bytes, native_rate: int) -> bytes` (extracted from existing resample code).

- [ ] **Step 1: Write the failing test for routing + fallback**

Create `tests/python/test_tts_router.py`:

```python
import numpy as np

from voice import tts, kokoro_client


def _pcm(n, rate):
    # n samples of silence-ish int16 at the given rate
    return np.zeros(n, dtype=np.int16).tobytes()


def test_kokoro_voice_resampled_to_48k(monkeypatch):
    # 2400 samples @ 24k (0.1s) should become ~4800 samples @ 48k
    monkeypatch.setattr(
        kokoro_client, "synthesize", lambda text, voice, speed: (_pcm(2400, 24000), 24000)
    )
    out = tts.synthesize("hola", "ef_dora", 1.0)
    assert len(out) // 2 == 4800  # 2 bytes per int16 sample


def test_kokoro_failure_falls_back_to_piper(monkeypatch):
    def _boom(text, voice, speed):
        raise kokoro_client.KokoroUnavailable("down")

    monkeypatch.setattr(kokoro_client, "synthesize", _boom)

    called = {}

    def _fake_piper(text, voice_id):
        called["voice_id"] = voice_id
        return _pcm(4800, 48000)  # pretend Piper already returns 48k

    monkeypatch.setattr(tts, "_synthesize_piper", _fake_piper)
    out = tts.synthesize("hello", "af_heart", 1.0)
    assert called["voice_id"] == tts.DEFAULT_VOICE
    assert len(out) // 2 == 4800
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_tts_router.py -v`
Expected: FAIL — `synthesize` has no `speed` param / no routing; `_synthesize_piper` and `_resample_to_48k` don't exist yet.

- [ ] **Step 3: Refactor `voice/tts.py` — extract Piper synth + resample, add the router**

Replace the existing `synthesize` function (lines ~81-102) with the following, and keep everything above it (`VOICE_CATALOG`, `_get_voice`, etc.) unchanged:

```python
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
```

> Note: `DEFAULT_VOICE` currently is `"en_US-lessac-medium"` (a Piper voice) — keep it as-is; it is the fast fallback target. The browser default (`af_heart`) is separate and lives in `voice_catalog.DEFAULT_VOICE`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_tts_router.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full Python suite**

Run: `.venv-test/bin/pytest tests/python -v`
Expected: PASS (all tasks 1-3 tests green).

- [ ] **Step 6: Commit**

```bash
git add voice/tts.py tests/python/test_tts_router.py
git commit -m "feat(tts): engine router — Kokoro dispatch with Piper fallback"
```

---

## Task 4: Server wiring — /api/voices, /api/preview, set_voice, WAV

**Files:**
- Create: `voice/wav.py`
- Test: `tests/python/test_wav.py`
- Modify: `voice/webrtc.py`
- Modify: `voice/server.py`

**Interfaces:**
- Produces (`voice/wav.py`): `pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes` — a complete WAV byte string, mono int16.
- Produces (`voice/webrtc.py`): `Session.set_voice(voice_id: str, speed: float)`; `Session.speak_text(text, voice_id="", speed=1.0)`; session attrs `self.voice_id` (default `af_heart`) and `self.speed` (default `1.0`).
- Produces (HTTP): `GET /api/voices` → `voice_catalog.grouped_for_ui()`; `POST /api/preview` (`{voiceId}`) → `audio/wav`.
- Produces (WS): inbound `set_voice {voiceId, speed}`; outbound `voice_notice {text}`.

- [ ] **Step 1: Write the failing test for WAV wrapping**

Create `tests/python/test_wav.py`:

```python
import struct

from voice.wav import pcm_to_wav


def test_wav_header_is_valid():
    pcm = b"\x00\x00" * 2400  # 2400 int16 samples
    wav = pcm_to_wav(pcm, 48000)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    # data chunk length equals the PCM byte length
    data_idx = wav.index(b"data")
    (data_len,) = struct.unpack("<I", wav[data_idx + 4:data_idx + 8])
    assert data_len == len(pcm)
    # sample rate stored correctly (offset 24 in the standard 44-byte header)
    (rate,) = struct.unpack("<I", wav[24:28])
    assert rate == 48000
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_wav.py -v`
Expected: FAIL — `voice/wav.py` does not exist.

- [ ] **Step 3: Create `voice/wav.py`**

```python
"""Wrap raw int16 PCM into a WAV byte string (used by the preview endpoint)."""

import io
import wave


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Return a complete mono/int16 WAV file as bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_wav.py -v`
Expected: PASS.

- [ ] **Step 5: Update `voice/webrtc.py` — session voice state + set_voice**

In `Session.__init__` (after `self._closed = False`, around line 53) add:

```python
        # Selected voice for this session (browser default: Kokoro af_heart).
        self.voice_id = "af_heart"
        self.speed = 1.0
```

Add a method to `Session` (e.g. right after `stop_speaking`):

```python
    def set_voice(self, voice_id: str, speed: float):
        """Update the voice + speed used for subsequent replies."""
        if voice_id:
            self.voice_id = voice_id
        try:
            self.speed = max(0.5, min(2.0, float(speed)))
        except (TypeError, ValueError):
            pass
        log.info("Voice set: %s (speed=%.2f)", self.voice_id, self.speed)
```

Change `speak_text` signature (line ~162) from `async def speak_text(self, text: str, voice_id: str = ""):` to:

```python
    async def speak_text(self, text: str, voice_id: str = "", speed: float = 1.0):
```

And inside it change the executor call (line ~175) from:

```python
            pcm_48k = await loop.run_in_executor(None, synthesize, sentence, voice_id)
```

to pass speed:

```python
            pcm_48k = await loop.run_in_executor(
                None, synthesize, sentence, voice_id, speed
            )
```

- [ ] **Step 6: Update `voice/server.py` — routes, preview, set_voice, notice**

Add imports near the top (after existing imports):

```python
from voice import voice_catalog
from voice.tts import synthesize as tts_synthesize
from voice.wav import pcm_to_wav
from voice import kokoro_client
```

Add two HTTP handlers (before `create_app`):

```python
_PREVIEW_SAMPLES = {
    "a": "Hi, this is how I sound.",
    "b": "Hi, this is how I sound.",
    "e": "Hola, así es como sueno.",
}


async def voices_handler(request: web.Request) -> web.Response:
    return web.json_response(voice_catalog.grouped_for_ui())


async def preview_handler(request: web.Request) -> web.Response:
    body = await request.json()
    voice_id = body.get("voiceId", "")
    entry = voice_catalog.lookup(voice_id)
    if not entry:
        raise web.HTTPBadRequest(text="unknown voice")
    sample = _PREVIEW_SAMPLES.get(entry.get("lang", "a"), _PREVIEW_SAMPLES["a"])
    loop = asyncio.get_running_loop()
    pcm_48k = await loop.run_in_executor(None, tts_synthesize, sample, voice_id, 1.0)
    wav = pcm_to_wav(pcm_48k, 48000)
    return web.Response(body=wav, content_type="audio/wav")
```

Register them in `create_app` (add before the catch-all `/{filename}` route so it isn't shadowed):

```python
    app.router.add_get("/api/voices", voices_handler)
    app.router.add_post("/api/preview", preview_handler)
```

> Order matters: `add_get("/{filename}", static_handler)` is a catch-all. Add the `/api/*` routes **before** it in `create_app` so they win.

Handle the `set_voice` WS message. In `websocket_handler`, add a branch alongside the others (e.g. after `text_message`):

```python
            elif msg_type == "set_voice":
                if not session:
                    continue
                voice_id = msg.get("voiceId", "")
                session.set_voice(voice_id, msg.get("speed", 1.0))
                # Proactively warn if a Kokoro voice was picked but the service
                # is down — the reply will still work (Piper fallback).
                entry = voice_catalog.lookup(voice_id)
                if entry and entry["engine"] == "kokoro":
                    loop = asyncio.get_running_loop()
                    healthy = await loop.run_in_executor(None, kokoro_client.is_healthy)
                    if not healthy:
                        await ws.send_json({
                            "type": "voice_notice",
                            "text": "Kokoro voice unavailable — using the fast voice.",
                        })
```

Thread the session's voice into speech. In `_speak_with_events`, change the `speak_text` call to pass the session's selection:

```python
    try:
        await session.speak_text(text, session.voice_id, session.speed)
    finally:
```

- [ ] **Step 7: Manually verify the endpoints (with the TTS service running)**

Start the TTS service (Task 1 Step 10) and the app (`./run.sh`), then:

```bash
curl -s localhost:9090/api/voices | python3 -m json.tool | head -30
# preview returns a WAV:
curl -s -X POST localhost:9090/api/preview \
  -H 'Content-Type: application/json' -d '{"voiceId":"af_heart"}' --output /tmp/preview.wav
file /tmp/preview.wav   # → "RIFF (little-endian) data, WAVE audio"
afplay /tmp/preview.wav # you should hear the Heart voice
```

Expected: `/api/voices` shows 4 groups; `/tmp/preview.wav` is a playable WAV.

- [ ] **Step 8: Commit**

```bash
git add voice/wav.py voice/webrtc.py voice/server.py tests/python/test_wav.py
git commit -m "feat(voice): /api/voices, /api/preview, set_voice WS wiring"
```

---

## Task 5: Frontend — voice picker, grades, preview, speed slider

**Files:**
- Create: `voice/web/voice-ui.js`
- Test: `tests/voice-ui.test.mjs`
- Modify: `voice/web/index.html`
- Modify: `voice/web/app.js`
- Modify: `voice/web/styles.css`

**Interfaces:**
- Produces (`voice/web/voice-ui.js`, attached to `window.VoiceUI`):
  - `groupVoices(uiCatalog) -> Array<{label, options:[{id,label}]}>` — turns `/api/voices` JSON into `<optgroup>`-ready data; each option label is `"Name (Grade)"` for Kokoro or just `"Name"` for Piper.
  - `sampleTextForLang(lang) -> string`.
- Consumes (`app.js`): `sendMsg("set_voice", {voiceId, speed})`, `handleMessage` switch, `fetch("/api/voices")`, `fetch("/api/preview")`.

- [ ] **Step 1: Write the failing test for the pure UI helper**

Create `tests/voice-ui.test.mjs`:

```javascript
import assert from "node:assert/strict";

await import("../voice/web/voice-ui.js");
const VoiceUI = globalThis.VoiceUI;
assert.ok(VoiceUI, "VoiceUI must be exported to the global scope");

const ui = {
    groups: [
        { label: "American English", voices: [
            { id: "af_heart", name: "Heart", engine: "kokoro", grade: "A" },
        ]},
        { label: "Piper — fast", voices: [
            { id: "en_US-lessac-medium", name: "Lessac (US)", engine: "piper", grade: null },
        ]},
    ],
    default: "af_heart",
};

const grouped = VoiceUI.groupVoices(ui);
assert.equal(grouped.length, 2);
assert.equal(grouped[0].label, "American English");
assert.equal(grouped[0].options[0].id, "af_heart");
assert.equal(grouped[0].options[0].label, "Heart (A)", "Kokoro options show the grade");
assert.equal(grouped[1].options[0].label, "Lessac (US)", "Piper options have no grade");

assert.equal(VoiceUI.sampleTextForLang("e").startsWith("Hola"), true);
assert.equal(VoiceUI.sampleTextForLang("a").length > 0, true);

console.log("voice-ui tests passed");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node --test tests/voice-ui.test.mjs` (or `npx vitest run tests/voice-ui.test.mjs`)
Expected: FAIL — `voice/web/voice-ui.js` does not exist.

> The existing `tests/phone-vad.test.mjs` runs under vitest via `npm test`. `node --test` also executes `.mjs` top-level asserts. Use whichever the repo CI uses; both work for this file.

- [ ] **Step 3: Create `voice/web/voice-ui.js`**

```javascript
(function (global) {
    "use strict";

    var SAMPLES = {
        a: "Hi, this is how I sound.",
        b: "Hi, this is how I sound.",
        e: "Hola, así es como sueno.",
    };

    function sampleTextForLang(lang) {
        return SAMPLES[lang] || SAMPLES.a;
    }

    function optionLabel(voice) {
        if (voice.engine === "kokoro" && voice.grade) {
            return voice.name + " (" + voice.grade + ")";
        }
        return voice.name;
    }

    function groupVoices(uiCatalog) {
        return (uiCatalog.groups || []).map(function (group) {
            return {
                label: group.label,
                options: group.voices.map(function (v) {
                    return { id: v.id, label: optionLabel(v) };
                }),
            };
        });
    }

    global.VoiceUI = {
        groupVoices: groupVoices,
        sampleTextForLang: sampleTextForLang,
    };
}(typeof window !== "undefined" ? window : globalThis));
```

- [ ] **Step 4: Run to verify it passes**

Run: `node --test tests/voice-ui.test.mjs`
Expected: PASS — "voice-ui tests passed".

- [ ] **Step 5: Add the picker markup to `voice/web/index.html`**

Add the `voice-ui.js` script tag before `app.js`:

```html
  <script src="phone-vad.js"></script>
  <script src="voice-ui.js"></script>
  <script src="app.js"></script>
```

Add the picker UI inside `<footer id="controls">`, right after the `#text-input-row` div:

```html
      <div id="voice-row">
        <label for="voice-select">Voice</label>
        <select id="voice-select" disabled></select>
        <button id="voice-preview-btn" type="button" title="Preview voice" disabled>▶</button>
        <span id="speed-control">
          <label for="speed-slider">Speed</label>
          <input id="speed-slider" type="range" min="0.5" max="2" step="0.1" value="1">
          <span id="speed-value">1.0×</span>
        </span>
      </div>
```

- [ ] **Step 6: Wire the picker in `voice/web/app.js`**

Add DOM element handles near the top (after line 15, with the other `getElementById` calls):

```javascript
const voiceSelect = document.getElementById("voice-select");
const voicePreviewBtn = document.getElementById("voice-preview-btn");
const speedSlider = document.getElementById("speed-slider");
const speedValue = document.getElementById("speed-value");
```

Add this voice-picker module near the WebSocket section (e.g. after `sendMsg`):

```javascript
// ── Voice picker ─────────────────────────────────────────────
var LS_VOICE = "nanoclaw.voiceId";
var LS_SPEED = "nanoclaw.speed";
var currentVoiceId = localStorage.getItem(LS_VOICE) || "af_heart";
var currentSpeed = parseFloat(localStorage.getItem(LS_SPEED) || "1") || 1;
var previewAudio = new Audio();

function renderVoiceOptions(uiCatalog) {
    voiceSelect.innerHTML = "";
    VoiceUI.groupVoices(uiCatalog).forEach(function (group) {
        var og = document.createElement("optgroup");
        og.label = group.label;
        group.options.forEach(function (opt) {
            var o = document.createElement("option");
            o.value = opt.id;
            o.textContent = opt.label;
            og.appendChild(o);
        });
        voiceSelect.appendChild(og);
    });
    voiceSelect.value = currentVoiceId;
    if (!voiceSelect.value) {
        currentVoiceId = uiCatalog.default;
        voiceSelect.value = currentVoiceId;
    }
    voiceSelect.disabled = false;
    voicePreviewBtn.disabled = false;
}

function pushVoice() {
    sendMsg("set_voice", { voiceId: currentVoiceId, speed: currentSpeed });
}

function loadVoices() {
    fetch("/api/voices")
        .then(function (r) { return r.json(); })
        .then(function (uiCatalog) {
            renderVoiceOptions(uiCatalog);
            speedSlider.value = String(currentSpeed);
            speedValue.textContent = currentSpeed.toFixed(1) + "×";
            pushVoice();
        })
        .catch(function () { statusText.textContent = "Could not load voices"; });
}

voiceSelect.addEventListener("change", function () {
    currentVoiceId = voiceSelect.value;
    localStorage.setItem(LS_VOICE, currentVoiceId);
    pushVoice();
});

speedSlider.addEventListener("input", function () {
    currentSpeed = parseFloat(speedSlider.value);
    speedValue.textContent = currentSpeed.toFixed(1) + "×";
    localStorage.setItem(LS_SPEED, String(currentSpeed));
});
speedSlider.addEventListener("change", pushVoice);

voicePreviewBtn.addEventListener("click", function () {
    voicePreviewBtn.disabled = true;
    fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voiceId: currentVoiceId }),
    })
        .then(function (r) { return r.blob(); })
        .then(function (blob) {
            previewAudio.src = URL.createObjectURL(blob);
            return previewAudio.play();
        })
        .catch(function () { /* ignore preview errors */ })
        .finally(function () { voicePreviewBtn.disabled = false; });
});
```

Call `loadVoices()` once the socket is open. In `ws.onopen` (line ~360), after `sendMsg("hello");` add:

```javascript
        loadVoices();
```

Handle the `voice_notice` message. In the `handleMessage` switch (line ~382), add a case:

```javascript
        case "voice_notice":
            statusText.textContent = msg.text;
            break;
```

- [ ] **Step 7: Style the picker in `voice/web/styles.css`**

Append:

```css
#voice-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.5rem;
    flex-wrap: wrap;
    font-size: 0.85rem;
}
#voice-row label { opacity: 0.8; }
#voice-select {
    flex: 1 1 8rem;
    min-width: 8rem;
    padding: 0.35rem;
    border-radius: 0.4rem;
}
#voice-preview-btn {
    padding: 0.35rem 0.6rem;
    border-radius: 0.4rem;
    cursor: pointer;
}
#speed-control { display: flex; align-items: center; gap: 0.35rem; }
#speed-slider { width: 6rem; }
#speed-value { min-width: 2.5rem; opacity: 0.8; }
```

- [ ] **Step 8: Manual smoke test in the browser**

With `./run.sh` up and the TTS service running, open `http://localhost:9090`:
- The Voice dropdown lists American / British / Spanish / Piper — fast, with grades like "Heart (A)".
- Select `af_heart`, speak → hear Kokoro. Select Piper "Lessac (US)" → hear the fast voice.
- Click ▶ → hear a preview. Move the speed slider, ▶ again → tempo changes (Kokoro).
- Select a Spanish voice, ▶ → Spanish sample.

- [ ] **Step 9: Commit**

```bash
git add voice/web/voice-ui.js voice/web/index.html voice/web/app.js voice/web/styles.css tests/voice-ui.test.mjs
git commit -m "feat(web): voice picker with grades, preview, and speed slider"
```

---

## Task 6: run.sh auto-start + end-to-end verification + docs

**Files:**
- Modify: `run.sh`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `tts-service/run.sh`-equivalent inline logic; `TTS_SERVICE_URL` env into the container.
- Produces: single-command startup that boots STT (8200) + TTS (8300) + container (9090).

- [ ] **Step 1: Add a TTS-service auto-start block to `run.sh`**

Directly after the STT service block (after its `fi` that closes the "already running / start" branch, before the `cleanup()` definition), add a parallel block:

```bash
# Start TTS service (Kokoro) if not already running
TTS_SERVICE_URL="${TTS_SERVICE_URL:-http://host.docker.internal:8300}"
TTS_CHECK_URL="${TTS_SERVICE_URL/host.docker.internal/localhost}"
TTS_PID=""

if curl -sf "$TTS_CHECK_URL/health" >/dev/null 2>&1; then
  echo "TTS service already running at $TTS_CHECK_URL"
else
  echo "=== Starting TTS service (Kokoro) ==="
  TTS_VENV="$SCRIPT_DIR/tts-service/.venv"
  if [ ! -d "$TTS_VENV" ]; then
    echo "Creating TTS virtual environment..."
    python3 -m venv "$TTS_VENV"
  fi
  "$TTS_VENV/bin/pip" install -q -r "$SCRIPT_DIR/tts-service/requirements.txt"
  PYTORCH_ENABLE_MPS_FALLBACK=1 "$TTS_VENV/bin/python" "$SCRIPT_DIR/tts-service/server.py" &
  TTS_PID=$!

  # Wait for readiness — first run downloads the ~310MB model, so allow longer.
  for i in $(seq 1 60); do
    if curl -sf "$TTS_CHECK_URL/health" >/dev/null 2>&1; then
      echo "TTS service ready"
      break
    fi
    sleep 1
  done
  echo ""
fi
```

> `SCRIPT_DIR` is already defined in the STT block; this reuses it. If the STT block is skipped (STT already running), ensure `SCRIPT_DIR` is still set — move `SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"` to just before the STT block so both services can rely on it.

- [ ] **Step 2: Extend the cleanup trap to also kill the TTS service**

Change the `cleanup()` function to also stop `TTS_PID`:

```bash
cleanup() {
  if [ -n "$STT_PID" ]; then
    echo ""
    echo "Stopping STT service (pid $STT_PID)..."
    kill $STT_PID 2>/dev/null
    wait $STT_PID 2>/dev/null
  fi
  if [ -n "$TTS_PID" ]; then
    echo "Stopping TTS service (pid $TTS_PID)..."
    kill $TTS_PID 2>/dev/null
    wait $TTS_PID 2>/dev/null
  fi
}
trap cleanup EXIT
```

- [ ] **Step 3: Pass `TTS_SERVICE_URL` into the container**

In the `docker run` invocation, add the env flag alongside `STT_SERVICE_URL`:

```bash
  -e STT_SERVICE_URL="$STT_SERVICE_URL" \
  -e TTS_SERVICE_URL="$TTS_SERVICE_URL" \
```

- [ ] **Step 4: Full single-command end-to-end verification**

Run:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # if not already in .env
./run.sh
```

Expected console: "STT service ready", "TTS service ready", "Open http://localhost:9090". Then in the browser confirm the full spec acceptance:
- Picker shows grouped voices with grades; default is `af_heart`.
- `af_heart` speaks (Kokoro); Piper "Lessac" is fast.
- Preview ▶ plays a sample; speed slider changes Kokoro tempo.
- A Spanish voice reads a Spanish sample.

- [ ] **Step 5: Degraded-mode verification (TTS service down)**

Stop only the TTS service (Ctrl-C its process, or `lsof -ti :8300 | xargs kill`), keep the container running. In the browser select `af_heart` → expect the status bar to show "Kokoro voice unavailable — using the fast voice." Speak → you still hear a reply (Piper fallback). Confirms the loop is never silent.

- [ ] **Step 6: Update `README.md`**

- In the architecture diagram, add the native TTS service (port 8300) beside the STT service, and note Piper stays in the container as the fast path.
- Under the data-flow / TTS description, replace "Piper converts to speech (in Docker)" with: the voice server routes the selected voice — Kokoro voices to the native TTS service (port 8300), Piper voices locally — then streams audio back.
- Add a short "Voices" subsection: default `af_heart`; English + Spanish; picker with grades, preview, and a Kokoro speed slider; Piper is the low-latency option.

- [ ] **Step 7: Update `CHANGELOG.md`**

Add an entry:

```markdown
### Added
- Kokoro-82M TTS as a native Mac service (port 8300) with a browser voice picker
  (American/British English + Spanish), quality-grade labels, per-voice preview,
  and a speed slider. Piper remains as the fast, low-latency option. Selecting a
  Kokoro voice while the service is down falls back to Piper automatically.
```

- [ ] **Step 8: Commit**

```bash
git add run.sh README.md CHANGELOG.md
git commit -m "feat: auto-start Kokoro TTS service in run.sh; document voices"
```

---

## Self-Review (completed during authoring)

**Spec coverage:**
- Kokoro alongside Piper → Task 3 router. ✓
- Native Mac TTS service → Task 1. ✓
- English + Spanish only → Task 1 catalog + `test_only_english_and_spanish`. ✓
- Default `af_heart` → `voice_catalog.DEFAULT_VOICE`, session default (Task 4), browser default (Task 5). ✓
- Speed slider (Kokoro only) → Task 4/5; Piper ignores speed (Task 3). ✓
- Preview button (WAV, out-of-band from WebRTC) → Task 4 `/api/preview` + Task 5. ✓
- Quality grades → Task 1 catalog, Task 5 `optionLabel`. ✓
- Persistence → Task 5 localStorage. ✓
- Degraded mode fallback + notice → Task 3 (`_synthesize_kokoro`), Task 4 (`voice_notice`), Task 6 Step 5. ✓
- run.sh auto-start → Task 6. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every test step shows the assertion and the run command. ✓

**Type consistency:** `synthesize(text, voice_id, speed)` signature consistent across `tts.py`, `webrtc.speak_text`, and the executor call. `KokoroUnavailable` defined in `kokoro_client` and caught in `tts._synthesize_kokoro`. `grouped_for_ui()` shape matches `VoiceUI.groupVoices` input. `X-Sample-Rate` header written by the service and read by the client. ✓

## Out of scope (per spec)
- Languages beyond English + Spanish.
- Piper speed control.
- Streaming/partial-sentence Kokoro synthesis.
