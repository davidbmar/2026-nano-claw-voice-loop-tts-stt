"""Phone-audio primitives: PCMU/L16 codecs, resampling, and endpointing.

The Telnyx media WebSocket carries base64 PCMU @ 8 kHz by default or raw L16
PCM @ 16 kHz when wideband audio is enabled. The rest of nano-claw speaks
PCM16 (STT accepts any rate via X-Sample-Rate, TTS emits 48 kHz). This module
owns every conversion in between, plus the silence-based utterance endpointer
phone callers need (there is no mic button on a phone).

μ-law kernels adapted from riff/phone/audio_codec.py (same owner) — they
have carried live PSTN traffic on this exact number.
"""

from __future__ import annotations

import os

import numpy as np

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635

TELNYX_RATE = 8000
TTS_RATE = 48000
FRAME_MS = 20  # Telnyx media frame duration
FRAME_SAMPLES = TELNYX_RATE * FRAME_MS // 1000  # 160 samples / 20 ms

PCMU_RMS_MIN = 350.0
PCMU_RMS_RATIO = 0.0
L16_RMS_MIN = 120.0
L16_RMS_RATIO = 3.0
NOISE_FLOOR_ALPHA = 0.05


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
_FIR_48K_TO_16K: np.ndarray | None = None


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


def _fir_48k_to_16k() -> np.ndarray:
    """Unity-gain Hamming-windowed sinc lowpass for 48k→16k decimation.

    A 7.8 kHz cutoff preserves the wideband speech range while remaining
    below the 8 kHz Nyquist frequency of the 16 kHz output.
    """
    global _FIR_48K_TO_16K
    if _FIR_48K_TO_16K is None:
        num_taps = 127
        cutoff_hz = 7800.0
        n = np.arange(num_taps, dtype=np.float64) - ((num_taps - 1) / 2.0)
        normalized = cutoff_hz / TTS_RATE
        taps = 2.0 * normalized * np.sinc(2.0 * normalized * n)
        taps *= np.hamming(num_taps)
        taps /= taps.sum()
        _FIR_48K_TO_16K = taps
    return _FIR_48K_TO_16K


def resample_8k_to_16k(pcm16_8k: np.ndarray) -> np.ndarray:
    """Upsample 8 kHz PCM16 to 16 kHz by linear interpolation (exactly 2×).

    Adequate for upsampling (no aliasing risk); used to feed 16 kHz models
    (Whisper-family, smart-turn) from the 8 kHz phone leg.
    """
    if len(pcm16_8k) == 0:
        return np.zeros(0, dtype=np.int16)
    x = pcm16_8k.astype(np.int32)
    mid = (x + np.concatenate([x[1:], x[-1:]])) // 2
    out = np.empty(2 * len(x), dtype=np.int32)
    out[0::2] = x
    out[1::2] = mid
    return out.astype(np.int16)


def resample_48k_to_8k(pcm16_48k: np.ndarray) -> np.ndarray:
    """Downsample 48 kHz PCM16 to 8 kHz: FIR lowpass then take every 6th."""
    if len(pcm16_48k) == 0:
        return np.zeros(0, dtype=np.int16)
    filtered = np.convolve(pcm16_48k.astype(np.float64), _fir_48k_to_8k(), mode="same")
    decimated = filtered[::6]
    return np.clip(decimated, -32768, 32767).astype(np.int16)


def resample_48k_to_16k(pcm16_48k: np.ndarray) -> np.ndarray:
    """Downsample 48 kHz PCM16 to 16 kHz: FIR lowpass then decimate by 3."""
    if len(pcm16_48k) == 0:
        return np.zeros(0, dtype=np.int16)
    filtered = np.convolve(pcm16_48k.astype(np.float64), _fir_48k_to_16k(), mode="same")
    decimated = filtered[::3]
    return np.clip(decimated, -32768, 32767).astype(np.int16)


def _nonnegative_env_float(name: str, default: float) -> float:
    """Read a finite, non-negative float from the environment."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if np.isfinite(value) and value >= 0.0 else default


class NoiseFloorEstimator:
    """Rolling non-speech RMS floor and its adaptive speech threshold.

    ``floor`` and ``min_threshold`` are linear int16 RMS values. Each frame
    already classified as non-speech updates ``floor`` with an exponential
    moving average; speech frames leave it untouched. The resulting decision
    boundary is always ``max(min_threshold, floor * ratio)``.
    """

    def __init__(
        self,
        *,
        min_threshold: float,
        ratio: float,
        alpha: float = NOISE_FLOOR_ALPHA,
    ) -> None:
        if not np.isfinite(min_threshold) or min_threshold < 0.0:
            raise ValueError("min_threshold must be finite and non-negative")
        if not np.isfinite(ratio) or ratio < 0.0:
            raise ValueError("ratio must be finite and non-negative")
        if not np.isfinite(alpha) or not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be finite and in (0, 1]")
        self.min_threshold = float(min_threshold)
        self.ratio = float(ratio)
        self.alpha = float(alpha)
        self.floor = 0.0
        self._initialized = False

    @property
    def effective_threshold(self) -> float:
        """Current speech boundary in linear int16 RMS units."""
        return max(self.min_threshold, self.floor * self.ratio)

    def observe(self, rms: float, *, is_speech: bool) -> None:
        """Observe one classified frame, updating only for non-speech."""
        if is_speech or not np.isfinite(rms):
            return
        sample = max(0.0, float(rms))
        if not self._initialized:
            self.floor = sample
            self._initialized = True
            return
        self.floor += self.alpha * (sample - self.floor)

    def classify(self, rms: float) -> bool:
        """Classify one RMS sample, then learn from it when it is noise."""
        is_speech = float(rms) >= self.effective_threshold
        self.observe(rms, is_speech=is_speech)
        return is_speech


class UtteranceEndpointer:
    """Energy-based end-of-utterance detection for PCM16 phone frames.

    feed() consumes PCM16 frames and returns a completed utterance's PCM
    bytes when the caller has spoken and then gone quiet, else None.

    Energy and floor values are linear RMS over int16 samples (0–32768). The
    rolling floor is an EMA of frames classified non-speech, and the effective
    threshold is ``max(rms_min, floor * rms_ratio)``. The floor is frozen on
    speech so a caller's voice cannot raise the boundary and swallow their
    next soft word. By default PCMU retains the historical fixed 350-RMS
    decision exactly (its ratio is zero); L16 uses a lower minimum plus the
    adaptive floor. ``NANO_CLAW_PHONE_RMS_MIN`` and
    ``NANO_CLAW_PHONE_RMS_RATIO`` override those codec defaults.

    Utterances are capped so a monologue (or hold music) cannot buffer
    unbounded audio.
    """

    def __init__(
        self,
        *,
        rms_threshold: float | None = None,
        min_speech_ms: int = 250,
        end_silence_ms: int = 700,
        max_utterance_ms: int = 15_000,
        preroll_ms: int = 240,
        rate_hz: int = 8000,
        codec: str | None = None,
        rms_min: float | None = None,
        rms_ratio: float | None = None,
        noise_floor_alpha: float = NOISE_FLOOR_ALPHA,
    ) -> None:
        self.codec = (codec or ("l16" if rate_hz == 16000 else "pcmu")).lower()
        if self.codec not in ("pcmu", "l16"):
            raise ValueError("codec must be 'pcmu' or 'l16'")
        if rms_threshold is not None and rms_min is not None:
            raise ValueError("use rms_threshold or rms_min, not both")
        default_min = L16_RMS_MIN if self.codec == "l16" else PCMU_RMS_MIN
        default_ratio = L16_RMS_RATIO if self.codec == "l16" else PCMU_RMS_RATIO
        explicit_min = rms_min if rms_min is not None else rms_threshold
        resolved_min = (
            float(explicit_min)
            if explicit_min is not None
            else _nonnegative_env_float("NANO_CLAW_PHONE_RMS_MIN", default_min)
        )
        resolved_ratio = (
            float(rms_ratio)
            if rms_ratio is not None
            else _nonnegative_env_float("NANO_CLAW_PHONE_RMS_RATIO", default_ratio)
        )
        self.rms_min = resolved_min
        self.rms_ratio = resolved_ratio
        # Preserve the public legacy attribute: for PCMU's default ratio=0 it
        # remains the exact fixed threshold used before adaptive endpointing.
        self.rms_threshold = resolved_min
        self._noise_floor = NoiseFloorEstimator(
            min_threshold=resolved_min,
            ratio=resolved_ratio,
            alpha=noise_floor_alpha,
        )
        self.current_rms = 0.0
        self.min_speech_ms = min_speech_ms
        self.end_silence_ms = end_silence_ms
        self.max_utterance_ms = max_utterance_ms
        self.rate_hz = rate_hz
        frame_samples = max(1, rate_hz * FRAME_MS // 1000)
        preroll_samples = rate_hz * preroll_ms // 1000
        self._preroll_frames = max(1, preroll_samples // frame_samples)
        self.reset()

    @property
    def noise_floor(self) -> float:
        """Current rolling non-speech floor in linear int16 RMS units."""
        return self._noise_floor.floor

    @property
    def effective_threshold(self) -> float:
        """Current energy-mode speech threshold in int16 RMS units."""
        return self._noise_floor.effective_threshold

    @property
    def in_utterance(self) -> bool:
        """Whether caller audio is currently being accumulated."""
        return self._in_utterance

    def reset(self) -> None:
        self._frames: list[np.ndarray] = []
        self._preroll: list[np.ndarray] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_utterance = False

    def prime(self, frames: list[np.ndarray]) -> None:
        """Begin an utterance pre-seeded with frames captured elsewhere
        (barge-in: the speech that interrupted playback IS the next turn)."""
        self.reset()
        if not frames:
            return
        self._in_utterance = True
        self._frames = list(frames)
        self._speech_ms = sum(len(f) for f in frames) * 1000 // self.rate_hz
        self._silence_ms = 0

    def feed(self, frame: np.ndarray, is_speech: bool | None = None) -> bytes | None:
        """Consume one PCM16 frame (any length ≥ 1 sample at ``rate_hz``).

        is_speech: externally-decided speech flag (e.g. Silero VAD). When
        None, uses the adaptive energy threshold. External decisions remain
        authoritative but still teach the floor when they classify non-speech.
        """
        frame_ms = len(frame) * 1000 // self.rate_hz
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) if len(frame) else 0.0
        self.current_rms = rms
        if is_speech is None:
            is_speech = self._noise_floor.classify(rms)
        else:
            is_speech = bool(is_speech)
            self._noise_floor.observe(rms, is_speech=is_speech)

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

        utterance_ms = sum(len(f) for f in self._frames) * 1000 // self.rate_hz
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


# Words that end an utterance only when the speaker isn't done: prepositions,
# conjunctions, articles, fillers, and dangling verbs. Derived from riff's
# observed fragmented turns ("...like tell me about", "What is the next",
# "...hear about, um,") — a transcript ending here means "keep listening".
# "more" and "this" are excluded because they often finish a thought cleanly
# ("tell me more", "what is this").
_INCOMPLETE_TAIL_WORDS = {
    # prepositions / particles
    "about", "with", "for", "of", "to", "in", "on", "at", "by", "from",
    "into", "onto", "over", "under", "between", "through", "during",
    # conjunctions / connectors
    "and", "or", "but", "so", "because", "if", "when", "while", "than",
    "that", "which", "whose", "where",
    # articles / determiners / possessives
    "the", "a", "an", "my", "your", "our", "their", "his", "her", "its",
    "these", "those", "some", "any", "every", "each",
    # fillers / hesitations
    "um", "uh", "umm", "uhh", "er", "ah", "hmm", "like", "you know",
    # dangling verbs / auxiliaries
    "is", "are", "was", "were", "be", "being", "have", "has", "had",
    "do", "does", "did", "can", "could", "will", "would", "should",
    "want", "need", "gonna", "wanna", "let", "tell", "give", "show",
    # dangling adjectives/ordinals before an elided noun ("the next…")
    "next", "last", "latest", "first", "second", "other", "another",
    "most", "best", "new",
}


def transcript_looks_incomplete(text: str) -> bool:
    """Semantic tail check: does this transcript end mid-thought?

    The cheap, license-free half of turn detection: acoustics miss callers
    who trail off with 'finished' prosody, but a sentence ending in a
    preposition/article/filler is incomplete no matter how it sounded.
    """
    cleaned = text.strip().rstrip(".!?").strip()
    if not cleaned:
        return False
    if cleaned.endswith(",") or cleaned.endswith("-") or cleaned.endswith("…"):
        return True
    last = cleaned.split()[-1].lower().strip(",;:")
    return last in _INCOMPLETE_TAIL_WORDS


class BargeInDetector:
    """Detects sustained caller speech while the agent is talking.

    Stricter than the endpointer on purpose: the trigger threshold is higher
    and requires sustained speech, so line noise, clicks, or acoustic echo
    from a speakerphone don't cut the agent off mid-sentence.
    """

    def __init__(
        self,
        *,
        rms_threshold: float = 550.0,
        trigger_ms: int = 240,
        window_ms: int = 800,
        rate_hz: int = 8000,
    ) -> None:
        self.rms_threshold = rms_threshold
        self.trigger_ms = trigger_ms
        self.rate_hz = rate_hz
        self._window_frames = max(1, window_ms // FRAME_MS)
        self.reset()

    def reset(self) -> None:
        self._recent: list[tuple[np.ndarray, bool]] = []

    def feed(self, frame: np.ndarray, is_speech: bool | None = None) -> bool:
        """Returns True when the caller has clearly started talking over us.

        is_speech: externally-decided flag (Silero); None = internal RMS.
        Neural VAD matters most here — TTS echo and line noise cross energy
        thresholds but don't classify as speech.
        """
        if is_speech is None:
            rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) if len(frame) else 0.0
            is_speech = rms >= self.rms_threshold
        self._recent.append((frame, is_speech))
        if len(self._recent) > self._window_frames:
            self._recent.pop(0)
        speech_ms = sum(len(f) for f, s in self._recent if s) * 1000 // self.rate_hz
        return speech_ms >= self.trigger_ms

    def take_frames(self) -> list[np.ndarray]:
        """The buffered window (the interruption itself), for endpointer priming."""
        frames = [f for f, _ in self._recent]
        self.reset()
        return frames


def pcm48k_to_ulaw_frames(pcm48k_bytes: bytes) -> list[bytes]:
    """48 kHz PCM16 bytes (TTS output) → list of 20 ms μ-law frames."""
    pcm48k = np.frombuffer(pcm48k_bytes, dtype=np.int16)
    pcm8k = resample_48k_to_8k(pcm48k)
    ulaw = ulaw_encode(pcm8k)
    return [ulaw[i : i + FRAME_SAMPLES] for i in range(0, len(ulaw), FRAME_SAMPLES)]


def pcm48k_to_l16_frames(pcm48k_bytes: bytes) -> list[bytes]:
    """48 kHz PCM16 bytes → raw 16 kHz PCM16 in 20 ms Telnyx frames."""
    pcm48k = np.frombuffer(pcm48k_bytes, dtype=np.int16)
    pcm16k = resample_48k_to_16k(pcm48k)
    frame_samples = 16000 * FRAME_MS // 1000
    return [
        pcm16k[i : i + frame_samples].tobytes()
        for i in range(0, len(pcm16k), frame_samples)
    ]
