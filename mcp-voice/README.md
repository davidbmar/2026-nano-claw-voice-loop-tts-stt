# mcp-voice — nano-claw's local voice services over MCP

One thin stdio shim in front of the three local services (Kokoro TTS,
LuxTTS, Whisper STT) — the standard voice door for harnesses and agents,
per the Gemini-minimization directive. Services unchanged; HTTP consumers
keep working. Spec: `~/.claude/plans/nano-claw-mcp-voice.md` (v0.3).

## Client config (Claude Code / any MCP client)

```json
{
  "mcpServers": {
    "nano-claw-voice": {
      "command": "bash",
      "args": ["/Users/davidmar/src/nano-claw/mcp-voice/run.sh"]
    }
  }
}
```

## Tools

| Tool | What |
|---|---|
| `tts_synthesize` | text → WAV (inline base64 ≤1 MiB decoded, or file under `MCP_VOICE_OUT_ROOT`); engines kokoro/luxtts; `target_rate` resamples via ffmpeg |
| `tts_voices` | voices per engine, with reachability |
| `stt_transcribe` | WAV (PCM16 mono) → text; English only (the service hardcodes it) |
| `stt_stream_start/feed/finish/cancel` | streaming STT as an explicit stateful resource protocol — 60 s idle expiry and per-stream rate mirrored from the backend, `committed_text` passed through untranslated |
| `voice_health` | reachability (deliberately not readiness); `probe: true` adds a bounded readiness check |

Every result carries `schema_version: "1.0"`. Every failure is a structured
`{error: {code, message}}` from the spec's closed code list — the server
always starts and never crashes on backend failure.

## Verified

Unit: 47 tests, stubbed backends (`tests/python/test_mcp_voice.py`) — the
full validator matrix, the 1,048,576/1,048,578-byte inline boundary, out_dir
containment, stream lifecycle. Integration (real services, 2026-08-04):
kokoro→ffmpeg 16 kHz→whisper round trip at rapidfuzz 99.2 (threshold 85);
a 5-chunk streaming session with monotonic `committed_text`.

No determinism claims: Kokoro/LuxTTS are not bit-stable. Consumers that
need determinism commit bytes + digests (the riff canary bank already does).
