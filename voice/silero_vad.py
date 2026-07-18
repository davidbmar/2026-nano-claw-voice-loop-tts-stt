"""Silero VAD (MIT, snakers4/silero-vad) — neural speech/no-speech scoring.

The industry-consensus base VAD (LiveKit and pipecat both wrap this exact
model). Replaces RMS-threshold speech detection on the phone leg: echo of
our own TTS, clicks, hold music, and breath stop counting as "speech", and
quiet trailing words stop being dropped.

Streaming API: feed 20 ms phone frames (160 samples @ 8 kHz int16); the
wrapper rebuffers into Silero's 256-sample chunks and carries the model's
recurrent state across the call. `prob` holds the latest speech probability.

Vendored model: voice/models_static/silero_vad.onnx (~2.2 MB). CPU inference
runs ~1 ms per chunk via onnxruntime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger("nano-claw.vad")

MODEL_PATH = Path(__file__).parent / "models_static" / "silero_vad.onnx"
CHUNK_8K = 256  # samples per Silero inference at 8 kHz (32 ms)

_session = None
_load_failed = False


def _get_session():
    """Lazy-load the shared ONNX session; None if unavailable (fallback)."""
    global _session, _load_failed
    if _session is None and not _load_failed:
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            _session = ort.InferenceSession(
                str(MODEL_PATH), opts, providers=["CPUExecutionProvider"]
            )
            log.info("Silero VAD loaded (%s)", MODEL_PATH.name)
        except Exception:
            _load_failed = True
            log.exception("Silero VAD unavailable — callers fall back to energy VAD")
    return _session


def available() -> bool:
    return _get_session() is not None


class SileroVAD:
    """Per-call streaming scorer: one instance per phone call."""

    # Hysteresis: enter speech at ENTER, stay until prob drops below EXIT —
    # intra-word dips don't flicker the decision (standard VAD practice).
    # Tunable without a rebuild: live phone audio runs quieter/noisier than
    # archived recordings, and silero's 8k mode is weaker than 16k.
    ENTER = float(os.environ.get("NANO_CLAW_PHONE_VAD_ENTER", "0.5"))
    EXIT = float(os.environ.get("NANO_CLAW_PHONE_VAD_EXIT", "0.35"))

    def __init__(self, sample_rate: int = 8000, upsample_phone_audio: bool = False) -> None:
        # KNOWN BROKEN as of 2026-07-16: the 16k-upsampled path scores ~0.0
        # on everything (silero v5 likely wants its 64-sample context prefix
        # per chunk at 16k). The raw 8k path scores real callers correctly.
        # Keep upsample opt-in for debugging only.
        self._upsample = upsample_phone_audio and sample_rate == 8000
        rate = 16000 if self._upsample else sample_rate
        self._sr = np.array(rate, dtype=np.int64)
        self._chunk = CHUNK_8K if rate == 8000 else 512
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self.prob = 0.0
        self._in_speech = False
        # Rolling diagnostics (read by the phone gateway's periodic stats log)
        self.window_max = 0.0
        self.window_sum = 0.0
        self.window_n = 0

    def feed(self, frame_int16: np.ndarray) -> float:
        """Consume one frame; returns the latest speech probability."""
        sess = _get_session()
        if sess is None:
            return self.prob
        if self._upsample:
            from voice.phone_audio import resample_8k_to_16k

            frame_int16 = resample_8k_to_16k(np.asarray(frame_int16, dtype=np.int16))
        self._buf = np.concatenate(
            [self._buf, frame_int16.astype(np.float32) / 32768.0]
        )
        while len(self._buf) >= self._chunk:
            chunk, self._buf = self._buf[: self._chunk], self._buf[self._chunk:]
            out, self._state = sess.run(
                None,
                {
                    "input": chunk[np.newaxis, :],
                    "state": self._state,
                    "sr": self._sr,
                },
            )
            self.prob = float(out.squeeze())
            self.window_max = max(self.window_max, self.prob)
            self.window_sum += self.prob
            self.window_n += 1
        return self.prob

    def take_stats(self) -> tuple[float, float]:
        """(max, mean) prob since last call — for periodic diagnostics."""
        if not self.window_n:
            return 0.0, 0.0
        stats = (self.window_max, self.window_sum / self.window_n)
        self.window_max = self.window_sum = 0.0
        self.window_n = 0
        return stats

    def feed_speech(self, frame_int16: np.ndarray) -> bool:
        """feed() + hysteresis: the per-frame speech decision detectors use."""
        self.feed(frame_int16)
        threshold = self.EXIT if self._in_speech else self.ENTER
        self._in_speech = self.prob >= threshold
        return self._in_speech
