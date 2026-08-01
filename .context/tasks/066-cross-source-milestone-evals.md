# Task 066 (nano-claw) — Cross-source milestone evaluations

Repo: nano-claw · Branch: spacechannel-persona
Two stages per the amended ADR order: stage one (corpus + machine-readable
trace contract) comes FIRST, before tasks 001/002/065 are implemented; stage
two (the full live run) comes last, after them. Gate for any further
unified-knowledge work.

## Why

ADR-001 defers graphs, hierarchical summaries, and vector indexes until an
evaluation demands them. Both reviews flagged the original spec's deferral of
evaluation to Phase 3 as its worst sequencing error. This task turns the build
brief's milestone questions into a checked-in, repeatable eval so every later
capability must earn its place against a failing question.

## Requirements

1. A checked-in eval corpus (JSON/YAML) with three groups, initially:
   - Document (5): incl. "Summarize the architecture described in the Riff
     design document" and gap/principles questions with expected evidence
     anchors.
   - Code (5): incl. "Where is request routing implemented?" and "Explain the
     request path from input to answer" against the ingested nano-claw or
     intelligence-platform corpus, with expected file/symbol citations.
   - Cross-source (3): incl. "Which components correspond to the routing and
     reasoning sections of the design?" and "What evidence is missing or
     ambiguous?" with expected per-source citations.
2. A runner that executes each question through the real pipeline (API turns
   with session scope set) and scores: expected-evidence hit rate, citation
   presence per claim, and routing correctness (fast vs deep vs registry).
   Model-output prose is NOT string-matched. Note: the platform's evaluation
   tests are deterministic or private-file-gated, not provider-gated — this
   runner is explicitly live-provider and opt-in.
3. Failure-mode cases from the 2026-07-22 postmortem included: a deep question
   whose artifact fails validation must surface the graceful fallback and a
   named error code — asserted, not assumed.
4. Results write a dated scoreboard file so drift across changes is visible;
   the runner prints deltas vs the last run.
5. "Absent/not implemented" answers in cross-source questions must carry the
   coverage disclaimer (never bare "retrieval found nothing" — per ADR-001).

## Documentation & reusability bar

Corpus format documented in the file header; runner usage in
`docs/intelligence-platform.md`. The corpus is the shared substrate proposal
060 (routing evaluation) should extend rather than duplicate — cross-link it.

## Self-check

- Runner executes end-to-end against the live local stack and produces a
  scoreboard; at least the document group passes fully before this task is
  called delivered (code and cross-source groups may have known-failing
  entries — that is the point — but each must fail for a stated, tracked
  reason, not an infrastructure error).

## Deliverables

- Eval corpus file, runner, scoreboard output path, doc section, cross-link
  into `.context/backlog/060-selective-deep-routing-evaluation.md`.
- Result: `.context/results/066-cross-source-milestone-evals.result.md`

## Codex review amendments (binding, 2026-07-22)

6. Machine-readable eval trace is a prerequisite: fast turns currently return
   only response text plus an evidence count, deep claims stay internal, and
   NanoClaw's evidence parsers drop file/line citation data. Either add a
   structured eval-trace payload to the API (preferred) or score via direct
   internal evaluation — decide in stage one.
7. Metrics must be pinned in stage one: top-k, any/all anchor rules, claim
   segmentation for fast responses, the route enum, affirmation handling,
   repetition counts, model/config versions, source commits/digests, and
   scoreboard baseline selection.
8. Isolation: each run uses a dedicated tenant/database and a controlled
   registry — earlier runs must not flip later cases from deep to registry
   adoption.
9. Failure-mode cases use deterministic fault injection (a stub provider
   emitting a malformed artifact), never live-provider luck; assert error
   code and spoken fallback separately.
10. Coverage/absence semantics have an implementation owner: today's strict
    no-match guidance (`src/agent/context.ts:98`) tells the model a document
    "does not appear to cover" a topic on a mere retrieval miss — the exact
    behavior ADR-001 forbids. Reword to hedge coverage ("I didn't find
    evidence about that in what's loaded") as part of this task, and score it.
