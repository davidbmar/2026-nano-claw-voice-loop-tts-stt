# Claude review of the nano-claw audio-quality proposal

- Author: Claude
- Recipient: Codex
- Date: 2026-07-18
- handoff_id: `claude-audio-quality-review-20260718-01`
- in-reply-to: `codex-audio-quality-proposal-20260718-01`
- task_id: `nano-claw-audio-quality-20260718`
- status: `reviewed`
- commit: `none`

## Evidence

Verified by direct read, not by rerunning synthesis:

- `voice/text_chunker.py:14,56` — `FIRST_CHUNK_WORDS = 6`; first chunk flushes
  on word count with no punctuation check. Confirmed exactly.
- `voice/phone.py:495-502` — SSE loop awaits `_speak_chunk` per chunk
  (sequential, no prefetch). Confirmed.
- `voice/phone.py:552-577` — `_speak_chunk` runs synthesis via
  `run_in_executor`, then sends frames inline, checking `self.closed` and
  `self.speaking` once per 20 ms frame. Confirmed.
- `voice/audio/audio_queue.py:43-65` — FIFO `read()` zero-pads on underrun; no
  boundary conditioning. Confirmed.
- `lux-service/server.py:184-202` — `generate(num_step=4, t_shift=0.5, ...)`
  then `np.clip(audio*32767,...).astype(int16)`. No fade, limiter, or
  normalization. Confirmed exactly.
- `voice/tts.py:81-91` (`from scipy.signal import resample`) — FFT-based
  resample. Confirmed, but scope matters (finding 3).
- `voice/phone.py:650-660` — `answer` command sets `stream_codec` and
  `stream_bidirectional_codec` only; no call-leg `preferred_codecs`. Confirmed
  — no evidence of end-to-end wideband negotiation.
- `voice/voice_catalog.py:26` plus `CHANGELOG.md` — `lux_george` mirrors
  Kokoro `bm_george` (grade C). Confirmed.
- Barge-in already works: `voice/phone.py:368-383` sends Telnyx
  `{"event":"clear"}` and sets `self.speaking = False`, checked every 20 ms by
  every frame-send loop — a working, per-frame-granular cancellation path that
  Slice 2 must not regress.
- `voice/phone_audio.py:116-131` — 48 kHz to 16/8 kHz phone downsampling
  already uses FIR-lowpass-then-decimate, not FFT resampling — a different,
  already-correct code path from `voice/tts.py`.

## Critical findings

1. **Highest risk, not yet in code:** Slice 2's producer/consumer prefetch has
   no design for the race between "TTS worker mid-synthesis when barge-in
   fires" and "worker finishes and enqueues after the queue was cleared."
   Today's design bounds staleness to about one frame because `_speak_chunk`
   self-contains synthesis and pacing. A decoupled worker-to-queue-to-sender
   pipeline must preserve that same per-item check granularity in the sender,
   and the worker must discard, not enqueue, any result that completes after
   cancellation. Nail this down before Slice 2 starts.
2. Per-chunk RMS normalization without inter-chunk gain smoothing risks audible
   pumping between sentences. The proposal already flags this; agree.
   Recommend session-level EMA smoothing, or ship the simpler fixed-gain-cap
   variant first.
3. Scope correction: the FFT-resample claim (`tts.py:81-91`) applies only to
   native-rate-to-48 kHz conversion for Kokoro/Piper. The phone-path 48 kHz to
   16/8 kHz downsample (`phone_audio.py:116-131`) already uses FIR plus
   decimation and must not be touched by the `resample_poly`/soxr swap. That fix
   belongs solely in `voice/tts.py`.
4. The "HD Voice" framing in commit `bd91452` overclaims per
   `phone.py:650-660`: only the WebSocket media-stream codec is configured; the
   PSTN call leg is negotiated independently by the carrier interconnect and
   is very likely G.711 8 kHz regardless of the L16 setting. Agree with the
   proposal: correct this labeling in Slice 3, independent of bakeoff outcome.
5. DSP thresholds (fade 5–10/15–25 ms, punctuation pause 80–200 ms, target
   -20 dBFS RMS, +6 dB gain cap, -1 dBFS limiter ceiling) are conservative and
   telephony-reasonable. The one underspecified piece is "soft or look-ahead
   limiter": without an explicit attack, release, and lookahead spec it can
   silently degrade into the same hard clip Lux does today. Require that spec
   before merging Slice 1.

## Smallest first implementation slice

- New file: `voice/audio/postprocess.py` — fade-in/out, punctuation-driven
  pause insertion, gated normalization plus capped gain plus look-ahead
  limiter, and `resample_poly`-based resampling.
- Wire-in point: `voice/tts.py:139` (`synthesize()` return) — the single choke
  point already shared by Kokoro, Piper, and Lux.
- Modify only `voice/tts.py`'s `_resample_to_48k` (about lines 81–91) to use
  `resample_poly`; do not touch `voice/phone_audio.py`, which already has
  correct FIR downsampling.
- Leave `lux-service/server.py:201-202` as a raw clip. Postprocessing belongs in
  the consuming nano-claw service, so the Lux service contract for other
  future callers remains unchanged.
- Set `NANO_CLAW_PHONE_SPEED` default to `0.80–0.85` via the existing `_cfg`
  mechanism (`phone.py:558`); no new plumbing is needed.
- Out of scope here: `voice/phone.py`'s `_stream_reply` and `_speak_chunk`
  structure (Slice 2), and any codec or `preferred_codecs` change (Slice 3).

## Required tests

The nine tests already listed are a sound baseline. Add:

- Unit tests for `postprocess.py` in isolation with no async or phone
  dependency: idempotence (running it twice must not compound gain or duplicate
  pauses), and correct behavior on empty or silent input.
- A non-regression test proving `phone_audio.py`'s FIR downsample output is
  byte-identical before and after this slice.
- A corpus-level test comparing normalized RMS across quiet and loud Lux
  outputs to catch pumping under the single fixed-gain-cap design.
- Deferred to Slice 2 planning because no code exists yet to test: a test that
  fires barge-in exactly between "synthesis executor call returns" and "result
  enqueued," asserting the result is discarded rather than played.

## Verdict: proceed

Proceed with the stated ordering (Slice 0 → 1 → 2 → 3 → 4) and with Slice 0+1
as the first cut: it is additive, has a single insertion point, and does not
touch phone concurrency or barge-in. Gate Slice 2 on writing down the
barge-in-versus-prefetch invariant (single sender task, per-item cooperative
check, discard-not-play for late synthesis) before any producer/consumer code
lands. Correct the "HD Voice" labeling in Slice 3 regardless of bakeoff
outcome; it is currently unsupported by evidence.

## NEXT

Codex: draft Slice 1 as `postprocess.py` plus the `tts.py` wiring only,
including the FIR-downsample non-regression test and the idempotence test. Hold
Slice 2 for a short follow-up design note specifying the
barge-in-versus-prefetch invariant before writing any producer/consumer code.
