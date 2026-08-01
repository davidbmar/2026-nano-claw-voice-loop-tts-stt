# NanoClaw Speech Delivery Phase 0

Status: First vertical slice implemented; superseded by the Phase 2 speech compiler  
Date: 2026-07-21  
Source architecture: `../../../intelligence-platform/docs/riff-speech-preparation-and-tts-architecture.md`  
Scope: NanoClaw browser voice path, `text_only` fidelity tier

## Outcome

NanoClaw now has the correctness boundary required before speech rewriting is introduced:

- one `Session` allocates monotonically increasing playback generations;
- synthesis output is admitted only when its generation is still current;
- manual stop and confirmed barge-in invalidate the generation before clearing audio;
- WebRTC and WebSocket transports confirm the bytes handed across their transport boundary;
- playback emits chunk-level `completed`, `partial`, or `not_delivered` receipts;
- normal agent history is completed only after a complete delivery receipt;
- the browser flushes cancelled audio and ignores mismatched terminal events.

This is deliberately not the speech compiler yet. It establishes the lifecycle and measurement
semantics that deterministic normalization and chunk planning must use.

## Existing path found during Phase 0

```text
Node agent SSE or JSON response
        |
        v
voice/server.py
  TextChunker -> worker-thread TTS call -> Session admission
        |
        v
voice/webrtc.py Session
  one AudioQueue + one active PlaybackToken
        |
        +--------------------------+
        |                          |
        v                          v
WebRTCAudioSource.recv()      WsAudioTransport._pump()
  48 kHz PCM frame             48 kHz PCM frame
        |                          |
        v                          v
aiortc/browser audio          WebSocket -> AudioWorklet ring buffer
```

The WebSocket PCM path is the default when `NANO_CLAW_WS_AUDIO=1`. The microphone remains 16 kHz
for STT and agent playback remains 48 kHz. The WebRTC path uses 48 kHz in both directions.

## Current engine and configuration baseline

| Item | Observed implementation |
|---|---|
| Lux endpoint | `POST /synthesize` on port 8301 |
| Lux output | Whole-request, mono PCM16 at 48 kHz |
| Lux repository pin | `28ae6a61151684fffc9d1a7aa15eafa02286fe0b` |
| Hugging Face revision | `527f245a276a0eb42ea103a7a512bcfd771eb9b6` |
| Runtime | Native Python/PyTorch service; MPS or CPU selection; background prewarm |
| Lux controls in use | voice, speed; fixed four steps, guidance 3.0, time shift 0.5 |
| Lux streaming | No incremental model audio; each text chunk returns one complete PCM body |
| TTS fallback | Piper when Lux is unavailable or invalid |
| Sentence pause | 240 ms of PCM silence by default after `.`, `!`, or `?` |
| Browser buffer | One AudioWorklet ring buffer with 150 ms initial lead |

The MLX runtime is not the current execution path. Changing runtime may change latency and memory,
but it does not by itself improve wording, pronunciation, or prosody.

## Defect confirmed by inspection

Before this slice, `server.py` ran `Session.enqueue_chunk` in an executor. That method both
synthesized and enqueued PCM. A stop could clear the queue while the blocking Lux call was still
running; when the worker returned, it could enqueue the cancelled answer again. Pause/resume or a
later generator attachment could then make that stale audio audible.

The fix separates asynchronous work from playback authority:

```text
worker: synthesize(text) -> PCM tagged by captured PlaybackToken
event/session boundary: if token == active token, enqueue; otherwise drop
```

Task cancellation remains an efficiency mechanism. The generation check is the correctness
mechanism because a local model, HTTP request, or executor worker may finish after cancellation.

## Implemented identity and receipt contract

`PlaybackToken` carries:

```json
{
  "conversation_id": "voice-...",
  "utterance_id": "utt-...",
  "generation": 7
}
```

Every admitted chunk records sequence, byte range, and `audio_role`. NanoClaw's first slice is
`text_only`, so receipts intentionally contain an empty `acts` array.

```json
{
  "event_type": "utterance_delivery_receipt",
  "conversation_id": "voice-...",
  "utterance_id": "utt-...",
  "generation": 7,
  "status": "partial",
  "reason": "confirmed_barge_in",
  "chunks": [
    {
      "chunk_id": "chunk_0",
      "sequence": 0,
      "audio_role": "answer",
      "status": "partial",
      "played_audio_ms": 240
    }
  ],
  "acts": []
}
```

The partial boundary is exact for this implementation:

- `partial`: the transport confirmed at least one byte from a chunk, but not the whole chunk;
- `not_delivered`: the transport confirmed no bytes from the chunk;
- `finished`: the transport confirmed the chunk's entire admitted byte range.

For WebRTC, confirmation means the frame was produced for aiortc. For WebSocket audio, it means
`send_bytes` completed. Neither is proof that a human perceived every phoneme; it is the closest
deterministic boundary exposed by the current transports and must be labeled accordingly.

## Browser lifecycle behavior

`agent_audio_start` and `agent_audio_end` now include utterance ID and generation when the modern
session path is used. The browser tracks the active generation. A normal `completed` end stops
accepting network frames but lets already buffered audio finish. A partial or undelivered end
flushes the AudioWorklet immediately. An end event for a different generation is ignored.

The raw WebSocket PCM packets do not yet include an application header. WebSocket message ordering
plus the server-side generation admission fence prevents a cancelled worker from producing new
stale packets, and the terminal control event flushes packets already buffered in the browser.
Per-frame wire identity remains a follow-up hardening option if stress tests expose cross-event
ordering or proxy behavior that violates this assumption.

## Observability available now

Existing turn metrics already record STT latency, LLM first-token latency, model duration,
first-audio latency, TTS-to-first-audio time, and end-to-end time. This slice adds privacy-safe
playback logs containing:

- utterance ID and generation;
- final delivery status and reason;
- confirmed and enqueued byte counts;
- late-generation drop count;
- chunk sequence, role, status, and played duration in WebSocket receipts.

No raw transcript is added to these lifecycle logs.

## Verification evidence

The focused regression command is:

```bash
.venv-test/bin/python -m pytest -q \
  tests/python/test_playback_generation.py \
  tests/python/test_ws_audio.py \
  tests/python/test_deep_voice.py \
  tests/python/test_history_api.py \
  tests/python/test_voice_flow.py
```

It covers:

- the exact delayed-synthesis-after-cancel race;
- monotonic generation allocation;
- stale generator silence;
- partial versus undelivered receipts;
- fully confirmed delivery;
- WebSocket and WebRTC transport behavior;
- deep-analysis speech behavior;
- delivery-gated history persistence;
- existing scheduler and voice-flow regressions.

Browser and TypeScript checks:

```bash
node tests/ws-audio-player.test.mjs
node --check voice/web/app.js
npm run build
```

## Live measurement protocol

Static inspection and deterministic tests are complete. Do not invent production percentile
numbers. Capture them on the deployed NanoClaw path as follows:

1. Restart with WebSocket audio enabled and the intended Lux voice selected.
2. Warm Lux with one non-scored utterance.
3. Run at least 30 ordinary answers and 15 deep-analysis projections from the representative
   prompt corpus.
4. For five turns, press Stop while Lux is still synthesizing; for five more, commit barge-in
   during playback.
5. Confirm there are no frames or words from an invalidated generation after its terminal event.
6. Export aggregate latency and receipt counts without transcript text.
7. Report p50 and p95 for first audio, TTS first chunk, completed playback, cancellation latency,
   and late-event drops, separated by voice engine and warm/cold state.

## Phase 0 exit status

| Gate | Status |
|---|---|
| Existing path and exact engine/runtime identified | Complete |
| Current static controls and audio formats identified | Complete |
| Generation authority and atomic admission implemented | Complete |
| Hard stop cannot be refilled by a late TTS worker | Complete in deterministic race test |
| Chunk delivery receipts emitted | Complete for `text_only` browser paths |
| Planned response withheld from history after partial delivery | Complete for streamed and JSON agent paths |
| Full live prompt-corpus baseline | Pending deployment measurement |
| Zero audible stale frames under production stress | Pending deployment stress run |

## Next implementation slice

Implemented on 2026-07-22. See
[`2026-07-22-speech-preparation-phase2.md`](2026-07-22-speech-preparation-phase2.md).

The completed deterministic `text_only` slice includes:

1. protect canonical numbers, dates, times, prices, phone numbers, and identifiers;
2. normalize the high-value forms with deterministic rules;
3. compile semantic chunks with stable IDs and pause policies;
4. use the existing generation token and receipt path unchanged;
5. compare raw versus prepared turns using the same TTS engine and voice.

No second LLM rewrite was added. The existing main model receives a voice-output contract, while
the compiler remains deterministic and preserves the raw rollback.
