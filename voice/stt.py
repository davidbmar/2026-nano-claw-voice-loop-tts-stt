"""Faster-Whisper STT wrapper — audio bytes to text."""

import logging

import numpy as np
from scipy.signal import resample

log = logging.getLogger("stt")

MODEL_SIZE = "base"  # ~75MB, good accuracy for short utterances

# Lazy-loaded Whisper model
_model = None


def _get_model():
    """Load the faster-whisper model on first use (auto-downloads)."""
    global _model
    if _model is not None:
        return _model

    from faster_whisper import WhisperModel

    log.info("Loading faster-whisper model: %s (first run downloads ~75MB)...", MODEL_SIZE)
    _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    log.info("Whisper model loaded: %s", MODEL_SIZE)
    return _model


def transcribe(audio_bytes: bytes, sample_rate: int = 48000):
    """Transcribe PCM int16 audio bytes to text.

    Args:
        audio_bytes: Raw PCM int16 mono audio bytes.
        sample_rate: Sample rate of the audio (default 48kHz from WebRTC).

    Returns:
        Tuple of (text, no_speech_prob, avg_logprob, segment_timing).
    """
    if not audio_bytes:
        return "", 0.0, 0.0, []

    model = _get_model()

    # Convert int16 PCM to float32 normalized [-1.0, 1.0]
    samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    duration = len(samples) / sample_rate
    rms = float(np.sqrt(np.mean(samples ** 2)))
    peak = float(np.max(np.abs(samples)))
    log.info("STT input: %.2fs, %d samples @ %dHz — rms=%.4f peak=%.4f",
             duration, len(samples), sample_rate, rms, peak)

    # Resample to 16kHz — faster-whisper expects 16kHz input
    WHISPER_RATE = 16000
    if sample_rate != WHISPER_RATE:
        num_output = int(len(samples) * WHISPER_RATE / sample_rate)
        samples = resample(samples, num_output).astype(np.float32)

    segments, info = model.transcribe(samples, beam_size=5, language="en", word_timestamps=True)

    text_parts = []
    worst_no_speech = 0.0
    avg_logprobs = []
    segment_timing = []
    for segment in segments:
        text_parts.append(segment.text.strip())
        worst_no_speech = max(worst_no_speech, segment.no_speech_prob)
        avg_logprobs.append(segment.avg_logprob)
        words = []
        if hasattr(segment, 'words') and segment.words:
            for w in segment.words:
                words.append({"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3)})
        segment_timing.append({
            "text": segment.text.strip(),
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "words": words,
        })

    result = " ".join(text_parts).strip()
    avg_logprob = sum(avg_logprobs) / len(avg_logprobs) if avg_logprobs else 0.0
    log.info("Transcription: %r (no_speech=%.2f, avg_logprob=%.2f, %d segments)",
             result[:100], worst_no_speech, avg_logprob, len(segment_timing))
    return result, worst_no_speech, avg_logprob, segment_timing
