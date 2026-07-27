# Streaming Prepared Speech: Sentence-Level Compilation on the Phone Line

2026-07-27 · branch `call-review-panel` · proposed by David ("break this into
sentences, still run prepared speech mode on each sentence"), architecture
refined to recompute-with-frontier.

## Problem

The phone's "prepared" speech mode buffered the ENTIRE LLM reply before
compiling it into SpeechChunks (pauses, comma/dash clause expansion,
spoken-form normalization) — the caller waited for full generation (~2.3s)
plus prep plus first-sentence synthesis after STT before hearing anything.

## Invariant

For ANY partitioning of a reply into deltas, streaming output is IDENTICAL
to batch `compile_speech(whole)` (with jitter=0): same texts, kinds, pauses,
sequences, list numbering ("First,/Second,"), label merges, normalizations.
Enforced by a partition-fuzz golden suite
(`tests/python/test_streaming_speech_compiler.py`).

## Architecture: recompute-with-frontier

`StreamingSpeechCompiler` (voice/speech_preparer.py) keeps only the raw
buffer, the emitted-unit texts, and a finished flag. Every `feed(delta)`
re-runs the exact batch pipeline over the whole buffer (pure regex,
microseconds) and emits units up to a conservative **stability frontier** —
so document-level state (list counters, label merges, paragraph joins) can
never drift from batch: equivalence is by construction, not bookkeeping.

Frontier holds back:
1. The open tail region — a trailing paragraph (even newline-terminated:
   the next line would join it), a heading/list line missing its newline,
   or a trailing bare `\r` (half a CRLF). Sealed by a blank line or a
   newline-terminated structural line.
2. Within an open paragraph: the last sentence (verified: no normalization
   pattern spans two `[.!?]\s+` boundaries; the held last sentence absorbs
   single-boundary patterns like `No. 5`, `e.g.`, dotted acronyms).
3. A closed label-like segment ≤10 words with no follower yet (the batch
   label-merge may still claim it).
4. An open tail containing unclosed markup (odd ``` ``` ``` / `` ` `` /
   `*` counts, unbalanced `[`/`]`) — `_clean_inline` could rewrite it.

Streamed chunks always get punctuation-table pauses (`more_coming=True`);
only `finish()`'s last chunk carries `FINAL_TAIL_PAD_MS`. Safe because
pauses are punctuation-local (`_ensure_terminal` makes `_boundary_pause`'s
next-kind branch unreachable — a tripwire test pins this). Emission is
strictly in order with one jitter draw per non-final chunk, so even seeded
jitter reproduces the batch draw sequence.

`finish(final_text)` treats the API's `final.response` as authoritative,
with a longest-common-prefix guard: on any divergence it emits only units
beyond the emitted count and logs — never re-speaks, never crashes.

Latency shape: the first sentence speaks once the second begins (one-
sentence lag); single-sentence replies degrade to batch timing.

## Phone integration (voice/phone.py `stream_sentences`)

Three-way mode via `NANO_CLAW_PHONE_SPEECH_PREPARATION` (console-settable,
persisted): `1` = streaming prepared (default), `batch` = the old
buffer-all behavior (escape hatch), `raw` = no compiler.

Streaming arm safety around server-side rewrites (`holdResponse` guards in
src/api/server.ts can replace or prepend text after streaming):
- A **giant first delta** (>350 chars) is the held-response synthetic
  delta → the turn compiles at final (batch), never streaming pre-rewrite
  text. `deep_started` turns are likewise held.
- At `final`, if the response diverges from the fed deltas (not equal, not
  an extension) after chunks were already spoken: tap event
  `prepared_stream_mismatch`, `preparedStreamMismatch: true` on the
  assistant_turn payload, and the rewritten tail is NOT spoken (never
  double-speak). Extension (`response.startswith(fed)`) flows through
  `finish(response)` naturally.

Thinking cue stops at `_speak_sentences` as before — its window shortens
automatically since first speech now lands sooner. Barge-in teardown is the
existing generator-abandon path.

## Bundled fix: call-time env reads (the reload flake family)

Pause/clause/jitter knobs were frozen at module import, forcing tests to
`importlib.reload` the module — which rebound the `SpeechChunk` class and
order-poisoned other test files (the historical
`test_prepared_phone_units...` flake). All knobs now read `os.environ` per
call; every `importlib.reload` was deleted from `test_speech_preparer.py`.
`SPEECH_COMPILER_VERSION` stays `nanoclaw-speech-v1` (no output divergence).

Note: `test_history_api.py::test_all_completion_paths_and_partial_error_rules`
turned out to be a deterministic pre-existing failure (fails in isolation),
unrelated to the reload family — out of scope here.

## Docs shipped with this feature

- User explainer: `docs/html/speech-pipeline.html` (+ `.md` twin).
- Agent self-knowledge: `data/base/knowledge.md` — chime/tick meanings and
  sentence-by-sentence speaking, so the live AI can explain itself.
- `docs/CALL-REVIEW.md` (payload field), `docs/PHONE-QUALITY.md` (tap
  events + new synth ordering), `.env.example`.

## Verification

Full pytest suite green (modulo the one documented pre-existing
test_history_api failure); loopback multi-sentence prompt shows first
`synth_start` BEFORE `agent_done` and a lower FIRST ANSWER AUDIO; live call
A/B `prepared` vs `batch` for pause parity; ask the line "what's that
ticking?" to verify the self-knowledge digest.
