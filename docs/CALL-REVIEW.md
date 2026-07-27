# Call Review Panel

`/calls` lists every phone call this node served and lets an operator
retrace one turn by turn — verbatim transcript, scheduler decisions,
barge-ins — and listen back to both sides of the line.

## Access

The panel and its API share the phone gateway token
(`NANO_CLAW_PHONE_TOKEN`). The page asks for it once and stores it in
`localStorage`; every request sends it as the `X-NC-Phone-Token` header so
it never appears in URLs or access logs. The legacy `?token=` query form
still works (Telnyx webhook/media URLs depend on it).

Each node keeps its own database, so `/calls` on the M3 shows M3-served
calls and `/calls` on the M1 shows failover calls. `scripts/call-log.sh`
remains the merged cross-node view.

## API

- `GET /api/calls` — recent call metadata (existing endpoint, snake_case).
- `GET /api/calls/{id}/timeline` — full retrace for one call, where `{id}`
  is the integer `id` from `/api/calls`. camelCase payload:
  `{call, events: [{ts, iso, seq, kind, payload}], cost, costMeta, audio}`.
  `cost` rows carry a `model` column (`whisper/<size>`, `<engine>/<voice>`,
  or the LLM wire model; `""` on rows written before the column existed) and
  `costMeta` maps each ledger component to its pricing label/color/math for
  display.
- `GET /api/calls/{id}/audio/{leg}` — recorded WAV; `leg` is `inbound`
  (caller), `outbound` (what the caller heard), or `tts` (48 kHz TTS
  source before phone-rate resampling).

The timeline payload also carries `timings`: the tap's `timings.jsonl`
events projected onto wall-clock via the `meta.json` anchor — rendered in
the panel as a collapsible "Call log" section (STT/synthesis/pacing/barge
diagnostics per call).

## Web UI sessions

Browser voice conversations on the console appear in the list too, tagged
**web UI** (source is inferred from the id prefix: `voice-*` = web,
`loopback-*` = loopback test, anything else = phone). Only metadata is
recorded for web sessions — start/end, turn count, signed-in flag —
because anonymous browser transcripts deliberately stay ephemeral;
signed-in users' transcripts live in their own conversation history.

## Console banner

The voice console shows a "Call the line" banner with the DID currently
pointed at this node, driven by `NANO_CLAW_PHONE_DISPLAY_NUMBER` via
`/api/phone/config` (`voice/web/phone-banner.js`). Keep it in sync with
the live Telnyx routing (`/phone-routing`); unset hides the banner.

Console-set phone settings (voice, model, speed, STT size, speech mode,
VAD) persist to `NANO_CLAW_PHONE_SETTINGS_PATH` (default
`/app/data/phone-settings.json` on the data volume) and reload at boot,
so they survive container restarts; `.env` remains the factory default
underneath.

## Event vocabulary (`call_events` table, `voice/call_log.py`)

| kind | payload |
|---|---|
| `call_start` | codec, vad, voice, engine (TTS catalog engine), sttSize, speed, model (null → server default chain), mode (`persona`/`scheduler`), flowDomain, sessionId |
| `user_turn` | text (verbatim caller ASR) |
| `assistant_turn` | text, mode (`persona`/`scheduler`/`greeting`/`idle`/`error`); persona adds complete + interrupted + model/modelRequested/modelFallback (the model that actually wrote the turn — differs from the request when the LLM fallback chain answered) + preparedStreamMismatch (present/true only when the server rewrote the reply after streamed sentences were already spoken — the tail was withheld); scheduler adds outcome, slots, rejected, supervisorMs, turnsUsed, maxTurns, eventId, done, model |
| `barge_in` | — |
| `call_end` | — |

Persona `assistant_turn.text` is accumulated at the point sentences are
handed to synthesis, so an interrupted reply records only what the caller
actually heard (`interrupted: true`).

Writers are best-effort (cost_ledger pattern): a broken database can never
take a call down. In-process subscribers (`call_log.subscribe`) observe the
same stream — the attachment point for a future live listen-in view.

## Recording posture

- **Recording is on by default.** `CallTap` writes per-call WAVs + timing
  JSONL to `NANO_CLAW_PHONE_TAP_DIR` (default `/app/data/phone-taps`,
  inside the `nano-claw-data` volume so recordings survive rebuilds).
  `NANO_CLAW_PHONE_TAP=0` disables audio capture. Each tap directory also
  gets a `meta.json` with a `wall_t0`/`mono_t0` clock-anchor pair that
  projects the monotonic `timings.jsonl` values onto wall-clock time.
- The greeting gains a spoken disclosure (default: *"This call may be
  recorded for quality and training."*). `NANO_CLAW_PHONE_RECORD_NOTICE`
  overrides the line; `off` keeps the plain greeting. The disclosure is
  appended at the single greeting site in `media_ws_handler`, so persona,
  scheduler, and per-domain flow greetings all get it without touching
  flow logic.
- **This deliberately reverses, for phone calls only, the earlier decision
  to never persist transcript text** (the browser `turns.asked_text` /
  `said_text` scrub in `metrics_db.py` remains in force). Review requires
  the verbatim record; the disclosure + retention sweep are the
  counterweights.

## Retention

A daily sweep (`NANO_CLAW_CALL_RETENTION_DAYS`, default 30; `0`/empty
disables) deletes `call_events` rows and tap directories older than the
window. Call **metadata is deliberately kept**: `phone_calls` rows (which
include caller numbers — retained PII) and `cost_ledger` receipts are
small and serve as the durable failover call record.

## Ops notes

- Tap files contain callers' voices; handle as sensitive data.
- Disk: roughly 10 MB per 5-minute call across the three WAVs; the sweep
  bounds total growth.
- Scheduler calls previously left no transcript at all; the
  `assistant_turn` scheduler payload is now the persisted decision trail
  (outcome, slots, rejected, supervisor latency).
- `scripts/phone_loopback_test.py` produces a fully reviewable call
  (metadata row, events, tap WAVs) without a PSTN call.
