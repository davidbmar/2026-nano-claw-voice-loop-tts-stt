"""Per-call semantic event log backing the call review panel.

Persists the turn-by-turn record of a phone call — transcript text,
scheduler decisions, barge-ins, lifecycle — as append-only rows in the
shared metrics database. Follows the cost_ledger pattern: writers are
best-effort, never raise, and no-op when the connection is missing, so
a broken database can never take down a live call.

In-process subscribers observe the same event stream; this is the seam
a future live listen-in view attaches to. A failing subscriber never
affects the database write or the other subscribers.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("call_log")

# Per-call seq counters for calls currently in flight. Bounded so calls
# that never emit call_end (crashed sockets) cannot grow the dict forever.
MAX_TRACKED_CALLS = 256
_seq: dict[str, int] = {}

_subscribers: list[Callable[[dict], None]] = []

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  ts REAL NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_call_events_call ON call_events(call_id, seq);
"""


def ensure_schema(conn) -> bool:
    """Create ``call_events`` on *conn*; return success and never raise."""

    if conn is None:
        return False
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        return True
    except Exception:
        log.exception("call_events schema initialization failed")
        return False


def subscribe(fn: Callable[[dict], None]) -> None:
    """Register an in-process observer of every emitted event."""

    _subscribers.append(fn)


def unsubscribe(fn: Callable[[dict], None]) -> None:
    """Remove a previously registered observer; unknown observers are ignored."""

    try:
        _subscribers.remove(fn)
    except ValueError:
        pass


def _next_seq(call_id: str, kind: str) -> int:
    seq = _seq.get(call_id)
    if seq is None:
        seq = 0
        while len(_seq) >= MAX_TRACKED_CALLS:
            _seq.pop(next(iter(_seq)))
    if kind == "call_end":
        _seq.pop(call_id, None)
    else:
        _seq[call_id] = seq + 1
    return seq


def emit(
    conn,
    call_id: str,
    kind: str,
    payload: dict | None = None,
    *,
    ts: float | None = None,
) -> bool:
    """Append one event for *call_id*; never raises.

    Subscribers are notified even when the database write fails — a live
    view should keep working through a database outage.
    """

    if not isinstance(call_id, str) or not call_id:
        return False
    if not isinstance(kind, str) or not kind:
        return False

    event = {
        "call_id": call_id,
        "ts": float(ts) if ts is not None else time.time(),
        "seq": _next_seq(call_id, kind),
        "kind": kind,
        "payload": dict(payload or {}),
    }

    written = False
    if conn is not None:
        try:
            with conn:
                conn.execute(
                    "INSERT INTO call_events(call_id, ts, seq, kind, payload)"
                    " VALUES(?,?,?,?,?)",
                    (
                        event["call_id"],
                        event["ts"],
                        event["seq"],
                        event["kind"],
                        json.dumps(event["payload"]),
                    ),
                )
            written = True
        except Exception:
            log.exception("call event write failed for call %s", call_id[:16])

    for fn in list(_subscribers):
        try:
            fn(event)
        except Exception:
            log.exception("call event subscriber failed for call %s", call_id[:16])

    return written


def read_timeline(conn, call_id: str) -> list[dict]:
    """Return the ordered events for one call; database errors become ``[]``."""

    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT ts, seq, kind, payload FROM call_events"
            " WHERE call_id = ? ORDER BY seq, id",
            (call_id,),
        ).fetchall()
    except Exception:
        log.exception("call timeline read failed for call %s", call_id[:16])
        return []
    events = []
    for ts, seq, kind, payload in rows:
        try:
            parsed = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            parsed = {}
        events.append({"ts": ts, "seq": seq, "kind": kind, "payload": parsed})
    return events


def sweep(conn, tap_root: str | Path, older_than_days: float) -> None:
    """Drop call content older than the retention window; never raises.

    Deletes aged ``call_events`` rows and per-call tap directories. Call
    metadata (``phone_calls``) and cost receipts are deliberately kept —
    they are small and serve as the durable call record.
    """

    try:
        days = float(older_than_days)
    except (TypeError, ValueError):
        return
    if days <= 0:
        return
    cutoff = time.time() - days * 86400

    if conn is not None:
        try:
            with conn:
                conn.execute("DELETE FROM call_events WHERE ts < ?", (cutoff,))
        except Exception:
            log.exception("call_events retention sweep failed")

    try:
        root = Path(tap_root)
        subdirs = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    except Exception:
        log.exception("tap directory listing failed during retention sweep")
        return
    for d in subdirs:
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
        except Exception:
            log.exception("tap directory removal failed: %s", d)
