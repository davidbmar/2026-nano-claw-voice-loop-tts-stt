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
    ENTER = 0.5
    EXIT = 0.35

    def __init__(self, sample_rate: int = 8000) -> None:
        self._sr = np.array(sample_rate, dtype=np.int64)
        self._chunk = CHUNK_8K if sample_rate == 8000 else 512
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self.prob = 0.0
        self._in_speech = False

    def feed(self, frame_int16: np.ndarray) -> float:
        """Consume one frame; returns the latest speech probability."""
        sess = _get_session()
        if sess is None:
            return self.prob
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
        return self.prob

    def feed_speech(self, frame_int16: np.ndarray) -> bool:
        """feed() + hysteresis: the per-frame speech decision detectors use."""
        self.feed(frame_int16)
        threshold = self.EXIT if self._in_speech else self.ENTER
        self._in_speech = self.prob >= threshold
        return self._in_speech
