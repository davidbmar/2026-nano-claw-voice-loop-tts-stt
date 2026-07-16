# Overnight report — riff issues, LiveKit ideas, what got built

*Autonomous session, 2026-07-16 ~02:30–05:00. Companion to
`hybrid-flows-goal-regions.md` (the design) and `scripts/turn_detection/`
(the benches).*

## What the riff logs showed (evidence, not vibes)

1. **Turn fragmentation is the #1 caller-facing failure**, and it is
   **Gemini-server-VAD-specific**: frame-peak analysis of the worst real
   call (`v3:YLa_t`) shows one continuous 6 s speech run with no ≥450 ms
   quiet gap — yet Gemini's VAD split it into three turns ("…like tell me" /
   "Like" / "Mars"). Riff has no local VAD at all (zero silero/webrtcvad
   hits in the codebase).
2. Dead-air is an FSM disease (open bugs B-031/B-303/B-413); energy-only
   barge-in misses and double-fires; terse-caller STT (B-321) is riff's own
   documented top problem; content-fetch turns run 7–16 s.

## LiveKit: what to take, what to skip

- Their **turn-detector models are license-locked to LiveKit Agents** — not
  usable in riff or nano-claw. Their *algorithm* (short endpoint when the
  utterance seems complete, long when it doesn't) is freely emulatable.
- **pipecat smart-turn-v3** (BSD-2, 8 MB, 8–35 ms CPU) is the open acoustic
  scorer — but benched on riff's real caller audio it **missed the marquee
  fragment** (trail-off prosody sounds "finished"). Acoustic-only is not
  sufficient; semantic-first is the right order.
- LiveKit does not answer the FSM-vs-freeform question at all; that layer
  (goal regions) is riff-side design regardless of transport.

## Built and deployed tonight (nano-claw, all pushed)

1. **Streaming phone turns** — first sentence speaks at ~1.1–1.4 s while
   the model writes the rest (was: 3–5 s full-generation wait). Both nodes.
2. **Dynamic semantic endpointing** (`NANO_CLAW_PHONE_DYNAMIC_ENDPOINT=1`,
   canary on M3): 450 ms endpoint + deterministic transcript tail check
   (ends in preposition/article/filler/dangling verb ⇒ keep listening,
   merge continuation, re-transcribe whole). Catches every fragment class
   in riff's logs; complete questions endpoint 250 ms faster.
3. **Loopback + replay harnesses** (`scripts/phone_loopback_test.py`,
   benches in `scripts/turn_detection/`): fake-Telnyx caller, no PSTN.
   **Replayed riff's real fragmented audio through our gateway: one clean
   turn** where riff produced three fragments.
4. Fixed en route: container name-filtering (rebuilds made deploys silent
   no-ops), 8k→16k upsampler, smart-turn v3.2 input format (published
   example is for an older version; v3.2 wants left-padded 8 s mels
   (1,80,800) and its own vendored feature code).

## Recommendations for riff (in order of evidence-backed value)

1. **Own your endpointing** — local energy/Silero VAD + the semantic tail
   check in `turn_router`, demoting Gemini's server VAD. This alone would
   have kept the worst observed call intact.
2. **Goal regions** (see design doc): free-form conversation inside FSM
   flows, bounded by typed extractors/budgets/escape hatches — kills the
   dead-air class where a region replaces a silent hub.
3. Silero VAD for barge-in (replaces the peak/sustain thresholds that both
   missed and double-fired).
4. Streaming/filler patterns for the 7–16 s content-fetch turns.
5. LiveKit adoption stays a transport-layer decision for later; nothing
   above depends on it.

## Waiting on you

- Test-call 512-356-9101: M3 runs the canary (dynamic endpointing +
  streaming + Emma→George British voice + medium Whisper). Trail off
  mid-sentence ("tell me about the…") and watch it wait for you.
- The reuse-check hook asked twice to vet the design doc against past
  portfolio work — needs your opt-in.
- Decide whether the riff-side changes (local endpointing, goal-region
  pilot) get scheduled; they belong in riff's own worktree/backlog flow.
