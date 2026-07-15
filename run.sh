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

# Remove old container if running
OLD=$(docker ps -aq --filter ancestor=nano-claw-voice 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "Stopping old container(s)..."
  docker rm -f $OLD
fi

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
    python3 -m venv "$STT_VENV"
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
    python3 -m venv "$TTS_VENV"
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
docker run -it --rm \
  -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e STT_SERVICE_URL="$STT_SERVICE_URL" \
  -e TTS_SERVICE_URL="$TTS_SERVICE_URL" \
  -v nano-claw-models:/app/voice/models \
  nano-claw-voice
