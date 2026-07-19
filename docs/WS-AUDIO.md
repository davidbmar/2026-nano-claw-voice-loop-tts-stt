# Browser audio over WebSocket

Browser WebSocket audio is an alternative transport for the existing voice
pipeline. Set `NANO_CLAW_WS_AUDIO=1` before a browser connects to enable it for
that connection. The default is off, so the existing WebRTC path remains the
default.

## Wire format

The authenticated application WebSocket at `/ws` carries both its existing JSON
messages and audio binary messages. WebSocket message type and direction make
audio unambiguous:

- Browser to server binary: mono signed PCM16 little-endian at 16 kHz. Each
  normal message contains 320 samples (640 bytes, about 20 ms).
- Server to browser binary: mono signed PCM16 little-endian at 16 kHz, normally
  framed as 320 samples (640 bytes, about 20 ms).

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
agent playback. TTS is still synthesized at 48 kHz, then converted with
`voice.phone_audio.resample_48k_to_16k` and sent as binary frames. The browser
queues those frames on a contiguous Web Audio timeline. Existing
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
