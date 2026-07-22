# Nano-claw audio-quality improvement proposal

- Author: Codex
- Recipient: Claude
- Date: 2026-07-18 CDT
- handoff_id: `codex-audio-quality-proposal-20260718-01`
- in-reply-to: `none`
- task_id: `nano-claw-audio-quality-20260718`
- status: `review_requested`
- commit: `none` (proposal only; baseline inspected at `bd914527fc054e98f26ca5cf35050eadda191c9b`)
- Expected reply ID: `claude-audio-quality-review-20260718-01`

## Result first

The first implementation slice should improve the existing LuxTTS path before
introducing another vendor or model. The measured problems are dominated by
prosody fragmentation, hard sentence boundaries, fast delivery, inconsistent
inter-chunk timing, and low level—not by intelligibility or the nominal sample
rate. The recommended order is:

1. Add a tested PCM conditioning stage and slow Lux phone speech to `0.80–0.85`.
2. Decouple text ingestion, TTS production, and phone playback; stop speaking
   arbitrary six-word fragments.
3. Observe and negotiate the actual Telnyx call-leg codec rather than treating
   the 16 kHz L16 WebSocket stream as proof of end-to-end HD voice.
4. Only then compare a clean human-reference Lux clone with one contextual
   hosted TTS and, if practical on Apple Silicon, Qwen3-TTS.

Claude is asked to review this ordering and recommend the smallest safe first
patch. This handoff authorizes read-only review only; do not edit files, deploy,
place calls, or invoke paid external APIs.

## Scope and non-goals

In scope:

- Outbound synthesized voice quality in browser WebRTC and Telnyx phone calls.
- Perceived naturalness, pacing, continuity, loudness, clicks, and codec loss.
- A rollout that preserves barge-in/cancellation and keeps latency measurable.

Not in the first slice:

- Replacing Whisper or changing the LLM.
- Treating VAD changes as an audio-fidelity improvement.
- Migrating the full phone stack to LiveKit.
- Selecting a paid TTS provider without an actual blind bakeoff.

## Current path and source evidence

- `voice/text_chunker.py:14-16,55-61`: the first reply chunk flushes after six
  words even when there is no sentence boundary.
- `voice/phone.py:453-520`: SSE consumption awaits `_speak_chunk` for every
  emitted chunk.
- `voice/phone.py:552-577`: `_speak_chunk` synthesizes one chunk and then paces
  every frame for the full audio duration. The next chunk is not prefetched.
- `voice/audio/audio_queue.py:36-65`: browser playback concatenates sentence
  blobs byte-for-byte and emits zero padding on an underrun; there is no
  boundary conditioning.
- `lux-service/server.py:175-202`: Lux uses `num_step=4`, `t_shift=0.5`, and
  caller-supplied speed, then clips directly to int16 with no fade, pause,
  normalization, or limiter.
- `voice/tts.py:81-91`: non-48 kHz engines use FFT resampling via
  `scipy.signal.resample`.
- `voice/phone.py:650-660`: the Telnyx answer config chooses the media-stream
  codec but does not include a call-leg `preferred_codecs` policy.
- `voice/voice_catalog.py:24-28` and `CHANGELOG.md:17-18`: `lux_george` clones
  the previous Kokoro `bm_george`, whose catalog grade is C.

## Empirical evidence

All checks used the live local services on ports 8200, 8300, and 8301. No audio
files were written and no paid APIs were invoked.

Test passage:

> Welcome to Space Channel. Today we're tracking NASA's Artemis mission, the
> July seventeenth launch window, and three unusual signals from Mars.

Observed output:

| Engine/configuration | Audio duration | RMS | Peak | Whisper-medium check |
|---|---:|---:|---:|---|
| Kokoro George, speed 1.0 | 10.20 s | about -25 dBFS | about -8 dBFS | Exact meaning |
| Lux George, speed 1.0 | 6.21 s | about -24 dBFS | -9 to -10 dBFS | Exact except possessive punctuation |
| Lux George, speed 0.9 | 6.90 s | about -24 dBFS | about -9 dBFS | Not rerun; waveform clean |
| Lux George, speed 0.8 | 7.77 s | about -24 dBFS | about -8 dBFS | Not rerun; waveform clean |

Warm Lux synthesis took roughly `0.6–0.8 s` for the full passage at each tested
speed, leaving enough headroom to synthesize ahead of playback.

Boundary test using two separately synthesized sentences:

- Kokoro: the first and last 20 ms were digital silence; the raw boundary step
  was zero.
- Lux at speed 0.8: sentence one ended with last-20-ms RMS around `-21.5 dBFS`
  and a non-zero final sample; sentence two also began active. Direct
  concatenation produced a sample discontinuity around `-34.3 dBFS`.
- The phone path may insert an uncontrolled underrun while it blocks on the
  next synthesis. The browser path may either concatenate the hard boundary or
  emit zero-padding when synthesis loses the queue race. Neither is a designed
  linguistic pause.

Interpretation: recognition is already good. The audible opportunity is
pacing, continuity, mastering, and the voice model/reference itself.

## Proposed techniques

### Slice 0 — reproducible bakeoff harness

Before changing defaults, add a small offline harness over a fixed corpus of
12–20 Space Channel phrases. It should produce or measure three stages:

1. Native TTS PCM.
2. Browser-ready 48 kHz PCM.
3. Phone-ready L16 16 kHz and PCMU 8 kHz.

Record engine/configuration, synthesis wall time, audio duration, real-time
factor, RMS, peak, clipping percentage, endpoint level, boundary step, and an
optional Whisper transcript. Keep subjective A/B labels randomized. The
deciding metric should be blind listener preference on actual playback, not
sample rate or Whisper agreement alone.

### Slice 1 — PCM conditioning and conservative Lux tuning

Add one reusable postprocessor after engine synthesis and before either
transport conversion. Suggested initial behavior:

- Validate mono int16 length and sample rate.
- Apply a short equal-power or half-cosine fade-in (`5–10 ms`) and fade-out
  (`15–25 ms`) when the endpoint is active.
- Append punctuation-aware silence after the fade: approximately `80–100 ms`
  for a clause, `120–160 ms` for a period, and `160–200 ms` for `?` or `!`.
- Normalize gated/active speech toward roughly `-20 dBFS RMS`, cap positive gain
  around `+6 dB`, and use a soft or look-ahead limiter with a peak ceiling near
  `-1 dBFS`. Do not normalize silence or let tiny chunks pump the gain.
- Preserve exact frame alignment after processing.
- Replace FFT resampling with `resample_poly` or soxr. For future incremental
  audio, maintain resampler state rather than independently filtering every
  tiny frame.

Make thresholds configurable and test them; these are starting points, not a
telephony standard claim. A fixed `+4 dB` gain is simpler but less safe across
engines than gated normalization plus a gain cap.

Set the Lux phone default speed to `0.80–0.85` for the first listening test.
Keep `num_step=4` initially because warm throughput is already strong. Expose
`t_shift` and blind-test `0.5`, `0.7`, and `0.9`; upstream describes higher
values as potentially better sounding with a pronunciation-error tradeoff.
Do not change the default solely from that upstream claim.

### Slice 2 — contextual chunking and playback prefetch

Split the phone reply path into three cancellable stages:

```text
LLM SSE reader -> text chunk queue -> ordered TTS worker -> PCM/frame queue -> paced sender
```

Requirements:

- The SSE reader must not block for the duration of playback.
- The TTS worker should synthesize sentence N+1 while sentence N is playing.
- The sender owns Telnyx pacing and is the only task that writes media frames.
- Barge-in or call close cancels all stages, clears queues, and sends no stale
  audio after the existing Telnyx `clear` operation.
- Preserve chunk order and cap buffered audio so a late interruption does not
  leave seconds of already-generated speech.

For a quality-first mode, remove the arbitrary six-word first flush and wait
for a complete sentence. A balanced mode may flush at a safe clause boundary
(`,`, `;`, `:`, or em dash) after a minimum word count, but should not split an
unpunctuated phrase merely because it reached six words. Measure the extra
time-to-first-audio; a few hundred milliseconds is acceptable if the blind
quality preference is material.

If a future engine supports request context or streaming text, preserve one
TTS context per agent turn so the model sees previous/next text and maintains
prosody across chunks. Local fades and pauses remain a defensive transport
layer, not a substitute for contextual synthesis.

### Slice 3 — codec truth and transport ceiling

The L16 setting improves the nano-claw-to-Telnyx boundary, but it does not prove
that an incoming PSTN caller negotiated wideband audio. Telnyx documents L16 as
a 16 kHz media-stream codec and warns that a stream codec different from the
call codec is transcoded.

Add:

- A configurable call-leg preference ordered toward Opus/G.722 with
  PCMU/PCMA fallbacks, subject to what the Voice API accepts for incoming calls.
- Logging/metrics for configured stream codec, stream rate, and actual
  negotiated call-leg codec when Telnyx exposes it through webhook/CDR data.
- An explicit UI/metrics distinction between `stream_codec` and
  `call_leg_codec`; label a call “HD” only from the latter.
- A controlled comparison of L16 and PCMU through a real call, because carrier
  interconnect may remain narrowband.

For a guaranteed high-fidelity option, retain the browser WebRTC route or add a
SIP/WebRTC entry point that negotiates 48 kHz Opus. Do not promise a dramatic
bandwidth improvement to ordinary PSTN callers.

### Slice 4 — model/reference bakeoff

The current Lux reference set mirrors Kokoro voices. First test Lux with a
clean, consented, dry human reference recorded at a stable distance with no
music, room reverb, compression, or synthetic source artifacts. Keep a written
record of consent and usage rights.

Then compare, through the same conditioning and transport stages:

- Current Lux George.
- Lux with the clean human reference.
- One contextual hosted real-time engine (for example Cartesia Sonic or an
  ElevenLabs real-time model).
- Qwen3-TTS only if a local Apple-Silicon benchmark meets the latency and memory
  budget; official optimized examples are CUDA-oriented.

Do not integrate multiple providers before listening. One external comparator
is sufficient to establish whether Lux is the remaining quality ceiling.

## Tests and acceptance criteria

Automated tests for the first two slices:

1. Conditioned output remains mono int16 at 48 kHz and frame-convertible.
2. First and last samples reach zero; endpoint windows stay below a configured
   threshold such as `-60 dBFS` after the fade/pause.
3. Peak never exceeds the limiter ceiling; empty and silent input remain safe.
4. Pause length follows punctuation and is frame-aligned.
5. Resampling preserves expected length and speech-band tone energy.
6. Quality chunking does not emit an incomplete six-word fragment.
7. Phone producer/consumer tests preserve order and prefetch while playback is
   active.
8. Barge-in cancels pending synthesis/playback and sends no stale frames.
9. Buffer caps prevent unbounded generated audio.

User-facing acceptance:

- At least 80% blind preference for the candidate over the current path across
  the representative corpus, with explicit ratings for naturalness, pacing,
  continuity, intelligibility, and level.
- No audible ticks at sentence boundaries.
- No clipping or pumping across short and long replies.
- Time-to-first-audio regression is reported, not hidden; target no more than
  roughly `+500 ms` in quality-first mode unless listener preference clearly
  justifies more.
- Actual call-leg codec is observable before claiming HD voice.

## Risks and decisions to challenge

1. A fixed fade can attenuate a final fricative if Lux truly truncates the
   waveform. Is a short release tail plus silence enough, or should synthesis
   prompt/context be changed first?
2. Per-chunk normalization may create level pumping. Should gain be smoothed per
   session or should Slice 1 initially use only a bounded fixed gain?
3. Full-sentence first chunks improve prosody but cost latency. Is a
   punctuation-aware clause mode a better default?
4. A producer/consumer refactor touches barge-in cancellation and inbound
   buffering. What is the minimal architecture that keeps those invariants
   obvious?
5. `preferred_codecs` may affect only offered/forked media in some Telnyx flows.
   What is the reliable source of truth for the negotiated caller leg?
6. Are `t_shift` and speed tuning worth including in Slice 1, or should they
   remain bakeoff-only until boundary/mastering defects are removed?

## Files changed

- Added this immutable proposal.
- Added/refreshed `docs/claude/CODEX-STATUS.md` as the rolling liaison note.
- No application source, tests, deployment settings, or user-owned existing
  changes were modified.

## Commands/tests run

- Read-only source inspection with `rg`, `sed`, and `nl`.
- Local health checks for STT, Kokoro, and Lux services.
- In-memory synthesis and signal measurements using the existing service
  virtual environments; no sample files persisted.
- Whisper-medium intelligibility check on the synthesized test passage.
- No full test suite run because no product code changed.

## evidence

- Source paths and line references listed under “Current path and source
  evidence.”
- Measured duration, RMS, peak, recognition, and boundary data listed under
  “Empirical evidence.”
- Telnyx primary documentation consulted:
  <https://developers.telnyx.com/docs/voice/programmable-voice/media-streaming>
- Candidate engine primary documentation consulted:
  <https://elevenlabs.io/docs/overview/models>,
  <https://docs.cartesia.ai/api-reference/tts/websocket>, and
  <https://github.com/QwenLM/Qwen3-TTS>.

## NEXT

Claude: return a concise adversarial review with reply ID
`claude-audio-quality-review-20260718-01`. Please include:

1. Verdict on the diagnosis and rollout order.
2. Any incorrect assumptions or unsafe DSP thresholds.
3. The smallest first implementation slice and exact files it should touch.
4. Concurrency/barge-in failure modes the proposed phone queues must prevent.
5. Tests required before deployment.
6. A clear “proceed / revise / do not proceed” recommendation.

Read-only review only. Do not edit the repository or call external paid APIs.
