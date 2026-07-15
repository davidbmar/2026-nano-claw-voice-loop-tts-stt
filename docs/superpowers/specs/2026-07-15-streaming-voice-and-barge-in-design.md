# Streaming Voice Replies + Barge-In — Design

**Date:** 2026-07-15
**Status:** Approved (design), pending implementation plan
**Builds on:** the Kokoro/Piper voice stack (`docs/superpowers/specs/2026-07-14-kokoro-tts-voice-selection-design.md`)

## Problem

Spoken replies feel slow to *start*. Two root causes:

1. **We wait for Claude's entire reply before any audio.** `src/api/server.ts::handleChat`
   runs `stepLoop(...)` to completion, then `sendJson`s the full text
   (`server.ts:279-280`); the provider does a single non-streaming
   `POST /chat/completions` (`base.ts:84`). TTS itself already streams
   sentence-by-sentence (`voice/webrtc.py::speak_text`), so the bottleneck is
   the round-trip wait for the *whole* LLM answer before the *first* sentence
   can be spoken.
2. **You can't interrupt Claude.** While Claude speaks, the browser mic is gated
   off (`agent_audio_start`/`agent_audio_end` mute VAD so Claude doesn't hear
   itself). There is no barge-in.

Measured context (this machine, warm): Kokoro synthesizes a sentence in ~0.15s
(RTF 0.07) — synthesis is *not* the bottleneck; the LLM wait and the missing
interrupt path are.

## Goals

1. **Stream Claude's output** so audio (and on-screen text) begins as soon as the
   first sentence exists, while Claude is still writing the rest.
2. **Barge-in:** keep the mic live during playback; when the user starts talking,
   pause immediately; if real speech follows it becomes the next turn; if it was
   a false alarm, resume the paused reply after a randomized exponential backoff.

## Scope decisions (agreed)

| Decision | Choice |
| --- | --- |
| Streaming scope | Full LLM→voice streaming (provider → API → voice server → TTS) |
| Transport (API ↔ voice server) | **SSE** (`text/event-stream`) |
| First-chunk granularity | Sentence boundary, **except** the first chunk also flushes after ~6 words so audio starts ASAP |
| Barge-in semantics | **Pause → confirm → resume-or-yield** (resume on false alarm after backoff) |
| Echo handling | Assume **speakers**; browser AEC (`echoCancellation`/`noiseSuppression`/`autoGainControl`) + VAD threshold above the residual-echo floor; backoff absorbs the occasional false trip |
| On-screen text | **Streams into the chat log** sentence-by-sentence, matching the audio |
| Provider without streaming | Graceful fallback to today's single-response path (never breaks) |
| Barge-in feature flag | `NANO_CLAW_BARGE_IN` env, **default off** (opt-in). When off, the mic stays gated during playback exactly as today — zero behavior change. The server surfaces the flag to the browser in `hello_ack` so the browser only keeps the mic live + runs barge-in logic when enabled. |

Build in **two phases**: Phase 1 (streaming) first — barge-in's "resume" is
cleaner to define once audio is a managed stream.

## Architecture

```
Phase 1 — streaming:
  provider.completeStream()  (base.ts, stream:true, SSE deltas)
     → stepLoop (server.ts) yields text deltas + tool_pending
        → GET/POST /api/chat  emits SSE: {delta}… {tool_pending} {done}
           → voice server reads SSE (httpx stream)
              → TextChunker buffers deltas → speakable chunks
                 → speak loop synthesizes each chunk → audio queue (unchanged)
                 → WS "agent_reply_delta" → browser fills chat bubble

Phase 2 — barge-in:
  browser mic stays LIVE during playback (AEC on) + VAD
     → VAD over threshold → WS "barge_in"
        → session.pause_speaking()  (hold the audio FIFO; cancel in-flight stream if user commits)
           → confirm window (~400ms):
                real speech  → WS mic_stop → discard queue → next turn
                false alarm  → resume_speaking() after Backoff.next() delay
```

## Phase 1 components

### `src/providers/base.ts` (+ concrete providers)
- Add `completeStream(messages, model, temperature, maxTokens, tools):
  AsyncIterable<StreamEvent>` alongside `complete()`. `StreamEvent` is one of
  `{type:'text', delta:string}`, `{type:'tool_calls', toolCalls:ToolCall[]}`,
  `{type:'done', finishReason, usage}`. Implemented with axios
  `responseType:'stream'` + `stream:true`, parsing OpenAI-style SSE
  (`data: {...}` lines, `[DONE]` sentinel). Providers that don't implement it
  inherit a base default that calls `complete()` once and yields a single
  `text` + `done` (transparent fallback).

### `src/api/server.ts`
- `stepLoopStream(memory, agentConfig, iteration): AsyncIterable<StreamEvent>`
  — a streaming sibling of `stepLoop` (`server.ts:109`) that drives
  `completeStream`. Text deltas pass through; a `tool_calls` event registers the
  pending request (same `pendingRequests` map) and ends the turn as a
  `tool_pending` event.
- `handleChat` (and `/approve`, `/reject`) detect a streaming request (e.g.
  `Accept: text/event-stream`) and write SSE: `event: delta`, `event:
  tool_pending`, `event: final` (carries the assembled text + debug), `event:
  error`. Non-streaming callers keep today's JSON path.
- Feature flag `NANO_CLAW_STREAM` (default on) to force the legacy path.

### `voice/text_chunker.py` (new, pure)
- `class TextChunker` with `push(delta:str) -> list[str]` (returns any
  now-complete speakable chunks) and `flush() -> str` (trailing remainder).
  Rule: emit on sentence-ending punctuation; for the **first** emission of a
  reply, also emit once ≥ `FIRST_CHUNK_WORDS` (~6) words have accumulated even
  without a boundary. Strips markdown via the existing `_clean_for_speech`
  logic (moved/shared).

### `voice/server.py` + `voice/webrtc.py`
- `_handle_agent_request` opens an SSE stream to nano-claw (`httpx`
  `client.stream("POST", …, headers={"Accept":"text/event-stream"})`), and for
  each `delta` feeds `TextChunker`; each returned chunk is (a) sent to the
  browser as `agent_reply_delta` and (b) synthesized+enqueued via the existing
  per-chunk path. `tool_pending`/`final`/`error` events map to the current WS
  messages. `Session.speak_text` is refactored into `enqueue_chunk(text,
  voice_id, speed)` (synth one chunk → queue) reused by both the streaming loop
  and any remaining whole-text callers.

### `voice/web/app.js`
- New WS `agent_reply_delta {text}` appends to the current agent bubble
  (create-on-first-delta, append thereafter); `agent_reply`/`final` finalizes.
  Audio behavior is unchanged (it already streams from the queue).

## Phase 2 components

### `voice/backoff.py` (new, pure)
- `class Backoff(base=0.5, factor=2.0, cap=8.0)` with `next() -> float`
  (returns a full-jitter delay `random.uniform(0, min(cap, base*factor**n))`
  and increments `n`) and `reset()`. `n` grows per consecutive false alarm,
  resets after a clean uninterrupted resume. (Randomness lives here, seeded from
  the stdlib — this module is Python, not a workflow script, so `random` is
  available.)

### `voice/webrtc.py`
- `pause_speaking()` — stop draining the audio FIFO (the WebRTC source yields
  silence/holds) without clearing it. `resume_speaking()` — continue draining.
  `cancel_stream()` — abort the in-flight SSE read + synthesis and clear the
  queue (used when the user commits a real barge-in). Session owns a `Backoff`
  instance and the paused/greeting state.

### `voice/server.py`
- WS handlers: `barge_in` → `session.pause_speaking()` + start a confirm timer;
  `barge_in_commit` (browser confirmed real speech) → `session.cancel_stream()`
  then proceed to capture the user turn; timer expiry with no commit (false
  alarm) → `session.resume_speaking()` after `session.backoff.next()` seconds.
  A clean full drain calls `backoff.reset()`.

### `voice/web/app.js` + `voice/web/phone-vad.js`
- `getUserMedia({audio:{echoCancellation:true, noiseSuppression:true,
  autoGainControl:true}})`. Keep the mic/VAD **live during agent playback**
  (remove the hard gate; raise the VAD start threshold to sit above the residual
  echo). On VAD `speech_start` during playback → send `barge_in` immediately;
  if VAD sustains speech past the confirm window → send `barge_in_commit` and
  capture the turn; if VAD returns to silence within the window → do nothing
  (server resumes on its timer).

## Error handling / degraded mode

- Provider/stream unsupported or `NANO_CLAW_STREAM=0` → legacy single-response
  path; voice server detects a JSON (non-SSE) response and speaks it whole.
- SSE breaks mid-stream → already-synthesized chunks finish playing; send an
  `error`/notice; do not crash the loop.
- Kokoro unavailable mid-stream → the existing per-chunk Piper fallback still
  applies (Phase-1 reuses `synthesize`, which already falls back).
- Barge-in echo false triggers → absorbed by the backoff (its whole purpose);
  persistent noise backs off toward the cap instead of stuttering.
- Tool approval mid-stream → stream ends at `tool_pending`; approve/reject opens
  a fresh stream; barge-in is disabled while a tool card is pending.

## Testing

- **Unit (Python):** `TextChunker` (first chunk flushes at ~6 words; later
  chunks only at boundaries; markdown stripped; `flush` returns remainder);
  `Backoff` (grows ×factor, jittered ≤ cap, `reset` returns to base).
- **Unit (TS):** SSE delta parsing in `completeStream` (text deltas assembled;
  `[DONE]`; a tool_calls delta surfaces as `tool_calls`); `stepLoopStream`
  yields deltas then `tool_pending` on a tool call.
- **Integration:** a streamed reply produces the first `agent_reply_delta` +
  first audio chunk before the full text is available; `barge_in` pauses the
  FIFO; a `barge_in_commit` discards it and starts capture; a false alarm
  resumes after the backoff delay.
- **Manual:** talk over Claude on speakers (AEC) and with headphones; confirm
  no self-trigger stutter; long reply starts speaking within ~1 first-sentence.

## Out of scope

- Word-level (sub-sentence) streaming beyond the first-chunk eager flush.
- Changing the STT path or the agent's tool set.
- Echo cancellation beyond what the browser provides (no server-side AEC/DSP).
- Persisting/replaying interrupted replies across turns.
