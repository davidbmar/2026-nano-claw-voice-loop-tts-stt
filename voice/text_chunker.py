"""Turn a stream of text deltas into speakable chunks for incremental TTS.

Rules:
- Chunks flush only on sentence-ending punctuation (. ! ?), never in the
  middle of a clause that is still arriving.
- Sentence-ending punctuation stays attached so the TTS engine can use it for
  prosody; commas remain inside the sentence for intra-sentence pauses.
- Markdown is stripped so TTS reads clean prose.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r".*?[.!?]\s", re.DOTALL)
_BARE_HOUR_MERIDIEM = re.compile(
    r"\b(0?[1-9]|1[0-2]):00\s+([ap]m)\b",
    re.IGNORECASE,
)


def normalize_for_speech(text: str) -> str:
    """Make scheduler-style prose friendlier for speech synthesis."""
    text = re.sub(r"\s*[–—]\s*", ", ", text)
    text = text.replace("(", "").replace(")", "")
    text = _BARE_HOUR_MERIDIEM.sub(
        lambda match: f"{int(match.group(1))} {match.group(2).upper()}",
        text,
    )
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def clean_for_speech(text: str) -> str:
    """Remove visual markup and normalize symbols before any TTS engine.

    Voice models pronounce ``#`` inconsistently ("hash", "pound", or a
    language-specific equivalent), so heading/citation syntax must never reach
    synthesis. Preserve a meaningful number sign as spoken "number" while
    dropping all other hash markers.
    """
    # Standard headings plus compact model-generated forms such as
    # ``###Summary``. A single ``#2`` is not a heading and is handled below.
    text = re.sub(r"^[ \t]*#{2,6}[ \t]*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*#[ \t]+", "", text, flags=re.MULTILINE)
    # Internal citation markers add no spoken value.
    text = re.sub(r"\[\s*#\s*\d+\s*\]", "", text)
    text = re.sub(r"#\s*(?=\d)", "number ", text)
    text = text.replace("#", " ")
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"\*{1,3}", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


class TextChunker:
    def __init__(self) -> None:
        self._buf = ""

    def push(self, delta: str) -> list[str]:
        """Add a delta; return any speakable chunks now complete."""
        self._buf += delta
        chunks: list[str] = []

        # Flush all complete sentences.
        while True:
            m = _SENTENCE_END.match(self._buf)
            if not m:
                break
            raw = m.group(0)
            self._buf = self._buf[m.end():]
            cleaned = clean_for_speech(raw)
            if cleaned:
                chunks.append(cleaned)

        return chunks

    def flush(self) -> str:
        """Return and clear the trailing remainder."""
        cleaned = clean_for_speech(self._buf)
        self._buf = ""
        return cleaned
