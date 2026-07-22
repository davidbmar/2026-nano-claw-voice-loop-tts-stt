# Intelligence Platform integration

NanoClaw can retrieve bounded evidence from the local `intelligence-platform`
before its conversational model call. This is prompt preparation, not an LLM
tool, so it does not enter the approval loop or add another model round trip.

## Local Owning the Demand setup

Index the source document once and start the evidence API:

```bash
cd /path/to/intelligence-platform
uv run intelligence-platform ingest-docx \
  /path/to/owningthedemand.docx \
  --tenant personal \
  --collection owning-the-demand \
  --document-key owning-the-demand

# The intelligence-platform .env selects DeepSeek and reuses DEEPSEEK_API_KEY
# from this repository's ignored .env.
uv run uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

In the shell that starts NanoClaw:

```bash
export NANO_CLAW_INTELLIGENCE_URL=http://127.0.0.1:8000
export NANO_CLAW_INTELLIGENCE_TENANT=personal
export NANO_CLAW_INTELLIGENCE_COLLECTIONS=owning-the-demand
export NANO_CLAW_INTELLIGENCE_PROFILE=intelligence
export NANO_CLAW_INTELLIGENCE_GROUNDING=strict
export NANO_CLAW_VOICE_FLOW=intelligence
```

Setting the URL enables retrieval. `strict` grounding makes document questions
abstain when no evidence matches and report temporary unavailability if the
service cannot be reached. `augment` leaves ordinary model behavior unchanged
on those two paths.

The profile assignment is intentional. Space Channel and Replicant PM remain isolated from the
private document collection; choose **Document Intelligence** in the browser's MODE selector to
send a voice turn through retrieval and deep reasoning.

The equivalent `agents.defaults` configuration is:

```json
{
  "intelligence": {
    "enabled": true,
    "apiUrl": "http://127.0.0.1:8000",
    "tenantId": "personal",
    "principalId": "nano-claw",
    "collectionIds": ["owning-the-demand"],
    "limit": 5,
    "candidatePool": 40,
    "maxChars": 16000,
    "timeoutMs": 750,
    "groundingMode": "strict"
  }
}
```

For a named assistant profile, put the same `intelligence` object inside that
profile. Known profiles deliberately do not inherit default intelligence scope,
which prevents one persona's private document collection from leaking into
another.

If NanoClaw runs in Docker while the evidence API runs on macOS, use
`http://host.docker.internal:8000` instead of `127.0.0.1`.

## Runtime behavior

For each turn, NanoClaw sends only the latest user question (plus one preceding
user turn for short referential follow-ups) and the configured tenant and
collection scope. It injects validated evidence after the provider prompt-cache
marker and before the changing timestamp. Logs contain retrieval status, count,
and duration, but never the user's question or evidence text.

The conversational model may phrase evidence naturally. It is instructed not
to add factual claims and not to read internal citation identifiers aloud unless
the user requests citations.

## Deep reasoning turns

Enable the deep path after the intelligence API has a reasoning provider configured:

```bash
export NANO_CLAW_DEEP_REASONING=1
export NANO_CLAW_DEEP_ROUTING=auto
export NANO_CLAW_DEEP_THRESHOLD=4
export NANO_CLAW_DEEP_TIMEOUT_MS=240000
export NANO_CLAW_ANALYSIS_STYLE=topic_map
```

## Artifact-aware answer routing (task 062)

A fresh conversation whose question is analysis-shaped ("key principles",
"what does it lack", "what's missing") first searches the platform's analysis
registry tenant-wide and, when a completed artifact matches, answers from that
artifact — topic navigation for principle questions, the `gaps` presentation
(built from the artifact's `missing_evidence`) for gap questions — instead of
spending a fresh deep pass or bare retrieval. Explicit fresh-analysis phrases
("think deeply", "re-analyze", "fresh analysis") always bypass reuse, and any
registry failure degrades silently to the normal path.

```bash
export NANO_CLAW_ARTIFACT_ROUTING=1      # 0 disables registry adoption
export NANO_CLAW_ARTIFACT_ROUTE_MIN=0.35 # normalized-score floor for adoption
```

Verify the provider and indexed document directly before enabling voice:

```bash
cd /path/to/intelligence-platform
uv run intelligence-platform reason \
  "Critique this business plan, test its assumptions, and recommend what to validate next." \
  --tenant personal \
  --collection owning-the-demand \
  --workflow strategy_review
```

In `auto` mode, short lookups remain on the existing fast retrieval path. Explicit requests to
“think deeply,” comparisons, cross-section synthesis, trade-off analysis, and recommendation or
sequencing questions can route to an asynchronous reasoning task. `always` forces every eligible
turn through the deep path; `never` disables routing without removing the rest of the intelligence
configuration.

Business-plan and strategy judgment selects the named `strategy_review` workflow. “What is the
pricing strategy?” remains a fast lookup; “Is this pricing strategy viable, which assumptions are
weak, and what should I change?” routes deep. The router considers the last three user turns, so a
follow-up such as “Is that viable?” retains recent strategy context. The task assembles evidence
across goal, market, economics, risk, and execution lenses before producing a grounded,
snapshot-bound `analysis_artifact_v1`. The artifact contains a bottom line, three to seven ranked
topics, source-backed claims, separate analytical findings, missing evidence, and model policy.
The intelligence service—not NanoClaw—configures DeepSeek V4 Pro with thinking enabled at high
effort for this selective path. Maximum effort remains available for explicit offline reviews but
is not the interactive default because it can exceed the voice timeout.

For a routed voice turn, NanoClaw immediately emits and speaks “Let me think deeply about this.”
It then forwards safe task progress and plays a quiet generated two-note processing cue at most
once every 2.6 seconds. When the platform returns a supported `TaskResult`, NanoClaw's configured
conversational model verbalizes it naturally but is instructed not to add or alter facts. Provider,
budget, or network failures fail closed instead of falling back to an ungrounded document answer.

### Progressive analysis follow-ups

The first reply contains only the platform-rendered bottom line and the first three ranked topic
choices, with a hard 65-word instruction for the speaking model. NanoClaw saves the validated
generated artifact in `<session>.analysis.json`, separately from the transcript. It does not save
raw source excerpts in that sidecar. Brief and menu naturalization is buffered until the fast
model finishes; if it exceeds 65 or 45 words respectively, NanoClaw substitutes the validated
deterministic projection before emitting any audio delta.

Follow-ups resolve before the ordinary deep-question router:

- “Tell me about the second one” opens the second topic from the exact menu order.
- “Tell me more about distribution” resolves a topic label or alias, then lexical overlap.
- “What other topics are there?” offers the next bounded menu.
- “What evidence supports that?” reloads the already-completed authorized task and supplies only
  evidence linked to the active topic; it does not run V4 Pro again.
- “Give me the full written report” renders the stored map without new analysis.
- A changed assumption such as “What if paid acquisition is unavailable?” creates a new deep task
  and logs `analysis_state_changed` as its routing reason.
- An unrelated source lookup bypasses artifact navigation and uses normal document retrieval.

Vectors are intentionally not required in this version. Exact controls, ordinals, active state,
labels, aliases, and lexical matching cover the reliable cases first. Topic embeddings should be
added only after measured follow-up misses justify their latency and operational cost.

### Topic-map versus principle-graph experiment

The configuration rail includes a **Principle graph** switch under Deep Analysis. Off is the
production `topic_map` control. On selects the experimental `principle_graph` organization for the
next deep strategy task. It does not rewrite the current session artifact; a newly completed task
becomes active only after it validates successfully. Older artifacts remain in the Intelligence
Platform's append-only registry.

For a controlled comparison, use a fresh conversation for each mode and ask the exact same
question:

> Think deeply about this business plan. What are its weakest assumptions, and what should I test
> before investing further?

Then ask “What would change your recommendation?”, “What conflicts with that?”, and “Show me the
source evidence.” Compare decision coverage, challenge quality, voice clarity, deep latency and
tokens, and whether follow-ups avoid another deep task. The server-wide default can be set with
`NANO_CLAW_ANALYSIS_STYLE`; the browser switch is a per-session override.

### Manual acceptance sequence

After starting both services and selecting Document Intelligence, say:

1. “Analyze the weaknesses in this business model and recommend what to validate first.”
2. After the short topic menu: “Tell me about the second one.”
3. “What evidence supports that?”
4. “What other topics are there?”
5. “What if paid acquisition is unavailable?”

The first and fifth turns should emit the deep acknowledgement and progress events. Turns two
through four should not. In final API debug metadata, the first turn includes `artifactId`,
`topicCount`, and provider `modelUsage`; navigation turns include `analysisNavigation.action`,
`reason`, and `selectedTopicIds`. Bounded brief/menu turns also include `analysisVoiceGuard` with
the enforced word limit and whether deterministic fallback was needed. The second turn should
report `ordinal_menu_selection` and make no new reasoning-task submission.

In the configured 2026-07-21 smoke run, the deep provider took about 182 seconds for one pass, so
keep `NANO_CLAW_DEEP_TIMEOUT_MS` at 240000 or higher for this model. That run returned a 48-word
brief and seven reusable topics; ordinal/topic follow-ups then avoid paying that latency again.

Verified browser and phone barge-in stop playback and close the NanoClaw stream. NanoClaw
propagates that disconnect as an abort and makes a best-effort call to the platform cancellation
endpoint. `NANO_CLAW_BARGE_IN=1` exposes the browser capability but does not activate it for the
listener: the versioned per-browser toggle defaults off and begins at low sensitivity. Open-speaker
listeners should leave voice interruption off because the current RMS-only detector can treat the
assistant's own TTS as a sustained interruption. They can use the Stop audio button shown during
playback; headset users may explicitly opt in. The phone gateway uses the independent
`NANO_CLAW_PHONE_BARGE_IN` flag.

The echo-safe open-source design is AEC-first, then Silero VAD for low-latency candidate onset,
then confirmation through NanoClaw's existing local faster-whisper service. Candidate onset may
pause playback; only a non-empty transcript that does not match the active assistant sentence may
commit cancellation. Empty or echo-matching transcripts resume the queued reply.

The complete architecture, state machine, contracts, audio specification, failure matrix, and
runbook live in the intelligence-platform repository at
`docs/html/deep-reasoning-voice-architecture.html`.
Artifact memory, routing, lifecycle, and experiment design live at
`docs/html/artifact-aware-analysis-memory.html`.
