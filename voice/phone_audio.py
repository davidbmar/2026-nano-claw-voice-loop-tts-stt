"""Phone-audio primitives: G.711 μ-law codec, resampling, and endpointing.

The Telnyx media WebSocket carries base64 μ-law @ 8 kHz in both directions;
the rest of nano-claw speaks PCM16 (STT accepts any rate via X-Sample-Rate,
TTS emits 48 kHz). This module owns every conversion in between, plus the
silence-based utterance endpointer phone callers need (there is no mic
button on a phone).

μ-law kernels adapted from riff/phone/audio_codec.py (same owner) — they
have carried live PSTN traffic on this exact number.
"""

from __future__ import annotations

import numpy as np

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635

TELNYX_RATE = 8000
TTS_RATE = 48000
FRAME_MS = 20  # Telnyx media frame duration
FRAME_SAMPLES = TELNYX_RATE * FRAME_MS // 1000  # 160 samples / 20 ms


def ulaw_encode(pcm16: np.ndarray) -> bytes:
    """Linear PCM16 → μ-law bytes (one byte per sample, G.711)."""
    if pcm16.dtype != np.int16:
        pcm16 = pcm16.astype(np.int16)
    x = pcm16.astype(np.int32)
    sign = (x < 0).astype(np.uint8) * 0x80
    x = np.abs(x)
    x = np.minimum(x, _ULAW_CLIP) + _ULAW_BIAS
    exp = np.zeros_like(x, dtype=np.uint8)
    mask = x >> 7
    for i in range(7, 0, -1):
        above = (mask >= (1 << i)).astype(np.uint8)
        not_set = exp == 0
        exp = np.where(above & not_set, np.uint8(i), exp)
    mantissa = ((x >> (exp.astype(np.int32) + 3)) & 0x0F).astype(np.uint8)
    ulaw = ~(sign | (exp << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


def ulaw_decode(ulaw_bytes: bytes) -> np.ndarray:
    """μ-law bytes → linear PCM16 samples (int16 numpy array)."""
    u = np.frombuffer(ulaw_bytes, dtype=np.uint8)
    u = ~u & 0xFF
    sign = (u & 0x80) != 0
    exp = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa.astype(np.int32) << 3) + _ULAW_BIAS) << exp.astype(np.int32)
    magnitude -= _ULAW_BIAS
    pcm = np.where(sign, -magnitude, magnitude)
    return pcm.astype(np.int16)


_FIR_48K_TO_8K: np.ndarray | None = None


def _fir_48k_to_8k() -> np.ndarray:
    """Unity-gain Hamming-windowed sinc lowpass for 48k→8k decimation.

    3.4 kHz cutoff keeps the telephone speech band and leaves transition
    room before the 4 kHz Nyquist of the 8 kHz output.
    """
    global _FIR_48K_TO_8K
    if _FIR_48K_TO_8K is None:
        num_taps = 127
        cutoff_hz = 3400.0
        n = np.arange(num_taps, dtype=np.float64) - ((num_taps - 1) / 2.0)
        normalized = cutoff_hz / TTS_RATE
        taps = 2.0 * normalized * np.sinc(2.0 * normalized * n)
        taps *= np.hamming(num_taps)
        taps /= taps.sum()
        _FIR_48K_TO_8K = taps
    return _FIR_48K_TO_8K


def resample_48k_to_8k(pcm16_48k: np.ndarray) -> np.ndarray:
    """Downsample 48 kHz PCM16 to 8 kHz: FIR lowpass then take every 6th."""
    if len(pcm16_48k) == 0:
        return np.zeros(0, dtype=np.int16)
    filtered = np.convolve(pcm16_48k.astype(np.float64), _fir_48k_to_8k(), mode="same")
    decimated = filtered[::6]
    return np.clip(decimated, -32768, 32767).astype(np.int16)


class UtteranceEndpointer:
    """Energy-based end-of-utterance detection for 8 kHz phone frames.

    feed() consumes PCM16 frames and returns a completed utterance's PCM
    bytes when the caller has spoken and then gone quiet, else None.

    Tuned for PSTN: μ-law noise floors are high, so the speech threshold is
    RMS over int16 samples rather than anything adaptive. Utterances are
    capped so a monologue (or hold music) can't buffer unbounded audio.
    """

    def __init__(
        self,
        *,
        rms_threshold: float = 350.0,
        min_speech_ms: int = 250,
        end_silence_ms: int = 700,
        max_utterance_ms: int = 15_000,
        preroll_ms: int = 240,
    ) -> None:
        self.rms_threshold = rms_threshold
        self.min_speech_ms = min_speech_ms
        self.end_silence_ms = end_silence_ms
        self.max_utterance_ms = max_utterance_ms
        self._preroll_frames = max(1, preroll_ms // FRAME_MS)
        self.reset()

    def reset(self) -> None:
        self._frames: list[np.ndarray] = []
        self._preroll: list[np.ndarray] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_utterance = False

    def feed(self, frame: np.ndarray) -> bytes | None:
        """Consume one PCM16 frame (any length ≥ 1 sample at 8 kHz)."""
        frame_ms = len(frame) * 1000 // TELNYX_RATE
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) if len(frame) else 0.0
        is_speech = rms >= self.rms_threshold

        if not self._in_utterance:
            # Keep a short preroll so the first syllable isn't clipped.
            self._preroll.append(frame)
            if len(self._preroll) > self._preroll_frames:
                self._preroll.pop(0)
            if is_speech:
                self._in_utterance = True
                self._frames = list(self._preroll)
                self._preroll = []
                self._speech_ms = frame_ms
                self._silence_ms = 0
            return None

        self._frames.append(frame)
        if is_speech:
            self._speech_ms += frame_ms
            self._silence_ms = 0
        else:
            self._silence_ms += frame_ms

        utterance_ms = sum(len(f) for f in self._frames) * 1000 // TELNYX_RATE
        ended = (
            self._silence_ms >= self.end_silence_ms
            or utterance_ms >= self.max_utterance_ms
        )
        if not ended:
            return None

        frames, spoke_enough = self._frames, self._speech_ms >= self.min_speech_ms
        self.reset()
        if not spoke_enough:
            return None  # a cough, click, or line noise — not a turn
        return np.concatenate(frames).astype(np.int16).tobytes()


def pcm48k_to_ulaw_frames(pcm48k_bytes: bytes) -> list[bytes]:
    """48 kHz PCM16 bytes (TTS output) → list of 20 ms μ-law frames."""
    pcm48k = np.frombuffer(pcm48k_bytes, dtype=np.int16)
    pcm8k = resample_48k_to_8k(pcm48k)
    ulaw = ulaw_encode(pcm8k)
    return [ulaw[i : i + FRAME_SAMPLES] for i in range(0, len(ulaw), FRAME_SAMPLES)]
