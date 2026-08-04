#!/usr/bin/env bash
# nano-claw MCP voice server — stdio transport, spawned by an MCP client.
# Stdlib-only; ffmpeg is discovered at call time (only target_rate needs it).
# The three backends are configured by env (defaults match the local stack):
#   NANO_CLAW_TTS_URL  (kokoro,  default http://127.0.0.1:8300)
#   NANO_CLAW_LUX_URL  (luxtts,  default http://127.0.0.1:8301)
#   NANO_CLAW_STT_URL  (whisper, default http://127.0.0.1:8200)
#   MCP_VOICE_OUT_ROOT (file-mode output root, default $TMPDIR/mcp-voice)
set -euo pipefail
cd "$(dirname "$0")"
exec python3 server.py
