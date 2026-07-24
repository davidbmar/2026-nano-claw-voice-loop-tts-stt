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
import os
import re
from typing import Callable, Literal


SPEECH_COMPILER_VERSION = "nanoclaw-speech-v1"
NORMALIZER_VERSION = "en-us-rules-v1"
DEFAULT_MAX_WORDS = 18
DEFAULT_MAX_CHUNK_DURATION_MS = 2_500
FINAL_TAIL_PAD_MS = 140


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
# itself, not by this table.
_PAUSE_AFTER_MS = {
    "period": _pause_ms("NANO_CLAW_PAUSE_PERIOD_MS", 450),        # fall
    "question": _pause_ms("NANO_CLAW_PAUSE_QUESTION_MS", 450),    # rise
    "exclamation": _pause_ms("NANO_CLAW_PAUSE_EXCLAMATION_MS", 450),  # fall, energetic
    "semicolon": _pause_ms("NANO_CLAW_PAUSE_SEMICOLON_MS", 300),  # level/slight fall
    "colon": _pause_ms("NANO_CLAW_PAUSE_COLON_MS", 300),          # level
    "comma": _pause_ms("NANO_CLAW_PAUSE_COMMA_MS", 200),          # slight rise, "more coming"
    "clause": _pause_ms("NANO_CLAW_PAUSE_CLAUSE_MS", 200),        # mid-clause split
}

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

    return merged or [_Segment(_clean_inline(source), "statement")]


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

    text = re.sub(r"\s*[–—]\s*", ", ", text)
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
    if text and not text.endswith((".", "!", "?", ",", ":")):
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
        # for the listener to perceive this as conversational hesitation.
        return FINAL_TAIL_PAD_MS
    # Pause by the strength of the boundary this chunk ends on (the cadence
    # table). Read the actual terminal punctuation first, then fall back to the
    # chunk kind for splits that carry no punctuation of their own.
    last = text.rstrip()[-1:] if text.rstrip() else ""
    if last == ",":
        return _PAUSE_AFTER_MS["comma"]
    if last == ";":
        return _PAUSE_AFTER_MS["semicolon"]
    if last == ":":
        return _PAUSE_AFTER_MS["colon"]
    if last == "?":
        return _PAUSE_AFTER_MS["question"]
    if last == "!":
        return _PAUSE_AFTER_MS["exclamation"]
    if last == ".":
        return _PAUSE_AFTER_MS["period"]
    # No terminal punctuation: a heading/list item reads like a full stop; any
    # other mid-clause split gets the short clause pause.
    if kind in ("heading", "list_item") or next_kind == "list_item":
        return _PAUSE_AFTER_MS["period"]
    return _PAUSE_AFTER_MS["clause"]


def compile_speech(
    source_text: str,
    *,
    max_words_per_chunk: int = DEFAULT_MAX_WORDS,
    max_chunk_duration_ms: int = DEFAULT_MAX_CHUNK_DURATION_MS,
) -> SpeechPlan:
    """Compile a complete model response into a deterministic speech plan."""

    if not isinstance(source_text, str):
        raise TypeError("source_text must be a string")
    max_words = max(8, min(40, int(max_words_per_chunk)))
    max_duration = max(1_200, min(8_000, int(max_chunk_duration_ms)))

    normalized_segments: list[_Segment] = []
    all_records: list[NormalizationRecord] = []
    for segment in _structured_segments(source_text):
        normalized, records = normalize_spoken_forms(segment.text)
        if normalized:
            normalized_segments.append(_Segment(normalized, segment.kind))
            all_records.extend(records)

    units: list[tuple[str, ChunkKind]] = []
    for segment in normalized_segments:
        sentences = _split_sentences(segment.text) or [segment.text]
        for sentence_index, sentence in enumerate(sentences):
            sentence = _ensure_terminal(sentence)
            sentence_kind = segment.kind
            if sentence_index and segment.kind in ("heading", "list_item"):
                sentence_kind = "continuation"
            for piece in _split_long_sentence(sentence, max_words):
                piece = _ensure_terminal(piece)
                if piece:
                    units.append((piece, _sentence_kind(piece, sentence_kind)))

    chunks: list[SpeechChunk] = []
    for sequence, (text, kind) in enumerate(units):
        next_text, next_kind = units[sequence + 1] if sequence + 1 < len(units) else (None, None)
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
                is_final=sequence == len(units) - 1,
            )
        )

    return SpeechPlan(
        source_text=source_text,
        spoken_text=" ".join(chunk.text for chunk in chunks),
        chunks=tuple(chunks),
        normalizations=tuple(all_records),
    )
