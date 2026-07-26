# /calls Panel: Provider Truth, Durable Phone Settings, Detailed Costs

## Context

Reviewing a live call, David suspected the phone line's TTS was secretly Google
("sounds like Gemini 3.1") despite the panel implying LuxTTS, and asked to fix the
attribution, do the same for STT, and improve the per-call cost breakdown. He also
observed the console at nano.chattychapters.com has phone settings the line
"doesn't seem to be taking."

Investigation established the ground truth:

- **Audio is genuinely local.** TTS: voice id → `voice_catalog` engine →
  `voice/tts.py:279-303` (luxtts→:8301, kokoro→:8300, piper in-process). STT: local
  faster-whisper (:8200, size header from `NANO_CLAW_PHONE_STT_SIZE=medium`). There is
  no cloud TTS/STT code in the repo.
- **But the words can be Gemini's.** The conversation LLM falls back
  `ollama/gemma4:e2b → gemini/gemini-flash-lite-latest → claude-haiku-4-5` on a 4s
  first-token timeout (`src/providers/index.ts:170-183,222-258`), and **nothing
  records which model actually served a turn** — `debug.model` in the SSE stream is
  the *requested* model (`src/api/server.ts:577,941`). David's ear-test was likely
  catching fallback turns; today that's unverifiable.
- **Settings amnesia is real.** Console phone settings (voice/model/speed/STT size/
  speech-prep via `/api/phone/config`, VAD via `/api/phone/vad`) live in in-memory
  maps (`voice/phone.py:184-190,304-318`) wiped on every container restart — and the
  watchdog restarted the stack twice today. `NANO_CLAW_PHONE_MODEL`/`SPEED` are also
  missing from `run.sh`'s env passthrough (:290-315), so they have no durable path.
- **Cost rows discard detail.** `cost_ledger` accumulates by (component, unit_kind,
  **model**) but drops model on write (`voice/cost_ledger.py:62-69,729-740`); the
  panel renders raw floats with no labels (`voice/web/calls.html:520-558`).

User decisions (locked): persist settings to the data volume; keep the LLM fallback
chain but surface per-turn attribution; per-component + per-model cost detail.

## Workstream A — Served-model attribution (TS + Python)

1. **`src/types.ts`** — add `model?: string` to `LLMResponse` (:165) and the `done`
   StreamEvent variant (:224-228).
2. **`src/providers/index.ts`** — in `complete()` (:196-224) return
   `{...response, model: m}` per attempt; in `completeStream()` (:232-260) wrap each
   attempt's generator, adding `model: m` to `done` events. (Keeps `fallback.ts` pure.)
3. **`src/api/server.ts`** — capture served model in the streaming loop (:925-928);
   `DebugInfo` (:65-118) gets `model` = served model and new `requestedModel?` set
   only when it differs; both build sites (:577, :941). `tool_pending` emitters reuse
   the same debug object and inherit it.
4. **`voice/phone.py` `_stream_reply`** (:872-1046) — capture `debug.model` /
   `debug.requestedModel` from `final`/`tool_pending` SSE events (and the non-stream
   JSON fallback :929-936); emit on the `assistant_turn` event (:1033-1043) as
   `model`, `modelRequested`, `modelFallback`. Scheduler branch (:836-852) adds
   `model` from the runner's `_model` attr (same chain cost_ledger trusts).
5. **`voice/phone.py` `call_start` payload** (:445-457) — add `sttSize`, `speed`,
   `model`, and catalog `engine` so every call is self-describing.

Note: `voice/cost_ledger.py:918-931` already reads `debug.get("model")` into the
ledger accumulation key — TS fix flows into cost attribution with no extra work.

## Workstream B — Durable phone settings

1. **`voice/phone.py`** near `_overrides` (:184):
   - `_SETTINGS_KEYS` = {VOICE, MODEL, SPEED, STT_SIZE, SPEECH_PREPARATION} + persist
     VAD choice (`NANO_CLAW_PHONE_VAD`) in the same file.
   - `_settings_path()` → `NANO_CLAW_PHONE_SETTINGS_PATH` env, default
     `/app/data/phone-settings.json` (env hook keeps tests hermetic).
   - Extract the existing POST validators (:1505-1540) into pure helpers shared by
     the loader: voice must pass `voice_catalog.lookup`, speed float in [0.5,2.0],
     stt_size in {tiny,base,small,medium}, VAD in VAD_MODES.
   - `_load_persisted_overrides()`: missing file → no-op; unknown keys ignored, bad
     values dropped (one warning each); any exception leaves `_overrides` untouched —
     a corrupt file can never brick call handling. Called from `register_phone_routes`
     right after the `phone_enabled()` gate.
   - `_persist_overrides()`: atomic write (tmp + `os.replace`), best-effort, filtered
     to known keys. Called write-through from `config_set_handler` (:1495-1547) and
     the VAD POST handler; a cleared model persists as absence. Update the stale
     "in-memory only" docstrings (:180-183, :1496-1498).
2. **`run.sh`** passthrough block (:290-310) — add `-e NANO_CLAW_PHONE_MODEL` and
   `-e NANO_CLAW_PHONE_SPEED` so .env can set factory defaults too. Precedence:
   persisted console overrides > .env > code default (already how `_cfg` works).

## Workstream C — Cost breakdown with details

1. **`voice/cost_ledger.py`**:
   - Schema: `model TEXT DEFAULT ''` in `_SCHEMA` (:48-59) + idempotent migration in
     `ensure_schema` (:72-83) via `PRAGMA table_info` guard, inside existing
     try/except (live-volume safe; old rows read back as `''` → "unattributed").
   - `LedgerEntry` (:62-69) gains `model`; `write_call` (:86-162) writes it;
     `finish_call` (:729-740) stops dropping the accumulation key's model.
   - Phone tracking: STT rows get `model="whisper/<stt_size>"` (:795-804); TTS rows
     get `model="<catalog engine>/<voice_id>"` e.g. `luxtts/lux_george` (:806-815).
     (Doc caveat: in-process piper *fallback* inside tts.py isn't distinguished.)
   - New `component_meta()` helper: pricing labels/colors/math per ledger component
     (reuse `_component_payloads` mapping :508-549), `{}` on failure.
2. **`voice/call_review.py`** `timeline_handler` (:126-146) — add
   `"costMeta": cost_ledger.component_meta()` to the payload.
3. **`voice/web/calls.html`**:
   - `renderEvent` (:435-482): `call_start` branch renders a compact **Providers**
     line (TTS engine/voice · STT whisper/size · VAD · codec · speed · model) instead
     of dropping those fields; agent bubbles get a model chip, styled distinctly with
     "(fallback)" when `modelFallback`.
   - `renderCost` (:520-558) rewrite: group by component (label + color swatch from
     costMeta) → per-model subrows; formatted units ("41.6s audio", "1,843 chars",
     "12.3k in / 800 out"); "unpriced" marker for null-rate rows instead of silent $0;
     note that telephony + infra bill the same connected minutes; total row kept.

## Tests (`.venv-test`, pytest.ini at root; `npm test` for TS)

- `test_cost_ledger.py`: model roundtrip; legacy 8-column DB migration (idempotent,
  old rows `model=''`); STT/TTS rows carry engine strings (monkeypatch `_cfg`);
  `_capture` attributes fallback model; `component_meta()` happy + bad-path.
- New `test_phone_settings_persistence.py`: POST writes file; fresh load restores
  exactly known keys; unknown/bad/corrupt handling; cleared model stays cleared.
- `test_phone_gateway.py`: point `NANO_CLAW_PHONE_SETTINGS_PATH` at tmp_path in the
  fixture that already clears `_overrides` so tests never touch /app/data.
- `test_call_review_api.py`: payload has `costMeta`; cost rows carry `model`.
- TS: assert `done` event / `LLMResponse` carry `model`; `debug.model` reflects the
  fallback winner; `requestedModel` present only on fallback.
- Known pre-existing flakes (not ours): `test_history_api.py::test_all_completion_...`,
  2 in `test_tts_sentence_pipeline.py`.

## Docs

- `docs/CALL-REVIEW.md`: API shape (+`costMeta`, cost `model`), event vocabulary
  (`call_start` + `assistant_turn` new keys).
- `docs/COSTS.md`: schema + migration note, model value conventions
  (`whisper/<size>`, `<engine>/<voice>`, LLM wire model), piper-fallback caveat.
- Both: phone settings persist at `/app/data/phone-settings.json`.

## Verification

1. `pytest tests/python -q` + `npm test` green (modulo known flakes).
2. Low-downtime rebuild + redeploy; then **mandatory**
   `.venv-test/bin/python scripts/phone_loopback_test.py "…"` → "FIRST ANSWER AUDIO".
3. In-container: `PRAGMA table_info(cost_ledger)` shows `model`; `/costs` still
   renders; old calls on `/calls` still open (pre-migration rows).
4. Settings: set voice/model/speed/VAD in console → check
   `/app/data/phone-settings.json` → restart container → `GET /api/phone/config`
   returns persisted values.
5. Real test call; on `/calls`: Providers line under call start; per-turn model chips
   (stop ollama briefly to force a fallback → "(fallback)" chip + correct ledger
   model row); cost card shows labels, per-model subrows, formatted units, the
   telephony/infra note, total consistent with `/costs`.

Branch: `call-review-panel`. Sequencing: C1 (schema) → A (TS→py) → B → C2-3 → tests
interleaved → docs.
