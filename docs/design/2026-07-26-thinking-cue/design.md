# Thinking Cue: Ack Chime + Clock Ticking During the STT/LLM Wait

2026-07-26 · branch `call-review-panel` · approved by David (sound: clock
ticking; timing: ack blip + repeat)

## Problem

After the caller stops speaking, the line is silent for the whole thinking
window — STT (~5s on whisper/medium) plus LLM (~2.8s hedged) plus first-
sentence synthesis. In the 2026-07-26 13:30 incident call the caller asked
"are you there?" and eventually hung up. Silence reads as a dead line.

## Caller experience

- The instant an utterance is **accepted**, the existing soft two-note chime
  plays — "I heard you." This is the acknowledgment that was missing.
- While the turn thinks, a quiet clock tick (~35 ms damped 2 kHz sine,
  ≈ −28 dBFS, clearly under speech level) plays every 0.5 s.
- Ticking stops the moment any real speech starts (answer, error line, idle
  prompt), on barge-in, on turn end, and on hangup.
- Fast turns: ack + a couple of ticks. Today's ~8 s turns: a steady clock.

## Design

1. **`voice/processing_audio.py`** — add `thinking_tick()`: lru-cached 48 kHz
   PCM16 tick with the same click-free attack/decay discipline as
   `processing_chime()` (which is reused unchanged as the ack). Constants:
   `TICK_SECONDS = 0.035`, tick peak ≈ −28 dBFS.
2. **`voice/phone.py`** — per-turn cue task, fully separate from the sentence
   pipeline (which does not exist during STT):
   - `_start_thinking_cue()` at the top of `_run_turn`: plays the ack chime,
     then ticks every `THINKING_TICK_INTERVAL_S = 0.5` (module constant, so
     tests can shrink it). Gated on
     `_cfg("NANO_CLAW_PHONE_THINKING_CUE", "on") != "off"` — env
     kill-switch, console-overridable and persisted for free via the
     settings file.
   - `_stop_thinking_cue()` — sets an `asyncio.Event` and cancels the task.
     Called from the top of `_speak_sentences` (every real speech path
     funnels through it), `_interrupt` (barge-in), `_turn_finished`, and
     `close()`. The event is checked before every frame send, so cue audio
     can never interleave with reply audio.
   - `_play_cue(pcm48k, stop)` — slim sender: converts via the existing
     `pcm48k_to_l16_frames` / `pcm48k_to_ulaw_frames`, paces with its **own**
     `FramePacer` (the speech pacer's `running`/anchoring semantics stay
     untouched), sends `media` frames, and tees `tap.outbound_frame` so
     recordings and `/calls` playback include what the caller actually
     heard. No gain normalizer — the cues are pre-tuned. Does not touch
     `self.speaking`, so inbound buffering and barge-in semantics are
     unchanged.
   - Tap events `thinking_cue_start` / `thinking_cue_stop` (with tick count)
     appear in the call log for review.
3. **Billing** — none: cues bypass `_synthesize_sentence`, so the cost
   wrapper never sees them (consistent with the existing earcon rule).
4. Scheduler-flow turns share `_run_turn`, so they get the cue automatically.

## Not doing (YAGNI)

- No volume/interval console controls (constants; env kill-switch only).
- No distinct sounds per phase (STT vs LLM) — one uniform ticking bed.
- No cue during greeting or idle prompts — thinking only.

## Tests

- `thinking_tick()` waveform: PCM16, expected length, peak under −26 dBFS,
  fully decayed tail (no click at cut), cached identity.
- Cue loop against a recording fake websocket with a shrunk interval:
  ack frames sent immediately; tick frames accumulate; stop halts sends;
  `NANO_CLAW_PHONE_THINKING_CUE=off` sends nothing; stop is idempotent.
- `_speak_sentences` stops a running cue before any speech frames go out.

## Verification

Full pytest suite → rebuild image → drain/undrain → loopback smoke test
("FIRST ANSWER AUDIO") → tap of the loopback shows `thinking_cue_start`,
tick count ≥ 1, `thinking_cue_stop` before `synth_done` playback of
sentence 1 → live test call to 512-277-7311.
