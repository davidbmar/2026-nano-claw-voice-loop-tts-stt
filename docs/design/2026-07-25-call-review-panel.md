# Call Review Panel — v1 design record (2026-07-25)

**Goal.** Let operators (and eventually customer businesses) retrace any
phone call: list → turn-by-turn timeline → audio playback. Agreed v1 scope
was review-first/post-call only; live listen-in and join/conference are
future work, deliberately unconstrained.

## Decisions

**Hybrid persistence.** Semantic timeline → SQLite (`call_events` in
metrics.db, own-table pattern copied from `cost_ledger`); bulky sensitive
artifacts (WAVs, perf timings) → the CallTap directory, now inside the
mounted data volume. The panel's list view needs SQL; audio needs files.
Both writers are best-effort and can never take a call down.

**Canonical URL id = `phone_calls.id`.** The raw Telnyx id (`v3:…`)
contains URL-hostile characters and the 24-char session form is a lossy
truncation with collision risk. The integer PK is stable and already
served by `/api/calls`; the raw `call_id` remains the join key across
`call_events`, `cost_ledger`, and tap directories. Calls that never hit
the webhook (loopback tests) get a fallback `phone_calls` row at media-WS
start (`INSERT OR IGNORE`, webhook row wins).

**Observer seam for live features.** `call_log.emit` fans out each event
to in-process subscribers after the DB write — and does so even when the
write fails, so a future live view survives a database outage. v1 ships
the seam empty; live listen-in later subscribes a WebSocket bridge here
plus an audio tee off `feed_media`/the send loop.

**Clock anchor.** `timings.jsonl` uses monotonic time. Each tap dir now
writes `meta.json` with a `{wall_t0, mono_t0}` pair, making the perf
timeline projectable onto wall-clock without changing the tap event
format.

**mixed.wav deferred.** `outbound.wav` contains only sent frames (silence
gaps are absent), so a correct two-party mix requires projecting sentences
onto the inbound timeline via `frames_sent` timings — real work with zero
v1 review value beyond the two per-leg players. Fast-follow.

**Recording posture.** Record everything, say so in the greeting
(`NANO_CLAW_PHONE_RECORD_NOTICE`, `off` opt-out for the spoken line only),
sweep content after `NANO_CLAW_CALL_RETENTION_DAYS` (default 30). This
consciously reverses the metrics-scrub precedent for phone calls only;
the browser transcript scrub stays.

**Auth.** Shared phone token, now also accepted via `X-NC-Phone-Token`
header so the panel never puts it in URLs. Multi-tenant scoping (business
key on `phone_calls`/`call_events`, Google-auth ownership) is future work
the schema does not preclude.

## Future work

- Live listen-in (subscriber WS + audio tee), join via Telnyx Conference
  rework of the answer flow.
- mixed.wav lazy server-side render.
- Cross-node merged panel view (today: per-node, like `/api/calls`).
- Per-business scoping + tenant auth.
