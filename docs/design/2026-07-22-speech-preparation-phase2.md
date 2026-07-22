# NanoClaw Speech Preparation Phase 2

Status: Deployed; public voice-path verification passed  
Date: 2026-07-22  
Compiler version: `nanoclaw-speech-v1`  
Source architecture: `../../../intelligence-platform/docs/riff-speech-preparation-and-tts-architecture.md`

## Outcome

NanoClaw now prepares complete model responses for listening before sending them to Lux. The
feature is enabled by default for browser and phone audio, with an immediate Prepared/Raw toggle
in the browser console and environment rollback for both paths.

```text
Node agent (voice response contract)
        |
        v
complete text_only response
        |
        v
deterministic speech compiler
  visual cleanup -> normalization -> semantic chunks -> pause plan
        |
        v
existing generation fence -> Lux -> browser or Telnyx
```

The main conversational model owns what to say. The deterministic compiler owns only safe spoken
rendering and delivery boundaries. It does not add a second model call and does not claim typed-act
semantic fidelity.

## Implemented behavior

- `responseMode: "voice"` gives the main model a concise spoken-output contract: conclusion first,
  short sentences, no Markdown or visual citation syntax, two or three principal points, and one
  primary question at a time.
- `voice/speech_preparer.py` compiles the complete response into ordered `SpeechChunk` records.
- Markdown headings, list markers, internal citations, raw URLs, code styling, and stray hashes do
  not reach TTS. A meaningful `#2` becomes “number two,” eliminating Lux's “pound” rendering.
- High-value dates, times, money, percentages, ranges, and phone numbers receive deterministic
  en-US spoken forms.
- A curated set of voice/business acronyms expands to natural phrases. Live Lux/Whisper checks
  showed “search engine optimization” is more reliable than asking Lux to spell “S E O.”
- Ordinary sentence boundaries use 280 ms, list/topic transitions use 320 ms, and clause
  continuations use 140 ms where appropriate.
- Every final chunk includes a 140 ms PCM safety tail. A live Lux-to-Whisper test lost the last word
  without this tail and preserved it with the tail.
- Browser and phone synthesis retain existing one-generation-at-a-time admission, cancellation,
  delivery receipts, and raw fallback behavior.
- The browser reports `nanoclaw-speech-v1`, the active mode, chunk count, and normalization count.

## Contracts and guarantees

The current plan declares:

```json
{
  "actsProvenance": "text_only",
  "guaranteeLevel": "text_structural",
  "compilerVersion": "nanoclaw-speech-v1",
  "normalizerVersion": "en-us-rules-v1"
}
```

These are structural guarantees only. The source model response remains authoritative. Riff's FSM
must adopt typed speech acts before the system can claim typed-act coverage or use act-level
delivery receipts.

No transcript or prepared text is included in public plan metadata. Existing transcript and
conversation-retention policies remain unchanged.

## Streaming decision

Prepared mode uses a complete response before compilation. Model deltas still update the visual
transcript immediately, but ordinary answer audio begins once the authoritative final response is
available and its first chunk has been synthesized. Deep analysis retains its immediate spoken
acknowledgement and quiet progress earcons.

This avoids reconciling already-spoken speculative chunks with a later final envelope. Raw mode
retains the older incremental sentence path for comparison and emergency rollback.

## Configuration and rollback

```text
NANO_CLAW_SPEECH_PREPARATION=1
NANO_CLAW_PHONE_SPEECH_PREPARATION=1
NANO_CLAW_SPEECH_MAX_WORDS=18
NANO_CLAW_SPEECH_MAX_CHUNK_MS=2500
```

Set either preparation flag to `0` for raw mode. In the browser, turn off **Natural delivery** under
Text-to-Speech to compare the paths without a restart. The model still writes for voice in both
modes, so the toggle isolates deterministic preparation rather than changing answer content.

The privacy-safe deployment endpoint is:

```text
GET /api/voice/version
```

## Verification

- Focused compiler, TTS, phone, generation, and browser tests pass.
- All 100 TypeScript Vitest tests pass and `npm run build` succeeds.
- The Python suite passes after aligning its stale voice-catalog assertion with the product's
  deliberate `lux_heart` default.
- A representative wall-of-text Lux probe produced its first prepared chunk in 468 ms versus
  2,067 ms for one paragraph-sized Lux request. This is a synthesis-only sample, not a production
  latency percentile.
- Lux-to-Whisper round trips contain no hash or “pound” token after preparation.

### Public deployment, 2026-07-22

- `https://nano.chattychapters.com/api/voice/version` reports app version `0.2.1`, speech
  compiler `nanoclaw-speech-v1`, and default mode `prepared`.
- A public WebSocket turn using DeepSeek V4 Flash and Lux George advertised the same compiler,
  produced a two-chunk deterministic speech plan with two normalizations, streamed 1,006,080
  bytes of 48 kHz PCM, and ended with `completed / playback_finished`. Both planned chunks had
  `finished` delivery receipts.
- A public normalization and tail-safety turn compiled five spoken-form normalizations and
  streamed 11.24 seconds of audio. Every acknowledgement, cue, and answer chunk finished.
- An independent Whisper Base listen-back heard “number two,” not “pound,” and retained the final
  phrase “customer acquisition costs,” including the final word.
- A realistic “think deeply about the three biggest weaknesses” turn moved through three distinct
  public progress states: queued, reasoning pass 1 with all five retrievals completed and 15
  evidence items, then completed with its artifact indexed. The answer stayed at 55 words including
  the acknowledgement, compiled into five planned chunks, delivered plan sequences 0 through 4,
  and ended `completed / playback_finished`. This directly covers the prior failure where the UI
  reached “Deep analysis complete” without a spoken answer.
- The public HTML and JavaScript expose the Natural delivery toggle, `v3` safer barge-in
  preference, and Stop audio control.
- The public masthead is branded `HYPERRIFF` and exposes the running application version in a
  small adjacent badge. Version `0.2.1` also clarifies that the deep-analysis pass count is
  conditional: it says a pass is in progress, distinguishes backend heartbeats from reasoning
  progress, and describes the configured maximum as “up to ... passes if needed.”
- The deployed image is tagged `nano-claw-voice:natural-speech-v1-20260722`; the immediately prior
  image remains tagged `nano-claw-voice:pre-natural-speech-20260722` for rollback.

The measured first-audio time for the ordinary two-sentence public turn was 5,982 ms, including
model generation and the complete-envelope wait. The explicit deep-analysis acknowledgement began
in 1,047 ms in the normalization probe. Prepared mode currently optimizes delivery quality and
correctness over speculative speech; reducing ordinary-answer time to first audio requires a
separately specified safe early-chunk contract.

## Barge-in safety

The browser's experimental barge-in preference was versioned to `v3`, resetting prior opt-ins to
the recommended off state. Re-enabled barge-in now requires 180 ms of voiced evidence after a
150 ms echo guard, within a 650 ms confirmation window. Stop audio remains an immediate hard stop.

This reduces accidental cancellation from speaker echo; it is not a full acoustic echo-cancellation
model. Open-speaker interruption should remain opt-in until double-talk tests justify a safer
default.
