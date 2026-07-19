# Browser audio over WebSocket

Browser WebSocket audio is the default transport for the existing voice
pipeline. `run.sh` assigns `NANO_CLAW_WS_AUDIO=1` when the variable is unset and
forwards the value into the voice container, so remote browsers carry voice and
text through the same tunnel-safe `/ws` connection.

WebRTC is retained as an explicit same-LAN, lower-latency compatibility path.
Set `NANO_CLAW_WS_AUDIO=0` (also accepts `false`, `off`, or `no`) before running
`run.sh` to select it. Processes started without `run.sh` should set the flag
explicitly. WebRTC was not retired because its direct peer-to-peer path remains
useful on a local network; it is not suitable for the remote tunnel path unless
STUN/TURN is added separately.

## Readiness contract

The browser treats text and voice as independent capabilities:

- **Link ready** means the application `/ws` WebSocket is open. The text input,
  Send button, and `text_message` path are available. A message entered during
  the brief transport initialization window is queued until the server Session
  exists. Link readiness never depends on mic permission, Web Audio, WebRTC
  ICE, or the WS-audio handshake.
- **Audio ready** means mic capture and the selected audio transport have both
  connected. Only the mic/talk button depends on this state.

The dock renders these as separate `TEXT` and `VOICE` indicators. If mic access
is denied, an audio format is rejected, or ICE/audio setup fails, `TEXT` remains
`READY`, `VOICE` reads unavailable, and the text conversation remains usable.
In the browser implementation, `syncReadinessControls()` is the single control
gate that enforces this ownership: link state owns text controls and audio state
owns only the talk control.

## Wire format

The authenticated application WebSocket at `/ws` carries both its existing JSON
messages and audio binary messages. WebSocket message type and direction make
audio unambiguous:

- Browser to server binary: mono signed PCM16 little-endian at 16 kHz. Each
  normal message contains 320 samples (640 bytes, about 20 ms).
- Server to browser binary: mono signed PCM16 little-endian at 48 kHz, normally
  framed as 960 samples (1,920 bytes, about 20 ms). This is about 96 KB/s.

After `hello`, an enabled server returns `wsAudio: true` and a
`wsAudioFormat` object in `hello_ack`. Before its first binary mic message, the
browser must send:

```json
{
  "type": "mic_audio_start",
  "format": "pcm_s16le",
  "sampleRate": 16000,
  "channels": 1,
  "frameSamples": 320
}
```

The server validates every announced field. It replies with
`mic_audio_ready` on success; an unsupported announcement receives
`mic_audio_error` and closes with WebSocket code 1003. Binary input before the
announcement, odd-length PCM16, empty frames, and incorrectly sized frames are
also rejected. Each binary mic frame must match the announced 320-sample size.

The existing `agent_audio_start` and `agent_audio_end` JSON messages bracket
agent playback. TTS is synthesized at 48 kHz and sent unchanged, preserving
the full band and avoiding per-frame resampling on the common 48 kHz Web Audio
context. The browser reads this rate from `wsAudioFormat.agent` and queues the
frames on a contiguous Web Audio timeline with a bounded 150 ms initial lead
to absorb tunnel and cellular jitter. A device whose native AudioContext rate
is not 48 kHz still requires browser resampling, so it remains a residual
device-specific quality case. Existing
`barge_in`, `barge_in_commit`, `barge_in_false`, and `stop_speaking` messages
remain the playback-control protocol.

## Pipeline and identity

The browser AudioWorklet continuously captures mic PCM. On the server,
`WsAudioTransport` feeds those bytes into the same `Session` preroll and
recording accumulator used by a WebRTC mic track. `mic_start`, `mic_stop`, STT,
VAD/endpointing decisions, turn dispatch, metrics, and history capture are
unchanged.

The transport is attached only to the server-created Session for the upgraded
socket. Conversation id, tenant, and user identity continue to come from the
HTTP/WebSocket upgrade; no audio message accepts identity fields from the
browser.
