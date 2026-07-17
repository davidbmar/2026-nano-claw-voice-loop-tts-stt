# Hybrid voice flows: FSM skeleton, free-form muscle

*Design exploration, 2026-07-16 (overnight). Evidence from riff's live call
logs (m3-dev node, /tmp/riff-phone-node + ~/riff-dev-data/sessions) and
nano-claw's Space Channel phone line. Companion research: LiveKit turn
detection, pipecat smart-turn.*

## The question

Riff is FSM-driven: flows are graphs of scripted states (menus, slot
collection, confirmations). nano-claw's Space Channel line is the opposite:
no states at all — a persona, grounded knowledge, and free conversation.
The free-form line *feels* dramatically better to talk to. Can one framework
be both — FSM where the business needs guarantees, free-form where the
caller needs freedom? Or is the answer to adopt something like LiveKit?

## What the logs actually say

Riff's worst caller-facing failures are not LLM failures — they are what
happens when **rigid states meet fragmented turns**:

1. **Turn fragmentation is the root multiplier.** Riff's only end-of-turn
   signal is Gemini Live's 700 ms silence VAD. Real call `v3:YLa_t`: one
   intent arrived as three turns — "tell me some other interesting things
   about space channel like tell me" / "Like" / "Mars". Single-word turns
   ("Like", "Okay.", "Yes.") then hit slot-filling states as if they were
   complete utterances → INTAKE STALL ×4 in recent sessions, re-asks,
   mishears ("stock news" → "stop news").
2. **Dead-air is an FSM disease.** Open bugs B-031 (30 s of silence after a
   menu no-match), B-303 (await_confirmation gate never fires, agent goes
   silent), B-413 (four silent choice-hub paths). A free-form agent
   physically cannot produce these: it always has something to say next.
3. **Barge-in by energy threshold fails both ways** — missed real
   interruptions (259 ms sustain vs the 280 ms bar; "I said web observatory
   and it kept reading") and spurious double-fires 0.3 s apart.
4. Content-fetch turns run 7–16 s with the caller waiting.

Meanwhile the nano-claw line's logs show the free-form failure class:
nothing catastrophic, but **no guarantees** — it cannot reliably collect a
callback number, enforce a confirmation, or follow a compliance script. Its
"flow" is one paragraph of persona prompt.

The conclusion the evidence forces: **neither pure architecture is right.**
Scripted states are where riff's callers suffer; free-form is where the
business loses control. The failure modes are complementary.

## The hybrid: goal regions inside FSM flows

Keep the flow graph. Add one new state kind.

```yaml
states:
  greet:            {kind: scripted, say: "...exact legal wording...", next: topic}
  topic:            {kind: goal,                      # ← the new thing
    goal: "Understand what the caller wants help with today.",
    knowledge: spacechannel,                          # grounding digest
    exits:
      - when: {slot_filled: topic}                    # extractor-satisfied
        next: route_topic
      - when: {caller_asks: human}                    # escape hatch
        next: transfer
      - when: {budget: {turns: 6, seconds: 90}}       # bounded freedom
        next: route_topic_fallback
    extract:                                          # typed exit conditions
      topic: {type: enum, values: [launches, ufo, news, shows, other]}
  }
  confirm_callback: {kind: scripted, ...}             # guarantees stay scripted
```

Semantics of a **goal region**:

- Entering it hands the conversation to a nano-claw-style agent: goal +
  persona + grounding knowledge + running transcript. It converses freely —
  fragments, digressions, follow-up questions all absorbed the way a
  conversation absorbs them, because nothing is pattern-matching single
  turns against a menu.
- Every caller turn ALSO passes through an **extractor** (a cheap
  structured-output LLM call — riff already has slot extractors; this
  generalizes them): "given the transcript, are any exit conditions met?
  fill the typed slots." The FSM resumes the moment an exit fires. The
  extractor is the trust boundary: slots are typed, validated, and the only
  channel by which free conversation writes business state.
- Budgets and escape hatches make the freedom bounded and auditable — the
  region can never trap a caller, and every entry/exit lands in the state
  trace exactly like any transition.

Why this fixes what the logs show: fragments stop mattering inside regions
(the agent replies to "Like… Mars" like a human would), dead-air states
disappear wherever a region replaces a silent hub (the agent always speaks),
and the FSM keeps its guarantees precisely where riff needs them — consent
lines, confirmations, payment, transfer.

### What riff already has that makes this cheap

`turn_router`'s deterministic-first routing, slot extractors, flow YAML +
loader, `state_trace`/bus-event auditing. The delta is: one new state kind,
a goal-agent runtime (nano-claw's turn loop is ~the reference
implementation), and an exit-condition evaluator. It is an incremental
riff feature, not a rewrite.

### Does LiveKit answer this instead?

No — this is the layer LiveKit does not have strong opinions about. LiveKit
Agents gives transport, turn detection, and interruption plumbing; its
workflow/handoff primitives are thinner than riff's flow engine. Adopting
LiveKit would still leave the FSM/free-form orchestration to build. The two
decisions are independent:

- **Hybrid orchestration** → build in riff (this doc).
- **Turn-taking substrate** → emulate LiveKit's ideas with open parts
  (below); revisit full LiveKit transport only if the substrate upgrade
  underdelivers.

## The shared substrate both need: better turn-taking

The hybrid inherits riff's fragmentation unless endpointing improves. The
LiveKit-idea kit, with licenses checked (2026-07-16):

| Piece | What | License / status |
|---|---|---|
| Dynamic endpointing algorithm | short wait (~0.5 s) when the utterance is semantically complete, long wait (up to ~6 s) when it isn't | An idea — freely emulatable |
| LiveKit turn-detector model | transcript- (v0) / audio-based (v1) EOU scorer | **Unusable**: license forbids use outside LiveKit Agents |
| pipecat **smart-turn-v3** | audio-native EOU, 8M params, Whisper-Tiny base, int8 ONNX, CPU | **BSD-2 — the legally clean choice** |
| Silero VAD | neural speech/no-speech, tiny ONNX | MIT — replaces RMS thresholds for VAD & barge-in |
| Streaming-first turns | speak sentence 1 while the LLM writes | Pattern — **already shipped** in nano-claw tonight (first sentence at ~1.1–1.4 s vs 3–5 s full-generation wait) |

Proposed endpointing flow (nano-claw gateway first, riff's transport next):

```
caller audio → Silero VAD frames
  pause ≥ ~350 ms detected
    → smart-turn-v3 on the utterance audio (CPU, ~10-50 ms)
        complete   → endpoint NOW  (beats today's fixed 700 ms)
        incomplete → keep listening, up to max_endpoint (~3-4 s)
  barge-in: Silero speech-prob over agent-speaking window replaces RMS+sustain
```

This attacks riff evidence items #1 and #4 with two open components and one
emulated algorithm — no LiveKit adoption, no license risk.

## Pilot plan

1. **nano-claw gateway as the lab** (it already answers a live number):
   integrate Silero + smart-turn-v3 endpointing behind an env flag; A/B with
   the loopback harness (`scripts/phone_loopback_test.py`) and real calls.
2. **Riff adopts the substrate** in `telnyx_transport`/`live_client` (local
   VAD + EOU instead of relying on Gemini's 700 ms server VAD), shrinking
   fragmentation for every existing flow untouched.
3. **Goal regions land in riff** as a new state kind, piloted on the
   `space_channel_widgets` flow (drop-in candidate: replace its free-Q&A-ish
   states with one `goal` region backed by the Space Channel digest — the
   nano-claw persona shows exactly how it should feel).
4. Revisit "do we need LiveKit" only after 1–3: if turn quality still lags
   their published false-cutoff numbers, a LiveKit-transport spike on a test
   number is the next experiment.

## Empirical addendum (overnight bench results)

smart-turn-v3.2 was benched on CPU (`scripts/turn_detection/`): 8–35 ms
inference, real discrimination on synthetic mid-word cuts (0.34 vs 0.93).
**But on riff's real archived caller audio it MISSED the marquee fragment**:
"…space channel like tell me about" scored 0.987 COMPLETE at the exact
boundary where riff cut the caller off (session `v3:YLa_t`, 16 kHz caller
track). The caller trailed off with finished-sounding prosody — acoustics
alone cannot catch this class.

What would catch it is trivial: the transcript ends in a **preposition**.
Same for the other observed fragments ("What is the next…" — article;
"…about, um," — filler). Revised substrate recommendation, in order:

1. **Text-tail completeness heuristic** (deterministic, license-free,
   ~zero cost): utterance transcript ending in preposition / conjunction /
   article / filler ⇒ treat as incomplete ⇒ extend the endpoint window.
   Catches every fragment observed in riff's logs.
2. **Dynamic two-stage endpointing**: short pause (~400 ms) → fast STT →
   tail heuristic (and optionally smart-turn acoustic score as a second
   vote) → endpoint now or keep listening (cap ~3 s).
3. smart-turn-v3 stays useful as the acoustic vote and for barge-in
   robustness, but is **not sufficient alone** for riff's #1 failure.

This mirrors where the industry actually went — LiveKit's v1 fuses
semantic + acoustic precisely because neither is enough alone.

## Codex review revisions (2026-07-16, 18 findings, 8 P1 — accepted changes)

The pre-implementation Codex pass reshaped the design. Decisions:

1. **One supervisor call per caller turn, not agent+extractor** (finding 16):
   the region LLM call returns `{reply, slot_candidates, exit_candidate,
   evidence}` in one shot; **deterministic validators** (typed enums, regex,
   global commands) accept/reject candidates and choose transitions. No
   second extractor, no agent/extractor race, defined ordering (finding 2):
   a validated exit wins and the region reply is dropped in favor of the
   next state's entry line.
2. **Trust boundary restated** (finding 3): validators + explicit scripted
   confirmation states establish consequential facts; the LLM only
   nominates candidates. Consent/identity/payment fields always confirm.
3. **Budgets are wall-clock deadlines** (finding 5): an asyncio timer per
   goal state fires the timeout transition even if the caller never speaks
   again; turn budgets count *completed logical turns*.
4. **Deterministic global escapes** (finding 9): operator/human/goodbye
   keywords, DTMF, hangup, and deadlines are code-level checks that run
   before the LLM sees the turn; the extractor is a fallback nominator only.
5. **Dead-air is a watchdog property** (finding 6): the existing idle
   watchdog stays authoritative; regions add nothing magical.
6. **Flow state lives in the gateway** (finding 8): the FlowRun is owned by
   the PhoneCall (single-writer), persisted as append-only `flow_events`
   rows (finding 12) with state/enter/exit/slot/budget records. The TS chat
   memory is NOT used for region turns — regions keep their own transcript.
7. **Fix lossless inbound buffering first** (finding 1): audio arriving
   while STT/LLM runs must keep feeding the endpointer and queue the next
   utterance; today it is dropped — which also eats tail-extension
   continuations.
8. Tail-heuristic softened (finding 14): "more"/"this" removed from the
   dangling set; treated as one vote, not an oracle.
9. Deferred (tracked, not in v1): smart-turn integration (15), 16k Silero
   path, cost/latency SLOs for the supervisor call (18), multi-language.

## Scheduling-eval verdict (2026-07-16, live run)

The hybrid was tested end-to-end on the plumber-booking benchmark:
`GoalRegionRunner` (voice/goal_region.py, the design's semantics exactly)
against a real Google Calendar week seeded with FAKE fixtures
(scripts/scheduling_eval/), eight LLM-simulated caller scenarios, scored
against ground-truth free windows — never against the transcript.

**Result: 8/8 scenarios passed. Zero invalid slot nominations.**

- Duration-aware booking worked at every size: 30 m landed in a fragment
  gap on the hardest day; 2 h and 4 h bookings ended flush against real
  appointments; the exact-fit boundaries held.
- The impossible ask (4 h, Friday-only) produced 7 turns of honest
  negotiation and **no booking** — the free-form agent could not improvise
  a phantom appointment because the validator is the only write path.
- Ambiguity resolved correctly ("Thursday-ish" 1 h → Thursday can't hold
  an hour → booked Wednesday). Change-of-mind rebooked cleanly. The
  human-escape fired deterministically before any LLM call.
- With the availability digest in the prompt, the supervisor never even
  nominated an invalid slot (0 validator rejections) — the trust boundary
  was load-bearing in design but untouched in practice.

**Verdict: the hybrid is a good path.** Goal regions deliver both halves:
free conversation absorbs fragments/digressions/negotiation, and the
business keeps guarantees (typed slots, validated writes, bounded budgets,
deterministic escapes). Proceed to riff (pilot steps 2–3).

**The open cost is latency**: supervisor p50 3.2–5.2 s/turn on
claude-opus-4-8 — acceptable for text, too slow for a phone turn. Before
the phone leg ships regions: sweep smaller/faster models
(SCHED_EVAL_MODEL), measure quality at lower effort, and stream the reply
sentence-first (nano-claw already does this for plain chat). Also fixed
en route: structured-outputs schemas reject `enum` on a union type — use
`anyOf` (real-API contract; invisible to fake-client tests).

## Supervisor latency ladder (2026-07-17, live sweeps)

Same 8-scenario eval, four supervisor configs (caller sim on the same
model; p50 per-turn supervisor latency ranges across scenarios):

| Config | Outcomes | p50/turn | Notes |
|---|---|---|---|
| opus-4-8 (thinking omitted = off) | 8/8 | 3.2–5.2 s | flawless |
| sonnet-5 (thinking omitted = ADAPTIVE ON) | 7/8¹ | 4.5–6.1 s | thinking tax |
| sonnet-5 (thinking disabled) | 8/8 outcomes² | 3.9–5.7 s | no latency win |
| haiku-4-5 | 5/8 | 2.1–3.6 s | fast; fails to close negotiations |

¹ harness fragility (empty caller reply), fixed in eval since.
² scored 7/8 on an exit-type technicality: the impossible ask ended
  no_booking (correct) via the caller cap rather than a budget exit.

Conclusions: model choice is NOT the phone-latency lever — Opus 4.8 wins
quality at competitive latency; Haiku's speed costs negotiation-closing
competence (it fills easy slots but never lands a valid start time).
The real levers for the phone leg: stream the region reply sentence-first
(as nano-claw chat already does), cap reply length in the region prompt,
and test output_config effort=low on Opus. Also note the API contract
gotcha that skewed round one: omitting `thinking` means OFF on Opus 4.8
but ADAPTIVE ON on Sonnet 5 (SCHED_EVAL_THINKING=disabled equalizes).

## Open questions (for the morning)

- Extractor cost/latency budget per turn inside regions (a second small LLM
  call per caller turn — cacheable prompt makes it cheap, but measure).
- Where region transcripts live in riff's session artifacts (bus events
  already capture turns; regions should tag them).
- Barge-in policy inside regions vs scripted states (regions can be more
  permissive).
- smart-turn-v3 on 8 kHz phone audio: model expects 16 k; upsampled
  narrowband performance needs a bench (tonight's prototype).
