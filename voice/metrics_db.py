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
    "stt_ms", "llm_ttft_ms", "llm_total_ms", "tokens_in", "tokens_out",
    "tok_per_sec", "tts_ms", "e2e_ms", "est_cost_usd",
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
CREATE TABLE IF NOT EXISTS phone_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT UNIQUE,
  caller TEXT, called TEXT, node TEXT,
  answered_at TEXT, ended_at TEXT,
  turns INTEGER DEFAULT 0
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
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version < 1:
            # Transcript columns existed in the original metrics schema. Keep
            # the nullable columns for an in-place migration, but overwrite
            # the deleted cell contents so they cannot be recovered by
            # scanning the database file at rest.
            conn.execute("PRAGMA secure_delete = ON")
            conn.execute(
                "UPDATE turns SET asked_text = NULL, said_text = NULL "
                "WHERE asked_text IS NOT NULL OR said_text IS NOT NULL"
            )
            conn.execute("PRAGMA user_version = 1")
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


# ── Phone call log (who called, when, which node served) ───────
# Every writer is best-effort: a telemetry failure must never take a call
# down. Each node writes its own DB, so calls in the failover node's log
# ARE the failover record.


def record_call_start(conn, call_id: str, caller: str, called: str, node: str) -> None:
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR IGNORE INTO phone_calls(call_id, caller, called, node, answered_at)"
            " VALUES(?,?,?,?,datetime('now'))",
            (call_id, caller, called, node),
        )
        conn.commit()
    except Exception:
        log.exception("metrics record_call_start failed")


def record_call_end(conn, call_id: str) -> None:
    if conn is None:
        return
    try:
        conn.execute(
            "UPDATE phone_calls SET ended_at = datetime('now') WHERE call_id = ?",
            (call_id,),
        )
        conn.commit()
    except Exception:
        log.exception("metrics record_call_end failed")


def bump_call_turns(conn, call_id: str) -> None:
    if conn is None:
        return
    try:
        conn.execute(
            "UPDATE phone_calls SET turns = turns + 1 WHERE call_id = ?", (call_id,)
        )
        conn.commit()
    except Exception:
        log.exception("metrics bump_call_turns failed")


def recent_calls(conn, limit: int = 100) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT * FROM phone_calls ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


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
