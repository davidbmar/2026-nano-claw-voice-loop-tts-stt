# Pipeline Settings — Switch STT / LLM / TTS — Design

**Date:** 2026-07-15
**Status:** Approved (design), pending implementation plan
**Builds on:** the voice stack (Kokoro/Piper voices, Anthropic streaming) already on `main`.

## Problem

The voice loop's three stages are effectively hard-wired:
- **STT** — `faster-whisper` with the model size hardcoded (`base`) in `stt-service/server.py`.
- **LLM** — the model is baked into `docker/default-config.json` (`agents.defaults.model`) and read per-request via `getAgentConfig()`; there's no runtime switch and no UI. Only the Anthropic provider streams (Phase-1 work); the 11 other registered providers fall back to non-streaming.
- **TTS** — already switchable at runtime via the voice picker (`/api/voices` + `set_voice`), but it lives on its own, separate from any "pipeline" concept.

We want one **⚙ Pipeline** settings panel that lets the user switch each stage live, and — critically — make LLM switching genuinely useful for a *fast* voice loop by giving every model token streaming.

## Scope decisions (agreed)

| Decision | Choice |
| --- | --- |
| Overall | Live **LLM** switching (the new work) + fold STT/LLM/TTS into one settings panel |
| STT | Dropdown of Whisper model **sizes**: `tiny · base · small · medium` |
| LLM options | **Curated, availability-aware** catalog; each model selectable if its provider key is configured, else shown greyed labeled **"no key"** |
| LLM streaming | **Add OpenAI-compatible streaming now** so Gemini/DeepSeek/Groq/OpenAI/Alibaba all stream (not just Anthropic) |
| LLM switching | **Live, per-session** (no restart) via a `model` override threaded to `/api/chat` |
| TTS | Reuse the existing voice picker, relocated into the panel — no backend change |
| Persistence | STT size, model, and voice persisted in `localStorage`; defaults `base` / `anthropic/claude-haiku-4-5` / `af_heart` |
| Alibaba/Qwen | Add `DASHSCOPE_API_KEY` env injection (config auto-injects only 6 providers today) |

## Architecture

```
Browser ⚙ Pipeline panel
  STT  [ Whisper: base ▼ ]     → ws set_stt {size}
  LLM  [ Claude Haiku 4.5 ▼ ]  → ws set_model {modelId}      (greyed "no key" if unavailable)
  TTS  [ Kokoro af_heart ▼ ]   → ws set_voice {voiceId,speed}  (existing)
        ▲ populated by GET /api/models + GET /api/voices
        │
  voice server (voice/server.py)
    GET /api/models      → proxies nano-claw GET /api/models (catalog + availability)
    ws set_stt/set_model → stored on session; size → /transcribe header; model → /api/chat override
        │                                   │
  STT service (:8200)                 nano-claw API (:3001)
    /transcribe honors X-Model-Size     GET /api/models (catalog + which providers have keys)
    lazy-loads+caches a model per size  /api/chat accepts {model} override
                                        OpenAIProvider.completeStream()  ← NEW (OpenAI SSE)
```

## Components

### LLM — nano-claw API (TypeScript)

- **Model catalog** (`src/agent/models.ts`, new): a curated list of voice-friendly models, each `{id, label, provider, streams}`. Initial set (editable):
  - `anthropic/claude-haiku-4-5` — "Claude Haiku 4.5" (default)
  - `anthropic/claude-sonnet-4-5` — "Claude Sonnet 4.5"
  - `gemini/gemini-2.0-flash` — "Gemini 2.0 Flash"
  - `deepseek/deepseek-chat` — "DeepSeek Chat"
  - `groq/llama-3.3-70b-versatile` — "Groq Llama 3.3 70B"
  - `dashscope/qwen-plus` — "Qwen Plus (Alibaba)"
  - `openai/gpt-4o-mini` — "GPT-4o mini"
- **`GET /api/models`**: returns the catalog with per-model `available` computed from whether the model's provider is configured (`config.providers[provider]?.apiKey` present — the same env-injection that already exists). Unavailable → the browser shows "no key".
- **`/api/chat` model override**: `handleChat`/`handleApprove`/`handleReject` accept an optional `model` in the request body; `getAgentConfig()` gains an override param so `stepLoop`/`stepLoopStream` use the chosen model instead of `config.agents.defaults.model`. Falls back to the default when absent or invalid.
- **`OpenAIProvider.completeStream()`** (new override in `src/providers/base.ts`): native OpenAI-compatible SSE streaming — POST `/chat/completions` with `stream:true`, parse `data:` frames (`choices[0].delta.content` → text deltas; `choices[0].delta.tool_calls` accumulated by index → a `tool_calls` StreamEvent; `[DONE]` sentinel; `usage` when present). Reuses the `readSSEFrames` helper from the Anthropic work. Covers Gemini/DeepSeek/Groq/OpenAI/Alibaba (all routed through `OpenAIProvider`).
- **Config**: add `DASHSCOPE_API_KEY` (and, opportunistically, the other registered-but-not-injected keys) to `src/config/index.ts`'s env injection so Alibaba/Qwen becomes available. Fix the Gemini `apiBase` to the OpenAI-compat path (`.../v1beta/openai`) so Gemini actually works.

### STT — service + voice server (Python)

- **`stt-service/server.py`**: `MODEL_SIZE` becomes a per-size cache. `/transcribe` reads an `X-Model-Size` header (default `base`), lazy-loads + caches a `WhisperModel` per size, and transcribes with it. `GET /health` unchanged. (`GET /sizes` optional — the size list is a static UI constant.)
- **`voice/server.py`/`voice/webrtc.py`**: `set_stt {size}` WS message stored on the session; the size is sent as the `X-Model-Size` header on the STT `/transcribe` call in `stop_recording`.

### LLM/TTS wiring — voice server (Python)

- **`GET /api/models`** on `voice/server.py`: proxies nano-claw's `GET /api/models` to the browser (like `/api/voices`).
- **`set_model {modelId}`** WS: stored on the session; included as `model` on every `/api/chat` (and approve/reject) request the voice server makes.
- **TTS**: unchanged — the existing `set_voice`/`/api/voices` path.

### Browser (`voice/web/`)

- A **⚙ Pipeline** panel (gear button in the controls opens a small panel/section) containing three labeled dropdowns: STT (Whisper sizes), LLM (from `/api/models`, unavailable = greyed "no key"), TTS (the existing voice picker + speed slider, relocated here).
- On load: fetch `/api/models` + `/api/voices`; restore `localStorage` selections (defaults `base` / `anthropic/claude-haiku-4-5` / `af_heart`); send the initial `set_stt`/`set_model`/`set_voice`.
- On change: persist + send the corresponding WS message. Unavailable LLM entries are `disabled` in the `<select>`.
- A small status line shows the active pipeline (e.g. "Whisper base · Gemini 2.0 Flash · af_heart").

## Error handling / degraded mode

- Unavailable models are `disabled` in the dropdown (can't be selected) — the "no key" case is prevented in the UI.
- If a chosen model fails server-side (revoked key, bad model id), the reply errors gracefully (existing error path), the browser shows a notice, and reverts the LLM dropdown to the last model that worked.
- A non-Anthropic provider that doesn't actually support streaming still works: `OpenAIProvider.completeStream` falls back to the base (whole-response) behavior on a non-SSE response, mirroring the Phase-1 voice-server JSON fallback.
- Switching STT size mid-session is safe (per-size model cache); first use of a new size incurs a one-time load.

## Testing

- **Unit (TS):** OpenAI SSE delta parser (text deltas assembled; `[DONE]`; a `tool_calls` delta surfaces; multi-chunk split); model-catalog availability (a model is `available` iff its provider key is present); `getAgentConfig` honors the model override.
- **Unit (Python):** `set_stt` stores size; the STT `/transcribe` call carries `X-Model-Size`.
- **Integration:** with keys present, switch to each keyed model and confirm a **streamed** reply (SSE deltas arrive incrementally); switch STT size and confirm transcription still works; switch TTS voice and confirm it speaks; an unavailable model shows greyed "no key".

## Out of scope

- Adding brand-new STT engines (only Whisper sizes).
- New TTS engines beyond Kokoro/Piper (voice picker unchanged).
- Provider auth flows / storing keys via UI (keys stay in `.env`/config).
- Per-message model switching mid-conversation history rewrites (switch applies to subsequent turns).
