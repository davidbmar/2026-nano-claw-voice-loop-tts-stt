# Codex implementation: NanoClaw speech delivery Phase 0/1 foundation

- Author: Codex
- Recipient: Claude (Fable 5) and David
- Date: 2026-07-21
- handoff_id: `2026-07-21-speech-delivery-phase0-codex-implementation`
- in-reply-to: `2026-07-21-riff-speech-tts-claude-verification`
- task_id: `riff-speech-tts-nanoclaw-phase0`
- status: `implementation_slice_complete`
- commit: none; this is an uncommitted shared worktree with earlier unrelated changes preserved

## Summary

The Fable-approved design received its six-item editorial polish pass and a self-contained HTML
rendering in the Intelligence Platform repository. The first NanoClaw implementation slice is
also complete: generation-fenced PCM admission, terminal hard cancellation, transport-confirmed
chunk delivery receipts, delivery-gated agent history, and generation-aware browser lifecycle
events.

This slice intentionally remains `text_only`. It makes structural and playback claims, not typed
semantic-act fidelity claims. Deterministic text normalization and speech-plan compilation are the
next feature-flagged slice after live baseline capture.

## Changes

NanoClaw:

- `voice/types.py`: added immutable `PlaybackToken`; added frame identity/accounting fields.
- `voice/audio/audio_queue.py`: added reads that distinguish real payload from silence padding.
- `voice/webrtc.py`: made `Session` the sole generation allocator; separated synthesis from
  atomic admission; added hard cancellation, stale-work rejection, chunk byte ranges, and
  delivery receipts.
- `voice/audio/webrtc_audio_source.py` and `voice/ws_audio.py`: confirm only transport-handed
  payload bytes and reject stale generators.
- `voice/server.py`: carries utterance/generation on lifecycle events, emits delivery receipts,
  labels answer versus processing-earcon chunks, and defers history completion until confirmed
  complete delivery for streamed, JSON, and scheduler replies.
- `voice/web/ws-audio-player.js` and `voice/web/app.js`: distinguish normal end from cancellation,
  flush partial/cancelled playback, reject frames outside an active lifecycle, and ignore terminal
  events for another generation.
- `tests/python/test_playback_generation.py`: exact late-worker race, monotonicity, stale silence,
  receipt boundaries, and post-delivery stop coverage.
- `tests/python/test_history_api.py`: proves a partially heard reply is not saved as a completed
  agent turn.
- `docs/design/2026-07-21-speech-delivery-phase0.md`: existing-path inventory, pinned Lux facts,
  receipt boundary, measurement protocol, and honest exit status.

Intelligence Platform:

- `docs/riff-speech-preparation-and-tts-architecture.md`: applied all six approved polish items.
- `docs/html/riff-speech-preparation-and-tts-architecture.html`: generated self-contained render.
- `docs/html/index.html` and `README.md`: linked the approved architecture.

## Evidence

Focused NanoClaw regression:

```text
47 passed in 1.43s
```

Command:

```bash
.venv-test/bin/python -m pytest -q \
  tests/python/test_playback_generation.py \
  tests/python/test_ws_audio.py \
  tests/python/test_deep_voice.py \
  tests/python/test_history_api.py \
  tests/python/test_voice_flow.py
```

Additional green checks:

```text
npm run build
node --check voice/web/app.js
node tests/ws-audio-player.test.mjs
git diff --check
```

The full Python suite reached `335 passed, 1 skipped, 1 failed`. The sole failure is the existing
voice-catalog expectation for `af_heart` while this shared worktree is intentionally configured
to return `lux_heart`; it is outside this slice and was not changed here.

The repository's aggregate `npm test -- --run` is not a clean all-JavaScript check: Vitest treats
self-executing `.mjs` scripts as empty suites. Its actual Vitest suites reported 98 passing tests.
Three standalone source-contract scripts also retain quote/marker expectations in unrelated
sections of `app.js`; those lines were not changed by this slice. The directly affected
AudioWorklet script passes when run through its intended `node` command.

HTML verification:

- all five files in `intelligence-platform/docs/html` parse with Python's `HTMLParser`;
- every local link in those HTML files resolves;
- both JSON examples in the NanoClaw Phase 0 note parse;
- the Phase 0 source-design path resolves to the approved Intelligence Platform document.

## Known limits

- Production p50/p95 numbers and audible stale-frame stress results have not been invented; the
  Phase 0 note gives the exact deployed measurement protocol.
- WebSocket PCM packets remain raw PCM without a per-frame application header. Server admission,
  ordered WebSocket lifecycle events, and browser flush semantics provide the current fence.
  Add wire headers only if deployment stress exposes a real ordering gap.
- Transport confirmation means handed to aiortc or accepted by `send_bytes`, not proven human
  perception.
- `acts` remains empty in NanoClaw. Typed act delivery is reserved for Riff FSM/template adoption.
- No LLM speech rewrite or TTS-engine replacement was added.

## NEXT

Deploy this slice to a controlled NanoClaw instance and execute the live measurement protocol in
`docs/design/2026-07-21-speech-delivery-phase0.md`. If the stale-audio stress gate is clean, begin
the disabled-by-default deterministic `text_only` compiler: protected values, high-value text
normalization, semantic chunk IDs, and pause calibration against the same Lux voice.
