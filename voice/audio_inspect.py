"""Seam-and-energy analysis for the call-review audio inspector.

The point of this module is to answer one question precisely: *where does the
outbound audio join, and does that join sound clean?* Chunk seams are where
LuxTTS artifacts (an abrupt onset, a truncated tail) become audible clicks.

TIMEBASE WARNING — the whole module depends on getting this right:
``outbound.wav`` is the concatenation of frames actually SENT, so its clock is
cumulative AUDIO time, which drifts from the tap's monotonic wall clock
whenever the line is silent. Everything here is therefore computed in audio
time, and seams are DETECTED from the waveform rather than reconstructed by
summing per-sentence durations (summing accumulates float error and, measured
over a 65-chunk call, put boundaries inside the following sentence).
"""

from __future__ import annotations

import json
import logging
import math
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("nano-claw.audio-inspect")

ENVELOPE_HOP_MS = 10
SILENCE_FLOOR = 60.0        # int16 amplitude treated as "line silent"
MIN_GAP_MS = 40             # a gap this long marks a chunk boundary
EDGE_WINDOW_MS = 1          # window used to score how abrupt an edge is
EDGE_CONTEXT_MS = 100       # local peak the edge is measured against
HARSH_EDGE_RATIO = 0.15     # >15% of local peak at an edge reads as a click


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a tap WAV; tolerate an unfinalized header (call never closed)."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16)
        if len(samples):
            return samples.astype(np.float32), rate
    except (wave.Error, EOFError, OSError):
        pass
    raw = path.read_bytes()
    if len(raw) <= 44:
        return np.zeros(0, dtype=np.float32), 16000
    return np.frombuffer(raw[44:], dtype=np.int16).astype(np.float32), 16000


def _envelope(samples: np.ndarray, rate: int) -> list[float]:
    hop = max(1, rate * ENVELOPE_HOP_MS // 1000)
    usable = len(samples) // hop * hop
    if not usable:
        return []
    blocks = samples[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    return [round(float(value), 1) for value in rms]


def _silence_gaps(samples: np.ndarray, rate: int) -> list[tuple[int, int]]:
    """Index ranges of sustained silence — the joins between spoken chunks."""
    if not len(samples):
        return []
    quiet = np.abs(samples) < SILENCE_FLOOR
    min_run = max(1, rate * MIN_GAP_MS // 1000)
    gaps: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_quiet in enumerate(quiet):
        if is_quiet:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= min_run:
                gaps.append((start, index))
            start = None
    if start is not None and len(quiet) - start >= min_run:
        gaps.append((start, len(quiet)))
    return gaps


def _edge_score(samples: np.ndarray, rate: int, index: int, *, leading: bool) -> float:
    """Level in the 1ms at an edge as a fraction of the local peak.

    A clean fade leaves this near zero; a hard step approaches 1.0 and is what
    a listener hears as a tick.
    """
    edge = max(1, rate * EDGE_WINDOW_MS // 1000)
    context = max(edge, rate * EDGE_CONTEXT_MS // 1000)
    if leading:  # audio starting at `index`
        window = samples[index : index + edge]
        local = samples[index : index + context]
    else:  # audio ending at `index`
        window = samples[max(0, index - edge) : index]
        local = samples[max(0, index - context) : index]
    if not len(window) or not len(local):
        return 0.0
    peak = float(np.max(np.abs(local)))
    if peak < 500.0:  # too quiet to judge; not a meaningful edge
        return 0.0
    return round(float(np.max(np.abs(window))) / peak, 4)


def analyze_outbound(tap_dir: Path) -> dict:
    """Envelope + scored seams for one call's outbound audio."""
    path = Path(tap_dir) / "outbound.wav"
    if not path.is_file():
        return {"available": False}
    samples, rate = _read_wav(path)
    if not len(samples):
        return {"available": False}

    gaps = _silence_gaps(samples, rate)
    seams = []
    for start, end in gaps:
        out_score = _edge_score(samples, rate, start, leading=False)
        in_score = _edge_score(samples, rate, end, leading=True)
        seams.append(
            {
                "gapStart": round(start / rate, 3),
                "gapEnd": round(end / rate, 3),
                "gapMs": round((end - start) / rate * 1000, 1),
                # How abruptly audio stopped before the gap / resumed after it.
                "fadeOut": out_score,
                "fadeIn": in_score,
                "harsh": bool(
                    out_score > HARSH_EDGE_RATIO or in_score > HARSH_EDGE_RATIO
                ),
            }
        )
    scores_in = [seam["fadeIn"] for seam in seams if seam["fadeIn"] > 0]
    scores_out = [seam["fadeOut"] for seam in seams if seam["fadeOut"] > 0]

    def _summary(values: list[float]) -> dict:
        if not values:
            return {"median": 0.0, "p90": 0.0, "worst": 0.0}
        array = np.array(values)
        return {
            "median": round(float(np.median(array)), 4),
            "p90": round(float(np.percentile(array, 90)), 4),
            "worst": round(float(array.max()), 4),
        }

    return {
        "available": True,
        "sampleRate": rate,
        "durationS": round(len(samples) / rate, 3),
        "hopMs": ENVELOPE_HOP_MS,
        "peak": int(np.max(np.abs(samples))),
        "envelope": _envelope(samples, rate),
        "seams": seams,
        "edgeSummary": {"fadeIn": _summary(scores_in), "fadeOut": _summary(scores_out)},
        "harshCount": sum(1 for seam in seams if seam["harsh"]),
    }


def band_energy(tap_dir: Path) -> dict:
    """Coarse spectral balance of the outbound leg (crispness diagnosis).

    Consonant intelligibility lives in 2-4 kHz; a voice with almost all of its
    energy below 1 kHz reads as warm but muddy over a phone.
    """
    path = Path(tap_dir) / "outbound.wav"
    if not path.is_file():
        return {}
    samples, rate = _read_wav(path)
    if len(samples) < rate:
        return {}
    middle = samples[len(samples) // 3 : len(samples) // 3 + rate * 10]
    if len(middle) < rate:
        middle = samples[:rate]
    windowed = middle * np.hanning(len(middle))
    power = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(len(windowed), 1.0 / rate)
    total = float(power.sum()) or 1.0
    bands = {"sub1k": (0, 1000), "b1k4k": (1000, 4000), "b4k7k": (4000, 7000)}
    return {
        name: round(float(power[(freqs >= lo) & (freqs < hi)].sum() / total), 4)
        for name, (lo, hi) in bands.items()
    }


def synth_markers(tap_dir: Path) -> list[dict]:
    """Per-sentence synthesis records, as DECLARED by the tap.

    Kept separate from detected seams on purpose: these are wall-clock events
    and their cumulative audio durations drift from the concatenated WAV, so
    they are shown as advisory labels, never as authoritative seam positions.
    """
    path = Path(tap_dir) / "timings.jsonl"
    if not path.is_file():
        return []
    markers: list[dict] = []
    cumulative = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event") != "frames_sent":
                continue
            audio_s = float(event.get("audio_s") or 0.0)
            markers.append(
                {
                    "sentenceIndex": event.get("sentence_index"),
                    "declaredStart": round(cumulative, 3),
                    "declaredEnd": round(cumulative + audio_s, 3),
                    "lastFrameMs": event.get("last_frame_audio_ms"),
                }
            )
            cumulative += audio_s
    except (OSError, ValueError, json.JSONDecodeError):
        log.exception("synth marker parse failed for %s", tap_dir)
        return []
    return markers


def audio_bytes_for_stt(tap_dir: Path) -> tuple[bytes, int]:
    """Raw PCM16 + rate for the outbound leg, ready to POST to stt-service."""
    samples, rate = _read_wav(Path(tap_dir) / "outbound.wav")
    if not len(samples):
        return b"", rate
    clipped = np.clip(samples, -32768, 32767).astype(np.int16)
    return clipped.tobytes(), rate


def summarize(tap_dir: Path) -> dict:
    """Everything the inspector needs except the (slow) word alignment."""
    analysis = analyze_outbound(Path(tap_dir))
    if not analysis.get("available"):
        return analysis
    analysis["bands"] = band_energy(Path(tap_dir))
    analysis["synthMarkers"] = synth_markers(Path(tap_dir))
    return analysis


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)
