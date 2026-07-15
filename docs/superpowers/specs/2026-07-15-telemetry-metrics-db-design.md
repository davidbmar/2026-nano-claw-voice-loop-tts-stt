# Telemetry Metrics DB — Design

**Date:** 2026-07-15
**Status:** Approved (design), pending implementation plan
**Builds on:** the pipeline-settings feature (model switching) already on `main`.

## Problem

We can now switch STT size, LLM model, and TTS voice — but we have no data on how each choice actually performs. Today there's only a transient `debug` object per reply (`durationMs` = total generation time, `tokenUsage`, model, finishReason) shown live in the browser Debug panel. There is **no time-to-first-token**, no per-stage latency (STT / TTS), and nothing is persisted. We can't answer "which model/config is fastest and cheapest for my real traffic?"

## Goal

A **small local SQLite database** that records one row per conversational turn with everything about it — model/version/provider, date+time, the transcripts (what was asked / what was said), full per-stage latency (STT, **LLM time-to-first-token** + total, TTS, end-to-end), tokens + throughput, and estimated cost from a maintainable prices table — plus a way to query it. So we accumulate real data and improve the system.

## Scope decisions (agreed)

| Decision | Choice |
| --- | --- |
| Metric scope | **Full pipeline breakdown** (STT + LLM TTFT/total + TTS + end-to-end) |
| Storage | **SQLite**, embedded, one file on a persisted volume; the **voice server owns writes** (it sees the whole turn) |
| Transcripts | Store **full** `asked_text` / `said_text` ("all the info") |
| Prices | A **maintainable `prices` table** seeded from the cost comparison; `est_cost` computed per turn. No live auto-fetch (no reliable pricing API) — prices are updated via a small script/endpoint |
| Query | `GET /api/metrics` (recent turns + per-model aggregates) + direct `sqlite3`; live per-stage breakdown in the Debug panel |
| Safety | DB writes are **best-effort** — a DB failure logs and is swallowed, never breaks the voice loop |

## Architecture

```
one turn:
  mic stop ─┬─ STT (:8200)  → returns text + processing_ms
            │
            ├─ voice server times each stage:
            │     t0 = mic stop
            │     stt_ms      (STT round-trip / processing)
            │     llm_ttft_ms (POST /api/chat → first `delta` frame)
            │     llm_total_ms, tokens_in/out, tok_per_sec  (from API `final` debug)
            │     tts_ms      (first chunk enqueued − first delta)
            │     e2e_ms      (first audio out − mic stop)
            │
            └─ record_turn(...) → SQLite  (metrics.db)  ── best effort
                                     │
                 GET /api/metrics ──┘  (recent + per-model averages)
```

New TTFT capture in nano-claw (`stepLoopStream`) also enriches the `debug` object (for the Debug panel), but the **canonical `llm_ttft_ms`** stored is what the voice server observes (request-sent → first delta) — the user-facing number.

## Components

### `voice/metrics_db.py` (new — the core)

- `init_db(path)` — opens/creates the SQLite file, creates tables if absent, seeds `prices`. Idempotent.
- Schema:
  ```sql
  CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                -- ISO 8601 local datetime
    session_id TEXT,
    provider TEXT, model TEXT, model_version TEXT,
    stt_size TEXT, voice_id TEXT,
    asked_text TEXT, said_text TEXT,
    stt_ms INTEGER, llm_ttft_ms INTEGER, llm_total_ms INTEGER,
    tokens_in INTEGER, tokens_out INTEGER, tok_per_sec REAL,
    tts_ms INTEGER, e2e_ms INTEGER,
    est_cost_usd REAL
  );
  CREATE TABLE IF NOT EXISTS prices (
    model TEXT PRIMARY KEY,
    input_per_1m REAL, output_per_1m REAL, updated_at TEXT
  );
  ```
- `record_turn(conn, record: dict) -> None` — inserts one row; wrapped so callers never see an exception.
- `estimate_cost(conn, model, tokens_in, tokens_out) -> float | None` — looks up `prices`, returns `(in/1e6*input_per_1m)+(out/1e6*output_per_1m)`, or `None` if the model isn't priced.
- `recent(conn, limit=50)` and `aggregates(conn)` — the query helpers: per-model `count, avg_ttft_ms, avg_tok_per_sec, avg_e2e_ms, avg_cost_usd`.
- `SEED_PRICES` — the model→(input,output) map from the cost table (Gemini Flash-Lite 0.10/0.40, GPT-4o mini 0.15/0.60, Groq 0.59/0.79, DeepSeek 0.28/1.10, Qwen 0.40/1.20, Gemini Flash 0.30/2.50, Haiku 1.00/5.00, Sonnet 3.00/15.00).
- Uses stdlib `sqlite3` + `datetime`. `DB_PATH` env `METRICS_DB_PATH` (default `/app/data/metrics.db`).

### nano-claw API (TypeScript) — TTFT in the debug object

- `DebugInfo` gains `firstTokenMs?: number`. In `stepLoopStream`, record `Date.now()` at the first `text` event and set `firstTokenMs = thatTime - startTime`; keep `durationMs` as the full time. (The provider-internal TTFT; complements the voice-server-observed TTFT.)
- No other API change — the `final` event already carries `debug` to the voice server.

### STT service (Python) — return processing time

- `/transcribe` adds `processing_ms` to its JSON response (it already computes `elapsed`); the `{text, duration_s}` shape is preserved, just extended.

### Voice server (`voice/server.py`, `voice/webrtc.py`) — timing + write

- Time each stage using a monotonic clock: `stop_recording` returns `(text, audio_s, stt_ms)` (reads the STT `processing_ms`); `_consume_sse` records the timestamp of the first `delta` (→ `llm_ttft_ms` from the request-sent time) and of the first enqueued audio chunk (→ `tts_ms`, `e2e_ms`); the `final` debug supplies `llm_total_ms`, tokens, `provider`/`model`/`model_version`.
- Assemble the record and call `metrics_db.record_turn(...)` (best-effort) at turn end. `est_cost_usd` via `estimate_cost`.
- `GET /api/metrics` handler → `{recent: recent(...), byModel: aggregates(...)}`.
- Init the DB once at startup (`init_db`).

### Docker / run.sh

- Add a persisted volume for the DB: `-v nano-claw-data:/app/data` (DB at `/app/data/metrics.db`), and create `/app/data` in the image. `run.sh` mounts the same volume.

### Browser (`voice/web/app.js`) — live breakdown (secondary)

- Extend the Debug panel entry to show the per-stage breakdown when present: `STT · LLM TTFT/total · TTS · e2e · $cost`. Uses the same `debug`/metrics the server already sends; small additive change.

## Error handling

- All DB operations are wrapped: a failed `init_db`/`record_turn` logs a warning and returns; the voice loop continues unaffected. Telemetry never degrades the conversation.
- Missing timings (e.g. non-streaming fallback has no TTFT) are stored as `NULL`, not zero, so aggregates stay honest.
- Unpriced models → `est_cost_usd = NULL`.

## Testing

- **Unit (Python):** `metrics_db` against a temp/in-memory DB — `init_db` idempotent + seeds prices; `record_turn` inserts and round-trips; `estimate_cost` math (and `None` for unpriced); `aggregates` computes per-model averages correctly; a `record_turn` that raises internally is swallowed.
- **Unit (TS):** `stepLoopStream` sets `firstTokenMs` to the first-text-event offset (fake provider stream with a delay between deltas).
- **Integration:** a real streamed turn writes one `turns` row with sane, non-null STT/LLM/TTS/e2e values and a computed cost; `GET /api/metrics` returns it and a per-model aggregate; DB persists across a container restart (volume).

## Out of scope

- Auto-fetching live prices from provider websites (fragile; prices are a maintained table).
- A metrics dashboard UI (the Debug-panel breakdown + `/api/metrics` JSON + `sqlite3` queries are enough for now).
- Quality/accuracy scoring of responses (latency + cost only).
