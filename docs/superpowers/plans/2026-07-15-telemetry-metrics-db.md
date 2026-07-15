# Telemetry Metrics DB — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small SQLite DB that records one row per conversational turn — model/version/provider, date+time, asked/said transcripts, full per-stage latency (STT, LLM time-to-first-token + total, TTS, end-to-end), tokens + throughput, and est cost — plus `GET /api/metrics` aggregates.

**Architecture:** A pure `voice/metrics_db.py` (stdlib `sqlite3`, best-effort) owns the DB. The voice server times each stage (threading turn state through the session across `mic_stop` → spawned handler → `_consume_sse`) and writes one row per turn. nano-claw adds time-to-first-token to its `debug` object; the STT service returns its processing time.

**Tech Stack:** Python 3.12 (stdlib sqlite3), TypeScript, vanilla JS. pytest + vitest.

## Global Constraints

- **Best-effort telemetry** — every DB call swallows its own exceptions and logs; a DB failure NEVER breaks the voice loop. `record_turn(None, ...)` is a no-op.
- **Missing timings stored as NULL, not 0** (e.g. a non-streaming turn has no TTFT; a text turn has no STT) — so aggregates stay honest.
- **Canonical `llm_ttft_ms`** = voice-server observed (request-sent → first `delta`). nano-claw's provider-internal `firstTokenMs` also goes into `debug` (for the panel) but the stored TTFT is the voice-server number.
- **DB path** env `METRICS_DB_PATH`, default `/app/data/metrics.db`; persisted via a `nano-claw-data` volume.
- **Prices** are a maintained `prices` table seeded from the cost comparison; `est_cost` computed per turn; unpriced model → NULL cost. No live price fetching.
- Store **full** `asked_text`/`said_text`.

## File Structure
**New:** `voice/metrics_db.py`, `tests/python/test_metrics_db.py`.
**Modified (TS):** `src/api/server.ts` (+`firstTokenMs`), `tests/streaming.test.ts`.
**Modified (Python):** `stt-service/server.py` (+`processing_ms`), `voice/webrtc.py` (return `stt_ms`; session turn state), `voice/server.py` (timing + `record_turn` + `GET /api/metrics` + `init_db`).
**Modified (JS/infra):** `voice/web/app.js` (Debug-panel breakdown), `Dockerfile` + `run.sh` (data volume), `README.md`/`CHANGELOG.md`.

---

## Task 1: `metrics_db.py` (SQLite core, pure) + tests

**Files:** Create `voice/metrics_db.py`; Test `tests/python/test_metrics_db.py`.

**Interfaces:**
- Produces: `connect(path)`, `init_db(path) -> conn|None` (creates tables, seeds prices, idempotent), `estimate_cost(conn, model, tin, tout) -> float|None`, `record_turn(conn, rec: dict) -> None` (no-op if conn None; swallows errors), `recent(conn, limit=50) -> list[dict]`, `aggregates(conn) -> list[dict]`, `SEED_PRICES: dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_metrics_db.py`:

```python
import os, tempfile
from voice import metrics_db as m


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "t.db")


def test_init_seeds_prices_and_is_idempotent():
    p = _tmp()
    c1 = m.init_db(p); assert c1 is not None
    n1 = c1.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert n1 == len(m.SEED_PRICES)
    c2 = m.init_db(p)  # again — must not duplicate
    n2 = c2.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert n2 == len(m.SEED_PRICES)


def test_estimate_cost_math_and_unpriced():
    c = m.init_db(_tmp())
    # gemini flash-lite 0.10 in / 0.40 out per 1M
    cost = m.estimate_cost(c, "gemini/gemini-flash-lite-latest", 1_000_000, 1_000_000)
    assert abs(cost - (0.10 + 0.40)) < 1e-9
    assert m.estimate_cost(c, "no/such-model", 100, 100) is None


def test_record_and_recent_and_aggregates():
    c = m.init_db(_tmp())
    m.record_turn(c, {"ts": "2026-07-15T10:00:00", "model": "gemini/gemini-flash-lite-latest",
                      "provider": "gemini", "llm_ttft_ms": 300, "tok_per_sec": 50.0,
                      "e2e_ms": 600, "tokens_in": 4800, "tokens_out": 7200, "est_cost_usd": 0.003,
                      "asked_text": "hi", "said_text": "hello"})
    m.record_turn(c, {"ts": "2026-07-15T10:01:00", "model": "gemini/gemini-flash-lite-latest",
                      "provider": "gemini", "llm_ttft_ms": 500, "tok_per_sec": 40.0, "e2e_ms": 800})
    r = m.recent(c)
    assert len(r) == 2 and r[0]["said_text"] in ("hello", None)
    agg = m.aggregates(c)
    row = next(a for a in agg if a["model"] == "gemini/gemini-flash-lite-latest")
    assert row["n"] == 2
    assert abs(row["avg_ttft_ms"] - 400) < 1e-6  # (300+500)/2


def test_record_turn_is_best_effort():
    m.record_turn(None, {"model": "x"})  # no conn → no-op, no raise
    c = m.init_db(_tmp())
    m.record_turn(c, {"unknown_column": 1})  # bad rec → swallowed, no raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_metrics_db.py -v`
Expected: FAIL — `voice/metrics_db.py` missing.

- [ ] **Step 3: Create `voice/metrics_db.py`**

```python
"""Lightweight SQLite telemetry — one row per conversational turn.

Best-effort: every public function swallows its own errors so telemetry can
never break the voice loop.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

log = logging.getLogger("metrics-db")

DB_PATH = os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db")

# model id -> (input $/1M, output $/1M); seeded into the `prices` table.
SEED_PRICES = {
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "anthropic/claude-sonnet-4-5": (3.00, 15.00),
    "gemini/gemini-flash-lite-latest": (0.10, 0.40),
    "gemini/gemini-flash-latest": (0.30, 2.50),
    "deepseek/deepseek-chat": (0.28, 1.10),
    "groq/llama-3.3-70b-versatile": (0.59, 0.79),
    "dashscope/qwen-plus": (0.40, 1.20),
    "openai/gpt-4o-mini": (0.15, 0.60),
}

_COLUMNS = [
    "ts", "session_id", "provider", "model", "model_version", "stt_size", "voice_id",
    "asked_text", "said_text", "stt_ms", "llm_ttft_ms", "llm_total_ms",
    "tokens_in", "tokens_out", "tok_per_sec", "tts_ms", "e2e_ms", "est_cost_usd",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, session_id TEXT,
  provider TEXT, model TEXT, model_version TEXT, stt_size TEXT, voice_id TEXT,
  asked_text TEXT, said_text TEXT,
  stt_ms INTEGER, llm_ttft_ms INTEGER, llm_total_ms INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, tok_per_sec REAL,
  tts_ms INTEGER, e2e_ms INTEGER, est_cost_usd REAL
);
CREATE TABLE IF NOT EXISTS prices (
  model TEXT PRIMARY KEY, input_per_1m REAL, output_per_1m REAL, updated_at TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | None = None) -> sqlite3.Connection | None:
    """Create tables + seed prices. Returns a connection, or None on failure."""
    try:
        p = path or DB_PATH
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = connect(p)
        conn.executescript(_SCHEMA)
        now = datetime.now().isoformat(timespec="seconds")
        for model, (i, o) in SEED_PRICES.items():
            conn.execute(
                "INSERT OR IGNORE INTO prices(model,input_per_1m,output_per_1m,updated_at) VALUES(?,?,?,?)",
                (model, i, o, now),
            )
        conn.commit()
        return conn
    except Exception:
        log.exception("metrics init_db failed")
        return None


def estimate_cost(conn, model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    try:
        row = conn.execute(
            "SELECT input_per_1m, output_per_1m FROM prices WHERE model=?", (model,)
        ).fetchone()
        if not row:
            return None
        return round((tokens_in or 0) / 1e6 * row["input_per_1m"]
                     + (tokens_out or 0) / 1e6 * row["output_per_1m"], 8)
    except Exception:
        return None


def record_turn(conn, rec: dict) -> None:
    """Insert one turn row. No-op if conn is None; never raises."""
    if conn is None:
        return
    try:
        vals = [rec.get(c) for c in _COLUMNS]
        conn.execute(
            f"INSERT INTO turns({','.join(_COLUMNS)}) VALUES({','.join('?' * len(_COLUMNS))})",
            vals,
        )
        conn.commit()
    except Exception:
        log.exception("metrics record_turn failed")


def recent(conn, limit: int = 50) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def aggregates(conn) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT model, COUNT(*) AS n,
                      AVG(llm_ttft_ms) AS avg_ttft_ms, AVG(tok_per_sec) AS avg_tok_per_sec,
                      AVG(e2e_ms) AS avg_e2e_ms, AVG(est_cost_usd) AS avg_cost_usd
               FROM turns GROUP BY model ORDER BY n DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_metrics_db.py -v` → PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add voice/metrics_db.py tests/python/test_metrics_db.py
git commit -m "feat(metrics): SQLite telemetry module (turns + prices, best-effort)"
```

---

## Task 2: nano-claw TTFT in the debug object (TS)

**Files:** Modify `src/api/server.ts`; Test `tests/streaming.test.ts`.

**Interfaces:** `DebugInfo` gains `firstTokenMs?: number`; `stepLoopStream` sets it to the offset of the first `text` event from the iteration start.

- [ ] **Step 1: Write the failing test**

Add to `tests/streaming.test.ts` (uses the existing `__setProviderManagerForTest` + a stream with a delay before the first text):

```ts
describe('stepLoopStream TTFT', () => {
  it('reports firstTokenMs on the final debug', async () => {
    __setProviderManagerForTest({
      async *completeStream() {
        await new Promise((r) => setTimeout(r, 20));
        yield { type: 'text', delta: 'Hello.' };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
      },
    } as any);
    const mem = new Memory('ttft-test');
    mem.addMessage({ role: 'user', content: 'hi' });
    let finalEvt: any;
    for await (const e of stepLoopStream(mem, { model: 'anthropic/x', temperature: 0.7, maxTokens: 100 } as any, 0)) {
      if ((e as any).type === 'final') finalEvt = e;
    }
    expect(finalEvt.debug.firstTokenMs).toBeGreaterThanOrEqual(15);
    expect(finalEvt.debug.firstTokenMs).toBeLessThanOrEqual(finalEvt.debug.durationMs);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/streaming.test.ts` → FAIL (`firstTokenMs` undefined).

- [ ] **Step 3: Capture first-token time in `stepLoopStream`**

In `src/api/server.ts`, add `firstTokenMs?: number;` to the `DebugInfo` interface. In `stepLoopStream`, track the first text event: before the `for await` add `let firstTokenAt: number | undefined;`, and in the `ev.type === 'text'` branch add `if (firstTokenAt === undefined) firstTokenAt = Date.now();`. Then in the `debug` object add:

```ts
      firstTokenMs: firstTokenAt !== undefined ? firstTokenAt - startTime : undefined,
```

(`startTime` is already captured at the top of the loop iteration.)

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run tests/streaming.test.ts` → PASS. `npm run build` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/api/server.ts tests/streaming.test.ts
git commit -m "feat(api): report LLM time-to-first-token (firstTokenMs) in debug"
```

---

## Task 3: STT service returns processing time (Python)

**Files:** Modify `stt-service/server.py`; Test `tests/python/test_stt_size.py` (extend).

- [ ] **Step 1: Add the failing assertion**

In `tests/python/test_stt_size.py` add (near the top, after the guarded import):

```python
def test_transcribe_returns_processing_ms_key():
    # The response contract must include processing_ms (pure structural check
    # on the module — the empty-body branch returns it too).
    import inspect
    src = inspect.getsource(stt.transcribe)
    assert "processing_ms" in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `stt-service/.venv/bin/python -m pytest tests/python/test_stt_size.py -v`
Expected: FAIL — `processing_ms` not in the handler.

- [ ] **Step 3: Return `processing_ms` from `/transcribe`**

In `stt-service/server.py::transcribe`, it already computes `elapsed = time.time() - start`. Add `processing_ms` to BOTH return paths:
- the empty-audio early return: `return {"text": "", "duration_s": 0.0, "processing_ms": 0}`
- the success return: `return {"text": text, "duration_s": round(duration_s, 2), "processing_ms": int(elapsed * 1000)}`

- [ ] **Step 4: Run to verify it passes**

Run: `stt-service/.venv/bin/python -m pytest tests/python/test_stt_size.py -v` → PASS.
Run: `python3 -m py_compile stt-service/server.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add stt-service/server.py tests/python/test_stt_size.py
git commit -m "feat(stt): return processing_ms in the transcribe response"
```

---

## Task 4: Voice server timing + record_turn wiring (Python)

**Files:** Modify `voice/webrtc.py`, `voice/server.py`.

**Interfaces:**
- Consumes: `metrics_db` (Task 1).
- Produces: `stop_recording` returns `(text, audio_s, stt_ms)`; the voice server threads per-turn timing via session attrs and writes one `turns` row on the `final` event.

- [ ] **Step 1: `stop_recording` returns `stt_ms` (voice/webrtc.py)**

Change `stop_recording` to read the STT response's `processing_ms` and return it. Update its signature/return to `-> tuple[str, float, int | None]`: capture `stt_ms = result.get("processing_ms")`, and `return text, audio_duration_s, stt_ms` (return `"", 0.0, None` on the no-frames / error paths). Add turn-state attrs to `Session.__init__`: `self._turn: dict = {}`.

- [ ] **Step 2: Init the DB + capture turn start (voice/server.py)**

Near the top imports: `from voice import metrics_db`. Add a module-level `METRICS = metrics_db.init_db()` (best-effort; may be None). Add a monotonic import: `import time`.

In `websocket_handler`, the `mic_stop` branch currently does `text, duration = await session.stop_recording()`. Change to capture timing + turn context:

```python
                t0 = time.monotonic()
                text, duration, stt_ms = await session.stop_recording()
                if not text:
                    await ws.send_json({"type": "transcription", "text": ""})
                    continue
                session._turn = {"t0": t0, "asked": text, "stt_ms": stt_ms,
                                 "stt_size": session.stt_size, "voice_id": session.voice_id,
                                 "model": session.model}
                await ws.send_json({"type": "transcription", "text": text})
                _spawn_agent(_handle_agent_request(ws, session, http_client, text))
```

For the `text_message` branch, set `session._turn = {"t0": time.monotonic(), "asked": text, "stt_ms": None, "stt_size": session.stt_size, "voice_id": session.voice_id, "model": session.model}` before spawning.

- [ ] **Step 3: Measure LLM/TTS/e2e in `_consume_sse` + write the row (voice/server.py)**

In `_consume_sse`, capture timestamps and accumulate the reply text, then write on `final`:

- At the top (after `session.set_stream_task(...)`): `req_start = time.monotonic(); first_delta = None; first_audio = None; said_parts = []`.
- In `speak_chunk(chunk)`: record first-audio time on the first call — `nonlocal first_audio` and `if first_audio is None: first_audio = time.monotonic()`; also append to `said_parts`.
- In the `delta` branch: on the first delta, `if first_delta is None: first_delta = time.monotonic()`.
- In the `final` branch (where `obj` has the debug): assemble and write:

```python
                elif ev == "final":
                    tail = chunker.flush()
                    if tail:
                        said_parts.append(tail)
                        await speak_chunk(tail)
                    debug = obj.get("debug") or {}
                    if debug:
                        await ws.send_json({"type": "debug", **debug})
                    _write_turn_metrics(session, req_start, first_delta, first_audio, said_parts, debug)
                    await ws.send_json({"type": "agent_reply_done"})
```

Add the helper (module level), best-effort:

```python
def _ms(a, b):
    return int((b - a) * 1000) if (a is not None and b is not None) else None


def _write_turn_metrics(session, req_start, first_delta, first_audio, said_parts, debug):
    try:
        turn = getattr(session, "_turn", {}) or {}
        t0 = turn.get("t0", req_start)
        tokens_in = (debug.get("tokenUsage") or {}).get("prompt")
        tokens_out = (debug.get("tokenUsage") or {}).get("completion")
        total_ms = debug.get("durationMs")
        gen_ms = None
        if total_ms is not None and debug.get("firstTokenMs") is not None:
            gen_ms = max(1, total_ms - debug["firstTokenMs"])
        tok_per_sec = round(tokens_out / (gen_ms / 1000), 2) if (tokens_out and gen_ms) else None
        model = turn.get("model") or debug.get("model") or ""
        provider = model.split("/")[0] if "/" in model else None
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_id": SESSION_ID, "provider": provider, "model": model,
            "model_version": debug.get("model"),
            "stt_size": turn.get("stt_size"), "voice_id": turn.get("voice_id"),
            "asked_text": turn.get("asked"), "said_text": " ".join(said_parts).strip() or None,
            "stt_ms": turn.get("stt_ms"),
            "llm_ttft_ms": _ms(req_start, first_delta),
            "llm_total_ms": total_ms,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "tok_per_sec": tok_per_sec,
            "tts_ms": _ms(first_delta, first_audio),
            "e2e_ms": _ms(t0, first_audio),
            "est_cost_usd": metrics_db.estimate_cost(METRICS, model, tokens_in, tokens_out) if METRICS else None,
        }
        metrics_db.record_turn(METRICS, rec)
    except Exception:
        log.exception("metrics: failed to assemble turn record")
```

Add `from datetime import datetime` to the imports.

- [ ] **Step 4: Verify**

Run: `python3 -m py_compile voice/webrtc.py voice/server.py` → clean.
Run: `.venv-test/bin/pytest tests/python -q` → green (metrics_db test passes; server/webrtc not imported).

- [ ] **Step 5: Commit**

```bash
git add voice/webrtc.py voice/server.py
git commit -m "feat(voice): time each stage and write one telemetry row per turn"
```

---

## Task 5: `GET /api/metrics` + Debug-panel breakdown

**Files:** Modify `voice/server.py`, `voice/web/app.js`.

- [ ] **Step 1: `GET /api/metrics` (voice/server.py)**

Add a handler + route (before the `/{filename}` catch-all):

```python
async def metrics_handler(request: web.Request) -> web.Response:
    if METRICS is None:
        return web.json_response({"recent": [], "byModel": []})
    return web.json_response({
        "recent": metrics_db.recent(METRICS, 50),
        "byModel": metrics_db.aggregates(METRICS),
    })
# create_app: app.router.add_get("/api/metrics", metrics_handler)  (before /{filename})
```

- [ ] **Step 2: Debug-panel breakdown (voice/web/app.js)**

In `addDebugEntry(info)`, when the metric fields are present, render the per-stage line. Append to the entry text (after the existing fields):

```javascript
    if (info.firstTokenMs !== undefined || info.durationMs !== undefined) {
        var ttft = info.firstTokenMs !== undefined ? info.firstTokenMs + "ms" : "–";
        parts.push("TTFT " + ttft + " · total " + (info.durationMs || "–") + "ms");
    }
```

(Match the existing `addDebugEntry` construction — read it first; the point is to surface `firstTokenMs`/`durationMs` in the live panel. The full per-stage row lives in the DB / `/api/metrics`; the panel shows the LLM TTFT/total the API already sends.)

- [ ] **Step 3: Verify**

Run: `python3 -m py_compile voice/server.py` → clean. `node --check voice/web/app.js` → clean.

- [ ] **Step 4: Commit**

```bash
git add voice/server.py voice/web/app.js
git commit -m "feat(metrics): GET /api/metrics aggregates + live TTFT in the Debug panel"
```

---

## Task 6: Data volume + docs + integration verification

**Files:** Modify `Dockerfile`, `run.sh`, `README.md`, `CHANGELOG.md`.

- [ ] **Step 1: Persist the DB (Dockerfile + run.sh)**

- `Dockerfile`: `RUN mkdir -p /app/data` (near the other mkdir).
- `run.sh`: add `-v nano-claw-data:/app/data` to the `docker run` invocation (next to the models volume).

- [ ] **Step 2: Docs**

- `README.md`: a "Metrics" section — every turn is logged to a local SQLite DB (`/app/data/metrics.db`, persisted in the `nano-claw-data` volume) with model, timings (STT, LLM TTFT/total, TTS, end-to-end), tokens, and est cost; `GET /api/metrics` for per-model averages; inspect with `sqlite3`.
- `CHANGELOG.md` `### Added`: telemetry DB + time-to-first-token.

- [ ] **Step 3: Integration verification (controller-run)**

Rebuild with the data volume, run a couple of real turns, then inspect:

```bash
npm run build && docker build -t nano-claw-voice . && docker rm -f nano-claw-voice 2>/dev/null
set -a; source .env; set +a
docker run -d --rm --name nano-claw-voice -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" $( [ -n "$GEMINI_API_KEY" ] && echo -e "-e GEMINI_API_KEY=$GEMINI_API_KEY" ) \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" -e TTS_SERVICE_URL="http://host.docker.internal:8300" \
  -v nano-claw-models:/app/voice/models -v nano-claw-data:/app/data nano-claw-voice
# drive a streamed turn (writes a row via the WS path in a browser; or check the endpoint)
curl -s localhost:9090/api/metrics | python3 -m json.tool | head -30
# inspect the DB directly
docker exec nano-claw-voice sh -c 'command -v sqlite3 >/dev/null && sqlite3 /app/data/metrics.db "SELECT model, llm_ttft_ms, e2e_ms, est_cost_usd FROM turns ORDER BY id DESC LIMIT 5;" || echo "(sqlite3 CLI not in image; use /api/metrics)"'
```

Expected: after a real spoken/typed turn in the browser, `/api/metrics` shows the turn with a non-null `llm_ttft_ms` and a per-model aggregate; the row persists across a container restart (same volume).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile run.sh README.md CHANGELOG.md
git commit -m "chore: persist metrics.db volume; document telemetry"
```

---

## Self-Review (completed during authoring)

**Spec coverage:** SQLite module + prices + est_cost (T1) · TTFT capture (T2) · STT processing_ms (T3) · per-stage timing + record_turn threaded through the session (T4) · /api/metrics + panel (T5) · volume + docs + integration (T6). ✓
**Placeholder scan:** every code step has full code / exact edits; test steps have assertions + commands. ✓
**Type consistency:** `record_turn`/`estimate_cost`/`init_db`/`recent`/`aggregates` signatures consistent across T1/T4/T5; `_COLUMNS` matches the record dict keys built in `_write_turn_metrics`; `firstTokenMs` written by T2, read by T4/T5; `stop_recording` new 3-tuple return consumed in T4. ✓

## Out of scope
- Live price fetching; a metrics dashboard UI; response-quality scoring.
