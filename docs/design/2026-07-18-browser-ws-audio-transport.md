# Browser voice over WebSocket (replace WebRTC) — Option B

Status: DESIGN. Fixes: remote users can't get voice audio because WebRTC
(no STUN/TURN) can't cross the cloudflared tunnel — server ICE checks
target the browser's private LAN IP (evidence: Connection→192.168.1.251
FAILED vs →192.168.86.20 SUCCEEDED, container logs 2026-07-18). Chosen
over TURN (proposal 045) because it needs no public relay and reuses the
proven phone-path audio code.

## The idea

The page already holds a WebSocket (`/ws`) that crosses the tunnel
cleanly (all signaling + transcripts ride it today). Move the AUDIO onto
it too — both directions — so ICE/NAT is out of the picture entirely.

- Mic in: browser AudioWorklet captures mic → PCM16 frames → WS binary
  → server feeds the SAME STT/VAD/turn pipeline.
- Agent out: server TTS PCM → WS binary → browser Web Audio playback.

This is what `voice/phone.py` already does with Telnyx's media WS (u-law
frames); the browser version is the same pattern with PCM16 over `/ws`.
Reuse `voice/phone_audio.py` resamplers (48k↔16k) rather than new DSP.

## The seam (what changes, what doesn't)

`voice/webrtc.py::Session` owns audio today:
- `_recv_mic_audio(track)` (webrtc.py:271) reads `frame.to_ndarray()`,
  accumulates PCM for STT — the STT/turn pipeline downstream is UNCHANGED.
- `speak_text()` (webrtc.py:255) synthesizes + sends agent audio.

Introduce a transport abstraction so the STT-in accumulation and the
TTS-out are shared, with two backends:
- `WebRtcTransport` — today's aiortc path (kept as an opt-in same-LAN
  fast path; not removed in v1).
- `WsAudioTransport` — receives PCM16 frames from the `/ws` message loop
  (server.py:213 area, a new `mic_audio` binary/message type) instead of
  a track; sends agent PCM frames back over the same socket.

The downstream STT, VAD, endpointing, turn dispatch, metrics, and
history capture do not change — only where the PCM comes from / goes to.

## Wire format

- Mic: browser sends PCM16 mono @ 16 kHz (STT's native rate — resample
  in the AudioWorklet or once server-side via phone_audio) in ~20 ms
  binary WS frames, or base64 in a JSON `mic_audio` message (binary
  preferred; less overhead). Include a tiny header/first-message
  announcing rate + format so the server validates.
- Agent: server sends TTS PCM (48 kHz, or 16 kHz to save bandwidth) as
  binary frames the browser queues into a Web Audio buffer source.
- Barge-in: reuse the existing browser barge detector; on interrupt the
  browser stops playback and signals the server (existing mechanism).

## Rollout (flagged, no regression)

`NANO_CLAW_WS_AUDIO` (default off in the first task) selects the
transport at connect time. WebRTC stays the default until WS-audio is
verified end-to-end, so nothing that works today breaks. The cutover
task flips the default and un-gates the UI.

## Decomposition

- Task 046 — WS-audio transport (bidirectional), behind the flag:
  browser AudioWorklet capture + WS framing, server WsAudioTransport
  ingest → existing STT/turn pipeline, agent PCM → WS → Web Audio
  playback. WebRTC untouched/default. Reuse phone_audio resamplers.
- Task 047 — cutover + UX: make WS-audio the default; DECOUPLE the text
  box + Send + mic buttons from the audio-connected state (today they
  enable only on ICE "connected", app.js:2057-2059 — so a user whose
  audio fails can't even type); connection-status UX; keep WebRTC as an
  explicit same-LAN option or retire it.

Both DEPEND on task 044 (GIS auth UI) committing first — it is editing
voice/web/app.js and server.py, which these tasks also touch. Run them
serially after 044, each through the Codex→judge loop; the audio
pipeline is high-risk (cf. the earlier "two voices" incident from a
half-built audio tree), so verify on a real remote path before flipping
the default.

## Acceptance

A browser on a DIFFERENT network than the server can hold a full voice
conversation (mic in, agent audio out) through nano.chattychapters.com
with WebRTC disabled. Text works even when audio doesn't. Local WebRTC
path unaffected while the flag is off.
