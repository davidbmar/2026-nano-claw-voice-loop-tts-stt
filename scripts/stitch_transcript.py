#!/usr/bin/env python3
"""Build readable per-session transcripts from a transcribe capture.

The capture is append-only arrival order. Probe requests complete concurrently,
so this script groups by the server-owned session id, sorts by speech sequence,
drops chunks already rejected by the capture gates, and re-derives every seam
from ``transcript_raw``.

Usage:
    python3 scripts/stitch_transcript.py [path] [--verbose]
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Iterable, TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice.transcript_overlap import find_overlap  # noqa: E402


DEFAULT_CAPTURE_PATH = Path(
    "~/riff-dev-data/nano-claw-transcribe/jspace.jsonl"
)


@dataclass(frozen=True)
class _IndexedEntry:
    arrival: int
    value: dict[str, Any]


@dataclass(frozen=True)
class _SessionGroup:
    session_id: str
    entries: tuple[_IndexedEntry, ...]
    inferred: bool

    @property
    def first_arrival(self) -> int:
        return self.entries[0].arrival


@dataclass(frozen=True)
class Seam:
    """One adjacent raw-chunk boundary and what was removed there."""

    previous_seq: int | None
    current_seq: int | None
    removed_words: tuple[str, ...]


@dataclass(frozen=True)
class SessionTranscript:
    session_id: str
    transcript: str
    seams: tuple[Seam, ...]
    gaps: tuple[tuple[int, int], ...]
    inferred: bool
    entries: int
    accepted_entries: int


@dataclass(frozen=True)
class StitchReport:
    entries: int
    sessions: tuple[SessionTranscript, ...]
    gated_counts: dict[str, int]
    seams_joined: int
    words_removed: int
    legacy_entries: int
    raw_fallback_entries: int
    unsequenced_entries: int


def read_entries(path: Path) -> list[dict[str, Any]]:
    """Read JSON objects from ``path``, naming the exact bad line on failure."""

    entries: list[dict[str, Any]] = []
    with path.expanduser().open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.expanduser()}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path.expanduser()}:{line_number}: expected a JSON object"
                )
            entries.append(value)
    return entries


def _seq(entry: _IndexedEntry) -> int | None:
    value = entry.value.get("seq")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _legacy_groups(entries: list[_IndexedEntry]) -> list[_SessionGroup]:
    """Best-effort grouping for rows captured before session ids existed.

    A new ``seq == 1`` is the only historical boundary signal. It cannot
    recover the later entries of concurrent sessions; callers must surface that
    limitation rather than presenting these groups as reliable.
    """

    groups: list[_SessionGroup] = []
    current: list[_IndexedEntry] = []
    has_numbered_entry = False

    def finish() -> None:
        if not current:
            return
        groups.append(
            _SessionGroup(
                session_id=f"legacy-{len(groups) + 1:03d}",
                entries=tuple(current),
                inferred=True,
            )
        )

    for entry in entries:
        seq = _seq(entry)
        if current and seq == 1 and has_numbered_entry:
            finish()
            current = []
            has_numbered_entry = False
        current.append(entry)
        if seq is not None:
            has_numbered_entry = True
    finish()
    return groups


def _group_entries(entries: Iterable[dict[str, Any]]) -> tuple[list[_SessionGroup], int]:
    indexed = [_IndexedEntry(i, value) for i, value in enumerate(entries)]
    known: dict[str, list[_IndexedEntry]] = {}
    legacy: list[_IndexedEntry] = []

    for entry in indexed:
        session_id = entry.value.get("session_id")
        if session_id is None or session_id == "":
            legacy.append(entry)
            continue
        # New captures always contain short hex strings. Stringifying here
        # keeps an inspectable transcript possible for a hand-edited file
        # without conflating those rows with genuinely missing legacy ids.
        known.setdefault(str(session_id), []).append(entry)

    groups = [
        _SessionGroup(session_id, tuple(group), inferred=False)
        for session_id, group in known.items()
    ]
    groups.extend(_legacy_groups(legacy))
    groups.sort(key=lambda group: group.first_arrival)
    return groups, len(legacy)


def _sort_key(entry: _IndexedEntry) -> tuple[int, int, int]:
    seq = _seq(entry)
    if seq is None:
        # Historical unnumbered rows have no sortable speech key. Preserve
        # their arrival order ahead of the numbered part of the same inferred
        # group, which is how the old capture was appended.
        return (0, entry.arrival, entry.arrival)
    return (1, seq, entry.arrival)


def _seq_gaps(entries: Iterable[_IndexedEntry]) -> tuple[tuple[int, int], ...]:
    sequences = sorted({seq for entry in entries if (seq := _seq(entry)) is not None})
    gaps: list[tuple[int, int]] = []
    previous = 0
    for seq in sequences:
        if seq > previous + 1:
            gaps.append((previous + 1, seq - 1))
        previous = seq
    return tuple(gaps)


def _raw_text(entry: _IndexedEntry) -> tuple[str, bool]:
    raw = entry.value.get("transcript_raw")
    if isinstance(raw, str):
        return raw, False
    legacy = entry.value.get("transcript")
    return (legacy if isinstance(legacy, str) else ""), True


def _stitch_group(group: _SessionGroup) -> tuple[SessionTranscript, int]:
    ordered = sorted(group.entries, key=_sort_key)
    accepted = [entry for entry in ordered if not entry.value.get("gated")]
    parts: list[str] = []
    seams: list[Seam] = []
    previous_raw: str | None = None
    previous_seq: int | None = None
    raw_fallbacks = 0

    for entry in accepted:
        raw, used_fallback = _raw_text(entry)
        raw_fallbacks += int(used_fallback)
        if not raw.strip():
            continue
        if previous_raw is None:
            parts.append(raw.strip())
        else:
            overlap = find_overlap(previous_raw, raw)
            words = raw.split()
            overlap = min(overlap, len(words))
            removed = tuple(words[:overlap])
            remainder = " ".join(words[overlap:]).strip()
            seams.append(
                Seam(
                    previous_seq=previous_seq,
                    current_seq=_seq(entry),
                    removed_words=removed,
                )
            )
            if remainder:
                parts.append(remainder)
        previous_raw = raw
        previous_seq = _seq(entry)

    return (
        SessionTranscript(
            session_id=group.session_id,
            transcript=" ".join(parts),
            seams=tuple(seams),
            gaps=_seq_gaps(group.entries),
            inferred=group.inferred,
            entries=len(group.entries),
            accepted_entries=len(accepted),
        ),
        raw_fallbacks,
    )


def stitch_entries(entries: list[dict[str, Any]]) -> StitchReport:
    """Group, order, gate, and join an in-memory capture."""

    groups, legacy_entries = _group_entries(entries)
    sessions: list[SessionTranscript] = []
    raw_fallback_entries = 0
    for group in groups:
        session, fallbacks = _stitch_group(group)
        sessions.append(session)
        raw_fallback_entries += fallbacks

    gated = Counter(
        str(entry.get("gated")) for entry in entries if entry.get("gated")
    )
    seams_joined = sum(
        bool(seam.removed_words)
        for session in sessions
        for seam in session.seams
    )
    words_removed = sum(
        len(seam.removed_words)
        for session in sessions
        for seam in session.seams
    )
    unsequenced = sum(
        1
        for i, entry in enumerate(entries)
        if _seq(_IndexedEntry(i, entry)) is None
    )
    return StitchReport(
        entries=len(entries),
        sessions=tuple(sessions),
        gated_counts=dict(sorted(gated.items())),
        seams_joined=seams_joined,
        words_removed=words_removed,
        legacy_entries=legacy_entries,
        raw_fallback_entries=raw_fallback_entries,
        unsequenced_entries=unsequenced,
    )


def _seq_label(seq: int | None) -> str:
    return "?" if seq is None else str(seq)


def _gap_label(gap: tuple[int, int]) -> str:
    start, end = gap
    return str(start) if start == end else f"{start}-{end}"


def print_report(
    report: StitchReport,
    *,
    verbose: bool = False,
    output: TextIO | None = None,
) -> None:
    """Render transcripts, auditable seams, and capture-integrity stats."""

    if output is None:
        output = sys.stdout

    if report.legacy_entries:
        print(
            "!!! WARNING: "
            f"{report.legacy_entries} entr"
            f"{'y has' if report.legacy_entries == 1 else 'ies have'} "
            "no session_id. Session boundaries below are BEST-EFFORT guesses "
            "from seq values resetting to 1. !!!",
            file=output,
        )
        print(
            "!!! Concurrent legacy sessions cannot be recovered reliably; "
            "do not treat these inferred groups as conversations that "
            "definitely happened. !!!",
            file=output,
        )
    if report.raw_fallback_entries:
        print(
            "!!! WARNING: "
            f"{report.raw_fallback_entries} ungated entr"
            f"{'y lacks' if report.raw_fallback_entries == 1 else 'ies lack'} "
            "transcript_raw; using the historical transcript field for those "
            "rows only. !!!",
            file=output,
        )
    if report.legacy_entries or report.raw_fallback_entries:
        print(file=output)

    if verbose:
        print("JOIN REPORT", file=output)
        for session in report.sessions:
            for seam in session.seams:
                removed = " ".join(seam.removed_words)
                print(
                    f"  {session.session_id} seq "
                    f"{_seq_label(seam.previous_seq)} -> "
                    f"{_seq_label(seam.current_seq)}: removed "
                    f"{len(seam.removed_words)} word(s) "
                    f"{json.dumps(removed, ensure_ascii=False)}",
                    file=output,
                )
        print(file=output)

    for session in report.sessions:
        reliability = " [BEST-EFFORT LEGACY]" if session.inferred else ""
        print(f"SESSION {session.session_id}{reliability}", file=output)
        print(session.transcript or "(no ungated transcript)", file=output)
        print(file=output)

    print("SUMMARY", file=output)
    print(f"  entries: {report.entries}", file=output)
    print(f"  sessions: {len(report.sessions)}", file=output)
    print("  gated counts by reason:", file=output)
    if report.gated_counts:
        for reason, count in report.gated_counts.items():
            print(f"    {reason}: {count}", file=output)
    else:
        print("    none: 0", file=output)
    print(f"  seams joined: {report.seams_joined}", file=output)
    print(f"  words removed: {report.words_removed}", file=output)
    print(f"  entries without seq: {report.unsequenced_entries}", file=output)
    print("  seq gaps:", file=output)
    sessions_with_gaps = [session for session in report.sessions if session.gaps]
    if sessions_with_gaps:
        for session in sessions_with_gaps:
            gaps = ", ".join(_gap_label(gap) for gap in session.gaps)
            print(f"    {session.session_id}: {gaps}", file=output)
    else:
        print("    none", file=output)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stitch raw overlapping transcribe chunks into one transcript per "
            "capture session."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_CAPTURE_PATH,
        help=f"capture JSONL (default: {DEFAULT_CAPTURE_PATH})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print every seam and the exact prefix words removed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        entries = read_entries(args.path)
        report = stitch_entries(entries)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_report(report, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
