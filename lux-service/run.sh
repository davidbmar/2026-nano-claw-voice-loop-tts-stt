#!/bin/bash
set -e
cd "$(dirname "$0")"

[ -d .venv ] || { echo "No venv — run lux-service/setup.sh first"; exit 1; }
[ -f .verified ] || { echo "Weights not verified — run lux-service/setup.sh first"; exit 1; }

source .venv/bin/activate

echo "Starting LuxTTS service on http://0.0.0.0:${LUX_PORT:-8301}"
echo "The Docker container will call this for LuxTTS voice-cloned speech."
echo ""
python server.py
