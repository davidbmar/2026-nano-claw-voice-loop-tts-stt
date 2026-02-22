#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Installing STT service dependencies..."
pip install -r requirements.txt

echo ""
echo "Starting STT service on http://0.0.0.0:8200"
echo "The Docker container will call this for speech-to-text."
echo ""
python server.py
