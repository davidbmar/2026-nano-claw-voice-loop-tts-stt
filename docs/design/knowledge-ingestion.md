# Space Channel knowledge ingestion: measured hybrid pre-retrieval

Status: DESIGN / MEASURED SPIKE, 2026-07-19. No production behavior changes in
this task.

Decision: keep a small generated core in every prompt, select one or at most
two generated per-feed detail files with an in-process lexical index before the
LLM request, and retain the current flat `knowledge.md` as the fallback on every
retrieval failure. This is **pre-retrieval, not an agent tool**: it creates no
tool call, approval, network request, or extra LLM round trip.

## Constraints and success criteria

- Voice time to first token (TTFT) is the primary latency constraint. Retrieval
  must happen between transcription and the existing provider call and should
  consume only a few milliseconds of the budget.
- A retrieval error must never take down the assistant. Missing, corrupt,
  stale, over-budget, or low-confidence retrieval falls back to the last good
  flat digest. If that digest is also unreadable, today's existing
  "knowledge unavailable" prompt remains the final fail-open behavior.
- `site_index.json` remains builder input only. Raw crawl data is never prompt
  input.
- The artifact and selection contracts must be site-neutral so riff can use the
  shared `build_knowledge.py` output without importing nano-claw runtime code.
- This design does not add production wiring. It evaluates the seam and leaves
  implementation to separately reviewed follow-up tasks.

## Current pipeline, precisely

```text
scripts/crawl_site.py
  -> data/<site>/site_index.json              raw pages + JSON feeds
  -> scripts/build_knowledge.py
       -> data/<site>/knowledge.md            dense prompt digest
       -> data/<site>/knowledge/*.md          fuller per-feed details

run.sh -> NANO_CLAW_KNOWLEDGE=/app/sites/<site>/knowledge.md
  -> resolveKnowledgeFiles()
  -> AgentConfig.knowledgeFiles
  -> ContextBuilder.buildSystemPrompt()
  -> providerManager.completeStream()
```

For the current Space Channel tree:

1. `scripts/crawl_site.py` writes `site_index.json` atomically. The current
   index has three pages and eight captured feeds.
2. `scripts/build_knowledge.py` reads that index and the optional authored
   `docs/knowledge/spacechannel.md` overview. It renders a deterministic digest
   with stable sections first, feed capture times, normalized dates, and
   precomputed rollups such as the next launch and upcoming launch count. It
   refuses to replace the last good output when all feeds are degraded, the
   result is under 2,000 characters, or the digest exceeds 60,000 characters.
   It uses `chars // 4` as its token-budget estimate.
3. The same successful build writes eight fuller detail files using the same
   renderers with a larger per-item text limit. They are safe prompt material;
   the raw JSON is not.
4. Unless `NANO_CLAW_KNOWLEDGE` is already set, `run.sh:192-209` expands only
   `data/*/knowledge.md` and rewrites those host paths to the read-only
   container mount. That glob cannot match `data/*/knowledge/*.md`, so only
   each site's flat digest enters `NANO_CLAW_KNOWLEDGE`.
5. `src/agent/knowledge.ts` resolves configured and environment paths, calls
   `statSync` each time, re-reads a file only when its mtime changes, and logs
   and skips unreadable files. `src/agent/context.ts:18-61` places the combined
   digest and grounding rules in the system message.
6. The knowledge sits before `SYSTEM_CACHE_MARKER`. `src/providers/base.ts`
   turns that prefix into an explicit Anthropic cache-control block; other
   providers strip the internal marker. A digest refresh changes the cached
   prefix. Prompt caching may reduce repeated provider prefill cost, but the
   entire digest is still in every request's context.
7. `handleChat` adds the transcribed user message to `Memory`; then
   `src/api/server.ts:stepLoopStream` builds the prompt and calls
   `providerManager.completeStream`. No reader in `voice/*.py` or
   `src/agent/*.ts` enumerates the detail directory today.
8. The existing `launchd` job runs `scripts/refresh_site.sh spacechannel` at
   02:00 and 14:00 local time. A successful crawl invokes the builder; the
   knowledge loader sees the new digest mtime on a later turn.

### Measured corpus and current per-turn load

Measurements below are from the checked-out 2026-07-18 snapshot, taken on
2026-07-19. Bytes are filesystem bytes. Characters are decoded Unicode
characters. "Estimated tokens" deliberately uses the repository builder's
reproducible `characters // 4` rule; nano-claw does not bundle the proprietary
`deepseek-v4-flash` tokenizer, so these are budget estimates rather than
provider billing tokens.

| Artifact | Bytes | Characters | Estimated tokens |
|---|---:|---:|---:|
| `site_index.json` (never injected) | 422,874 | 422,397 | n/a |
| `knowledge.md` | 39,195 | 38,490 | 9,622 |
| `knowledge/launches.md` | 9,504 | 9,409 | 2,352 |
| `knowledge/ufo-cases.md` | 4,063 | 4,045 | 1,011 |
| `knowledge/ufo-wire.md` | 9,815 | 9,703 | 2,425 |
| `knowledge/ufo-podcast.md` | 1,347 | 1,299 | 324 |
| `knowledge/maxq-podcast.md` | 1,324 | 1,294 | 323 |
| `knowledge/becker-tour.md` | 1,127 | 1,113 | 278 |
| `knowledge/data-lens-articles.md` | 18,190 | 17,852 | 4,463 |
| `knowledge/dsn-snapshot.md` | 313 | 311 | 77 |
| **All eight detail files** | **45,683** | **45,026** | **11,256** |

`loadKnowledge()` trims the final newline, so the current Space Channel payload
is 38,489 prompt characters, still about 9,622 estimated tokens. Including the
existing `## Knowledge` header and grounding directive, the Knowledge section
is 39,107 characters, or about **9,776 estimated tokens on every provider
request/voice turn**. Persona, timestamp, history, and tool definitions are
outside that number. Space Channel is currently the only auto-loaded site in
this tree.

The immediate scaling issue is visible in the numbers: the unused detail
corpus is already larger than the always-on digest, and one feed alone is about
4,463 tokens. Adding histories to the flat digest either loses detail to
renderer limits or approaches the builder's hard 60,000-character ceiling.

## Options evaluated

The hybrid measurements are from the spike in the next section. Full-RAG
latency is intentionally not presented as measured: it depends on an embedding
model and vector implementation that this repository does not contain.

| Axis | A. Flat digest (status quo) | B. Core + lexical pre-retrieval | C. Full local RAG | D. Section-routed digest |
|---|---|---|---|---|
| Added selection latency / voice TTFT | 0 ms retrieval, but the model receives about 9,776 knowledge tokens. | **0.014 ms** median of query medians warm; **0.022 ms** maximum p95 in this spike. Cold file read + index build was 3.243 ms and can run outside the turn path. Smaller prompts may also reduce model prefill. | Vector search is cheap, but a query embedding dominates and adds model/runtime-dependent milliseconds or tens of milliseconds. It needs a measured `<50 ms` gate before voice rollout. | Sub-millisecond keyword routing is plausible; an LLM router is rejected because it adds a network/model round trip before TTFT. |
| Grounding / recall | Good for rollups and items near the start/end; dense middle detail competes for attention and is shortened to fit. | Strong for feed-shaped questions because the complete relevant feed is adjacent to the question. The 8-query spike routed 8/8. Lexical aliases need evaluation coverage for synonyms, ambiguity, compound questions, and follow-ups. | Best fine-grained semantic recall when chunking, embeddings, and metadata are tuned; can lose document-wide context or return a semantically close but wrong snapshot. | Better position than one flat blob, but coarse routing still injects a whole section and a hand-maintained table of contents can drift. |
| Corpus-scale ceiling | Hard 60,000-character build guard and the model's prompt/context budget. Growth directly increases every turn. | Feed count and total corpus can grow without increasing every prompt. Ceiling is the size of one selected detail file and linear scan over feed count; graduate to chunking if files or feed count become large. | Highest ceiling; chunk count can grow far beyond the prompt budget. Local index size and embedding refresh cost become the limits. | Better than flat while sections remain bounded; eventually has the same oversized-section problem as file-level hybrid. |
| Freshness | Excellent and simple: twice-daily builder output is picked up by mtime. Last good digest survives failed builds. | Reuses the same build. A manifest/digest commit marker refreshes the in-memory index within 60 seconds; validation failure uses the new/last-good flat digest instead. | Requires a second, atomic embed/index job after every successful build. A digest/index snapshot mismatch is an additional stale-data failure mode. | Can reuse the builder and mtime, but router metadata must be regenerated with the sections. |
| Prompt-cache interaction | Entire digest is before the marker and cacheable on Anthropic, but any digest refresh invalidates it. Non-Anthropic paths receive the text without explicit cache control from nano-claw. | Stable core stays before the marker; selected detail is after it because it changes per question. Only the small per-turn suffix misses the explicit cache. Core refresh invalidates a much smaller stable block. | Same core/suffix split, but retrieved chunks churn more often and are unlikely to cache across turns. | Stable table/core can be cached; routed section belongs after the marker and churns by route. |
| Infrastructure / complexity | Lowest: files and one loader. | Low: standard-library-style tokenization/BM25, a small manifest, an in-memory cache, and prompt plumbing. No service, database, model, network, or new secret. | Highest: chunker, embedding model/API, vector store, snapshot/version management, reindexing, dependency packaging, and observability. | Medium-low: router rules and section boundaries are simple but become site-specific unless generated. |
| Portability to riff | The shared digest already ports, but brings the same scale/recall ceiling. | High. `build_knowledge.py` can emit a site-neutral manifest; nano-claw TypeScript and riff Python can implement the same small scoring contract. | Medium-low until both runtimes standardize the embedding model, storage, and deployment lifecycle. | Medium. Generated metadata is portable; hand-coded Space Channel routes are not. |
| Graceful degradation | Existing loader skips unreadable files and keeps the assistant alive, but there is no richer source to fall back from. | Straightforward: disabled, missing core, no confident hit, refresh/hash/read error, or budget violation all select the existing flat digest. Missing digest retains today's unavailable note. | Must explicitly bypass a missing/corrupt store and embedding failure to the digest; there are more failure points. | Router failure can fall back to the digest, but stale section boundaries also need validation. |

Option D is viable but offers no material advantage over B for the current
builder: the eight generated files already are stable section boundaries. Full
RAG is the right future graduation path if feed-level retrieval stops meeting
recall or size gates, not the right first dependency for eight files.

## Measured selection spike

`scripts/knowledge_retrieval_spike.py` is a standalone, standard-library-only
spike. It reads the eight generated Markdown files once, builds file-level BM25
statistics, and adds explicit query aliases to disambiguate related feeds (for
example, UFO cases vs. UFO wire vs. UFO Files podcast). Unknown future files
still participate through their text. The production design moves route
metadata into the generated manifest rather than hard-coding Space Channel in
runtime code.

Command:

```bash
python3 scripts/knowledge_retrieval_spike.py
```

Method: CPython, 1,000 warmed in-memory selections per question using
`perf_counter_ns`; `top-k=1`. File reads and index construction are reported
separately. This measures selection, not model answer quality or provider TTFT.

| # | Representative question | Selected detail | Median ms | p95 ms | Check |
|---:|---|---|---:|---:|---|
| 1 | What is the next launch on the mission tracker? | `launches.md` | 0.013 | 0.013 | PASS |
| 2 | What happened in the USS Nimitz Tic Tac UFO debate case? | `ufo-cases.md` | 0.015 | 0.018 | PASS |
| 3 | What is the newest MAXQ podcast episode saying about the market? | `maxq-podcast.md` | 0.014 | 0.016 | PASS |
| 4 | Where is David Becker taking the PLANETS show next? | `becker-tour.md` | 0.014 | 0.017 | PASS |
| 5 | What is the latest UFO wire headline about declassified files? | `ufo-wire.md` | 0.014 | 0.018 | PASS |
| 6 | What is the newest UFO Files podcast episode? | `ufo-podcast.md` | 0.014 | 0.017 | PASS |
| 7 | What does Data Lens say about Skyroot's Vikram-1 reaching orbit? | `data-lens-articles.md` | 0.018 | 0.022 | PASS |
| 8 | Which antenna is active in the Deep Space Network snapshot? | `dsn-snapshot.md` | 0.015 | 0.018 | PASS |

Measured result: **8 passed, 0 failed**; median of the eight per-question
medians was **0.014 ms**, maximum p95 was **0.022 ms**, and cold index build
including all file reads was **3.243 ms**. The selected payloads range from 77
to 4,463 estimated tokens instead of injecting all 11,256 detail tokens.

This is evidence for the routing mechanism and latency shape, not a complete
retrieval-quality claim. A production evaluation must add paraphrases,
misspellings from STT, compound questions, no-match questions, and referential
follow-ups such as "what about the one after that?" It must also verify grounded
answer accuracy with the configured default model.

## Recommendation: hybrid core + generated-feed retrieval

Choose option B now.

The corpus already has the right retrieval unit: each builder renderer emits a
coherent, timestamped, prompt-safe file. The spike identifies all eight units
with effectively negligible local selection time and no new runtime. Keeping
the digest as a fallback preserves current coverage and degraded behavior.
Compared with full RAG, this recommendation avoids an embedding model and
vector lifecycle before the corpus demonstrates a need for either.

The operating invariant is:

```text
retrieval disabled/shadowed -> full flat digest
retrieval succeeds          -> small core + selected detail suffix
anything about retrieval fails or is uncertain -> full flat digest
flat digest also unavailable -> existing unavailable note; assistant continues
```

### Builder artifact contract

A follow-up should extend the shared `scripts/build_knowledge.py` successful
build to emit, without removing or weakening `knowledge.md`:

- `data/<site>/knowledge-core.md`: identity/site overview plus small explicit
  rollups (for example next launch/count, newest episode/headline, next event,
  and each feed's captured-at time). It should have its own 8,000-character
  hard cap (about 2,000 estimated tokens). The current authored Space Channel
  overview is only 1,961 bytes, leaving room for these rollups. Core renderers
  should be explicit; do not obtain a core by blindly truncating Markdown.
- `data/<site>/knowledge/manifest.json`: a versioned, site-neutral list of the
  generated detail files with title, feed slug, aliases/keywords, character
  count, SHA-256, and snapshot id. This is the portable contract consumed by
  nano-claw and riff. Unknown feeds get at least filename and first-heading
  terms.
- The existing full `knowledge.md` remains mandatory fallback material and
  retains its current guards and stable-first ordering.

All candidate outputs must be rendered and validated before replacement.
Write each detail, core, and manifest atomically, then write `knowledge.md`
last as the snapshot commit marker. A validation failure performs no writes. A
mid-write process crash leaves the old commit marker in place; manifest hashes
make the selector reject that partial set and use the flat digest until the
next successful build repairs it, rather than mixing snapshots in a prompt.

### Selection module

Add `src/agent/knowledge-retrieval.ts` with a process-wide
`KnowledgeRetrievalManager`:

1. Derive `knowledge-core.md` and `knowledge/manifest.json` from each configured
   `.../knowledge.md`; no Space Channel path is hard-coded.
2. Validate the manifest snapshot and hashes, read each listed detail, and
   build an immutable in-memory BM25 index over content plus generated route
   metadata. Build a replacement index off to the side and swap it only after
   complete validation.
3. Select from the latest `role: "user"` transcription already in `Memory`.
   Return the highest confident file by default. Permit a second file only for
   a close, independently matching result and only while the total character
   budget remains satisfied. An empty query or no confident score is a digest
   fallback, not an empty knowledge prompt.
4. Return a discriminated result such as
   `{mode: "retrieved", core, details, sources, selectionMs}` or
   `{mode: "digest-fallback", digest, reason, selectionMs}`. The public method
   catches all filesystem, parse, index, and scoring errors; callers never see
   an exception.
5. Log mode, reason, selected filenames, snapshot, and latency, but never the
   user's transcript. Add counters for retrieval success/fallback and selected
   characters.

The first production version should remain file-level. Trigger a new full-RAG
evaluation if any detail exceeds the injection cap, the feed list grows beyond
roughly 100 files, or a broadened routing/answer eval falls below its agreed
recall target.

### Exact runtime hook and prompt shape

In `src/api/server.ts:stepLoopStream`, call the manager after `startTime` (so
debug TTFT includes selection) and after obtaining the current `Memory`
messages, but before constructing the final context and **before
`providerManager.completeStream`**:

```ts
const messages = memory.getMessages();
const turnKnowledge = knowledgeRetrieval.forTurn(messages, agentConfig.knowledgeFiles);
const contextBuilder = new ContextBuilder(agentConfig);
const contextMessages = contextBuilder.buildContextMessages(
  messages,
  skills,
  tools,
  turnKnowledge
);

for await (const ev of providerManager.completeStream(/* existing args */)) {
  // unchanged streaming loop
}
```

Apply the same helper before `providerManager.complete` in non-streaming
`stepLoop` so API modes cannot diverge, although the streaming hook is the
latency-critical voice path.

Extend `ContextBuilder` with an optional per-turn knowledge value rather than
mutating shared `AgentConfig`:

- Success: place grounding rules and `knowledge-core.md` before
  `SYSTEM_CACHE_MARKER`; place `## Retrieved knowledge for this turn` and the
  selected details immediately after the marker. The detail suffix is
  authoritative for that turn and includes its own capture timestamp.
- Digest fallback or feature off: preserve today's exact full-digest placement
  before the marker, with no retrieved suffix.
- Flat digest missing: preserve today's knowledge-unavailable note.

This makes retrieval wholly internal prompt preparation. It must not register
a tool definition or emit a `ToolCall`, so the browser approval path cannot be
entered.

### Configuration and rollout

Add a validated `agents.defaults.knowledgeRetrieval` object with environment
overrides:

| Setting / environment override | Default | Meaning |
|---|---:|---|
| `mode` / `NANO_CLAW_KNOWLEDGE_RETRIEVAL` | `off` | `off`, `shadow`, or `hybrid`. Shadow scores/logs but still injects the flat digest. |
| `topK` / `NANO_CLAW_KNOWLEDGE_RETRIEVAL_TOP_K` | `1` | Clamp to 1-2 selected files. |
| `maxChars` / `NANO_CLAW_KNOWLEDGE_RETRIEVAL_MAX_CHARS` | `20000` | Hard selected-detail budget (about 5,000 estimated tokens); the current largest detail fits. |
| `refreshMs` / `NANO_CLAW_KNOWLEDGE_RETRIEVAL_REFRESH_MS` | `60000` | Maximum delay before noticing a committed builder snapshot. |

Do not add a separate site path: derive artifacts from the existing
`knowledgeFiles`, which is what makes the feature portable and preserves
multi-site behavior. Invalid configuration should fail configuration parsing;
runtime artifact failure should fall back.

Roll out `off` by default, then `shadow` while collecting selection latency and
route/fallback counters, then a Space Channel `hybrid` canary. Acceptance gates
for the canary are no increase above 5 ms retrieval p95, zero retrieval-caused
request failures, expected prompt-token reduction on matched turns, and a
broadened route/grounded-answer eval passing its reviewed threshold.

### Refresh cadence and degraded mode

Keep the existing twice-daily crawl/build cadence. During shared server
initialization, warm the immutable index and start an unref'd refresh timer at
`refreshMs`. The timer watches the full digest commit marker/manifest snapshot,
builds a replacement cache away from request handling, and atomically swaps it.
Per-turn selection then does no file I/O.

On a detected snapshot change, do not use an old index with a new core. Until
the new set validates, return `digest-fallback`. Specific fallback reasons
include: feature disabled or shadowed, missing core/manifest/detail, unknown
manifest version, hash/snapshot mismatch, read/parse error, empty question,
low-confidence/no match, selected detail over budget, refresh in progress, or
unexpected selector exception. Every branch still calls the provider.

## Follow-up task decomposition

1. **Builder/core/manifest contract.** Extend the shared builder, add unit
   fixtures for deterministic core and manifest output, validate caps/hashes,
   and prove a failed build replaces none of the last-good artifacts. Keep the
   flat digest byte-for-byte compatible unless its source data changed.
2. **Portable lexical selector and config.** Implement the TypeScript manager,
   schema/env parsing, immutable refresh cache, BM25 + manifest aliases,
   budgets, structured no-text telemetry, and unit tests for all eight spike
   questions plus paraphrase, STT-noise, no-match, ambiguous/top-2, and corrupt
   snapshot cases. Document the small manifest/scoring contract for riff's
   Python implementation.
3. **Prompt/server integration and canary.** Add the optional `TurnKnowledge`
   input to `ContextBuilder`; hook both streaming and non-streaming loops;
   assert core/detail placement around `SYSTEM_CACHE_MARKER`; stub the provider
   to prove every retrieval failure receives the flat digest and still streams;
   run shadow metrics, then enable the Space Channel canary only after the TTFT
   and grounded-answer gates pass.

## Task self-check

- Digest and all eight detail token estimates are tabulated above; current
  per-request Knowledge load is about 9,776 estimated tokens.
- The spike reports per-question selection latency and 8 passed / 0 failed.
- The recommendation is a non-tool pre-LLM step. Every retrieval failure falls
  back to the flat digest and never prevents the provider call.
- `python3 scripts/knowledge_retrieval_spike.py` exited 0 and printed the table
  above.
- This task changes only this design, the spike script, and its protocol result
  file. It makes no production changes, commits, or pushes.
