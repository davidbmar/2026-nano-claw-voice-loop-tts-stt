"""Read-only HTTP API for the call review panel.

Serves the per-call timeline (semantic events + cost receipts + audio
availability) and the recorded audio legs. Calls are addressed by their
integer ``phone_calls.id`` — the raw Telnyx call id contains characters
that are hostile to URLs and the truncated session form is lossy — and
resolved back to the raw ``call_id`` join key internally.

Token-gated like the existing ``/api/calls`` log; the panel sends the
shared token via the ``X-NC-Phone-Token`` header so it never lands in
URLs. Read paths degrade to an ``error`` key rather than 5xx, matching
the phone gateway convention.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from voice import call_log, cost_ledger, phone
from voice.phone_tap import DEFAULT_TAP_ROOT

log = logging.getLogger("call_review")

# leg name in the URL → file inside the per-call tap directory
LEG_FILES = {
    "inbound": "inbound.wav",
    "outbound": "outbound.wav",
    "tts": "tts_48k.wav",
}


def _tap_root() -> Path:
    return Path(os.environ.get("NANO_CLAW_PHONE_TAP_DIR", DEFAULT_TAP_ROOT))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


# A 5-minute call emits a few hundred timing events; the cap only guards
# against pathological files.
MAX_TIMING_EVENTS = 2000


def _read_timings(tap_dir: Path) -> list[dict]:
    """Tap timings projected onto wall-clock via the meta.json anchor.

    Returns ``[]`` for calls without a tap or predating the anchor — the
    panel simply hides the log section then.
    """
    meta_path = tap_dir / "meta.json"
    timings_path = tap_dir / "timings.jsonl"
    if not meta_path.is_file() or not timings_path.is_file():
        return []
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        wall_t0 = float(meta["wall_t0"])
        mono_t0 = float(meta["mono_t0"])
        entries: list[dict] = []
        with timings_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                mono_t = record.get("t")
                if not isinstance(mono_t, (int, float)):
                    continue
                wall = wall_t0 + (float(mono_t) - mono_t0)
                entries.append({**record, "wall": wall, "iso": _iso(wall)})
        return entries[-MAX_TIMING_EVENTS:]
    except Exception:
        log.exception("tap timings read failed: %s", tap_dir)
        return []


def _resolve_call(conn, raw_pk: str) -> dict | None:
    """Map the URL's integer id to the phone_calls row, or ``None``."""
    try:
        pk = int(raw_pk)
    except (TypeError, ValueError):
        return None
    try:
        row = conn.execute(
            "SELECT * FROM phone_calls WHERE id = ?", (pk,)
        ).fetchone()
    except Exception:
        log.exception("call lookup failed for id %s", raw_pk[:16])
        return None
    return dict(row) if row is not None else None


async def timeline_handler(request: web.Request) -> web.Response:
    """Full retrace payload for one call: events, cost, audio flags."""
    if not phone._token_ok(request):
        return web.Response(status=403, text="bad token")
    conn = phone._metrics_conn
    if conn is None:
        return web.json_response({"error": "db unavailable"})
    call = _resolve_call(conn, request.match_info["call_pk"])
    if call is None:
        return web.json_response({"error": "unknown call"}, status=404)
    call_id = call["call_id"]
    tap_dir = _tap_root() / call_id
    events = [
        {
            "ts": event["ts"],
            "iso": _iso(event["ts"]),
            "seq": event["seq"],
            "kind": event["kind"],
            "payload": event["payload"],
        }
        for event in call_log.read_timeline(conn, call_id)
    ]
    return web.json_response(
        {
            "call": {
                "id": call["id"],
                "callId": call_id,
                "caller": call["caller"],
                "called": call["called"],
                "node": call["node"],
                "answeredAt": call["answered_at"],
                "endedAt": call["ended_at"],
                "turns": call["turns"],
            },
            "events": events,
            "cost": cost_ledger.read_entries(conn, call_id),
            "costMeta": cost_ledger.component_meta(),
            "audio": {
                leg: (tap_dir / filename).is_file()
                for leg, filename in LEG_FILES.items()
            },
            "timings": _read_timings(tap_dir),
        }
    )


async def audio_handler(request: web.Request) -> web.Response:
    """Stream one recorded audio leg (inbound/outbound/tts) as a WAV file."""
    if not phone._token_ok(request):
        return web.Response(status=403, text="bad token")
    conn = phone._metrics_conn
    if conn is None:
        return web.json_response({"error": "db unavailable"}, status=404)
    filename = LEG_FILES.get(request.match_info["leg"])
    if filename is None:
        return web.json_response({"error": "unknown leg"}, status=404)
    call = _resolve_call(conn, request.match_info["call_pk"])
    if call is None:
        return web.json_response({"error": "unknown call"}, status=404)
    path = _tap_root() / call["call_id"] / filename
    if not path.is_file():
        return web.json_response({"error": "audio unavailable"}, status=404)
    return web.FileResponse(path)


def register_call_review_routes(app: web.Application) -> None:
    """Attach the review API next to the phone gateway routes."""
    app.router.add_get("/api/calls/{call_pk}/timeline", timeline_handler)
    app.router.add_get("/api/calls/{call_pk}/audio/{leg}", audio_handler)
