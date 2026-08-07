"""Remove the duplicated words that overlapping audio chunks produce.

Transcribe mode carries two seconds of audio from the end of each chunk into the
start of the next, so a word straddling the cut is whole in at least one of them.
The cost is that the overlapping audio is transcribed twice, and those repeated
words would otherwise appear in the record as things the speaker said twice.

This finds the longest aligned run of words that ends the previous transcript
and begins the current one, and drops it from the current one. One word may
differ inside the run, because STT commonly renders the same boundary word two
ways (for example, "13" and "thirteen").

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

import difflib
import re

# Below this, a repeat is more likely to be genuine speech than a seam artifact.
MIN_OVERLAP_WORDS = 3
# Historical one-second captures and the established live contract contain
# exact two-word seams. Exact equality is stronger evidence than a fuzzy match,
# so retain that compatibility without lowering the fuzzy evidence threshold.
_MIN_EXACT_OVERLAP_WORDS = 2
# Two seconds of speech is still only a handful of words; searching far
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
    # Longest alignment first: a short match nested inside a longer one would
    # strip too little and leave part of the duplicate behind. Requiring both
    # ends to match anchors the alignment at the seam; without those anchors,
    # ordinary repetition inside a sentence ("count with me count") can look
    # like an overlap even though it does not reach the previous chunk's end.
    for k in range(limit, MIN_OVERLAP_WORDS - 1, -1):
        matcher = difflib.SequenceMatcher(
            None,
            prev_words[-k:],
            cur_words[:k],
            autojunk=False,
        )
        blocks = [block for block in matcher.get_matching_blocks() if block.size]
        if not blocks:
            continue
        matching_words = sum(block.size for block in blocks)
        first = blocks[0]
        last = blocks[-1]
        anchored = (
            first.a == 0
            and first.b == 0
            and last.a + last.size == k
            and last.b + last.size == k
        )
        if (
            anchored
            and matching_words >= MIN_OVERLAP_WORDS
            and matching_words >= k - 1
        ):
            return k

    # Keep exact two-word joins for captures made before the overlap widened.
    # Fuzzy evidence still requires MIN_OVERLAP_WORDS matching positions.
    if limit >= _MIN_EXACT_OVERLAP_WORDS:
        k = _MIN_EXACT_OVERLAP_WORDS
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
