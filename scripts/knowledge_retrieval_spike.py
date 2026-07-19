#!/usr/bin/env python3
"""Measure file-level pre-retrieval over generated knowledge detail files.

This is an evaluation spike, not production wiring.  It builds a tiny in-memory
BM25 index over ``data/spacechannel/knowledge/*.md``, adds deterministic aliases
for feed names, and benchmarks representative queries.  It deliberately uses
only the Python standard library so the same approach is portable to riff.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_DIR = ROOT / "data" / "spacechannel" / "knowledge"
TOKEN_RE = re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)?")

# Query-only words contribute little to routing and otherwise reward whichever
# long feed happens to repeat them most often.
STOP_WORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "can",
    "did",
    "do",
    "does",
    "for",
    "from",
    "give",
    "i",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "say",
    "see",
    "show",
    "tell",
    "that",
    "the",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}


@dataclass(frozen=True)
class RouteProfile:
    phrases: tuple[str, ...]
    keywords: tuple[str, ...]


# Generated filenames are a stable builder contract.  These aliases prevent
# lexical ambiguity between, for example, the UFO case, wire, and podcast
# feeds.  Unknown future files still participate through ordinary BM25.
ROUTE_PROFILES = {
    "launches.md": RouteProfile(
        phrases=("launch command", "mission tracker", "next launch", "launch schedule"),
        keywords=("launch", "launches", "liftoff", "mission", "rocket", "schedule", "upcoming"),
    ),
    "ufo-cases.md": RouteProfile(
        phrases=("anomaly division", "debate case", "project blue book", "ufo case", "ufo cases"),
        keywords=("case", "cases", "consensus", "debate", "incident", "sighting"),
    ),
    "ufo-wire.md": RouteProfile(
        phrases=("news wire", "uap wire", "ufo wire"),
        keywords=("congressional", "disclosure", "headline", "headlines", "news", "wire"),
    ),
    "ufo-podcast.md": RouteProfile(
        phrases=("ufo files podcast", "ufo podcast"),
        keywords=("audio", "episode", "episodes", "listen", "podcast"),
    ),
    "maxq-podcast.md": RouteProfile(
        phrases=("max q", "maxq"),
        keywords=("bearish", "bullish", "desk", "market", "podcast", "sector", "stocks"),
    ),
    "becker-tour.md": RouteProfile(
        phrases=("becker tour", "david becker", "planets show", "planets tour"),
        keywords=("concert", "guitar", "jazz", "live", "music", "tour"),
    ),
    "data-lens-articles.md": RouteProfile(
        phrases=("data lens", "intelligence feed", "space industry articles"),
        keywords=("article", "articles", "industry", "spaceflight", "spacenews"),
    ),
    "dsn-snapshot.md": RouteProfile(
        phrases=("deep space network", "dsn"),
        keywords=("antenna", "antennas", "dish", "signal", "snapshot", "station", "tracking"),
    ),
}


@dataclass(frozen=True)
class Document:
    path: Path
    text: str
    terms: Counter[str]
    length: int


@dataclass(frozen=True)
class Selection:
    path: Path
    score: float


@dataclass(frozen=True)
class EvaluationCase:
    question: str
    expected: str


EVALUATION_CASES = (
    EvaluationCase("What is the next launch on the mission tracker?", "launches.md"),
    EvaluationCase("What happened in the USS Nimitz Tic Tac UFO debate case?", "ufo-cases.md"),
    EvaluationCase("What is the newest MAXQ podcast episode saying about the market?", "maxq-podcast.md"),
    EvaluationCase("Where is David Becker taking the PLANETS show next?", "becker-tour.md"),
    EvaluationCase("What is the latest UFO wire headline about declassified files?", "ufo-wire.md"),
    EvaluationCase("What is the newest UFO Files podcast episode?", "ufo-podcast.md"),
    EvaluationCase("What does Data Lens say about Skyroot's Vikram-1 reaching orbit?", "data-lens-articles.md"),
    EvaluationCase("Which antenna is active in the Deep Space Network snapshot?", "dsn-snapshot.md"),
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens sufficient for a tiny, English feed index."""
    return [token for token in TOKEN_RE.findall(text.lower()) if token not in STOP_WORDS]


class LexicalSelector:
    """Cached file-level BM25 plus small, explicit feed-routing aliases."""

    def __init__(self, knowledge_dir: Path) -> None:
        paths = sorted(knowledge_dir.glob("*.md"))
        if not paths:
            raise FileNotFoundError(f"no detail files found under {knowledge_dir}")

        documents: list[Document] = []
        document_frequency: Counter[str] = Counter()
        for path in paths:
            text = path.read_text(encoding="utf-8")
            terms = Counter(tokenize(text))
            documents.append(Document(path=path, text=text, terms=terms, length=sum(terms.values())))
            document_frequency.update(terms.keys())

        self.documents = tuple(documents)
        self.document_frequency = document_frequency
        self.average_length = sum(document.length for document in documents) / len(documents)

    def _bm25(self, query_terms: list[str], document: Document) -> float:
        score = 0.0
        document_count = len(self.documents)
        k1 = 1.2
        b = 0.75
        length_factor = 1.0 - b + b * document.length / self.average_length
        for term in set(query_terms):
            frequency = document.terms.get(term, 0)
            if not frequency:
                continue
            containing = self.document_frequency[term]
            inverse_frequency = math.log(1.0 + (document_count - containing + 0.5) / (containing + 0.5))
            score += inverse_frequency * frequency * (k1 + 1.0) / (frequency + k1 * length_factor)
        return score

    @staticmethod
    def _routing_bonus(query: str, query_terms: set[str], filename: str) -> float:
        profile = ROUTE_PROFILES.get(filename)
        if not profile:
            return 0.0
        phrase_bonus = sum(6.0 for phrase in profile.phrases if phrase in query)
        keyword_bonus = sum(0.75 for keyword in profile.keywords if keyword in query_terms)
        return phrase_bonus + keyword_bonus

    def select(self, question: str, top_k: int = 1) -> list[Selection]:
        normalized_query = " ".join(TOKEN_RE.findall(question.lower()))
        query_terms = tokenize(question)
        query_term_set = set(query_terms)
        ranked = []
        for document in self.documents:
            score = self._bm25(query_terms, document)
            score += self._routing_bonus(normalized_query, query_term_set, document.path.name)
            if score > 0:
                ranked.append(Selection(path=document.path, score=score))
        ranked.sort(key=lambda item: (-item.score, item.path.name))
        return ranked[:top_k]


def token_estimate(text: str) -> int:
    """Match build_knowledge.py's repository-wide chars/4 budget heuristic."""
    return len(text) // 4


def percentile_95(samples: list[int]) -> float:
    ordered = sorted(samples)
    index = math.ceil(0.95 * len(ordered)) - 1
    return ordered[max(0, index)] / 1_000_000


def benchmark(selector: LexicalSelector, question: str, top_k: int, iterations: int) -> tuple[float, float]:
    # Warm caches and adaptive interpreter paths before taking samples.
    for _ in range(min(100, iterations)):
        selector.select(question, top_k)
    samples: list[int] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        selector.select(question, top_k)
        samples.append(time.perf_counter_ns() - started)
    return statistics.median(samples) / 1_000_000, percentile_95(samples)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=DEFAULT_KNOWLEDGE_DIR,
        help=f"generated detail directory (default: {DEFAULT_KNOWLEDGE_DIR})",
    )
    parser.add_argument("--iterations", type=int, default=1_000, help="timed selections per question")
    parser.add_argument("--top-k", type=int, default=1, help="maximum detail files selected per question")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    knowledge_dir = args.knowledge_dir.resolve()

    build_started = time.perf_counter_ns()
    try:
        selector = LexicalSelector(knowledge_dir)
    except (OSError, UnicodeError) as error:
        print(f"ERROR: unable to build detail index: {error}", file=sys.stderr)
        return 2
    build_ms = (time.perf_counter_ns() - build_started) / 1_000_000

    digest_path = knowledge_dir.parent / "knowledge.md"
    if digest_path.exists():
        digest = digest_path.read_text(encoding="utf-8")
        print(
            f"Current digest: {digest_path.name} — {len(digest):,} chars, "
            f"~{token_estimate(digest):,} tokens (chars/4 estimator)"
        )
    detail_chars = sum(len(document.text) for document in selector.documents)
    print(
        f"Detail corpus: {len(selector.documents)} files — {detail_chars:,} chars, "
        f"~{token_estimate(''.join(document.text for document in selector.documents)):,} tokens"
    )
    print(f"Index build (file reads included): {build_ms:.3f} ms")
    print(
        f"Warm in-memory selection: BM25 + feed aliases; top-k={args.top_k}; "
        f"{args.iterations:,} timed runs/question\n"
    )
    print("| # | Representative question | Selected detail file(s) | Score(s) | Median ms | p95 ms | Check |")
    print("|---:|---|---|---:|---:|---:|---|")

    passed = 0
    failed = 0
    medians: list[float] = []
    p95s: list[float] = []
    for number, case in enumerate(EVALUATION_CASES, start=1):
        selected = selector.select(case.question, args.top_k)
        names = [item.path.name for item in selected]
        scores = ", ".join(f"{item.score:.2f}" for item in selected) or "—"
        median_ms, p95_ms = benchmark(selector, case.question, args.top_k, args.iterations)
        medians.append(median_ms)
        p95s.append(p95_ms)
        ok = case.expected in names
        passed += int(ok)
        failed += int(not ok)
        check = "PASS" if ok else f"FAIL (expected {case.expected})"
        print(
            f"| {number} | {case.question} | {', '.join(names) or 'digest fallback'} | "
            f"{scores} | {median_ms:.3f} | {p95_ms:.3f} | {check} |"
        )

    print(
        f"\nEvaluation: {passed} passed, {failed} failed. "
        f"Median of per-question medians: {statistics.median(medians):.3f} ms; "
        f"maximum p95: {max(p95s):.3f} ms."
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
