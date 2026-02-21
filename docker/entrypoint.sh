#!/bin/bash
set -e

# Start nano-claw API (internal, not exposed)
node /app/dist/cli/index.js serve --port 3001 &
NC_PID=$!

# Wait for API readiness (fail if not ready after 30s)
READY=0
for i in $(seq 1 30); do
  if curl -sf http://localhost:3001/api/health >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "ERROR: nano-claw API did not become ready within 30s" >&2
  kill $NC_PID 2>/dev/null
  exit 1
fi

# Start voice server (exposed on 8080)
python -m voice &
VOICE_PID=$!

# Handle shutdown — kill children and wait for them to exit
trap "kill $NC_PID $VOICE_PID 2>/dev/null; wait $NC_PID $VOICE_PID 2>/dev/null" SIGTERM SIGINT
wait
