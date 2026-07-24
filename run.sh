#!/bin/bash
set -e

# Load .env if present
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Show free disk space
echo "=== Disk Space ==="
df -h / | tail -1 | awk '{
  total = $2; free = $4
  t = total + 0; f = free + 0
  used_pct = int((1 - f/t) * 100)
  print "Free: " free " / " total " (" used_pct "% used)"
}'
echo ""

# Remove old container if running. Filter by NAME, not ancestor: after a
# rebuild the tag points at the new image and ancestor= stops matching the
# old container — which then wins the port race against the new one.
OLD=$(docker ps -aq --filter name='^nano-claw-voice$' 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "Stopping old container(s)..."
  docker rm -f $OLD
fi

# Fast path for supervisors (launchd watchdog): reuse the existing image
# instead of a minutes-long --no-cache rebuild. Only skips when the image
# actually exists; a fresh machine still builds.
if [ "${NANO_CLAW_SKIP_BUILD:-}" = "1" ] && docker image inspect nano-claw-voice >/dev/null 2>&1; then
  echo "=== Reusing existing image (NANO_CLAW_SKIP_BUILD=1) ==="
else
  # Remove old image
  if docker image inspect nano-claw-voice >/dev/null 2>&1; then
    echo "Removing old image..."
    docker rmi nano-claw-voice
  fi

  # Prune dangling images/layers from previous builds
  echo "Pruning dangling images..."
  docker image prune -f
  echo ""

  # Build
  echo "=== Building ==="
  docker build --no-cache -t nano-claw-voice .
fi

echo ""
echo "=== Disk Space After Build ==="
df -h / | tail -1 | awk '{
  total = $2; free = $4
  t = total + 0; f = free + 0
  used_pct = int((1 - f/t) * 100)
  print "Free: " free " / " total " (" used_pct "% used)"
}'
echo ""

# Run with any provider supported by the model catalog. The default config uses
# DeepSeek, so Anthropic is optional rather than a startup requirement.
LLM_PROVIDER_KEY=""
for key_name in DEEPSEEK_API_KEY ANTHROPIC_API_KEY GEMINI_API_KEY GROQ_API_KEY DASHSCOPE_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY; do
  if [ -n "${!key_name:-}" ]; then
    LLM_PROVIDER_KEY="$key_name"
    break
  fi
done
if [ -z "$LLM_PROVIDER_KEY" ]; then
  echo "ERROR: no supported LLM provider key found in .env or the environment."
  echo "Set DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, GROQ_API_KEY,"
  echo "DASHSCOPE_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY."
  exit 1
fi
echo "LLM provider credential: $LLM_PROVIDER_KEY"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Python for the native STT/TTS venvs: torch needs modern-but-not-bleeding
# (no 3.14 wheels yet; macOS system 3.9 is too old for faster-whisper).
SERVICE_PYTHON="$(command -v python3.12 || command -v python3.13 || command -v python3.11 || command -v python3)"

# Start STT service if not already running
STT_SERVICE_URL="${STT_SERVICE_URL:-http://host.docker.internal:8200}"
STT_CHECK_URL="${STT_SERVICE_URL/host.docker.internal/localhost}"
STT_PID=""

if curl -sf "$STT_CHECK_URL/health" >/dev/null 2>&1; then
  echo "STT service already running at $STT_CHECK_URL"
else
  echo "=== Starting STT service ==="
  STT_VENV="$SCRIPT_DIR/stt-service/.venv"
  if [ ! -d "$STT_VENV" ]; then
    echo "Creating STT virtual environment..."
    "$SERVICE_PYTHON" -m venv "$STT_VENV"
  fi
  # Prefer the lockfile (exact pinned versions — no auto-updates); fall back
  # to requirements.txt only when no lock has been generated yet.
  if [ -f "$SCRIPT_DIR/stt-service/requirements.lock" ]; then
    "$STT_VENV/bin/pip" install -q -r "$SCRIPT_DIR/stt-service/requirements.lock"
  else
    "$STT_VENV/bin/pip" install -q -r "$SCRIPT_DIR/stt-service/requirements.txt"
  fi
  "$STT_VENV/bin/python" "$SCRIPT_DIR/stt-service/server.py" &
  STT_PID=$!

  # Wait for STT service to be ready
  for i in $(seq 1 15); do
    if curl -sf "$STT_CHECK_URL/health" >/dev/null 2>&1; then
      echo "STT service ready"
      break
    fi
    sleep 1
  done
  echo ""
fi

# Start TTS service (Kokoro) if not already running
TTS_SERVICE_URL="${TTS_SERVICE_URL:-http://host.docker.internal:8300}"
TTS_CHECK_URL="${TTS_SERVICE_URL/host.docker.internal/localhost}"
TTS_PID=""

if curl -sf "$TTS_CHECK_URL/health" >/dev/null 2>&1; then
  echo "TTS service already running at $TTS_CHECK_URL"
else
  echo "=== Starting TTS service (Kokoro) ==="
  TTS_VENV="$SCRIPT_DIR/tts-service/.venv"
  if [ ! -d "$TTS_VENV" ]; then
    echo "Creating TTS virtual environment..."
    "$SERVICE_PYTHON" -m venv "$TTS_VENV"
  fi
  if [ -f "$SCRIPT_DIR/tts-service/requirements.lock" ]; then
    "$TTS_VENV/bin/pip" install -q -r "$SCRIPT_DIR/tts-service/requirements.lock"
  else
    "$TTS_VENV/bin/pip" install -q -r "$SCRIPT_DIR/tts-service/requirements.txt"
  fi
  PYTORCH_ENABLE_MPS_FALLBACK=1 "$TTS_VENV/bin/python" "$SCRIPT_DIR/tts-service/server.py" &
  TTS_PID=$!

  # Wait for readiness — first run downloads the ~310MB model, so allow longer.
  for i in $(seq 1 60); do
    if curl -sf "$TTS_CHECK_URL/health" >/dev/null 2>&1; then
      echo "TTS service ready"
      break
    fi
    sleep 1
  done
  echo ""
fi

# Start LuxTTS service (optional voice cloning) if set up and not running.
# Setup is manual (lux-service/setup.sh) because it installs a large pinned
# dependency set and pickle-scans the model weights; without it the LuxTTS
# dropdown entry simply falls back to Piper (degraded-mode convention).
LUX_SERVICE_URL="${LUX_SERVICE_URL:-http://host.docker.internal:8301}"
LUX_CHECK_URL="${LUX_SERVICE_URL/host.docker.internal/localhost}"
LUX_PID=""

if curl -sf "$LUX_CHECK_URL/health" >/dev/null 2>&1; then
  echo "LuxTTS service already running at $LUX_CHECK_URL"
elif [ -f "$SCRIPT_DIR/lux-service/.verified" ]; then
  echo "=== Starting LuxTTS service ==="
  PYTORCH_ENABLE_MPS_FALLBACK=1 "$SCRIPT_DIR/lux-service/.venv/bin/python" "$SCRIPT_DIR/lux-service/server.py" &
  LUX_PID=$!
  for i in $(seq 1 30); do
    if curl -sf "$LUX_CHECK_URL/health" >/dev/null 2>&1; then
      echo "LuxTTS service ready"
      break
    fi
    sleep 1
  done
  echo ""
else
  echo "LuxTTS service not set up (optional) — run lux-service/setup.sh to enable the cloned voice"
fi

# On exit, DO NOT kill the STT/TTS/Lux host services. run.sh exits on every
# container rebuild (docker stop → the foreground `docker run` returns), and
# killing the services there silently breaks transcription until the next
# manual restart — a real outage hit on 2026-07-19. The services are designed
# to be reused (the /health check above reuses a running one), so leaving them
# up makes a rebuild a seamless swap instead of a service-down window.
# To stop them deliberately, use NANO_CLAW_STOP_SERVICES=1.
cleanup() {
  if [ "${NANO_CLAW_STOP_SERVICES:-}" != "1" ]; then
    return 0
  fi
  for pid in "$STT_PID" "$TTS_PID" "$LUX_PID"; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
    fi
  done
}
trap cleanup EXIT

echo "=== Starting container ==="
echo "Open http://localhost:9090 in your browser"
echo ""
# Site knowledge (scripts/crawl_site.py + scripts/build_knowledge.py):
# auto-load every data/*/knowledge.md unless NANO_CLAW_KNOWLEDGE is already
# set in .env. Host data/ is mounted read-only at /app/sites (the /app/data
# named volume holds runtime state and stays untouched).
if [ -z "${NANO_CLAW_KNOWLEDGE:-}" ]; then
  NANO_CLAW_KNOWLEDGE=$(ls "$SCRIPT_DIR"/data/*/knowledge.md 2>/dev/null \
    | sed "s|^$SCRIPT_DIR/data/|/app/sites/|" | paste -sd, -)
else
  # Users naturally put host paths in .env (data/<site>/knowledge.md or an
  # absolute path under this repo); rewrite those to the container mount so
  # the file actually exists inside docker.
  NANO_CLAW_KNOWLEDGE=$(echo "$NANO_CLAW_KNOWLEDGE" | tr ',' '\n' \
    | sed -e "s|^$SCRIPT_DIR/data/|/app/sites/|" -e 's|^data/|/app/sites/|' \
    | paste -sd, -)
fi
export NANO_CLAW_KNOWLEDGE
if [ -n "$NANO_CLAW_KNOWLEDGE" ]; then
  echo "Knowledge: $NANO_CLAW_KNOWLEDGE"
fi
# WebSocket audio survives the same HTTP tunnel as text and is the remote-safe
# default. Set NANO_CLAW_WS_AUDIO=0 (or false/off/no) to retain WebRTC for a
# same-LAN, lower-latency deployment.
NANO_CLAW_WS_AUDIO="${NANO_CLAW_WS_AUDIO:-1}"
NANO_CLAW_MEMORY_DIR="${NANO_CLAW_MEMORY_DIR:-/app/data/memory}"
# Bare `-e VAR` forwards a variable only when it is set in this shell
# (.env is sourced above with `set -a`), so optional keys/flags pass
# through automatically without being required.
# -it only when attached to a real terminal, so run.sh also works headless
# (e.g. launched by an agent or launchd).
TTY_FLAGS=""
if [ -t 0 ]; then
  TTY_FLAGS="-it"
fi
docker run $TTY_FLAGS --rm \
  --name nano-claw-voice \
  -p 127.0.0.1:9090:8080 \
  -e ANTHROPIC_API_KEY \
  -e GEMINI_API_KEY \
  -e DEEPSEEK_API_KEY \
  -e XAI_API_KEY \
  -e GROQ_API_KEY \
  -e DASHSCOPE_API_KEY \
  -e OPENAI_API_KEY \
  -e OPENROUTER_API_KEY \
  -e NANO_CLAW_BARGE_IN \
  -e NANO_CLAW_MEMORY_DIR="$NANO_CLAW_MEMORY_DIR" \
  -e NANO_CLAW_SENTENCE_GAP_MS \
  -e NANO_CLAW_DECLICK_IN_MS \
  -e NANO_CLAW_DECLICK_OUT_MS \
  -e NANO_CLAW_LUX_TRIM_MS \
  -e NANO_CLAW_SPEECH_PREPARATION \
  -e NANO_CLAW_SPEECH_MAX_WORDS \
  -e NANO_CLAW_SPEECH_MAX_CHUNK_MS \
  -e NANO_CLAW_STREAM \
  -e NANO_CLAW_WS_AUDIO="$NANO_CLAW_WS_AUDIO" \
  -e NANO_CLAW_KNOWLEDGE \
  -e NANO_CLAW_INTELLIGENCE_URL \
  -e NANO_CLAW_INTELLIGENCE_ENABLED \
  -e NANO_CLAW_INTELLIGENCE_TENANT \
  -e NANO_CLAW_INTELLIGENCE_COLLECTIONS \
  -e NANO_CLAW_INTELLIGENCE_PROFILE \
  -e NANO_CLAW_INTELLIGENCE_GROUNDING \
  -e NANO_CLAW_DEEP_REASONING \
  -e NANO_CLAW_DEEP_ROUTING \
  -e NANO_CLAW_DEEP_THRESHOLD \
  -e NANO_CLAW_DEEP_TIMEOUT_MS \
  -e NANO_CLAW_ANALYSIS_STYLE \
  -e NANO_CLAW_ARTIFACT_ROUTING \
  -e NANO_CLAW_ARTIFACT_ROUTE_MIN \
  -e NANO_CLAW_DEEP_CONFIRM \
  -e NANO_CLAW_DISABLE_TOOLS \
  -e NANO_CLAW_PHONE \
  -e TELNYX_API_KEY \
  -e NANO_CLAW_PHONE_WEBHOOK_BASE \
  -e NANO_CLAW_PHONE_TOKEN \
  -e NANO_CLAW_PHONE_GREETING \
  -e NANO_CLAW_PHONE_VOICE \
  -e NANO_CLAW_PHONE_STT_SIZE \
  -e NANO_CLAW_PHONE_CODEC \
  -e NANO_CLAW_PHONE_BARGE_IN \
  -e NANO_CLAW_PHONE_SPEECH_PREPARATION \
  -e NANO_CLAW_PHONE_DYNAMIC_ENDPOINT \
  -e NANO_CLAW_PHONE_VAD \
  -e NANO_CLAW_PHONE_TAP \
  -e NANO_CLAW_PHONE_TAP_DIR \
  -e NANO_CLAW_PHONE_RMS_MIN \
  -e NANO_CLAW_PHONE_RMS_RATIO \
  -e NANO_CLAW_PHONE_GAIN \
  -e NANO_CLAW_PHONE_GAIN_TARGET_DB \
  -e NANO_CLAW_PHONE_PREBUFFER_MS \
  -e NANO_CLAW_PHONE_PACE_FACTOR \
  -e NANO_CLAW_GOOGLE_CLIENT_ID \
  -e NANO_CLAW_AUTH \
  -e NANO_CLAW_PUBLIC_HTTPS \
  -e NANO_CLAW_VOICE_FLOW \
  -e SCHED_EVAL_MODEL \
  -e SCHED_EVAL_THINKING \
  -e NANO_CLAW_FLOW_AVAILABILITY \
  -e STT_SERVICE_URL="$STT_SERVICE_URL" \
  -e TTS_SERVICE_URL="$TTS_SERVICE_URL" \
  -e LUX_SERVICE_URL="$LUX_SERVICE_URL" \
  -v nano-claw-models:/app/voice/models \
  -v nano-claw-data:/app/data \
  -v "$SCRIPT_DIR/data":/app/sites:ro \
  nano-claw-voice
