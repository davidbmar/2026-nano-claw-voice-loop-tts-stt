"""Turn a stream of text deltas into speakable chunks for incremental TTS.

Rules:
- The FIRST chunk of a reply flushes as soon as FIRST_CHUNK_WORDS words have
  accumulated, even without a sentence boundary — so audio starts fast.
- Every later chunk flushes only on sentence-ending punctuation (. ! ?).
- Markdown is stripped so TTS reads clean prose.
"""

from __future__ import annotations

import re

FIRST_CHUNK_WORDS = 6

_SENTENCE_END = re.compile(r".*?[.!?]\s", re.DOTALL)


def _clean(text: str) -> str:
    """Strip markdown formatting (shared intent with webrtc._clean_for_speech)."""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
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
        self._first_done = False

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
            cleaned = _clean(raw)
            if cleaned:
                chunks.append(cleaned)
                self._first_done = True

        # Eager first chunk: if nothing spoken yet and enough words piled up.
        if not self._first_done and len(self._buf.split()) >= FIRST_CHUNK_WORDS:
            cleaned = _clean(self._buf)
            self._buf = ""
            if cleaned:
                chunks.append(cleaned)
                self._first_done = True

        return chunks

    def flush(self) -> str:
        """Return and clear the trailing remainder."""
        cleaned = _clean(self._buf)
        self._buf = ""
        if cleaned:
            self._first_done = True
        return cleaned
