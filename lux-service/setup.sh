#!/bin/bash
# One-time setup for lux-service. Creates an isolated venv (python3.12 — torch
# has no 3.14 wheels), installs LuxTTS + LinaCodec at audited pins, downloads
# the pinned HF model snapshot, and pickle-scans the weights BEFORE anything
# ever loads them. server.py refuses to start until the .verified marker this
# script writes exists.
#
# Audit: .gstack/security-reports/2026-07-17-luxtts-supply-chain.json
set -e
cd "$(dirname "$0")"

LUXTTS_COMMIT=28ae6a61151684fffc9d1a7aa15eafa02286fe0b
LINACODEC_COMMIT=c0ae7c7285e121475c27592cfbb600624b714290
HF_REVISION=527f245a276a0eb42ea103a7a512bcfd771eb9b6
WHISPER_REVISION=e37978b90ca9030d5170a5c07aadb050351a65bb  # openai/whisper-base

PY=python3.12
command -v $PY >/dev/null || { echo "python3.12 not found (brew install python@3.12)"; exit 1; }

if [ ! -d .venv ]; then
  echo "Creating venv (.venv, $PY)..."
  $PY -m venv .venv
fi
source .venv/bin/activate

echo "Cloning LuxTTS at audited pin $LUXTTS_COMMIT ..."
if [ ! -d LuxTTS ]; then
  git clone https://github.com/ysharma3501/LuxTTS.git LuxTTS
fi
git -C LuxTTS fetch --quiet origin
git -C LuxTTS checkout --quiet "$LUXTTS_COMMIT"

echo "Installing dependencies ..."
# requirements.lock is a full pip freeze of the audited working venv — every
# package (including LinaCodec at its audited commit) at an exact version so
# nothing auto-updates. piper_phonemize wheels live on the k2-fsa find-links
# page (same source upstream ZipVoice documents).
if [ -f requirements.lock ]; then
  pip install --quiet --find-links https://k2-fsa.github.io/icefall/piper_phonemize.html \
    -r requirements.lock
else
  # Bootstrap path (no lock yet): resolve once, then freeze so the next
  # machine installs exactly this set.
  echo "No requirements.lock — resolving fresh, then freezing."
  pip install --quiet "linacodec @ git+https://github.com/ysharma3501/LinaCodec.git@$LINACODEC_COMMIT"
  grep -v "LinaCodec" LuxTTS/requirements.txt > /tmp/lux-reqs.txt
  pip install --quiet -r /tmp/lux-reqs.txt
  pip install --quiet fastapi 'uvicorn>=0.27' picklescan
  pip freeze --exclude-editable > requirements.lock
fi

echo "Downloading pinned model snapshots (LuxTTS $HF_REVISION, whisper-base ${WHISPER_REVISION:0:8}) ..."
SNAPSHOT=$(python - <<EOF
from huggingface_hub import snapshot_download
# Whisper transcriber used for reference-prompt encoding: prefetch at a pinned
# revision so the server can run with HF_HUB_OFFLINE=1 and never fetch at runtime.
snapshot_download("openai/whisper-base", revision="$WHISPER_REVISION")
print(snapshot_download("YatharthS/LuxTTS", revision="$HF_REVISION"))
EOF
)
echo "Snapshot: $SNAPSHOT"

echo "Pickle-scanning weights (model.pt, vocoder/vocos.bin) ..."
picklescan -p "$SNAPSHOT/model.pt"
picklescan -p "$SNAPSHOT/vocoder/vocos.bin"

echo "$HF_REVISION" > .verified
echo ""
echo "✓ lux-service verified and ready. Start with: lux-service/run.sh"
