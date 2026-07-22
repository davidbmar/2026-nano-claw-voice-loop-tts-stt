#!/usr/bin/env python3
"""Summarize audio quality and timing measurements from one CallTap directory."""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np

WAV_NAMES = ("inbound.wav", "tts_48k.wav", "outbound.wav")


def _dbfs(amplitude: float) -> float:
    return -math.inf if amplitude <= 0.0 else 20.0 * math.log10(amplitude / 32768.0)


def _read_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.getnframes()
        if width != 2:
            raise ValueError(f"expected PCM16, found {width * 8}-bit samples")
        samples = np.frombuffer(source.readframes(frames), dtype="<i2")
    if channels > 1 and samples.size:
        samples = samples.reshape(-1, channels).astype(np.float64).mean(axis=1)
    values = samples.astype(np.float64)
    rms = float(np.sqrt(np.mean(values * values))) if values.size else 0.0
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    return {
        "rate": rate,
        "channels": channels,
        "frames": frames,
        "duration_s": frames / rate if rate else 0.0,
        "rms_dbfs": _dbfs(rms),
        "peak_dbfs": _dbfs(peak),
        "samples": values,
    }


def _above_4khz_fraction(samples: np.ndarray, rate: int) -> float:
    if samples.size < 2 or rate <= 8_000:
        return 0.0
    centered = samples - float(np.mean(samples))
    windowed = centered * np.hanning(centered.size)
    spectrum = np.fft.rfft(windowed)
    energy = np.abs(spectrum) ** 2
    total = float(np.sum(energy))
    if total <= 0.0:
        return 0.0
    frequencies = np.fft.rfftfreq(windowed.size, d=1.0 / rate)
    return float(np.sum(energy[frequencies > 4_000.0]) / total)


def _load_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return events, malformed
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    events.append(item)
                else:
                    malformed += 1
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
    events.sort(key=lambda item: float(item.get("t", 0.0)))
    return events, malformed


def _fmt_db(value: float) -> str:
    return "-inf" if not math.isfinite(value) else f"{value:.1f}"


def _frame_time(event: dict[str, Any], key: str) -> float:
    return float(event.get(key, event.get("t", 0.0)))


def _inter_sentence_gaps(frames_sent: list[dict[str, Any]]) -> list[float]:
    ordered = sorted(frames_sent, key=lambda item: _frame_time(item, "first_frame_t"))
    gaps: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = _frame_time(previous, "last_frame_t")
        previous_end += float(previous.get("last_frame_audio_ms", 0.0)) / 1000.0
        current_start = _frame_time(current, "first_frame_t")
        gaps.append((current_start - previous_end) * 1000.0)
    return gaps


def _print_gap_histogram(gaps: list[float]) -> None:
    print("\nInter-sentence gaps")
    if not gaps:
        print("  no adjacent sent sentences")
        return
    values = np.asarray(gaps, dtype=np.float64)
    print(
        "  count={} p50={:.1f} ms p95={:.1f} ms max={:.1f} ms".format(
            values.size,
            float(np.percentile(values, 50)),
            float(np.percentile(values, 95)),
            float(np.max(values)),
        )
    )
    bins = (
        ("< 0 ms", -math.inf, 0.0),
        ("0-100 ms", 0.0, 100.0),
        ("100-250 ms", 100.0, 250.0),
        ("250-500 ms", 250.0, 500.0),
        ("500-1000 ms", 500.0, 1_000.0),
        (">= 1000 ms", 1_000.0, math.inf),
    )
    for label, low, high in bins:
        count = sum(low <= value < high for value in gaps)
        print(f"  {label:>12}: {count:4d} {'#' * min(count, 40)}")


def _print_pacing(frames_sent: list[dict[str, Any]]) -> None:
    print("\nPacing")
    if not frames_sent:
        print("  no frames_sent events")
        return
    total_frames = sum(int(item.get("count", 0)) for item in frames_sent)
    total_audio = sum(float(item.get("audio_s", 0.0)) for item in frames_sent)
    total_elapsed = sum(float(item.get("elapsed_s", 0.0)) for item in frames_sent)
    total_surplus = sum(float(item.get("surplus_s", 0.0)) for item in frames_sent)
    p50s = [float(item.get("interval_p50_ms", 0.0)) for item in frames_sent]
    p95s = [float(item.get("interval_p95_ms", 0.0)) for item in frames_sent]
    maxima = [float(item.get("interval_max_ms", 0.0)) for item in frames_sent]
    print(f"  sentences={len(frames_sent)} frames={total_frames}")
    print(
        "  send intervals: median sentence p50={:.1f} ms, "
        "median sentence p95={:.1f} ms, worst={:.1f} ms".format(
            float(np.median(p50s)), float(np.median(p95s)), max(maxima)
        )
    )
    print(
        f"  audio={total_audio:.3f} s wall={total_elapsed:.3f} s "
        f"surplus={total_surplus:+.3f} s"
    )


def _matching_frames_event(
    barge: dict[str, Any], frames_sent: list[dict[str, Any]]
) -> dict[str, Any] | None:
    sentence_index = barge.get("sentence_index")
    if sentence_index is not None:
        matches = [
            item for item in frames_sent if item.get("sentence_index") == sentence_index
        ]
        if matches:
            return matches[-1]
    barge_t = float(barge.get("t", 0.0))
    completed_after = [item for item in frames_sent if float(item.get("t", 0.0)) >= barge_t]
    if completed_after:
        return min(completed_after, key=lambda item: float(item.get("t", 0.0)))
    if frames_sent:
        return max(frames_sent, key=lambda item: _frame_time(item, "last_frame_t"))
    return None


def _print_barge_latency(
    barges: list[dict[str, Any]], frames_sent: list[dict[str, Any]]
) -> None:
    print("\nBarge-in to last outbound frame")
    if not barges:
        print("  no barge_in events")
        return
    for number, barge in enumerate(barges, start=1):
        sent = _matching_frames_event(barge, frames_sent)
        if sent is None or "last_frame_t" not in sent:
            print(f"  barge {number}: unavailable (no timestamped outbound frame)")
            continue
        latency_ms = (
            float(sent["last_frame_t"]) - float(barge.get("t", 0.0))
        ) * 1000.0
        relation = "after" if latency_ms >= 0.0 else "before"
        print(
            f"  barge {number}: {abs(latency_ms):.1f} ms {relation} barge-in "
            f"(signed {latency_ms:+.1f} ms)"
        )


def report(tap_dir: Path) -> int:
    print(f"Phone tap report: {tap_dir}")
    print("\nWAV summary")
    audio: dict[str, dict[str, Any]] = {}
    for name in WAV_NAMES:
        path = tap_dir / name
        if not path.exists():
            print(f"  {name:<14} missing")
            continue
        try:
            stats = _read_wav(path)
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            print(f"  {name:<14} unreadable: {exc}")
            continue
        audio[name] = stats
        print(
            f"  {name:<14} {stats['duration_s']:.3f} s, {stats['rate']} Hz, "
            f"RMS {_fmt_db(stats['rms_dbfs'])} dBFS, "
            f"peak {_fmt_db(stats['peak_dbfs'])} dBFS"
        )

    print("\nSpectral energy above 4 kHz")
    for name in ("inbound.wav", "outbound.wav"):
        stats = audio.get(name)
        if stats is None:
            print(f"  {name:<14} unavailable")
            continue
        fraction = _above_4khz_fraction(stats["samples"], int(stats["rate"]))
        print(f"  {name:<14} {fraction:.6f} ({fraction * 100.0:.3f}%)")

    events, malformed = _load_events(tap_dir / "timings.jsonl")
    frames_sent = [item for item in events if item.get("event") == "frames_sent"]
    barges = [item for item in events if item.get("event") == "barge_in"]
    print(f"\nTiming events: {len(events)}" + (f" ({malformed} malformed lines ignored)" if malformed else ""))
    _print_gap_histogram(_inter_sentence_gaps(frames_sent))
    _print_pacing(frames_sent)
    _print_barge_latency(barges, frames_sent)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tap_dir", type=Path, help="per-call CallTap directory")
    args = parser.parse_args(argv)
    if not args.tap_dir.is_dir():
        parser.error(f"not a tap directory: {args.tap_dir}")
    return report(args.tap_dir)


if __name__ == "__main__":
    sys.exit(main())
