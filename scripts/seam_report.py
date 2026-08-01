#!/usr/bin/env python3
"""Corpus-level seam report across recorded call taps.

`audio_inspect` answers "did THIS call click?" for one call at a time, in the
review panel. This answers the questions that actually drive a fix:

    how often does it happen, on which voices, and is it getting worse?

Run inside the container (the taps live at /app/data/phone-taps):

    docker exec nano-claw-voice python3 scripts/seam_report.py
    docker exec nano-claw-voice python3 scripts/seam_report.py --json
    docker exec nano-claw-voice python3 scripts/seam_report.py --worst 5

A harsh seam is one whose edge score exceeds audio_inspect.HARSH_EDGE_RATIO —
i.e. audio starts or stops at >15% of local peak, which reads as a click.

Note on interpretation: outbound.wav is CONCATENATED SENT AUDIO, not a
real-time recording, so positions here are audio-time and cannot be mapped to
wall-clock event timestamps. It is captured after transport send, so it is what
actually went on the wire.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice import audio_inspect  # noqa: E402

DEFAULT_ROOT = os.environ.get("NANO_CLAW_PHONE_TAP_DIR", "/app/data/phone-taps")


def scan(root: Path) -> list[dict]:
    """One record per tap directory that has usable outbound audio."""

    if not root.is_dir():
        return []
    rows = []
    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for d in dirs:
        try:
            analysis = audio_inspect.analyze_outbound(d)
        except Exception as exc:  # a corrupt tap must not abort the sweep
            rows.append({"call": d.name, "error": str(exc)})
            continue
        if not analysis.get("available"):
            continue
        seams = analysis.get("seams") or []
        harsh = int(analysis.get("harshCount", 0))
        duration = float(analysis.get("durationS") or 0.0)
        edge = analysis.get("edgeSummary") or {}
        rows.append(
            {
                "call": d.name,
                "durationS": round(duration, 1),
                "seamCount": len(seams),
                "harshCount": harsh,
                "harshRate": round(harsh / len(seams), 4) if seams else 0.0,
                "harshPerMinute": round(harsh / (duration / 60), 3) if duration else 0.0,
                "worstFadeIn": (edge.get("fadeIn") or {}).get("worst", 0.0),
                "worstFadeOut": (edge.get("fadeOut") or {}).get("worst", 0.0),
                "synthetic": d.name.startswith("loopback"),
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    """Corpus totals, split real vs loopback.

    The split matters: loopback exercises the same synthesis and framing with
    no carrier path, so it is the control. A defect present in both is ours; a
    defect only in real calls is downstream of our transmit point.
    """

    usable = [r for r in rows if "error" not in r]
    out = {"taps": len(rows), "analyzed": len(usable), "groups": {}}
    for label, subset in (
        ("real", [r for r in usable if not r["synthetic"]]),
        ("loopback", [r for r in usable if r["synthetic"]]),
    ):
        if not subset:
            continue
        seams = sum(r["seamCount"] for r in subset)
        harsh = sum(r["harshCount"] for r in subset)
        out["groups"][label] = {
            "calls": len(subset),
            "seams": seams,
            "harsh": harsh,
            "harshRate": round(harsh / seams, 4) if seams else 0.0,
            "medianHarshPerMinute": round(
                statistics.median([r["harshPerMinute"] for r in subset]), 3
            ),
            "worstFadeIn": max(r["worstFadeIn"] for r in subset),
            "worstFadeOut": max(r["worstFadeOut"] for r in subset),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT, help="tap directory root")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--worst", type=int, default=10, help="how many worst calls to list (0 = none)"
    )
    args = parser.parse_args()

    rows = scan(Path(args.root))
    if not rows:
        print(f"no usable taps under {args.root}", file=sys.stderr)
        return 1
    summary = summarize(rows)

    if args.json:
        print(json.dumps({"summary": summary, "calls": rows}, indent=2))
        return 0

    print(f"seam report · {summary['analyzed']} of {summary['taps']} taps analyzed\n")
    for label, g in summary["groups"].items():
        print(
            f"  {label:<9} calls={g['calls']:<3} seams={g['seams']:<5} "
            f"harsh={g['harsh']:<4} rate={g['harshRate']:<7} "
            f"median harsh/min={g['medianHarshPerMinute']:<6} "
            f"worst in/out={g['worstFadeIn']:.3f}/{g['worstFadeOut']:.3f}"
        )

    if args.worst:
        ranked = sorted(
            (r for r in rows if "error" not in r),
            key=lambda r: (r["harshCount"], r["worstFadeIn"]),
            reverse=True,
        )[: args.worst]
        print(f"\n  worst {len(ranked)} by harsh count:")
        print(
            f"    {'call':<26}{'dur':>7}{'seams':>7}{'harsh':>7}{'/min':>7}"
            f"{'fadeIn':>8}{'fadeOut':>9}"
        )
        for r in ranked:
            print(
                f"    {r['call'][:24]:<26}{r['durationS']:>7}{r['seamCount']:>7}"
                f"{r['harshCount']:>7}{r['harshPerMinute']:>7}"
                f"{r['worstFadeIn']:>8.3f}{r['worstFadeOut']:>9.3f}"
            )

    errors = [r for r in rows if "error" in r]
    if errors:
        print(f"\n  {len(errors)} tap(s) failed to analyze:")
        for r in errors:
            print(f"    {r['call'][:40]}: {r['error'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
