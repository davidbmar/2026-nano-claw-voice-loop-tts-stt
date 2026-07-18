#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Installing STT service dependencies..."
if [ -f requirements.lock ]; then
  pip install -r requirements.lock   # exact pinned versions — no auto-updates
else
  pip install -r requirements.txt
fi

echo ""
echo "Starting STT service on http://0.0.0.0:8200"
echo "The Docker container will call this for speech-to-text."
echo ""
python server.py
