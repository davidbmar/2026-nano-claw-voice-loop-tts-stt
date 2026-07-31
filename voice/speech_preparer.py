"""Deterministic text-only speech preparation for NanoClaw.

The compiler in this module is deliberately narrower than a conversational
model.  It does not decide what the assistant should say and it does not make
semantic-fidelity claims about model-authored prose.  It removes visual-only
markup, renders a small set of unambiguous spoken forms, and creates bounded
chunks with explicit pause targets for the playback layer.

This is the NanoClaw ``text_only`` implementation described by the approved
Riff speech architecture.  The source response remains the content authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import html
import logging
import os
import random
import re
from typing import Callable, Literal

log = logging.getLogger("nano-claw.speech")


SPEECH_COMPILER_VERSION = "nanoclaw-speech-v1"
NORMALIZER_VERSION = "en-us-rules-v1"
DEFAULT_MAX_WORDS = 18
DEFAULT_MAX_CHUNK_DURATION_MS = 2_500
FINAL_TAIL_PAD_MS = 140


def _clause_pauses_enabled() -> bool:
    # Split sentences at internal , and ; so each clause becomes its own chunk
    # and the cadence table can pause after it. Without this the comma/semicolon
    # rows never fire — chunks only ever end at sentence terminals. On by default.
    return os.environ.get("NANO_CLAW_CLAUSE_PAUSES", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _clause_min_words() -> int:
    # A comma only becomes a pause when the clause before it AND the remainder
    # after it are both at least this many words. Keeps "Well, sure" and lists
    # of one-word items whole instead of turning them staccato.
    try:
        return max(1, min(8, int(os.environ.get("NANO_CLAW_CLAUSE_MIN_WORDS", "3"))))
    except ValueError:
        return 3


def _pause_ms(name: str, default: int) -> int:
    try:
        return max(0, min(2000, int(os.environ.get(name, str(default)))))
    except ValueError:
        return default


# Cadence table: how long to pause after each boundary, by the strength of the
# punctuation. Natural reading scales the pause with boundary strength — a
# sentence-final pause runs ~2x a comma so speech reads grouped, not run-on
# (see prosody research; values are typical human reading pauses). Each is
# env-tunable so cadence can be adjusted by ear without a rebuild. The pitch
# move noted alongside (fall/rise) is rendered by the TTS from the punctuation
# itself, not by this table. Read per call (cold path) so tests and live
# tuning never need an import-order-sensitive module reload.
def _pause_table() -> dict[str, int]:
    return {
        "period": _pause_ms("NANO_CLAW_PAUSE_PERIOD_MS", 600),        # fall
        "question": _pause_ms("NANO_CLAW_PAUSE_QUESTION_MS", 600),    # rise
        "exclamation": _pause_ms("NANO_CLAW_PAUSE_EXCLAMATION_MS", 600),  # fall, energetic
        "semicolon": _pause_ms("NANO_CLAW_PAUSE_SEMICOLON_MS", 400),  # level/slight fall
        "colon": _pause_ms("NANO_CLAW_PAUSE_COLON_MS", 400),          # level
        "comma": _pause_ms("NANO_CLAW_PAUSE_COMMA_MS", 270),          # slight rise, "more coming"
        "clause": _pause_ms("NANO_CLAW_PAUSE_CLAUSE_MS", 270),        # mid-clause split
    }


def _jitter_fraction() -> float:
    # Humans are not metronomes: real reading pauses vary a little around their
    # nominal length. A small random wobble (default ±15%) on each cadence pause
    # keeps the rhythm from sounding mechanically regular. Set to 0 to disable.
    try:
        pct = float(os.environ.get("NANO_CLAW_PAUSE_JITTER", "0.15"))
    except ValueError:
        return 0.15
    return max(0.0, min(0.5, pct))


def _jitter_pause(ms: int) -> int:
    """Apply the anti-metronome wobble to one cadence pause. Never touches the
    final transport tail (that is a fixed guard, not conversational cadence)."""
    jitter = _jitter_fraction()
    if jitter <= 0 or ms <= 0:
        return ms
    factor = 1.0 + random.uniform(-jitter, jitter)
    return max(0, int(round(ms * factor)))

ChunkKind = Literal["statement", "question", "list_item", "heading", "continuation"]


@dataclass(frozen=True, slots=True)
class NormalizationRecord:
    """One deterministic source-to-spoken rendering kept in-memory only."""

    kind: str
    source_text: str
    spoken_text: str


@dataclass(frozen=True, slots=True)
class SpeechChunk:
    """One complete, ordered TTS input with a total target trailing gap."""

    chunk_id: str
    sequence: int
    text: str
    kind: ChunkKind
    estimated_duration_ms: int
    pause_after_ms: int
    is_final: bool


@dataclass(frozen=True, slots=True)
class SpeechPlan:
    """Complete deterministic plan for a ``text_only`` response."""

    source_text: str
    spoken_text: str
    chunks: tuple[SpeechChunk, ...]
    normalizations: tuple[NormalizationRecord, ...]
    compiler_version: str = SPEECH_COMPILER_VERSION
    normalizer_version: str = NORMALIZER_VERSION
    mode: str = "deterministic"
    acts_provenance: str = "text_only"
    guarantee_level: str = "text_structural"

    def public_metadata(self) -> dict[str, object]:
        """Return privacy-safe plan metadata suitable for the browser/logs."""

        return {
            "compilerVersion": self.compiler_version,
            "normalizerVersion": self.normalizer_version,
            "mode": self.mode,
            "actsProvenance": self.acts_provenance,
            "guaranteeLevel": self.guarantee_level,
            "chunkCount": len(self.chunks),
            "normalizationCount": len(self.normalizations),
            "estimatedDurationMs": sum(
                chunk.estimated_duration_ms + chunk.pause_after_ms
                for chunk in self.chunks
            ),
        }


@dataclass(frozen=True, slots=True)
class _Segment:
    text: str
    kind: ChunkKind
    label_like: bool = False


_SMALL = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_MONTHS = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_ORDINAL_UNDER_TWENTY = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
}
_TENS_ORDINAL = {20: "twentieth", 30: "thirtieth"}
_LIST_ORDINALS = (
    "First",
    "Second",
    "Third",
    "Fourth",
    "Fifth",
    "Sixth",
    "Seventh",
    "Eighth",
    "Ninth",
    "Tenth",
)
_ACRONYMS = {
    "AI": "artificial intelligence",
    "API": "application programming interface",
    "CAC": "customer acquisition cost",
    "CRM": "customer relationship management",
    "HVAC": "heating and air conditioning",
    "LLM": "language model",
    "LTV": "lifetime value",
    "PPC": "pay per click",
    "ROI": "return on investment",
    "SEO": "search engine optimization",
    "SLA": "service level agreement",
    "SMS": "text message",
    "STT": "speech to text",
    "TTS": "text to speech",
    "URL": "web address",
    "VAD": "voice activity detection",
}
_TERMINAL = (".", "!", "?")


def _int_words(value: int) -> str:
    if value < 0:
        return "negative " + _int_words(-value)
    if value < 20:
        return _SMALL[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        return _TENS[tens] + (" " + _SMALL[remainder] if remainder else "")
    if value < 1_000:
        hundreds, remainder = divmod(value, 100)
        return _SMALL[hundreds] + " hundred" + (
            " " + _int_words(remainder) if remainder else ""
        )
    for scale, label in (
        (1_000_000_000, "billion"),
        (1_000_000, "million"),
        (1_000, "thousand"),
    ):
        if value >= scale:
            major, remainder = divmod(value, scale)
            return _int_words(major) + " " + label + (
                " " + _int_words(remainder) if remainder else ""
            )
    raise ValueError("integer is outside the supported spoken range")


def _ordinal_words(value: int) -> str:
    if value in _ORDINAL_UNDER_TWENTY:
        return _ORDINAL_UNDER_TWENTY[value]
    if value in _TENS_ORDINAL:
        return _TENS_ORDINAL[value]
    tens, remainder = divmod(value, 10)
    if 2 <= tens <= 3 and remainder:
        return f"{_TENS[tens]} {_ORDINAL_UNDER_TWENTY[remainder]}"
    return _int_words(value)


def _decimal_words(value: str) -> str:
    if "." not in value:
        return _int_words(int(value.replace(",", "")))
    whole, decimal = value.replace(",", "").split(".", 1)
    return f"{_int_words(int(whole))} point {' '.join(_SMALL[int(d)] for d in decimal)}"


def _year_words(year: int) -> str:
    if 2000 <= year <= 2009:
        return "two thousand" + (" " + _int_words(year - 2000) if year > 2000 else "")
    if 2010 <= year <= 2099:
        return f"twenty {_int_words(year - 2000)}"
    if 1900 <= year <= 1999:
        remainder = year - 1900
        return "nineteen hundred" if not remainder else f"nineteen {_int_words(remainder)}"
    return _int_words(year)


def _recording_sub(
    pattern: str | re.Pattern[str],
    kind: str,
    renderer: Callable[[re.Match[str]], str],
    text: str,
    records: list[NormalizationRecord],
    *,
    flags: int = 0,
) -> str:
    def replace(match: re.Match[str]) -> str:
        rendered = renderer(match)
        if rendered != match.group(0):
            records.append(NormalizationRecord(kind, match.group(0), rendered))
        return rendered

    return re.sub(pattern, replace, text, flags=flags)


def _clean_inline(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"```(?:[^`]|`(?!``))*```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\[(?:\s*#?\d+\s*|\s*Evidence\s+\d+\s*)\]", " ", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("(", "").replace(")", "")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _structured_segments(source: str) -> list[_Segment]:
    """Preserve paragraph/list boundaries that visual cleanup normally loses."""

    return _parse_segments(source)[0]


_STRUCT_LINE = re.compile(r"^(?:#{1,6}\s*.+|\d{1,2}[.)]\s+.+|[-*•]\s+.+)$")


def _open_tail_raw(source: str) -> str:
    """Return the raw text of the still-growable tail region, or "".

    Streaming feeds arbitrary prefixes; a later delta can only rewrite text
    that is not yet sealed by a structural boundary. Sealed: everything up to
    the last blank line, and any heading/list line terminated by a newline.
    Open: a trailing paragraph (even newline-terminated — the next line would
    join it with a space), a heading/list line still missing its newline, and
    anything ending in a bare carriage return (half of a split CRLF).
    """

    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return ""
    if re.search(r"\n[ \t]*\n[ \t]*$", normalized):
        return ""  # sealed by a trailing blank line
    lines = normalized.split("\n")
    tail: list[str] = []
    if normalized.endswith("\n"):
        lines = lines[:-1]  # split artifact of the final newline
    else:
        # The last line has no newline yet — it is still growing. A growing
        # structural line stands alone; a growing paragraph (or an ambiguous
        # whitespace-only line that may yet become content) joins the
        # paragraph lines before it.
        last = lines[-1]
        lines = lines[:-1]
        if _STRUCT_LINE.match(last.strip()):
            return last
        tail.append(last)
    # Walk back over newline-terminated lines: paragraph lines stay open
    # (a future line would join them with a space); a confirmed blank line
    # or a structural line seals everything before it.
    for line in reversed(lines):
        if not line.strip():
            break
        if _STRUCT_LINE.match(line.strip()):
            break
        tail.append(line)
    if not any(part.strip() for part in tail):
        return ""
    return "\n".join(reversed(tail))


def _parse_segments(source: str) -> tuple[list[_Segment], str]:
    """Segments plus the raw text of the open (still-growable) tail region."""

    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[_Segment] = []
    paragraph: list[str] = []
    list_index = 0

    def flush_paragraph() -> None:
        nonlocal paragraph, list_index
        if not paragraph:
            return
        cleaned = _clean_inline(" ".join(paragraph))
        if cleaned:
            segments.append(_Segment(cleaned, "statement"))
        paragraph = []
        list_index = 0

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            flush_paragraph()
            continue

        heading = re.match(r"^#{1,6}\s*(.+)$", stripped)
        ordered = re.match(r"^(\d{1,2})[.)]\s+(.+)$", stripped)
        bullet = re.match(r"^[-*•]\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            cleaned = _clean_inline(heading.group(1))
            if cleaned:
                segments.append(_Segment(cleaned, "heading", True))
            continue
        if ordered or bullet:
            flush_paragraph()
            content = ordered.group(2) if ordered else bullet.group(1)
            cleaned = _clean_inline(content)
            if not cleaned:
                continue
            list_index = int(ordered.group(1)) if ordered else list_index + 1
            prefix = (
                _LIST_ORDINALS[list_index - 1]
                if 1 <= list_index <= len(_LIST_ORDINALS)
                else f"Point {_int_words(list_index)}"
            )
            label_like = not cleaned.endswith(_TERMINAL)
            segments.append(_Segment(f"{prefix}, {cleaned}", "list_item", label_like))
            continue

        paragraph.append(stripped)

    flush_paragraph()

    # A short heading/list label followed by prose is more naturally spoken as
    # one labelled statement than as a disconnected fragment.
    merged: list[_Segment] = []
    index = 0
    while index < len(segments):
        current = segments[index]
        if (
            current.label_like
            and index + 1 < len(segments)
            and segments[index + 1].kind == "statement"
            and len(current.text.split()) <= 10
        ):
            following = segments[index + 1]
            merged.append(
                _Segment(
                    f"{current.text.rstrip('.:')}: {following.text}",
                    current.kind,
                )
            )
            index += 2
            continue
        merged.append(current)
        index += 1

    if not merged:
        fallback = _clean_inline(source)
        return [_Segment(fallback, "statement")], _open_tail_raw(source)
    return merged, _open_tail_raw(source)


# Postal abbreviations a TTS engine reads as words. "Ave" comes out "Ove", which
# is what a caller heard when asked to confirm their own address (captured
# through the call harness, 2026-07-31).
_STREET_SUFFIX_WORDS = {
    "ave": "Avenue", "st": "Street", "rd": "Road", "blvd": "Boulevard",
    "dr": "Drive", "ln": "Lane", "ct": "Court", "pl": "Place",
    "hwy": "Highway", "pkwy": "Parkway", "ter": "Terrace", "cir": "Circle",
}
# A house number, one to three name words, then a suffix abbreviation. Anchored
# on the SUFFIX so a bare number in a sentence ("press 1", "for 3 days") is never
# mistaken for an address — the suffix is what makes it one.
_STREET_ADDRESS_RE = re.compile(
    r"\b(\d{3,6})\s+((?:[A-Za-z][\w'’-]*\s+){1,3}?)"
    r"(" + "|".join(_STREET_SUFFIX_WORDS) + r")\b\.?",
    re.IGNORECASE,
)


def _render_street_address(match: re.Match[str]) -> str:
    """Say a street address the way a caller can check it against their door.

    The house number becomes a digit run rather than one cardinal: TTS reads
    "14723" as "fourteen thousand seven hundred twenty-three", which nobody
    matches against an address. The suffix becomes a word. The street NAME is
    left exactly as written — pronunciation there belongs to the voice.

    This lives at the SPEECH boundary, not in the app, so it applies to every
    line carrying an address — the identity confirmation, the pre-file readback,
    and anything added later — rather than to whichever template someone
    remembered to change.
    """
    number, name, suffix = match.group(1), match.group(2).strip(), match.group(3)
    return f"{' '.join(number)} {name} {_STREET_SUFFIX_WORDS[suffix.lower()]}"


def normalize_spoken_forms(text: str) -> tuple[str, tuple[NormalizationRecord, ...]]:
    """Render only unambiguous, high-value en-US forms for a TTS engine."""

    records: list[NormalizationRecord] = []

    text = _recording_sub(
        r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/((?:19|20)\d{2})\b",
        "date",
        lambda match: _render_date(match),
        text,
        records,
    )
    text = _recording_sub(
        re.compile(
            r"(?<!\d)(?:\+?1[ .-]?)?(?:\((\d{3})\)|(\d{3}))[ .-](\d{3})[ .-](\d{4})(?!\d)"
        ),
        "phone",
        _render_phone,
        text,
        records,
    )
    text = _recording_sub(
        _STREET_ADDRESS_RE,
        "address",
        _render_street_address,
        text,
        records,
    )
    text = _recording_sub(
        r"(?<!\w)\$(\d[\d,]*)(?:\.(\d{1,2}))?",
        "currency",
        _render_currency,
        text,
        records,
    )
    text = _recording_sub(
        r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*([AaPp])\.?[Mm]\.?(?!\w)",
        "time",
        _render_time,
        text,
        records,
    )
    text = _recording_sub(
        r"\b(\d{1,4})\s*[-–—]\s*(\d{1,4})(?=\s*(?:hours?|days?|weeks?|months?|minutes?|percent|%))",
        "range",
        lambda match: f"{_int_words(int(match.group(1)))} to {_int_words(int(match.group(2)))}",
        text,
        records,
        flags=re.I,
    )
    text = _recording_sub(
        r"\b(\d+(?:\.\d+)?)\s*%",
        "percentage",
        lambda match: f"{_decimal_words(match.group(1))} percent",
        text,
        records,
    )
    text = _recording_sub(
        r"\bNo\.\s*(\d+)\b",
        "number_label",
        lambda match: f"number {_int_words(int(match.group(1)))}",
        text,
        records,
        flags=re.I,
    )
    text = _recording_sub(
        r"#\s*(\d+)\b",
        "number_label",
        lambda match: f"number {_int_words(int(match.group(1)))}",
        text,
        records,
    )

    for source, spoken in (
        (r"\be\.g\.(?=\s|$)", "for example"),
        (r"\bi\.e\.(?=\s|$)", "that is"),
        (r"\bvs\.(?=\s|$)", "versus"),
        (r"\bw/o\b", "without"),
        (r"\bw/\b", "with"),
    ):
        text = _recording_sub(
            source,
            "abbreviation",
            lambda _match, replacement=spoken: replacement,
            text,
            records,
            flags=re.I,
        )

    for acronym, spoken in _ACRONYMS.items():
        dotted = r"\.?".join(re.escape(character) for character in acronym)
        text = _recording_sub(
            rf"(?<!\w){dotted}\.?(?!\w)",
            "acronym",
            lambda _match, replacement=spoken: replacement,
            text,
            records,
        )

    # Models use em-dashes (and spaced hyphens) as light connectors, the way a
    # writer uses a comma — not as dramatic breaks. Treating them as their own
    # long pause chopped speech into staccato, so normalize both to a comma and
    # let the comma logic (with its list/appositive guard) handle them. Intra-
    # word hyphens ("well-known") have no surrounding spaces and are left intact.
    text = re.sub(r"\s*[–—]\s*", ", ", text)
    text = re.sub(r"(?<=[A-Za-z]) - (?=[A-Za-z])", ", ", text)
    text = text.replace("…", ".")
    text = re.sub(r"\s*&\s*", " and ", text)
    text = text.replace("#", " ")
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text, tuple(records)


def _render_date(match: re.Match[str]) -> str:
    month, day, year = (int(match.group(index)) for index in (1, 2, 3))
    try:
        date(year, month, day)
    except ValueError:
        return match.group(0)
    return f"{_MONTHS[month]} {_ordinal_words(day)}, {_year_words(year)}"


def _render_phone(match: re.Match[str]) -> str:
    area = match.group(1) or match.group(2)
    groups = (area, match.group(3), match.group(4))
    return ", ".join(" ".join(_SMALL[int(digit)] for digit in group) for group in groups)


def _render_currency(match: re.Match[str]) -> str:
    dollars = int(match.group(1).replace(",", ""))
    cents_text = (match.group(2) or "").ljust(2, "0")
    cents = int(cents_text) if cents_text else 0
    parts: list[str] = []
    if dollars or not cents:
        parts.append(f"{_int_words(dollars)} {'dollar' if dollars == 1 else 'dollars'}")
    if cents:
        parts.append(f"{_int_words(cents)} {'cent' if cents == 1 else 'cents'}")
    return " and ".join(parts)


def _render_time(match: re.Match[str]) -> str:
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = "A M" if match.group(3).lower() == "a" else "P M"
    minute_words = ""
    if minute:
        minute_words = " oh " + _int_words(minute) if minute < 10 else " " + _int_words(minute)
    return f"{_int_words(hour)}{minute_words} {meridiem}"


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text))


def _sentence_kind(text: str, segment_kind: ChunkKind) -> ChunkKind:
    if text.rstrip().endswith("?"):
        return "question"
    return segment_kind


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()]


def _split_clauses(text: str) -> list[str]:
    """Split a sentence at internal ``,`` and ``;`` boundaries into clause
    pieces, each keeping its boundary punctuation so ``_pause_after`` reads it
    and the cadence table pauses after it. A boundary only splits when both the
    clause before it and the remainder after it have at least
    ``_CLAUSE_MIN_WORDS`` words, so short appositives ("Well, sure") and lists of
    one-word items stay whole instead of turning staccato. Joining the pieces
    back with a single space reproduces the original text exactly."""
    if not _clause_pauses_enabled():
        return [text]
    clause_min_words = _clause_min_words()
    pieces: list[str] = []
    last_cut = 0
    for match in re.finditer(r"[,;]", text):
        cut = match.end()  # keep the punctuation with the left clause
        left = text[last_cut:cut].strip()
        right = text[cut:].strip()
        # Commas need the list/appositive guard; a semicolon is always a
        # deliberate break, so it splits whenever both sides are non-empty.
        min_words = 1 if match.group() == ";" else clause_min_words
        if _word_count(left) >= min_words and _word_count(right) >= min_words:
            pieces.append(left)
            last_cut = cut
    tail = text[last_cut:].strip()
    if tail:
        pieces.append(tail)
    return pieces or [text]


def _split_long_sentence(text: str, max_words: int) -> list[str]:
    """Split at audible clause boundaries; never cut at an arbitrary word."""

    if _word_count(text) <= max_words:
        return [text]

    remaining = text.strip()
    output: list[str] = []
    boundary = re.compile(
        r";\s+|:\s+|,\s+(?=(?:and|but|because|while|which|so|yet)\b)|\s+(?=(?:and|but|because|while|which|so|yet)\b)",
        re.I,
    )
    while _word_count(remaining) > max_words:
        candidates: list[tuple[int, int]] = []
        for match in boundary.finditer(remaining):
            left = remaining[: match.end()].strip()
            left_words = _word_count(left)
            right_words = _word_count(remaining[match.end() :])
            if 5 <= left_words <= max_words and right_words >= 4:
                candidates.append((match.end(), left_words))
        if not candidates:
            break
        split_at, _ = max(candidates, key=lambda item: item[1])
        left = remaining[:split_at].strip()
        remaining = remaining[split_at:].strip()
        if left.endswith(";"):
            left = left[:-1] + "."
        elif left and left[-1] not in ".!?,:":
            left += "."
        output.append(left)

    if remaining:
        output.append(remaining)
    return output


def _ensure_terminal(text: str) -> str:
    text = text.strip()
    # A clause that already ends on any boundary punctuation is complete — do
    # not append a stray period (that is what produced "We shipped it;." and
    # gave semicolons a period-length pause).
    if text and not text.endswith((".", "!", "?", ",", ":", ";")):
        return text + "."
    return text


def _pause_after(
    text: str,
    kind: ChunkKind,
    next_text: str | None,
    next_kind: ChunkKind | None,
) -> int:
    if next_text is None:
        # Lux can end on the final phoneme with no measurable trailing PCM.
        # A tiny transport tail prevents browsers, carriers, and downstream
        # recognizers from losing that last word; there is no following phrase
        # for the listener to perceive this as conversational hesitation. This
        # is a fixed transport guard, so it is never jittered.
        return FINAL_TAIL_PAD_MS
    # Pause by the strength of the boundary, then apply the anti-metronome
    # wobble so successive same-strength boundaries don't land on an identical,
    # mechanical-sounding duration.
    return _jitter_pause(_boundary_pause(text, kind, next_kind))


def _boundary_pause(
    text: str,
    kind: ChunkKind,
    next_kind: ChunkKind | None,
) -> int:
    # Read the actual terminal punctuation first, then fall back to the chunk
    # kind for splits that carry no punctuation of their own.
    table = _pause_table()
    last = text.rstrip()[-1:] if text.rstrip() else ""
    if last == ",":
        return table["comma"]
    if last == ";":
        return table["semicolon"]
    if last == ":":
        return table["colon"]
    if last == "?":
        return table["question"]
    if last == "!":
        return table["exclamation"]
    if last == ".":
        return table["period"]
    # No terminal punctuation: a heading/list item reads like a full stop; any
    # other mid-clause split gets the short clause pause.
    if kind in ("heading", "list_item") or next_kind == "list_item":
        return table["period"]
    return table["clause"]


def _expand_sentences(
    sentences: list[str],
    segment_kind: ChunkKind,
    max_words: int,
    *,
    first_index: int = 0,
) -> list[tuple[str, ChunkKind]]:
    """Expand normalized sentences into ordered (text, kind) units.

    ``first_index`` is the sentence's position within its full segment so a
    streamed prefix reproduces the batch continuation-kind decisions exactly.
    """

    units: list[tuple[str, ChunkKind]] = []
    for sentence_index, sentence in enumerate(sentences, start=first_index):
        sentence = _ensure_terminal(sentence)
        sentence_kind: ChunkKind = segment_kind
        if sentence_index and segment_kind in ("heading", "list_item"):
            sentence_kind = "continuation"
        # Break prose sentences at commas/semicolons so each clause gets its
        # cadence pause. Headings and list items keep their structure whole.
        clauses = (
            _split_clauses(sentence)
            if sentence_kind in ("statement", "question", "continuation")
            else [sentence]
        )
        for clause in clauses:
            for piece in _split_long_sentence(clause, max_words):
                piece = _ensure_terminal(piece)
                if piece:
                    units.append((piece, _sentence_kind(piece, sentence_kind)))
    return units


def _expand_units(
    source_text: str, max_words: int
) -> tuple[list[tuple[str, ChunkKind]], list[NormalizationRecord]]:
    """The full batch text→units pipeline (segments → normalize → expand)."""

    units: list[tuple[str, ChunkKind]] = []
    all_records: list[NormalizationRecord] = []
    for segment in _structured_segments(source_text):
        normalized, records = normalize_spoken_forms(segment.text)
        if normalized:
            all_records.extend(records)
            sentences = _split_sentences(normalized) or [normalized]
            units.extend(_expand_sentences(sentences, segment.kind, max_words))
    return units, all_records


def _build_chunks(
    units: list[tuple[str, ChunkKind]],
    *,
    max_duration: int,
    sequence_offset: int = 0,
    more_coming: bool = False,
) -> list[SpeechChunk]:
    """Turn ordered units into SpeechChunks.

    ``more_coming=True`` means later units exist beyond this batch: the last
    unit here gets its punctuation pause, never the final transport tail.
    (Safe because pauses are punctuation-local — ``_boundary_pause`` only
    consults ``next_kind`` for units without terminal punctuation, which
    ``_ensure_terminal`` rules out.) Jitter draws exactly one random sample
    per non-final chunk, in unit order, matching the batch draw sequence.
    """

    chunks: list[SpeechChunk] = []
    for index, (text, kind) in enumerate(units):
        if index + 1 < len(units):
            next_text, next_kind = units[index + 1]
        elif more_coming:
            next_text, next_kind = "", None  # non-None: punctuation pause path
        else:
            next_text, next_kind = None, None
        sequence = sequence_offset + index
        words = max(1, _word_count(text))
        estimate = min(max_duration, max(600, round(words / 2.7 * 1000)))
        chunks.append(
            SpeechChunk(
                chunk_id=f"chunk_{sequence}",
                sequence=sequence,
                text=text,
                kind=kind,
                estimated_duration_ms=estimate,
                pause_after_ms=_pause_after(text, kind, next_text, next_kind),
                is_final=not more_coming and index == len(units) - 1,
            )
        )
    return chunks


def _clamp_max_words(max_words_per_chunk: int) -> int:
    return max(8, min(40, int(max_words_per_chunk)))


def _clamp_max_duration(max_chunk_duration_ms: int) -> int:
    return max(1_200, min(8_000, int(max_chunk_duration_ms)))


def compile_speech(
    source_text: str,
    *,
    max_words_per_chunk: int = DEFAULT_MAX_WORDS,
    max_chunk_duration_ms: int = DEFAULT_MAX_CHUNK_DURATION_MS,
) -> SpeechPlan:
    """Compile a complete model response into a deterministic speech plan."""

    if not isinstance(source_text, str):
        raise TypeError("source_text must be a string")
    max_words = _clamp_max_words(max_words_per_chunk)
    max_duration = _clamp_max_duration(max_chunk_duration_ms)

    units, all_records = _expand_units(source_text, max_words)
    chunks = _build_chunks(units, max_duration=max_duration)

    return SpeechPlan(
        source_text=source_text,
        spoken_text=" ".join(chunk.text for chunk in chunks),
        chunks=tuple(chunks),
        normalizations=tuple(all_records),
    )


def _tail_holds_markup(raw: str) -> bool:
    """True when the open tail contains markup a future delta could close.

    ``_clean_inline`` rewrites paired constructs (code fences, links, bold),
    so text inside an unclosed pair is not stable yet. Counting is
    conservative: a stray odd asterisk merely delays emission until the
    segment closes; it can never produce wrong speech.
    """

    if raw.count("```") % 2:
        return True
    if raw.count("`") % 2:
        return True
    if raw.count("[") != raw.count("]"):
        return True
    if raw.count("*") % 2:
        return True
    return False


class StreamingSpeechCompiler:
    """Sentence-level streaming front end for :func:`compile_speech`.

    ``feed(delta)`` accumulates model deltas and returns SpeechChunks for the
    units that are already *stable* — recomputed from the whole buffer every
    call, so list numbering, label merges, and paragraph joins can never
    drift from what batch compilation of the final text would produce. The
    stability frontier holds back: the open (still-growable) tail region,
    the last sentence of an open paragraph, short label-like segments that
    may merge with a follower, and tails containing unclosed markup.

    Invariant (tested): for ANY partitioning of the text into deltas,
    ``feed*() + finish(None)`` yields chunks identical to
    ``compile_speech(whole_text).chunks`` with jitter disabled. Emission is
    strictly in order and each chunk draws jitter exactly once, so the
    random draw sequence also matches batch — do not reorder or re-emit.
    """

    def __init__(
        self,
        *,
        max_words_per_chunk: int = DEFAULT_MAX_WORDS,
        max_chunk_duration_ms: int = DEFAULT_MAX_CHUNK_DURATION_MS,
    ) -> None:
        # Pinned at construction: one turn must never mix two configs.
        self._max_words = _clamp_max_words(max_words_per_chunk)
        self._max_duration = _clamp_max_duration(max_chunk_duration_ms)
        self._buffer = ""
        self._emitted_texts: list[str] = []
        self._finished = False

    @property
    def fed_text(self) -> str:
        return self._buffer

    @property
    def emitted_count(self) -> int:
        return len(self._emitted_texts)

    def feed(self, delta: str) -> list[SpeechChunk]:
        if self._finished or not isinstance(delta, str) or not delta:
            return []
        self._buffer += delta
        units = self._stable_units()
        new_units = units[len(self._emitted_texts):]
        if not new_units:
            return []
        chunks = _build_chunks(
            new_units,
            max_duration=self._max_duration,
            sequence_offset=len(self._emitted_texts),
            more_coming=True,
        )
        self._emitted_texts.extend(text for text, _ in new_units)
        return chunks

    def finish(self, final_text: str | None = None) -> list[SpeechChunk]:
        """Flush the remainder; the last chunk gets the final transport tail.

        ``final_text`` replaces the fed buffer as the authoritative source
        (the API's ``final.response`` can extend the streamed deltas). Units
        already emitted are never re-spoken: on any divergence between what
        was emitted and the final recompute, only units BEYOND the emitted
        count are produced and the divergence is logged.
        """

        if self._finished:
            return []
        self._finished = True
        source = final_text if final_text is not None else self._buffer
        units, _records = _expand_units(source, self._max_words)
        common = 0
        for emitted, (text, _kind) in zip(self._emitted_texts, units):
            if emitted != text:
                break
            common += 1
        if common < len(self._emitted_texts):
            log.warning(
                "speech stream divergence at unit %d (emitted %d, recomputed %d)",
                common,
                len(self._emitted_texts),
                len(units),
            )
        remaining = units[len(self._emitted_texts):]
        chunks = _build_chunks(
            remaining,
            max_duration=self._max_duration,
            sequence_offset=len(self._emitted_texts),
            more_coming=False,
        )
        self._emitted_texts.extend(text for text, _ in remaining)
        return chunks

    def _stable_units(self) -> list[tuple[str, ChunkKind]]:
        segments, open_raw = _parse_segments(self._buffer)
        if not segments:
            return []
        hold_from = len(segments)
        if open_raw:
            hold_from -= 1
        elif segments[-1].label_like and _word_count(segments[-1].text) <= 10:
            # A closed short label may still merge with a future statement.
            hold_from -= 1

        units: list[tuple[str, ChunkKind]] = []
        for segment in segments[:hold_from]:
            normalized, _records = normalize_spoken_forms(segment.text)
            if normalized:
                sentences = _split_sentences(normalized) or [normalized]
                units.extend(_expand_sentences(sentences, segment.kind, self._max_words))

        # Partial emission from the open tail: everything but its last
        # sentence is stable (no normalization pattern spans two sentence
        # terminals; the held last sentence absorbs single-boundary patterns).
        if open_raw and hold_from == len(segments) - 1:
            tail = segments[-1]
            may_merge = tail.label_like and _word_count(tail.text) <= 10
            if not may_merge and not _tail_holds_markup(open_raw):
                normalized, _records = normalize_spoken_forms(tail.text)
                if normalized:
                    sentences = _split_sentences(normalized)
                    if len(sentences) >= 2:
                        units.extend(
                            _expand_sentences(
                                sentences[:-1], tail.kind, self._max_words
                            )
                        )
        return units
