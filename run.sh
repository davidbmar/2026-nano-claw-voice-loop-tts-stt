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

# Run — pass ANTHROPIC_API_KEY from env
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "ERROR: ANTHROPIC_API_KEY not set. Export it first:"
  echo "  export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

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
  "$STT_VENV/bin/pip" install -q -r "$SCRIPT_DIR/stt-service/requirements.txt"
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
  "$TTS_VENV/bin/pip" install -q -r "$SCRIPT_DIR/tts-service/requirements.txt"
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

# Clean up services on exit
cleanup() {
  if [ -n "$STT_PID" ]; then
    echo ""
    echo "Stopping STT service (pid $STT_PID)..."
    kill $STT_PID 2>/dev/null
    wait $STT_PID 2>/dev/null
  fi
  if [ -n "$TTS_PID" ]; then
    echo "Stopping TTS service (pid $TTS_PID)..."
    kill $TTS_PID 2>/dev/null
    wait $TTS_PID 2>/dev/null
  fi
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
  -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e GEMINI_API_KEY \
  -e DEEPSEEK_API_KEY \
  -e GROQ_API_KEY \
  -e DASHSCOPE_API_KEY \
  -e OPENAI_API_KEY \
  -e NANO_CLAW_BARGE_IN \
  -e NANO_CLAW_STREAM \
  -e NANO_CLAW_KNOWLEDGE \
  -e NANO_CLAW_DISABLE_TOOLS \
  -e NANO_CLAW_PHONE \
  -e TELNYX_API_KEY \
  -e NANO_CLAW_PHONE_WEBHOOK_BASE \
  -e NANO_CLAW_PHONE_TOKEN \
  -e NANO_CLAW_PHONE_GREETING \
  -e NANO_CLAW_PHONE_VOICE \
  -e NANO_CLAW_PHONE_STT_SIZE \
  -e NANO_CLAW_PHONE_BARGE_IN \
  -e NANO_CLAW_PHONE_DYNAMIC_ENDPOINT \
  -e STT_SERVICE_URL="$STT_SERVICE_URL" \
  -e TTS_SERVICE_URL="$TTS_SERVICE_URL" \
  -v nano-claw-models:/app/voice/models \
  -v nano-claw-data:/app/data \
  -v "$SCRIPT_DIR/data":/app/sites:ro \
  nano-claw-voice
