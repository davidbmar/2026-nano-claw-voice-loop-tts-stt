# Voice latency & responsiveness — findings and plan

Date: 2026-07-25 · Status: **measured, deferred** (parked as backlog
proposals 082/083/084 — operator chose not to implement yet)

## The symptom

Callers say "hello", hear silence, and ask "are you there?" before the
assistant answers. This is real, not perceptual: measured on the live line.

## The evidence (`/api/metrics`, last 50 turns + all-time per-model aggregates)

End-to-end = end of caller speech → first assistant audio.

| Stage | Median | p90 | Max |
|---|---|---|---|
| STT (Whisper transcription) | 1,140 ms | 2,393 ms | 3,193 ms |
| LLM time-to-first-token | 2,486 ms | 9,287 ms | 14,368 ms |
| TTS first audio | 1,102 ms | 3,374 ms | 5,678 ms |
| **End-to-end** | **5,232 ms** | **13,506 ms** | **17,141 ms** |

Per-model TTFT, all recorded turns:

| Model | Turns | Avg TTFT | Avg e2e |
|---|---|---|---|
| deepseek/deepseek-v4-flash (**primary**) | 384 | **2,984 ms** | 5,529 ms |
| anthropic/claude-haiku-4-5 (fallback) | 107 | 843 ms | 3,678 ms |
| gemini/gemini-flash-lite-latest (fallback) | 44 | 586 ms | 3,763 ms |

Two structural findings:

1. **The primary model is the slowest configured model** — 4–5× the TTFT
   of either fallback. Every turn pays ~2.5 s for it.
2. **`fallbackTimeoutMs: 4000` stacks serially** — when the primary
   stalls, the caller waits the full 4 s *plus* the fallback's own
   latency. That is the 9–14 s TTFT tail.

Caveat: most sampled turns are the hourly synthetic health turns
(short prompts); real calls skew worse.

## Ranked plan

| # | Change | Effort | Expected effect | Backlog |
|---|---|---|---|---|
| 1 | Faster primary model (cloud swap, or local Gemma via Ollama — under evaluation) | config | median −2 s | 082 |
| 2 | `fallbackTimeoutMs` 4000 → ~1200–1500 | config | caps the tail | 082 |
| 3 | Instant ack: earcon on end-of-speech + optional ≥1.2 s micro-filler | small code | perceived dead-air → ~0 | 083 |
| 4 | Short-first-sentence bias in the spoken contract | one line | TTS starts sooner every turn | 083 |
| 5 | Hedged model racing (fallback fires at ~800 ms, first token wins) | medium | tail = fallback latency | 084 |
| 6 | STT outlier fix (0.4 s best vs 1.5–3 s common — bimodal) | medium | −0.5–1 s median | 084 |
| 7 | Connect-time warmup (prompt cache + connection pool + model warm) | medium | first-turn penalty → 0 | 084 |

## Related context

- Base-layer composition (2026-07-25) put the identity prefix in the
  cacheable region before `[[cache-breakpoint]]` — warmup (#7) makes the
  turn-1 cache write happen before the caller finishes speaking.
- Scheduler modes already greet instantly on activation; #3 extends that
  courtesy to every mode.
- Local-model option: this host is an M3 Pro/36 GB with Ollama and
  `gemma4:e2b` + `gemma4:26b` already pulled. Docker containers on macOS
  get **no Metal GPU**, so local inference must run natively on the host
  (or on a remote Mac) and be reached from the voice container via
  `host.docker.internal` — the same pattern the intelligence API uses.
