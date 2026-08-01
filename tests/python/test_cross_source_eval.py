import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts/cross_source_eval/run_eval.py"
CORPUS_PATH = ROOT / "scripts/cross_source_eval/corpus.json"
SPEC = importlib.util.spec_from_file_location("cross_source_eval_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def test_checked_in_corpus_has_pinned_group_counts() -> None:
    corpus = runner.read_json(CORPUS_PATH)

    runner.validate_corpus(corpus)

    counts = {
        group: sum(case["group"] == group for case in corpus["cases"])
        for group in ("document", "code", "cross_source")
    }
    assert counts == {"document": 5, "code": 5, "cross_source": 3}
    assert corpus["_meta"]["scoring"]["top_k"] == 5
    assert corpus["_meta"]["scoring"]["repetitions"] == 1
    assert corpus["_meta"]["scoring"]["affirmation_policy"] == "never"


def test_anchor_scoring_uses_evidence_not_model_prose() -> None:
    evidence = [
        {
            "title": "src/api/server.ts",
            "sourceRef": "eval://nano-claw-code/src/api/server.ts",
            "sectionPath": [],
            "text": "handleChat invokes stepLoop for the request.",
        }
    ]
    anchors = [
        {
            "id": "handler",
            "rule": "all",
            "matches": [
                {"field": "source_ref", "contains": "src/api/server.ts"},
                {"field": "text", "contains": "handleChat"},
            ],
        },
        {
            "id": "one-symbol",
            "rule": "any",
            "matches": [
                {"field": "text", "contains": "missingSymbol"},
                {"field": "text", "contains": "stepLoop"},
            ],
        },
    ]

    rate, passed, failed = runner.score_anchors(evidence, anchors)

    assert rate == 1.0
    assert passed == ["handler", "one-symbol"]
    assert failed == []


def test_response_scoring_checks_route_scope_citations_and_coverage_disclaimer() -> None:
    case = {
        "id": "coverage",
        "scope": ["riff-design", "nano-claw-code"],
        "expected_route": "fast",
        "anchors": [
            {
                "id": "design",
                "rule": "any",
                "matches": [{"field": "text", "contains": "Model Router"}],
            }
        ],
        "required_sources": ["riff-design"],
        "min_citation_presence": 1.0,
        "coverage_disclaimer_required": True,
    }
    response = {
        "type": "final",
        "response": (
            "I didn't find evidence in what's loaded that the graph component is implemented."
        ),
        "debug": {
            "evalTrace": {
                "version": runner.TRACE_VERSION,
                "route": "fast",
                "outcome": "answered",
                "config": {
                    "collectionIds": ["nano-claw-code", "riff-design"],
                },
                "claims": [
                    {
                        "text": "The implementation evidence is incomplete.",
                        "evidenceIds": ["ev_1"],
                        "citationIds": ["cite_1"],
                    }
                ],
                "evidence": [
                    {
                        "evidenceId": "ev_1",
                        "citationId": "cite_1",
                        "title": "Riff design",
                        "sourceRef": "eval://riff-design/design.md",
                        "sectionPath": [],
                        "text": "Component 5 — Model Router and Reasoner",
                    }
                ],
            }
        },
    }

    result = runner.evaluate_response(case, response)

    assert result["passed"] is True
    assert result["metrics"] == {
        "expected_evidence_hit_rate": 1.0,
        "citation_presence": 1.0,
        "routing_correctness": 1.0,
        "required_source_coverage": 1.0,
        "coverage_disclaimer": 1.0,
    }


def test_baseline_key_separates_partial_and_full_runs() -> None:
    common = {
        "corpus_digest": "corpus",
        "implementation_digest": "implementation",
        "model": "provider/model",
        "source_provenance": {"source": {"files": []}},
        "repositories": {"nano": {"commit": "abc", "dirty": False}},
        "scoring": {"top_k": 5},
    }

    partial = runner.baseline_key(**common, case_ids=["document-001"])
    full = runner.baseline_key(**common, case_ids=["document-001", "code-001"])

    assert partial != full
