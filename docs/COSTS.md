# Voice cost ledger and console

The voice service exposes a live cost console at `/costs` and its data at
`GET /api/costs`. Usage is recorded best-effort in the same SQLite database as
`phone_calls`; telemetry failures never fail a call.

## Ledger schema

`voice.cost_ledger.ensure_schema()` creates this table:

```sql
CREATE TABLE IF NOT EXISTS cost_ledger (
  call_id TEXT,
  ts REAL,
  business TEXT,
  flow TEXT,
  component TEXT,
  units REAL,
  unit_kind TEXT,
  usd_per_unit_snapshot REAL
);
```

One completed call is written atomically as several rows. A repeated call-end
event is idempotent: if that `call_id` already has receipts, it is not inserted
again. USD is never stored as a call total. At read time each row contributes
`units × usd_per_unit_snapshot`, preserving both raw usage and the rate used at
call end.

Components use these units:

| Component | Unit kinds | Meaning |
| --- | --- | --- |
| `telephony` | `minutes` | Connected call minutes |
| `scheduler_llm` | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | Provider-reported scheduler usage |
| `conversation_llm` | the same token kinds | Provider-reported persona/conversation usage |
| `stt` | `audio_seconds` | Audio submitted to transcription |
| `tts` | `characters` | Text successfully submitted to synthesis |
| `infra` | `call_minutes` | Connected minutes for host amortization |

STT, TTS, and token counts are accumulated in memory during the call. The
ledger performs SQLite I/O once, when the media call closes. Telephony and
infrastructure minutes come from that call's monotonic elapsed time.

## Pricing updates

All rates and scenario assumptions live in `voice/pricing.json`; there are no
pricing constants in the ledger or API code. The file contains:

- an `as_of` date and realistic duty cycle;
- telephony USD per minute and USD per DID-month;
- model USD per one million input/output tokens, plus cache read/write rates
  where known;
- realistic and 24×7×30 monthly model scenarios used by the what-if table;
- STT, TTS, infrastructure, and display metadata for the component drill-down.

Use the **scheduler-model-costs skill** as the rate source of truth. To update
rates:

1. Refresh/check the model and carrier rates with that skill.
2. Edit `voice/pricing.json`, including `as_of` and any known cache rate.
3. Validate it with `python3 -m json.tool voice/pricing.json`.
4. Run `python3 -m pytest tests/python -q` and load `/costs` once with an empty
   database to confirm the model table still renders.

Models with `"fleet": false` supply call-end pricing for conversation models
but are intentionally omitted from the supervisor what-if table. The six fleet
rows mirror the cost-console design model array.

Pricing is loaded only when a receipt or report needs it. A missing or malformed
file returns a valid API response with `status: "pricing_unavailable"`; the
console shows that state instead of throwing. Calls continue normally. Rows
written while pricing is unavailable retain their units and have a null rate
snapshot, so the report marks those component totals as not fully priced.

## API shape

`GET /api/costs` returns the mockup's four direct inputs—`models`, `components`,
`businesses`, and `duty`—plus totals and privacy-safe customer detail:

```json
{
  "status": "ok | awaiting_call_data | pricing_unavailable",
  "message": null,
  "awaitingCallData": false,
  "pricing": {
    "available": true,
    "asOf": "2026-07-18",
    "didMonth": 1.0
  },
  "duty": 0.139,
  "referenceModel": "haiku",
  "totals": {
    "calls": 2,
    "customers": 1,
    "minutes": 6.0,
    "dids": 1,
    "variableUsd": 0.0566,
    "fixedUsd": 1.0,
    "usd": 1.0566,
    "costPerCall": 0.5283,
    "costPerMinute": 0.1761
  },
  "models": [
    {
      "id": "haiku",
      "name": "Claude Haiku 4.5",
      "provider": "Anthropic",
      "pin": 1.0,
      "pout": 5.0,
      "pcache": 0.1,
      "lat": "1.6–3.2 s",
      "score": "11/11 every run",
      "zero": true,
      "m24": 72.0,
      "mreal": 10.0
    }
  ],
  "components": [
    {
      "id": "telephony",
      "component": "telephony",
      "label": "Telephony (Telnyx)",
      "color": "#ff775f",
      "perMin": 0.007,
      "math": "…",
      "usd": 0.042,
      "fixedUsd": 1.0,
      "units": {"minutes": 6.0},
      "priced": true,
      "observed": true
    }
  ],
  "businesses": [
    {
      "name": "Acme",
      "line": "+15125550100",
      "calls": 2,
      "customers": 1,
      "callsPerCustomer": 2.0,
      "minMin": 2.0,
      "medMin": 3.0,
      "maxMin": 4.0,
      "totalMin": 6.0,
      "bookings": 0,
      "dids": 1,
      "variableUsd": 0.0566,
      "usd": 1.0566,
      "flows": [
        {"flow": "scheduler", "calls": 1, "min": 2.0, "share": 0.333333, "usd": 0.0206}
      ]
    }
  ],
  "customers": [
    {
      "hash": "cust_…",
      "business": "Acme",
      "calls": 2,
      "totalMin": 6.0,
      "minMin": 2.0,
      "medMin": 3.0,
      "maxMin": 4.0
    }
  ],
  "byComponent": {"telephony": {}},
  "byBusiness": []
}
```

`byComponent` and `byBusiness` are convenience aliases for non-console clients;
the console consumes the arrays directly. The realistic/24×7 toggle and model
selector are client-side arithmetic over these aggregates and do not mutate the
ledger.

With no receipts, the endpoint still loads pricing, returns all fleet models,
and reports `awaiting_call_data`. This is why a fresh deployment can render the
model what-if table before its first call.

## Caller privacy

The endpoint never serializes `phone_calls.caller`. It converts normalized
caller numbers to HMAC-SHA256 identifiers and returns only a `cust_…` prefix.
Set `NANO_CLAW_COST_HASH_SALT` to a stable deployment secret. If absent, the
phone webhook token is used; local development has a deterministic namespaced
fallback. Called DIDs may appear as the business `line`, but raw caller numbers
do not leave the server.
