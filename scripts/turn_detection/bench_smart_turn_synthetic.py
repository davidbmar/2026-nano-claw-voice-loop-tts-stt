#!/usr/bin/env python3
"""Bench smart-turn-v3.2 on phone-realistic audio.

Cases: complete question vs mid-sentence cut, at clean 16k and at
8k-upsampled (narrowband, what the phone gateway would feed it).
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime as ort
from _whisper_features import compute_whisper_log_mel_features

ROOT = Path("/Users/davidmar/src/nano-claw")
sys.path.insert(0, str(ROOT))
from voice.phone_audio import resample_48k_to_8k, resample_8k_to_16k  # noqa: E402

MODEL = ROOT / "data/models/smart-turn-v3.2-cpu.onnx"

sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])



def tts48k(text: str) -> np.ndarray:
    body = json.dumps({"text": text, "voice": "af_heart", "speed": 1.0}).encode()
    req = urllib.request.Request("http://localhost:8300/synthesize", data=body,
                                 headers={"Content-Type": "application/json"})
    pcm = urllib.request.urlopen(req, timeout=60).read()
    return np.frombuffer(pcm, dtype=np.int16)


def score(pcm16k_f32: np.ndarray) -> tuple[float, float]:
    pcm = pcm16k_f32[-128000:]  # last 8s
    if len(pcm) < 128000:  # LEFT-pad: audio right-aligned per v3 spec
        pcm = np.concatenate([np.zeros(128000 - len(pcm), dtype=np.float32), pcm])
    mel = compute_whisper_log_mel_features(pcm, do_normalize=True)
    x = np.expand_dims(mel.astype(np.float32), 0)
    t0 = time.perf_counter()
    raw = float(sess.run(None, {"input_features": x})[0].squeeze())
    return raw, (time.perf_counter() - t0) * 1000


def to_f32(i16: np.ndarray) -> np.ndarray:
    return i16.astype(np.float32) / 32768.0


CASES = [
    ("complete: 'What is the next rocket launch?'", "What is the next rocket launch?"),
    ("complete: 'Tell me about the U F O cases.'", "Tell me about the U F O cases."),
    ("incomplete: 'What is the next...'", "What is the next"),
    ("incomplete: 'Tell me about the...'", "Tell me about the"),
    ("incomplete: 'I would like to hear about, um,'", "I would like to hear about, um,"),
]

print(f"{'case':<46} {'16k prob':>9} {'8k-band prob':>13} {'infer ms':>9}")
for name, text in CASES:
    pcm48 = tts48k(text)
    # trim TTS trailing silence (~find last sample above threshold)
    nz = np.where(np.abs(pcm48) > 500)[0]
    pcm48 = pcm48[: nz[-1] + 2400] if len(nz) else pcm48  # +50ms tail
    # clean 16k path (48k -> decimate by 3 via 8k*2 is lossy; use simple stride 3)
    pcm16_clean = to_f32(pcm48[::3])
    # phone path: 48k -> 8k (FIR) -> upsample 16k (what the gateway has)
    pcm16_phone = to_f32(resample_8k_to_16k(resample_48k_to_8k(pcm48)))
    p_clean, ms = score(pcm16_clean)
    p_phone, _ = score(pcm16_phone)
    print(f"{name:<46} {p_clean:>9.3f} {p_phone:>13.3f} {ms:>9.1f}")

print()
print("--- mid-audio hard cuts of a complete utterance (true acoustic incompleteness) ---")
pcm48 = tts48k("Tell me about the next rocket launch from Vandenberg in California.")
nz = np.where(np.abs(pcm48) > 500)[0]
pcm48 = pcm48[: nz[-1] + 2400]
for frac in (1.0, 0.75, 0.55, 0.35):
    cut = pcm48[: int(len(pcm48) * frac)]
    p_clean, ms = score(to_f32(cut[::3]))
    p_phone, _ = score(to_f32(resample_8k_to_16k(resample_48k_to_8k(cut))))
    print(f"cut at {int(frac*100):>3}%  clean16k={p_clean:.3f}  phone8k={p_phone:.3f}  ({ms:.0f}ms)")
p_sil, _ = score(np.zeros(32000, dtype=np.float32))
print(f"pure silence 2s: {p_sil:.3f}")
