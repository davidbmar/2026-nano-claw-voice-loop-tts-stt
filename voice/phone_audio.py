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

    def prime(self, frames: list[np.ndarray]) -> None:
        """Begin an utterance pre-seeded with frames captured elsewhere
        (barge-in: the speech that interrupted playback IS the next turn)."""
        self.reset()
        if not frames:
            return
        self._in_utterance = True
        self._frames = list(frames)
        self._speech_ms = sum(len(f) for f in frames) * 1000 // TELNYX_RATE
        self._silence_ms = 0

    def feed(self, frame: np.ndarray, is_speech: bool | None = None) -> bytes | None:
        """Consume one PCM16 frame (any length ≥ 1 sample at 8 kHz).

        is_speech: externally-decided speech flag (e.g. Silero VAD). When
        None, falls back to the internal RMS threshold (energy mode).
        """
        frame_ms = len(frame) * 1000 // TELNYX_RATE
        if is_speech is None:
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


# Words that end an utterance only when the speaker isn't done: prepositions,
# conjunctions, articles, fillers, and dangling verbs. Derived from riff's
# observed fragmented turns ("...like tell me about", "What is the next",
# "...hear about, um,") — a transcript ending here means "keep listening".
_INCOMPLETE_TAIL_WORDS = {
    # prepositions / particles
    "about", "with", "for", "of", "to", "in", "on", "at", "by", "from",
    "into", "onto", "over", "under", "between", "through", "during",
    # conjunctions / connectors
    "and", "or", "but", "so", "because", "if", "when", "while", "than",
    "that", "which", "whose", "where",
    # articles / determiners / possessives
    "the", "a", "an", "my", "your", "our", "their", "his", "her", "its",
    "this", "these", "those", "some", "any", "every", "each",
    # fillers / hesitations
    "um", "uh", "umm", "uhh", "er", "ah", "hmm", "like", "you know",
    # dangling verbs / auxiliaries
    "is", "are", "was", "were", "be", "being", "have", "has", "had",
    "do", "does", "did", "can", "could", "will", "would", "should",
    "want", "need", "gonna", "wanna", "let", "tell", "give", "show",
    # dangling adjectives/ordinals before an elided noun ("the next…")
    "next", "last", "latest", "first", "second", "other", "another",
    "more", "most", "best", "new",
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
    ) -> None:
        self.rms_threshold = rms_threshold
        self.trigger_ms = trigger_ms
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
        speech_ms = sum(len(f) for f, s in self._recent if s) * 1000 // TELNYX_RATE
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
