# Task 065 (nano-claw) — Session-scoped corpus selection ("load the riff design and the nano-claw code")

Repo: nano-claw · Branch: spacechannel-persona
Depends on intelligence-platform tasks 001/002 only for code corpora to exist;
the mechanism itself works today with document collections.

## Why

`KnowledgeScope.collection_ids` is plural end-to-end (platform
`contracts/policy.py:31`; NanoClaw sends `collection_ids` in
`src/agent/intelligence.ts:105`), but NanoClaw freezes the set at deploy time
via `NANO_CLAW_INTELLIGENCE_COLLECTIONS`. User direction 2026-07-22: "have a
way to load different things to talk about — a document, this code itself, or
multiple codebases — and ask architectural questions against the combination."
Multi-corpus questioning needs a conversational scope selector plus the contract work amended below.

## Requirements

1. Session scope state in `Memory` (like analysis state; in-memory + sidecar
   like the analysis sidecar): the active collection set, defaulting to the
   env-configured collections when unset.
2. Deterministic scope verbs resolved before evidence retrieval, in the same
   pattern as analysis navigation intents:
   - "what can we talk about / what's available" → list ingested collections
     (needs a platform listing: use `GET /v1/collections` if present, else add
     a minimal read-only endpoint to the platform as part of this task).
   - "load / talk about / open X [and Y]" → set scope (fuzzy-match collection
     names; confirm the resolved set back in one sentence).
   - "add X" / "drop X" → mutate; "what's loaded" → read back.
3. The session scope flows into `retrieveTurnEvidence` and every
   `TaskRequest.scope` (deep passes and registry adoption search alike —
   registry `POST /v1/analysis/search` should also filter or rank by the
   active scope so artifact reuse respects what is loaded).
4. Scope changes never mutate env config; a fresh session starts from the env
   default. Unknown collection names degrade gracefully ("I don't have that
   loaded; available: …").
5. Debug output records the active scope per turn.

## Documentation & reusability bar

Document the verbs in `docs/intelligence-platform.md` next to the deep
reasoning section. Export the scope-intent matcher for the 060 corpus.

## Self-check

- `npx vitest run` green; new tests: verb parsing (load/add/drop/list/what's
  loaded), scope plumb-through into retrieval and TaskRequest payloads
  (mock http asserts collection_ids), env-default fallback, unknown-name
  degradation.
- Live: rebuild container; "load the owning the demand playbook" then a gap
  question — debug shows the scoped collection; then "what's loaded".

## Deliverables

- `src/agent/memory.ts`, scope intent module, wiring in `intelligence.ts` /
  `deep-reasoning.ts` / `server.ts`, tests, doc section; platform listing
  endpoint if absent (kept minimal, read-only).
- Result: `.context/results/065-session-corpus-scope.result.md`

## Codex review amendments (binding, 2026-07-22)

6. Collection catalog is a prerequisite, not a nice-to-have: no
   `GET /v1/collections` exists and collections are unnormalized string tags.
   The platform endpoint must define stable IDs, display names, authorization,
   and an ambiguity threshold before fuzzy voice matching is testable.
7. Tri-state scope semantics: an empty `collection_ids` currently means "no
   filter" (`sqlite.py:822`), so dropping the last loaded collection would
   silently broaden access to every tenant collection. Distinguish
   unset/default vs explicit set vs explicitly-none (refuse retrieval with a
   spoken "nothing is loaded").
8. Scoped registry reuse requires a contract change: `AnalysisSearchRequest`
   has no scope field even though artifact records store their original scope.
   Specify subset/intersection/ranking semantics on the platform side.
9. Scope changes must invalidate or re-gate existing analysis state and any
   pending deep confirmation — a newly loaded corpus must not answer from an
   artifact created under the previous scope. Key session scope to
   tenant/profile so profile switches cannot reuse foreign state.
10. Sidecar acceptance: persistence across restart, corrupt-state recovery,
    `clear`/`delete` cleanup, ephemeral sweep, and two-session isolation.
