#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Installing TTS service dependencies..."
if [ -f requirements.lock ]; then
  pip install -r requirements.lock   # exact pinned versions — no auto-updates
else
  pip install -r requirements.txt
fi

echo ""
echo "Starting TTS service on http://0.0.0.0:8300"
echo "The Docker container will call this for Kokoro text-to-speech."
echo ""
python server.py
