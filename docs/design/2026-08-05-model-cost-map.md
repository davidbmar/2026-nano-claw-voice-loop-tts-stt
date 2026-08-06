# Model and cost map, by repo

**As of 2026-08-05.** Companion to backlog `110` (spend forecasting) and
`cal-provider` `F-010` (calendar quota accounting).

Where possible this uses **measured spend from the live ledger**
(`/api/costs`, 708 KB of receipts in `nano-claw-voice`), not a workload model.
Modelled figures are labelled as such.

---

## The actual bill so far

71 calls · 95.0 minutes · 8 customers · 4 DIDs

| component | spend | per minute | observed? |
|---|---|---|---|
| **Telephony (Telnyx)** | **$0.6653** | $0.00700 | yes |
| Conversation LLM | $0.3667 | $0.00386 | yes |
| Infra (nodes + tunnel) | $0.1901 | $0.00200 | yes |
| TTS (Kokoro/Lux, local) | $0.0460 | $0.00048 | yes |
| STT (Whisper, local) | $0.0063 | $0.00007 | yes |
| Scheduler LLM | **$0.0000** | — | **no — never fired** |
| | | | |
| **variable** | **$1.2743** | | |
| **fixed** (4 DIDs × $1/mo) | **$4.0000** | | |
| **total** | **$5.2743** | $0.0555/min · $0.0743/call | |

### Three things this overturns

1. **Telephony is the largest variable cost** — $0.67 against the LLM's
   $0.37. Nearly 2:1. Every model-selection debate in this estate has been
   optimising the *second* biggest line.
2. **Fixed cost dominates at this volume.** $4.00 of DID rental against $1.27
   of usage — **76% of the bill is rent.** Model choice barely moves the total
   until call volume is far higher. The 4th DID costs more per month than all
   LLM usage to date.
3. **The scheduler LLM has never run in production.** `observed: false`,
   $0.0000. Every goal-region measurement to date — the 33-run bakeoff, the
   model rankings — describes a code path no real caller has yet exercised.

### Rates the ledger prices against

`referenceModel: haiku`. Per-million rates on file: `haiku` 1.00/5.00
(cache 0.10) · `grok` 2.00/6.00 · `qwen` 0.05/0.40 · `scout` 0.08/0.30 ·
`deepseek` 0.07/0.28 (cache 0.014) · `gptoss` 0.03/0.14.

---

## nano-claw — the voice phone line (512-277-7311)

| role | model | cost |
|---|---|---|
| **primary** | `ollama/gemma4:e2b` on the M5 | free |
| **hedge 1** (1200 ms) | `gemini/gemini-flash-lite-latest` → **3.5**-flash-lite | *(inside the $0.3667 above)* |
| **hedge 2** | `anthropic/claude-haiku-4-5` | never observed |
| STT | whisper `medium`, local | $0.0063 |
| TTS | `lux_george`, LuxTTS local | $0.0460 |
| telephony | Telnyx, 2 legs × $0.0035/min | $0.6653 + $4.00 DIDs |

**The free primary is misleading.** Measured 2026-08-05: e2b's 0.9–1.7s p50
straddles the 1200 ms hedge, so **~64% of turns were answered by the paid
fallback** while the console displayed the free local model. The Conversation
LLM line is what that actually costs.

`-latest` resolves to **3.5**-flash-lite ($0.30/$0.03/$2.50), not 2.5
($0.10/$0.01/$0.40) — 6.25× the output price, and it moved without a config
change. Pin it.

**Quality context:** e2b scores 5.00/11 on the goal-region eval; `gemma4:26b`
on the same M5 scores 10.33 for the same $0. The primary is simultaneously the
weakest model in use and the reason the hedge fires.

---

## intelligence-platform — deep reasoning

| role | model | cost |
|---|---|---|
| **primary** | `deepseek-v4-pro` | ~$0.007/request *(modelled)* |
| **fallback 1** | `local/gemma4:26b` (M5) | free |
| **fallback 2** | `openrouter/llama-4-scout` | ~$0.007/request *(modelled)* |

**Not in the ledger at all** — this runs in a separate service and reports
nothing. Backlog `110` adds a `DEEP_REASONING` component.

Do not apply the voice workload here: deep reasoning is low-volume and
high-token (`MAX_TOKENS=32768`, `max_evidence_chars=80_000`,
`thinking=enabled`, `effort=high`) — roughly 20k in / 16k out per request. At
100 requests/day that is **~$21/mo**, an order of magnitude above the entire
voice bill to date.

The two fallbacks were added 2026-08-05 as deliberately different failure
domains: local survives a DeepSeek outage *and* a site internet outage but not
the M5 sleeping; openrouter is the hardware-independent backstop. Falls through
only on timeout, network error, 429 or 5xx — any other 4xx still raises.

---

## cal-provider — calendar and booking FSM

| role | model / API | cost |
|---|---|---|
| **extraction** | `deepseek-v4-flash` | not yet running |
| calendar reads/writes | Google Calendar API | **quota, not billed** |
| calendar reads/writes | CalDAV | depends whose server |

The FSM is **model-agnostic by construction** — nothing in `src/` builds an
`ExtractionEnvelope`, so the model belongs to the caller and is swappable
without touching the FSM.

**Google quota is a ceiling, not a cost.** Exhausting it produces HTTP 429 and
a line that cannot book; a dollar forecast would report "plenty of credit"
right up to that moment. Hence `F-010` treats quota headroom as a separate axis.

---

## riff — flows, live line, STT

| role | model | cost |
|---|---|---|
| live conversation | `gemini-3.1-flash-live-preview` | **unmeasured** — realtime API, priced differently |
| goal-region supervisor | **`claude-haiku-4-5`** (`goal_region_runtime.py:44`) | $1.00/$5.00 per M |
| alignment STT | `whisper-small.en-mlx`, local | free (GPU) |
| choice STT | `whisper-medium.en-mlx`, local | free (GPU) |

**Goal-region default resolved.** `_DEFAULT_REGION_MODEL = "claude-haiku-4-5"`,
selected at `:998` as `config.model or SCHED_EVAL_MODEL or _DEFAULT`. Nothing
sets either override in riff's `.env`, so **the supervisor runs Haiku** — the
most expensive model in the estate at $1.00/$5.00 per million.

That is defensible on quality (11.00/11, the only perfect scorer) but it is
worth knowing it was reached by default rather than by decision. `gemma4:26b`
scores 10.33 for free on the M5.

**The live model remains the one real gap.** A realtime streaming API is not
priced per-token like the rest of this table, and nobody has measured it.

---

## Provider balances (live, 2026-08-05)

| provider | balance |
|---|---|
| DeepSeek | **$3.60** |
| OpenRouter | **$10.00** ($0.077 used) |
| Anthropic | no balance endpoint |
| Gemini | no balance endpoint |

**The two providers carrying real production spend are the two that expose no
balance.** The voice line's LLM cost is Gemini; riff's supervisor is Anthropic.
Backlog `110` cannot solve those with a balance poll and needs billing-side
data.

---

## Remaining gaps

- **riff's live model** — unmeasured, and realtime pricing differs in kind.
- **intelligence-platform reports nothing to the ledger** — the largest
  modelled cost in the estate is invisible to the only system that measures.
- **GPU contention is a real cost paid in latency, not dollars.** Whisper, TTS
  and the local LLM share hardware; the intelligence timeout was raised to
  2500 ms because retrieval was losing that race.
- **The scheduler LLM line is $0 because it has never run.** When cal-provider's
  FSM goes live it will move from zero to a real number, and no forecast should
  assume the current shape holds.
