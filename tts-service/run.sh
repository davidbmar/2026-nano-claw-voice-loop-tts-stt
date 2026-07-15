#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Installing TTS service dependencies..."
pip install -r requirements.txt

echo ""
echo "Starting TTS service on http://0.0.0.0:8300"
echo "The Docker container will call this for Kokoro text-to-speech."
echo ""
python server.py
