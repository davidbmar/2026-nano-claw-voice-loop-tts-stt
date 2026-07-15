"""Wrap raw int16 PCM into a WAV byte string (used by the preview endpoint)."""

import io
import wave


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Return a complete mono/int16 WAV file as bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()
