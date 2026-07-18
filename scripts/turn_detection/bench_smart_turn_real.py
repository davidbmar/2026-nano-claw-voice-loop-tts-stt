#!/usr/bin/env python3
"""Score smart-turn-v3 on REAL riff caller audio (the fragmented-turn call).

Segments audio_caller.wav with a simple energy endpointer (mirroring riff's
700ms behavior so segments == what riff treated as 'turns'), then scores
each segment: did the caller sound DONE at the moment riff cut them off?

Expected if smart-turn works: fragments like 'tell me ... like tell me
about' score LOW (incomplete → riff should have kept listening) while real
completions ('Hi, can you tell me about the latest launches?') score HIGH.
"""
import glob
import sys
import time
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/Users/davidmar/src/nano-claw")
from _whisper_features import compute_whisper_log_mel_features  # noqa: E402

MODEL = "/Users/davidmar/src/nano-claw/data/models/smart-turn-v3.2-cpu.onnx"
sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])


def score(pcm16k_f32: np.ndarray) -> tuple[float, float]:
    pcm = pcm16k_f32[-128000:]
    if len(pcm) < 128000:
        pcm = np.concatenate([np.zeros(128000 - len(pcm), dtype=np.float32), pcm])
    mel = compute_whisper_log_mel_features(pcm, do_normalize=True)
    x = np.expand_dims(mel.astype(np.float32), 0)
    t0 = time.perf_counter()
    raw = float(sess.run(None, {"input_features": x})[0].squeeze())
    return raw, (time.perf_counter() - t0) * 1000


def segments(pcm: np.ndarray, rate: int = 16000, silence_ms: int = 700,
             min_speech_ms: int = 250, rms_thr: float = 300.0):
    """Riff-like segmentation: utterance ends after `silence_ms` of quiet."""
    frame = rate * 20 // 1000
    n = len(pcm) // frame
    in_utt, start, silence, speech = False, 0, 0, 0
    for i in range(n):
        f = pcm[i * frame:(i + 1) * frame].astype(np.float64)
        is_speech = np.sqrt(np.mean(f ** 2)) >= rms_thr
        if not in_utt:
            if is_speech:
                in_utt, start, silence, speech = True, max(0, i - 10), 0, 20
        else:
            if is_speech:
                silence, speech = 0, speech + 20
            else:
                silence += 20
            if silence >= silence_ms:
                if speech >= min_speech_ms:
                    yield start * frame, (i + 1) * frame
                in_utt = False
    if in_utt and speech >= min_speech_ms:
        yield start * frame, n * frame


for path in sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else
                             "/Users/davidmar/riff-dev-data/sessions/v3:YLa_t*/audio_caller.wav")):
    call = Path(path).parent.name[:12]
    w = wave.open(path)
    assert w.getframerate() == 16000
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    pcm_f = pcm.astype(np.float32) / 32768.0
    print(f"\n=== {call} ({len(pcm)/16000:.0f}s) ===")
    print(f"{'t(s)':>7} {'dur':>5} {'smart-turn':>10}  verdict")
    for s, e in segments(pcm):
        prob, ms = score(pcm_f[s:e])
        verdict = "COMPLETE " if prob > 0.5 else "INCOMPLETE"
        print(f"{s/16000:>7.1f} {(e-s)/16000:>5.1f} {prob:>10.3f}  {verdict}  ({ms:.0f}ms)")
