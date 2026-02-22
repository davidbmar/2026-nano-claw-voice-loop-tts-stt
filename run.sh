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
df -h / | tail -1 | awk '{print "Free: " $4 " / " $2 " (" $5 " used)"}'
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
docker build -t nano-claw-voice .

echo ""
echo "=== Disk Space After Build ==="
df -h / | tail -1 | awk '{print "Free: " $4 " / " $2 " (" $5 " used)"}'
echo ""

# Run — pass ANTHROPIC_API_KEY from env
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "ERROR: ANTHROPIC_API_KEY not set. Export it first:"
  echo "  export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

# Start STT service if not already running
STT_SERVICE_URL="${STT_SERVICE_URL:-http://host.docker.internal:8200}"
STT_CHECK_URL="${STT_SERVICE_URL/host.docker.internal/localhost}"
STT_PID=""

if curl -sf "$STT_CHECK_URL/health" >/dev/null 2>&1; then
  echo "STT service already running at $STT_CHECK_URL"
else
  echo "=== Starting STT service ==="
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  pip install -q -r "$SCRIPT_DIR/stt-service/requirements.txt"
  python "$SCRIPT_DIR/stt-service/server.py" &
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

# Clean up STT service on exit
cleanup() {
  if [ -n "$STT_PID" ]; then
    echo ""
    echo "Stopping STT service (pid $STT_PID)..."
    kill $STT_PID 2>/dev/null
    wait $STT_PID 2>/dev/null
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
  -v nano-claw-models:/app/voice/models \
  nano-claw-voice
