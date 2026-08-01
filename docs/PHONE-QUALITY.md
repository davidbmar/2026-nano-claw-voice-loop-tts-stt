# Phone quality measurement

The per-call tap turns phone-quality symptoms into WAV files and monotonic
timing measurements. It is disabled by default and is failure-isolated: a
capture error logs one warning and disables that call's tap without affecting
the call.

## Audio pipeline and tap points

```text
 caller
   │ encoded inbound audio
   ▼
 Telnyx media WebSocket
   │
   ├──► inbound.wav (PCMU decoded, or L16 PCM16)
   ▼
 utterance endpointer ──► STT ──► nano-claw agent
       │ timings             │ timings
       │                     ▼
       │                TTS source, 48 kHz PCM16
       │                     │
       │                     ├──► tts_48k.wav
       │                     ▼
       │                sentence peak normalization
       │                     ▼
       │                FIR resample to 8 or 16 kHz
       │                     ▼
       │                L16 or PCMU 20 ms frames
       │                     │
       │                     ├──► outbound.wav + pacing timings
       │                     ▼
       └──────────────── Telnyx media WebSocket ──► caller
```

`inbound.wav` represents bytes received from Telnyx after codec decoding.
`tts_48k.wav` is the source before FIR resampling. `outbound.wav` represents
frames successfully sent to Telnyx, decoded to PCM16 for inspection. All WAVs
are mono, 16-bit PCM; their headers carry the actual sampling rate.

`timings.jsonl` has one JSON object per line. Every object has an `event` and
a monotonic `t` in seconds. It records call and utterance boundaries, STT and
agent completion, synthesis, one aggregate frame-pacing record per sentence,
and barge-in. `utterance_start` and `utterance_end` include the frame's current
int16 RMS and the rolling noise `floor`. It intentionally does not write a JSON
record for every ordinary audio frame. While the agent is speaking, suppressed
barge votes are the exception: each `barge_candidate` records its decision
reason and scores so false interruptions can be tuned from real calls.

Each played sentence also records `gain_applied`, with its `sentence_index`,
source `measured_peak_dbfs`, and `applied_gain_db`. The `tts_48k.wav` tap stays
before normalization, so the event explains the gain between that source and
the resampler rather than hiding the original TTS level.

Streaming prepared speech changes the expected ordering on multi-sentence
turns: the first `synth_start` now precedes `agent_done`, because sentences
compile and synthesize while the model is still writing. `thinking_cue_start`
/ `thinking_cue_stop` (with `ticks`) bracket the acknowledgment-chime +
clock-tick window between utterance accept and first speech. A rare
`prepared_stream_mismatch` event means the server rewrote the reply after
streamed sentences were already spoken; the rewritten tail was deliberately
not spoken (never double-speak) and the assistant_turn payload carries
`preparedStreamMismatch: true`.

## Streaming STT (LocalAgreement-2)

`NANO_CLAW_PHONE_STT_STREAM=1` opts phone calls into incremental Whisper
decoding; it is `0`/off by default during live validation. The endpointer tees
the same PCM it is already retaining into the STT service in roughly 500 ms
batches. After about 700 ms of new audio, Whisper re-decodes the current
uncommitted window. When two consecutive hypotheses start with the same token
prefix, LocalAgreement-2 commits that prefix, carries it as Whisper's
`initial_prompt`, and trims its audio from later windows. Transcription
therefore happens mostly while the caller is speaking rather than entirely
after endpoint silence.

Dynamic-endpoint continuations keep the same session. A provisional transcript
such as “tell me about” can be finalized for the semantic-tail check, then new
audio continues with the already committed prefix instead of re-decoding the
merged utterance. The session closes only once the semantic tail is complete.
Any start, feed, or finish error logs one warning and falls back to the
unchanged one-shot `/transcribe` request with the endpointer's full PCM, so the
turn is not lost. Stream sessions expire after 60 seconds idle.

The tap exposes the incremental timeline:

- `stt_pass` records `pass_count`, the uncommitted `window_ms`, and decode
  `ms`.
- A successful streamed `stt_done` adds `streamed: true`,
  `committed_chars`, and `finish_ms`; its existing `ms` remains the phone-side
  wall time waiting at the endpoint.
- One-shot and fail-open turns retain the original `stt_done` shape.

## Real-voice loopback fixture (required for Silero validation)

Synthetic TTS caller audio scores low on the neural VAD (Silero correctly
doubts it's human — on one Kokoro question only ~0.9s of a 2.6s sentence
crossed 0.5 probability), so a text-prompt loopback cannot validate
`NANO_CLAW_PHONE_VAD=silero`. Use a real-speech recording instead:

    scripts/phone_loopback_test.py path/to/voice.wav   # mono 8k/16k PCM

To (re)build a fixture, extract a clean utterance from a recorded call tap
(e.g. `/app/data/phone-taps/<call>/inbound.wav` on the data volume) and
keep it OUT of git — it is a real caller's voice. The conventional local
path is `.loopback-voice.wav` (gitignored). Validated 2026-07-27: silero
on the live line captured a 3.8s real utterance fully with an exact
transcript, after the v5 context-prefix fix; energy VAD remains the
automatic fallback when the model is unavailable.

## Enable and report a tap

Set the variables in the voice server's environment, then restart that
process/container so it receives them:

```sh
export NANO_CLAW_PHONE_TAP=1
export NANO_CLAW_PHONE_TAP_DIR=/tmp/nano-claw-phone-taps
```

Make a call and hang up cleanly so WAV headers are finalized. Each call is in
`$NANO_CLAW_PHONE_TAP_DIR/<call_id>/`. Generate a report with:

```sh
python3 tools/phone_tap_report.py /tmp/nano-claw-phone-taps/<call_id>
```

Tap files contain callers' voices and should be handled as sensitive data.
Disable the tap after measuring and remove retained captures according to the
deployment's data policy.

## Read the report

- **WAV duration** exposes missing or truncated legs. The inbound duration is
  media received during the whole call; TTS and outbound durations cover only
  generated and successfully sent speech.
- **RMS and peak dBFS** expose gain staging. Values are negative because
  0 dBFS is the PCM limit. A peak near 0 dBFS suggests clipping; very negative
  RMS with a healthy source suggests insufficient gain. Compare calls made
  with the same controlled signal rather than treating one value as a universal
  threshold.
- **Spectral energy above 4 kHz** is always zero for an 8 kHz WAV, whose
  Nyquist limit is 4 kHz. A repeatable nonzero fraction in 16 kHz inbound audio
  demonstrates that high-band energy reached the gateway. A nonzero outbound
  fraction demonstrates that it survived TTS, FIR resampling, framing, and the
  send boundary.
- **Inter-sentence gaps** measure from the prior sentence's last frame end to
  the next sentence's first sent frame. Negative values mean frames were
  queued slightly ahead; large positive values are audible dead-air candidates.
- **Pacing** summarizes the one-per-sentence `frames_sent` records. After one
  reply-level prebuffer burst, frames have monotonic absolute deadlines at
  nominally 20 ms intervals. The summed surplus should stay near the configured
  prebuffer instead of growing with answer length; a negative surplus or high
  p95/max interval points to starvation or jitter.
- **STT timeline** lists each incremental decode window and its cost, followed
  by the final tail latency and how many transcript characters stabilized
  before endpointing.
- **Barge-in decisions** counts candidate votes, committed interruptions, and
  suppressions by reason (`low_conf`, `echo`, or `short`) for this call.
- **Barge-in to last outbound frame** is signed. A positive value means a
  frame crossed the send boundary after barge-in; a negative value means the
  final send preceded detection. This does not include audio already buffered
  inside Telnyx or the carrier, so compare it with what the caller hears after
  the `clear` command.

## Manual measurement A: tunnel WebSocket RTT

Measure WebSocket protocol ping/pong on the phone-media endpoint from the host
running nano-claw. This isolates the public tunnel path from application STT,
agent, and TTS time. Keep the voice server and tunnel running, use the same
phone token for both endpoints, and run at least three batches at a quiet and
a busy time. Run the snippet with a Python environment that has the voice
server's `aiohttp` dependency installed.

```sh
PHONE_WS_TOKEN='<NANO_CLAW_PHONE_TOKEN value>'
python3 - "$PHONE_WS_TOKEN" <<'PY'
import asyncio
import math
import statistics
import sys
import time
from urllib.parse import urlencode

from aiohttp import ClientSession, WSMsgType

TOKEN = sys.argv[1]
URLS = {
    "localhost": "ws://127.0.0.1:9090/ws/phone-media",
    "tunnel": "wss://nano.chattychapters.com/ws/phone-media",
}


async def measure(session, label, base_url):
    url = base_url + "?" + urlencode({"token": TOKEN})
    samples = []
    async with session.ws_connect(url, autoping=False) as ws:
        for sequence in range(55):
            payload = sequence.to_bytes(4, "big")
            started = time.perf_counter()
            await ws.ping(payload)
            while True:
                message = await asyncio.wait_for(ws.receive(), timeout=5)
                if message.type == WSMsgType.PONG and message.data == payload:
                    break
            if sequence >= 5:  # discard connection/TLS warmup
                samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    print(f"{label:9} median={statistics.median(samples):7.2f} ms "
          f"p95={p95:7.2f} ms max={max(samples):7.2f} ms")


async def main():
    async with ClientSession() as session:
        for label, url in URLS.items():
            await measure(session, label, url)


asyncio.run(main())
PY
```

Subtract localhost median from tunnel median for typical tunnel round-trip
overhead; do the same for p95 to expose tunnel jitter. Do not compare a public
run from a different client machine with a localhost run on the server—the
extra access-network distance would be mixed into the result.

## Manual measurement B: last-mile wideband check

1. Set `NANO_CLAW_PHONE_CODEC=l16`, enable the tap, and restart the voice
   server. Confirm the Telnyx call-control application accepts L16 at 16 kHz.
2. From the test handset, play a controlled 5–7 kHz sweep or a 6 kHz tone at a
   moderate, non-clipping level for several seconds, then speak normally. The
   controlled signal makes the high-band result less dependent on the speaker,
   microphone, or phrase.
3. Let the agent answer, then hang up. Run `phone_tap_report.py` on that call's
   directory.
4. Read the inbound and outbound **Spectral energy above 4 kHz** lines. An
   inbound fraction consistently above the fixture/noise-floor result shows
   that the handset-to-Telnyx-to-gateway path carried wideband energy. A
   nonzero outbound fraction shows that the gateway sent wideband energy. If
   outbound is nonzero but inbound is effectively zero, the source, handset,
   carrier route, or Telnyx inbound leg is still narrowband.

The outbound tap is at the gateway send boundary, not inside the receiving
handset. To prove the downlink's final acoustic hop as well, record the handset
output at 16 kHz or higher and compare its spectrum with `outbound.wav`.

## VAD and adaptive endpointing

`NANO_CLAW_PHONE_VAD=silero` selects the neural Silero VAD for new calls.
Unset or `energy` selects energy classification, which remains the application
default; selecting Silero falls back loudly to energy if its model/runtime is
unavailable. The web VAD selector is an in-memory override with precedence over
the environment and likewise applies to new calls only.

The current deployment does set `NANO_CLAW_PHONE_VAD`, but sets it to `energy`,
not `silero`: the current `.env` contains that value and `run.sh` forwards the
variable into the container. This task does not change that default or running
mode. Set the value to `silero` and restart the container to opt in.

In energy mode, L16 endpointing estimates the line's non-speech floor with an
EMA of frame RMS values. Each call owns its estimate; the first non-speech
frame seeds it, subsequent non-speech frames contribute 5%, and utterance
resets preserve it. RMS and floor are linear int16 units, not dBFS. The speech
boundary is `max(RMS minimum, floor × ratio)`, and speech frames do not update
the floor so caller energy cannot ratchet the threshold upward. PCMU keeps its
historical fixed 350-RMS behavior by default: its floor ratio is zero, so
tracking the diagnostic floor cannot change classification. Silero's speech
decisions remain authoritative when enabled, while its non-speech decisions
still update the floor reported by the tap.

## Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `NANO_CLAW_PHONE_TAP` | unset/off | Exact value `1` enables per-call capture; every other value creates no tap and performs no capture I/O. |
| `NANO_CLAW_PHONE_TAP_DIR` | `/tmp/nano-claw-phone-taps` | Root directory; each call ID gets one subdirectory. |
| `NANO_CLAW_PHONE_VAD` | `energy` | `silero` enables neural VAD for new calls when available; `energy` uses RMS endpointing. The current container configuration explicitly selects `energy`. |
| `NANO_CLAW_PHONE_RMS_MIN` | PCMU: `350`; L16: `120` | Minimum speech boundary in linear int16 RMS units. |
| `NANO_CLAW_PHONE_RMS_RATIO` | PCMU: `0.0`; L16: `3.0` | Noise-floor multiplier. PCMU's zero preserves its fixed-threshold compatibility default; setting a positive value opts it into adaptation. |
| `NANO_CLAW_PHONE_GAIN` | on | Exact value `off` (case-insensitive) bypasses phone gain processing with byte-identical 48 kHz PCM. |
| `NANO_CLAW_PHONE_GAIN_TARGET_DB` | `-3` | Per-sentence target peak in dBFS. Invalid or non-finite values fall back to `-3`. |
| `NANO_CLAW_PHONE_PREBUFFER_MS` | `200` | Milliseconds of audio sent immediately at the start of each reply. Zero disables the burst; invalid, negative, or non-finite values fall back to `200`. |
| `NANO_CLAW_PHONE_PACE_FACTOR` | `1.0` | Multiplies the 20 ms absolute-deadline interval. `1.0` is real time; values below `1.0` send faster and values above it send slower. Invalid, non-positive, or non-finite values fall back to `1.0`. |

## Outbound gain normalization

The phone speak path peak-normalizes each synthesized sentence while it is
still 48 kHz PCM16, immediately before the FIR resampler. The web TTS path is
unchanged. PCM16 peak level is calculated as
`20 × log10(max(abs(sample)) / 32768)` dBFS, so full scale is 0 dBFS and
digital silence is negative infinity.

The desired gain moves that measured peak toward
`NANO_CLAW_PHONE_GAIN_TARGET_DB` (`-3` dBFS by default). Amplification is
capped at +12 dB so a very quiet sentence or background noise cannot be lifted
all the way to full scale. Within one reply, gain may move by at most 3 dB from
one sentence to the next; the history resets at the next reply. Gain
is applied in float64, clamped to the exact PCM16 bounds as a final true-peak
guard, and only then converted back to int16. This prevents integer wrap even
when smoothing temporarily asks for more gain than a hot sentence can hold.

Set `NANO_CLAW_PHONE_GAIN=off` to compare against the prior path. Bypass returns
the original 48 kHz bytes unchanged; with the tap enabled, `gain_applied` then
reports 0 dB while retaining the measured source peak.

## Deadline-based frame pacing

The former loop slept for 90% of a frame after every send. The short interval
was intentional jitter-buffer protection, but it steadily queued about 100 ms
of surplus for every second of speech. Relative sleeps also made every slow
send and scheduler oversleep permanent: later frames inherited that drift.

Playback now sends `NANO_CLAW_PHONE_PREBUFFER_MS` of audio immediately once per
reply, then advances a monotonic absolute deadline by the 20 ms frame duration
times `NANO_CLAW_PHONE_PACE_FACTOR`. Each sleep is only the positive distance
to that deadline. A late iteration therefore makes following sleeps shorter or
zero until playback catches up instead of shifting the rest of the reply. The
default 200 ms prebuffer retains startup headroom, while the default 1.0 factor
stops that headroom from growing.

One pacer is reset at the first playable frame and shared by every sentence in
the reply. Synthesis look-ahead therefore does not consume the initial
prebuffer, and a sentence boundary cannot create a second burst. In tap output,
sum the per-sentence `surplus_s` values: for a reply longer than the prebuffer,
the result should remain near 0.200 s regardless of the reply's duration.
Short zero intervals at the beginning or immediately after an injected delay
are expected catch-up; steady-state p95 intervals should remain near 20 ms.

## Voice-selective barge-in and buffer flush

Phone barge-in is a deliberately stricter decision than ordinary utterance
acceptance. The endpointer still uses Silero's existing 0.5/0.35 enter/exit
hysteresis so quiet caller turns start promptly. While the agent is speaking,
the barge path instead requires the raw per-chunk Silero probability to reach
`NANO_CLAW_PHONE_BARGE_VAD_ENTER` (default `0.7`, clamped to `0.5`–`0.95`).
Energy VAD keeps its existing 550-RMS barge boundary.

A qualifying vote must accumulate for
`NANO_CLAW_PHONE_BARGE_TRIGGER_MS` (default `350`, clamped to
`120`–`1500`) inside the existing 800 ms window. This prevents a sub-300 ms
cough from committing while allowing a deliberate 400 ms interruption.

`NANO_CLAW_PHONE_ECHO_GATE` controls the own-voice defense and defaults to `1`
whenever phone barge-in is enabled. The gateway retains only about 1.2 seconds
of outbound phone-rate PCM actually sent to Telnyx. It compares 10 ms inbound
and outbound energy envelopes over a ±120 ms lag search; normalized correlation
at or above `NANO_CLAW_PHONE_ECHO_CORR` (default `0.75`, clamped to `0`–`1`)
suppresses that inbound vote as likely speakerphone echo. Set the gate to `0`
only as a diagnostic rollback.

The tap event vocabulary separates candidates from commits:

- `barge_candidate` with `reason=low_conf` means the endpointer considered the
  frame speech but its raw Silero probability missed the stricter barge level.
- `barge_candidate` with `reason=echo` means the outbound-reference correlation
  met the echo threshold.
- `barge_candidate` with `reason=short` means the frame qualified but the
  configured sustain has not yet been reached.
- `barge_in` remains the commit event. It means playback was cancelled and the
  captured interruption was promoted into the caller's next utterance.

The voice-selectivity ladder is:

1. **Level 1 — speech vs noise:** Silero supplies neural speech classification
   while energy VAD remains the fallback.
2. **Level 2 — caller intent vs our own voice:** the asymmetric raw-probability
   threshold, longer sustain, and outbound-reference echo gate described here.
3. **Level 3 — caller vs other human voices:** speaker-conditioned personal VAD
   is proposed, not yet active, in
   `.context/backlog/087-personal-vad-speaker-conditioned-barge.md`.

Historically, outbound 20 ms frames were sent at about 18 ms to keep Telnyx's
jitter buffer fed. During a long answer that 10% lead accumulated, so merely
stopping local sends on barge-in could leave roughly one second of already
queued speech after a ten-second response. Absolute pacing now bounds the
normal surplus near the one-time prebuffer, but even that bounded queue should
not play after the caller interrupts.

When barge-in is detected, the gateway now stops sending media and sends
Telnyx `{"event": "clear"}` exactly once. Telnyx immediately stops the media
playing on the stream and empties its media queue. A hangup during playback
uses the same flush while the media WebSocket is still available; a closed
socket or failed clear remains isolated from the call path.

To measure the change, enable the call tap and make a controlled call with a
long agent response, then interrupt it. `timings.jsonl` should contain paired
`barge_in` and `clear_sent` events, and `phone_tap_report.py`'s **Barge-in to
last outbound frame** value should be at or below a single frame interval. The
report measures the gateway send boundary; record or listen at the handset and
compare the audible post-interruption tail before and after this change to
confirm that Telnyx's queued audio was removed.

## Sentence synthesis look-ahead

Phone playback pre-synthesizes exactly one sentence ahead while the current
sentence's frames are being sent. The one-sentence bound removes most normal
inter-sentence synthesis gaps without building a large queue of audio that
would be discarded on barge-in. Hangup or barge-in cancels and awaits the
pending synthesis task, and a failed prefetched sentence is logged and skipped
without interrupting the sentence already playing.

With the call tap enabled, `synth_ahead_hit` means the next synthesis was ready
when playback needed it; `synth_ahead_miss` includes `wait_ms`, the remaining
synthesis wait at that boundary. Compare those events with the adjacent
`frames_sent` records and their inter-sentence gap to quantify the improvement.
