"""Remove the duplicated words that overlapping audio chunks produce.

Transcribe mode carries one second of audio from the end of each chunk into the
start of the next, so a word straddling the cut is whole in at least one of them.
The cost is that the overlapping second is transcribed twice, and those repeated
words would otherwise appear in the record as things the speaker said twice.

This finds the longest run of words that ends the previous transcript and begins
the current one, and drops it from the current one.

Deliberately conservative. Two rules keep it from eating real speech:

* A match must be at least ``MIN_OVERLAP_WORDS`` long. Real speech repeats
  single words constantly ("count, count, count with me") and a one-word rule
  would silently delete them. Leaving a duplicate word in is a far cheaper error
  than removing one that was actually said.
* Only a PREFIX of the current transcript is ever removed. Overlap can only
  appear at the seam, so nothing in the middle is ever touched.

The raw text is recorded alongside the cleaned text, so this heuristic is never
the only surviving copy of what was heard.
"""

from __future__ import annotations

import re

# Below this, a repeat is more likely to be genuine speech than a seam artifact.
MIN_OVERLAP_WORDS = 2
# One second of speech is rarely more than a handful of words; searching far
# past that invites a coincidental match with unrelated text.
MAX_OVERLAP_WORDS = 25

_WORD_NOISE = re.compile(r"[^\w']+")


def _normalize(word: str) -> str:
    """Compare on letters alone.

    STT punctuates the same word differently either side of a cut — "twelve."
    ending one chunk and "twelve" starting the next — and capitalizes the first
    word of a segment. Comparing raw strings would miss exactly the matches this
    exists to catch.
    """

    return _WORD_NOISE.sub("", word).lower()


def find_overlap(previous: str, current: str) -> int:
    """How many leading words of `current` repeat the tail of `previous`."""

    if not previous or not current:
        return 0
    prev_words = [_normalize(w) for w in previous.split()]
    cur_words = [_normalize(w) for w in current.split()]
    prev_words = [w for w in prev_words if w]
    cur_words = [w for w in cur_words if w]

    limit = min(len(prev_words), len(cur_words), MAX_OVERLAP_WORDS)
    # Longest match first: a short match nested inside a longer one would strip
    # too little and leave part of the duplicate behind.
    for k in range(limit, MIN_OVERLAP_WORDS - 1, -1):
        if prev_words[-k:] == cur_words[:k]:
            return k
    return 0


def strip_overlap(previous: str, current: str) -> str:
    """`current` with any repeated seam words removed from its front."""

    count = find_overlap(previous, current)
    if not count:
        return current
    # Split the ORIGINAL, not the normalized copy, so punctuation and casing in
    # the kept remainder survive untouched.
    return " ".join(current.split()[count:]).strip()
