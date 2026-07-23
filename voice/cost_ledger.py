"""Portable, best-effort cost receipts for voice calls.

The ledger deliberately stores usage rather than a pre-computed call total.
Component units are:

* ``telephony`` -- connected call minutes.
* ``scheduler_llm`` -- input, output, cache-read, or cache-write tokens.
* ``conversation_llm`` -- input, output, cache-read, or cache-write tokens.
* ``stt`` -- audio seconds submitted for transcription.
* ``tts`` -- characters successfully submitted for speech synthesis.
* ``infra`` -- connected call minutes used to amortize host infrastructure.

Every public writer is best-effort.  A missing database, malformed pricing
file, or SQLite error is logged and returned as a false-y result; none of
those failures may interrupt a live call.  The core schema/writer/report
functions are intentionally independent of nano-claw's phone gateway so the
riff parity port only needs to supply its own SQLite handle.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("cost-ledger")

PRICING_PATH = Path(__file__).with_name("pricing.json")

TELEPHONY = "telephony"
SCHEDULER_LLM = "scheduler_llm"
CONVERSATION_LLM = "conversation_llm"
STT = "stt"
TTS = "tts"
INFRA = "infra"

_SCHEMA = """
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
"""


@dataclass(frozen=True)
class LedgerEntry:
    """One component receipt; ``units`` follow the module unit contract."""

    component: str
    units: float
    unit_kind: str
    usd_per_unit_snapshot: float | None


def ensure_schema(conn) -> bool:
    """Create ``cost_ledger`` on *conn*; return success and never raise."""

    if conn is None:
        return False
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        return True
    except Exception:
        log.exception("cost ledger schema initialization failed")
        return False


def write_call(
    conn,
    call_id: str,
    business: str,
    flow: str,
    entries: Iterable[LedgerEntry | dict],
    *,
    ts: float | None = None,
) -> bool:
    """Write all usage for one completed call in one transaction.

    A call id already present in the ledger is treated as successfully
    written, making carrier/websocket end-event races idempotent.  Invalid or
    zero-unit entries are ignored.  This function never raises.
    """

    if conn is None or not isinstance(call_id, str) or not call_id:
        return False
    if not ensure_schema(conn):
        return False

    rows: list[tuple] = []
    written_at = float(ts if ts is not None else time.time())
    try:
        for raw in entries:
            if isinstance(raw, LedgerEntry):
                component = raw.component
                units = raw.units
                unit_kind = raw.unit_kind
                rate = raw.usd_per_unit_snapshot
            else:
                component = raw.get("component")
                units = raw.get("units")
                unit_kind = raw.get("unit_kind")
                rate = raw.get("usd_per_unit_snapshot")
            if (
                not isinstance(component, str)
                or not component
                or not isinstance(unit_kind, str)
                or not unit_kind
                or not _finite_number(units)
                or float(units) <= 0
            ):
                continue
            normalized_rate = float(rate) if _finite_number(rate) else None
            rows.append(
                (
                    call_id,
                    written_at,
                    str(business or ""),
                    str(flow or ""),
                    component,
                    float(units),
                    unit_kind,
                    normalized_rate,
                )
            )

        if not rows:
            return False
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM cost_ledger WHERE call_id = ? LIMIT 1", (call_id,)
            ).fetchone()
            if exists:
                return True
            conn.executemany(
                """INSERT INTO cost_ledger(
                       call_id, ts, business, flow, component, units,
                       unit_kind, usd_per_unit_snapshot
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                rows,
            )
        return True
    except Exception:
        log.exception("cost ledger write failed for call %s", call_id[:16])
        return False


def read_entries(conn, call_id: str | None = None) -> list[dict]:
    """Return ledger rows for reporting/tests; database errors become ``[]``."""

    if conn is None or not ensure_schema(conn):
        return []
    try:
        if call_id is None:
            rows = conn.execute(
                "SELECT * FROM cost_ledger ORDER BY ts, rowid"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cost_ledger WHERE call_id = ? ORDER BY ts, rowid",
                (call_id,),
            ).fetchall()
        columns = [column[0] for column in conn.execute(
            "SELECT * FROM cost_ledger LIMIT 0"
        ).description]
        return [
            dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
            for row in rows
        ]
    except Exception:
        log.exception("cost ledger read failed")
        return []


def load_pricing(path: str | Path | None = None) -> dict | None:
    """Load and minimally validate pricing lazily.

    Nothing is read at module import time.  Returning ``None`` is the public
    fallback for missing, malformed, or structurally invalid pricing.
    """

    pricing_path = Path(path) if path is not None else PRICING_PATH
    try:
        data = json.loads(pricing_path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("as_of"), str)
            or not data["as_of"].strip()
        ):
            raise ValueError("pricing root/as_of is invalid")
        if (
            not _finite_number(data.get("duty_cycle"))
            or not 0 < float(data["duty_cycle"]) <= 1
        ):
            raise ValueError("pricing duty_cycle is invalid")
        if not isinstance(data.get("reference_model"), str):
            raise ValueError("pricing reference_model is invalid")
        if not isinstance(data.get("models"), list) or not data["models"]:
            raise ValueError("pricing models must be a non-empty list")
        if not isinstance(data.get("components"), list):
            raise ValueError("pricing components must be a list")
        telephony = data.get("telephony")
        if not isinstance(telephony, dict):
            raise ValueError("pricing telephony block is missing")
        for key in ("usd_per_minute", "usd_per_did_month"):
            if not _nonnegative_number(telephony.get(key)):
                raise ValueError(f"telephony.{key} is invalid")
        for model in data["models"]:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                raise ValueError("pricing model id is invalid")
            if not _nonnegative_number(model.get("pin")) or not _nonnegative_number(model.get("pout")):
                raise ValueError(f"pricing rates are invalid for {model.get('id')}")
            if not isinstance(model.get("aliases", []), list):
                raise ValueError(f"pricing aliases are invalid for {model.get('id')}")
            for cache_key in ("pcache", "pcache_write"):
                if cache_key in model and not _nonnegative_number(model[cache_key]):
                    raise ValueError(f"pricing {cache_key} is invalid for {model.get('id')}")
            if model.get("fleet", True) and (
                not _nonnegative_number(model.get("m24"))
                or not _nonnegative_number(model.get("mreal"))
            ):
                raise ValueError(f"pricing scenarios are invalid for {model.get('id')}")
        if not any(
            model["id"] == data["reference_model"] for model in data["models"]
        ):
            raise ValueError("pricing reference_model does not exist")
        for component in data["components"]:
            if not isinstance(component, dict) or not isinstance(component.get("id"), str):
                raise ValueError("pricing component id is invalid")
            if not _nonnegative_number(component.get("per_minute")):
                raise ValueError(f"component per_minute is invalid for {component.get('id')}")
            rates = component.get("unit_rates", {})
            if not isinstance(rates, dict) or any(
                not _nonnegative_number(value) for value in rates.values()
            ):
                raise ValueError(f"component rates are invalid for {component.get('id')}")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("pricing unavailable from %s: %s", pricing_path, exc)
        return None


def hash_caller(caller: str, *, salt: str | bytes | None = None) -> str:
    """Return a stable HMAC identifier without retaining a caller number.

    Production should set ``NANO_CLAW_COST_HASH_SALT``.  The phone webhook
    token is the next-best deployment-specific secret; a namespaced fallback
    keeps local development/test output stable.
    """

    normalized = "".join(character for character in str(caller) if character.isdigit())
    if not normalized:
        normalized = str(caller).strip().lower()
    secret = salt
    if secret is None:
        secret = (
            os.environ.get("NANO_CLAW_COST_HASH_SALT")
            or os.environ.get("NANO_CLAW_PHONE_TOKEN")
            or "nano-claw-cost-ledger-local"
        )
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hmac.new(secret, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"cust_{digest[:20]}"


def build_report(
    conn,
    *,
    pricing_path: str | Path | None = None,
    hash_salt: str | bytes | None = None,
) -> dict:
    """Aggregate ledger, business/flow, and privacy-safe customer statistics."""

    pricing = load_pricing(pricing_path)
    rows = read_entries(conn)
    phone_meta = _phone_call_metadata(conn)

    calls: dict[str, dict] = {}
    component_totals: dict[str, dict] = defaultdict(
        lambda: {"usd": 0.0, "units": defaultdict(float), "priced": True}
    )
    for row in rows:
        call_id = str(row.get("call_id") or "")
        if not call_id:
            continue
        call = calls.setdefault(
            call_id,
            {
                "call_id": call_id,
                "business": "",
                "flow": "",
                "minutes": 0.0,
                "usd": 0.0,
            },
        )
        if row.get("business"):
            call["business"] = str(row["business"])
        if row.get("flow"):
            call["flow"] = str(row["flow"])
        units = float(row["units"]) if _finite_number(row.get("units")) else 0.0
        rate = row.get("usd_per_unit_snapshot")
        usd = units * float(rate) if _finite_number(rate) else 0.0
        component = str(row.get("component") or "unclassified")
        unit_kind = str(row.get("unit_kind") or "units")
        call["usd"] += usd
        if component == TELEPHONY and unit_kind == "minutes":
            call["minutes"] += units
        total = component_totals[component]
        total["usd"] += usd
        total["units"][unit_kind] += units
        if not _finite_number(rate):
            total["priced"] = False

    total_minutes = sum(call["minutes"] for call in calls.values())
    did_rate = (
        float(pricing["telephony"]["usd_per_did_month"])
        if pricing is not None
        else None
    )

    business_groups: dict[str, dict] = {}
    customer_groups: dict[tuple[str, str], dict] = {}
    all_customer_hashes: set[str] = set()
    for call in calls.values():
        meta = phone_meta.get(call["call_id"], {})
        business = call["business"] or str(meta.get("called") or "Unassigned")
        flow = call["flow"] or "unclassified"
        call["business"] = business
        call["flow"] = flow
        group = business_groups.setdefault(
            business,
            {
                "call_ids": set(),
                "minutes": [],
                "variable_usd": 0.0,
                "lines": set(),
                "customer_hashes": set(),
                "flows": {},
            },
        )
        group["call_ids"].add(call["call_id"])
        group["minutes"].append(call["minutes"])
        group["variable_usd"] += call["usd"]
        called = meta.get("called")
        if isinstance(called, str) and called and called != "?":
            group["lines"].add(called)

        flow_group = group["flows"].setdefault(
            flow, {"call_ids": set(), "minutes": 0.0, "usd": 0.0}
        )
        flow_group["call_ids"].add(call["call_id"])
        flow_group["minutes"] += call["minutes"]
        flow_group["usd"] += call["usd"]

        caller = meta.get("caller")
        if isinstance(caller, str) and caller and caller != "?":
            caller_hash = hash_caller(caller, salt=hash_salt)
            all_customer_hashes.add(caller_hash)
            group["customer_hashes"].add(caller_hash)
            customer = customer_groups.setdefault(
                (caller_hash, business),
                {"hash": caller_hash, "business": business, "minutes": []},
            )
            customer["minutes"].append(call["minutes"])

    businesses: list[dict] = []
    total_dids = 0
    for name, group in sorted(business_groups.items()):
        minutes = list(group["minutes"])
        calls_count = len(group["call_ids"])
        customers_count = len(group["customer_hashes"])
        dids = len(group["lines"])
        total_dids += dids
        fixed_usd = dids * did_rate if did_rate is not None else 0.0
        total_business_minutes = sum(minutes)
        flows = []
        for flow_name, flow_group in sorted(group["flows"].items()):
            flow_minutes = flow_group["minutes"]
            flows.append(
                {
                    "flow": flow_name,
                    "calls": len(flow_group["call_ids"]),
                    "min": _rounded(flow_minutes, 3),
                    "share": _rounded(
                        flow_minutes / total_business_minutes
                        if total_business_minutes
                        else 0.0,
                        6,
                    ),
                    "usd": _rounded(flow_group["usd"], 8),
                }
            )
        lines = sorted(group["lines"])
        businesses.append(
            {
                "name": name,
                "line": " · ".join(lines) if lines else "—",
                "calls": calls_count,
                "customers": customers_count,
                "callsPerCustomer": _rounded(
                    calls_count / customers_count if customers_count else 0.0, 3
                ),
                "minMin": _rounded(min(minutes), 3) if minutes else 0.0,
                "medMin": _rounded(statistics.median(minutes), 3) if minutes else 0.0,
                "maxMin": _rounded(max(minutes), 3) if minutes else 0.0,
                "totalMin": _rounded(total_business_minutes, 3),
                "bookings": 0,
                "dids": dids,
                "variableUsd": _rounded(group["variable_usd"], 8),
                "usd": _rounded(group["variable_usd"] + fixed_usd, 8),
                "flows": flows,
            }
        )

    customers = []
    for customer in sorted(customer_groups.values(), key=lambda item: (item["business"], item["hash"])):
        minutes = customer["minutes"]
        customers.append(
            {
                "hash": customer["hash"],
                "business": customer["business"],
                "calls": len(minutes),
                "totalMin": _rounded(sum(minutes), 3),
                "minMin": _rounded(min(minutes), 3),
                "medMin": _rounded(statistics.median(minutes), 3),
                "maxMin": _rounded(max(minutes), 3),
            }
        )

    components = _component_payloads(pricing, component_totals, total_minutes)
    variable_usd = sum(call["usd"] for call in calls.values())
    fixed_usd = total_dids * did_rate if did_rate is not None else 0.0
    grand_usd = variable_usd + fixed_usd
    if components:
        telephony_component = next(
            (component for component in components if component.get("id") == "telephony"),
            None,
        )
        if telephony_component is not None:
            telephony_component["fixedUsd"] = _rounded(fixed_usd, 8)

    models = _model_payloads(pricing)
    pricing_available = pricing is not None
    awaiting = not bool(calls)
    status = (
        "pricing_unavailable"
        if not pricing_available
        else "awaiting_call_data"
        if awaiting
        else "ok"
    )
    totals = {
        "calls": len(calls),
        "customers": len(all_customer_hashes),
        "minutes": _rounded(total_minutes, 3),
        "dids": total_dids,
        "variableUsd": _rounded(variable_usd, 8),
        "fixedUsd": _rounded(fixed_usd, 8),
        "usd": _rounded(grand_usd, 8),
        "costPerCall": _rounded(grand_usd / len(calls), 8) if calls else None,
        "costPerMinute": _rounded(grand_usd / total_minutes, 8) if total_minutes else None,
    }
    return {
        "status": status,
        "message": (
            "pricing unavailable"
            if not pricing_available
            else "awaiting call data"
            if awaiting
            else None
        ),
        "awaitingCallData": awaiting,
        "pricing": {
            "available": pricing_available,
            "asOf": pricing.get("as_of") if pricing else None,
            "didMonth": did_rate,
        },
        "duty": pricing.get("duty_cycle") if pricing else None,
        "referenceModel": pricing.get("reference_model") if pricing else None,
        "totals": totals,
        "models": models,
        "components": components,
        "businesses": businesses,
        "customers": customers,
        "byComponent": {component["id"]: component for component in components},
        "byBusiness": businesses,
    }


def _component_payloads(pricing, totals: dict, total_minutes: float) -> list[dict]:
    configured = pricing.get("components", []) if pricing else []
    payloads: list[dict] = []
    seen: set[str] = set()
    for config in configured:
        ledger_component = str(config.get("ledger_component") or config["id"])
        seen.add(ledger_component)
        observed = totals.get(ledger_component)
        usd = observed["usd"] if observed else 0.0
        units = dict(observed["units"]) if observed else {}
        payloads.append(
            {
                "id": config["id"],
                "component": ledger_component,
                "label": config.get("label", config["id"]),
                "color": config.get("color", "#969e9b"),
                "perMin": _rounded(usd / total_minutes, 8) if total_minutes else config.get("per_minute"),
                "math": config.get("math", ""),
                "usd": _rounded(usd, 8),
                "units": units,
                "priced": bool(observed is None or observed["priced"]),
                "observed": observed is not None,
            }
        )
    for ledger_component, observed in sorted(totals.items()):
        if ledger_component in seen:
            continue
        payloads.append(
            {
                "id": ledger_component,
                "component": ledger_component,
                "label": ledger_component.replace("_", " ").title(),
                "color": "#969e9b",
                "perMin": _rounded(observed["usd"] / total_minutes, 8) if total_minutes else 0.0,
                "math": "Ledger units × call-end price snapshot",
                "usd": _rounded(observed["usd"], 8),
                "units": dict(observed["units"]),
                "priced": bool(observed["priced"]),
                "observed": True,
            }
        )
    return payloads


def _model_payloads(pricing: dict | None) -> list[dict]:
    if pricing is None:
        return []
    return [
        {
            "id": model["id"],
            "name": model.get("name", model["id"]),
            "provider": model.get("provider", ""),
            "pin": model["pin"],
            "pout": model["pout"],
            "pcache": model.get("pcache"),
            "pcacheWrite": model.get("pcache_write"),
            "lat": model.get("lat", "—"),
            "score": model.get("score", "—"),
            "zero": bool(model.get("zero", False)),
            "m24": model.get("m24"),
            "mreal": model.get("mreal"),
        }
        for model in pricing["models"]
        if model.get("fleet", True)
    ]


def _phone_call_metadata(conn) -> dict[str, dict]:
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT call_id, caller, called, answered_at, ended_at FROM phone_calls"
        ).fetchall()
        columns = ("call_id", "caller", "called", "answered_at", "ended_at")
        normalized = [
            dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
            for row in rows
        ]
        return {str(row["call_id"]): row for row in normalized if row.get("call_id")}
    except Exception:
        return {}


def _finite_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _nonnegative_number(value) -> bool:
    return _finite_number(value) and float(value) >= 0


def _rounded(value: float, digits: int) -> float:
    return round(float(value), digits)


# ── Nano-claw phone adapter ──────────────────────────────────────────────
# The portable API above has no gateway dependency.  server.py installs this
# adapter at startup because the gateway moved to voice.phone after the
# original task/design was written.


@dataclass
class _TrackedCall:
    started_monotonic: float = field(default_factory=time.monotonic)
    flows: set[str] = field(default_factory=set)
    units: dict[tuple[str, str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )


_tracked_calls: dict[str, _TrackedCall] = {}
_tracking_lock = threading.Lock()
_phone_conn_getter: Callable[[], object | None] | None = None


def begin_call(call_id: str, flow: str | None = None) -> None:
    """Begin an in-memory receipt accumulator; safe to call repeatedly."""

    if not call_id:
        return
    with _tracking_lock:
        tracked = _tracked_calls.setdefault(call_id, _TrackedCall())
        if flow:
            tracked.flows.add(flow)


def add_units(
    call_id: str,
    component: str,
    units: float,
    unit_kind: str,
    *,
    model: str = "",
    flow: str | None = None,
) -> None:
    """Accumulate raw units in memory without doing SQLite I/O in the hot path."""

    if not call_id or not _finite_number(units) or float(units) <= 0:
        return
    with _tracking_lock:
        tracked = _tracked_calls.setdefault(call_id, _TrackedCall())
        tracked.units[(component, unit_kind, model)] += float(units)
        if flow:
            tracked.flows.add(flow)


def add_llm_usage(
    call_id: str,
    component: str,
    model: str,
    usage: dict | None,
    *,
    flow: str,
) -> None:
    """Normalize provider usage into separately-priced token unit kinds."""

    if not isinstance(usage, dict):
        return
    prompt = _number_from(usage, "prompt", "prompt_tokens", "input_tokens")
    completion = _number_from(
        usage, "completion", "completion_tokens", "output_tokens"
    )
    cache_read = _number_from(
        usage, "cacheRead", "cache_read", "cache_read_tokens"
    )
    cache_write = _number_from(
        usage, "cacheWrite", "cache_write", "cache_write_tokens"
    )
    uncached_prompt = max(0.0, prompt - cache_read - cache_write)
    for kind, units in (
        ("input_tokens", uncached_prompt),
        ("output_tokens", completion),
        ("cache_read_tokens", cache_read),
        ("cache_write_tokens", cache_write),
    ):
        add_units(
            call_id,
            component,
            units,
            kind,
            model=model,
            flow=flow,
        )


def finish_call(conn, call_id: str, *, duration_minutes: float | None = None) -> bool:
    """Snapshot rates and atomically persist one accumulated call receipt."""

    with _tracking_lock:
        tracked = _tracked_calls.pop(call_id, None)
    if tracked is None:
        return False
    if duration_minutes is None:
        duration_minutes = max(0.0, time.monotonic() - tracked.started_monotonic) / 60.0
    add_call_minutes = float(duration_minutes) if _finite_number(duration_minutes) else 0.0
    if add_call_minutes > 0:
        tracked.units[(TELEPHONY, "minutes", "")] += add_call_minutes
        tracked.units[(INFRA, "call_minutes", "")] += add_call_minutes

    pricing = load_pricing()
    metadata = _phone_call_metadata(conn).get(call_id, {})
    business = (
        os.environ.get("NANO_CLAW_COST_BUSINESS", "").strip()
        or str(metadata.get("called") or "nano-claw")
    )
    if len(tracked.flows) > 1:
        flow = "mixed"
    elif tracked.flows:
        flow = next(iter(tracked.flows))
    else:
        flow = "conversation"

    entries = [
        LedgerEntry(
            component=component,
            units=units,
            unit_kind=unit_kind,
            usd_per_unit_snapshot=_unit_rate(
                pricing, component, unit_kind, model=model
            ),
        )
        for (component, unit_kind, model), units in tracked.units.items()
        if units > 0
    ]
    return write_call(
        conn,
        call_id,
        business,
        flow,
        entries,
    )


def install_phone_tracking(phone_module, conn_getter: Callable[[], object | None]) -> None:
    """Install call-end instrumentation without modifying ``voice.phone``.

    The adapter subclasses the gateway's current ``PhoneCall`` at runtime.
    Installation is idempotent, and updating *conn_getter* on later app
    factories keeps tests/reloads pointed at the current metrics handle.
    """

    global _phone_conn_getter
    _phone_conn_getter = conn_getter
    if getattr(phone_module, "_cost_ledger_tracking_installed", False):
        return

    base = phone_module.PhoneCall

    class CostTrackedPhoneCall(base):
        def __init__(self, ws, call_id: str) -> None:
            super().__init__(ws, call_id)
            flow = "scheduler" if getattr(self, "flow", None) is not None else "conversation"
            begin_call(call_id, flow)
            self._http = _UsageHTTPClient(self._http, call_id)

        async def close(self) -> None:
            turn_task = getattr(self, "_turn_task", None)
            try:
                await super().close()
            finally:
                # voice.phone cancels but intentionally does not await its turn
                # task.  Let that task's provider-usage ``finally`` finish
                # before popping the call accumulator.
                if turn_task is not None and not turn_task.done():
                    try:
                        await turn_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception("phone turn failed while finalizing cost receipt")
                try:
                    conn = _phone_conn_getter() if _phone_conn_getter else None
                    finish_call(conn, self.call_id)
                except Exception:
                    log.exception("cost ledger call finalization failed")

        async def _transcribe(self, pcm: bytes) -> str:
            rate = phone_module.phone_rate()
            if rate:
                add_units(
                    self.call_id,
                    STT,
                    len(pcm) / (2.0 * rate),
                    "audio_seconds",
                )
            return await super()._transcribe(pcm)

        async def _synthesize_sentence(self, sentence):
            speech = await super()._synthesize_sentence(sentence)
            # Generated processing earcons use the sentence pipeline for ordered
            # playback but do not invoke TTS and must not be billed as characters.
            if sentence != getattr(phone_module, "PROCESSING_CUE_SENTINEL", None):
                # In prepared-speech mode the unit is a SpeechChunk, not a str;
                # bill on its rendered text length rather than crashing on len().
                billable_text = getattr(sentence, "text", sentence)
                add_units(self.call_id, TTS, len(billable_text), "characters")
            return speech

        async def _run_turn(self, pcm: bytes) -> None:
            try:
                return await super()._run_turn(pcm)
            finally:
                flow_session = getattr(self, "flow", None)
                if flow_session is None:
                    begin_call(self.call_id, "conversation")
                else:
                    begin_call(self.call_id, "scheduler")
                    runner = getattr(flow_session, "_runner", None)
                    provider = getattr(runner, "_supervisor", None)
                    drain = getattr(provider, "drain_usage", None)
                    if callable(drain):
                        try:
                            usage = drain()
                            add_llm_usage(
                                self.call_id,
                                SCHEDULER_LLM,
                                str(getattr(runner, "_model", "") or ""),
                                usage,
                                flow="scheduler",
                            )
                        except Exception:
                            log.exception("failed to collect scheduler token usage")

    CostTrackedPhoneCall.__name__ = base.__name__
    CostTrackedPhoneCall.__qualname__ = base.__qualname__
    phone_module.PhoneCall = CostTrackedPhoneCall
    phone_module._cost_ledger_tracking_installed = True


class _UsageHTTPClient:
    """Delegate httpx calls while observing nano-claw API debug receipts."""

    def __init__(self, inner, call_id: str) -> None:
        self._inner = inner
        self._call_id = call_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def stream(self, *args, **kwargs):
        context = self._inner.stream(*args, **kwargs)
        return _UsageStreamContext(context, self._call_id)


class _UsageStreamContext:
    def __init__(self, inner, call_id: str) -> None:
        self._inner = inner
        self._call_id = call_id

    async def __aenter__(self):
        response = await self._inner.__aenter__()
        return _UsageResponse(response, self._call_id)

    async def __aexit__(self, exc_type, exc, traceback):
        return await self._inner.__aexit__(exc_type, exc, traceback)


class _UsageResponse:
    def __init__(self, inner, call_id: str) -> None:
        self._inner = inner
        self._call_id = call_id
        self._captured = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def aread(self):
        body = await self._inner.aread()
        try:
            self._capture(json.loads(body))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        return body

    async def aiter_lines(self):
        event = ""
        data_lines: list[str] = []
        async for line in self._inner.aiter_lines():
            if line == "":
                if data_lines and event in ("final", "tool_pending"):
                    try:
                        self._capture(json.loads("\n".join(data_lines)))
                    except json.JSONDecodeError:
                        pass
                event = ""
                data_lines = []
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            yield line
        if data_lines and event in ("final", "tool_pending"):
            try:
                self._capture(json.loads("\n".join(data_lines)))
            except json.JSONDecodeError:
                pass

    def _capture(self, payload) -> None:
        if self._captured or not isinstance(payload, dict):
            return
        debug = payload.get("debug")
        if not isinstance(debug, dict) or not isinstance(debug.get("tokenUsage"), dict):
            return
        self._captured = True
        add_llm_usage(
            self._call_id,
            CONVERSATION_LLM,
            str(debug.get("model") or ""),
            debug["tokenUsage"],
            flow="conversation",
        )


def _number_from(mapping: dict, *keys: str) -> float:
    for key in keys:
        value = mapping.get(key)
        if _finite_number(value):
            return max(0.0, float(value))
    return 0.0


def _unit_rate(
    pricing: dict | None,
    component: str,
    unit_kind: str,
    *,
    model: str = "",
) -> float | None:
    if pricing is None:
        return None
    if component in (SCHEDULER_LLM, CONVERSATION_LLM):
        model_config = _find_model(pricing, model)
        if model_config is None:
            model_config = _find_model(pricing, str(pricing.get("reference_model") or ""))
        if model_config is None:
            return None
        rate_key = {
            "input_tokens": "pin",
            "output_tokens": "pout",
            "cache_read_tokens": "pcache",
            "cache_write_tokens": "pcache_write",
        }.get(unit_kind)
        if rate_key is None:
            return None
        per_million = model_config.get(rate_key)
        if not _finite_number(per_million):
            per_million = model_config.get("pin")
        return float(per_million) / 1_000_000 if _finite_number(per_million) else None

    for config in pricing.get("components", []):
        ledger_component = config.get("ledger_component") or config.get("id")
        if ledger_component != component:
            continue
        rate = config.get("unit_rates", {}).get(unit_kind)
        return float(rate) if _finite_number(rate) else None
    return None


def _find_model(pricing: dict, requested: str) -> dict | None:
    normalized = requested.strip().lower()
    if not normalized:
        return None
    for model in pricing.get("models", []):
        names = [model.get("id"), model.get("wire_model"), *model.get("aliases", [])]
        if normalized in {
            str(name).strip().lower() for name in names if isinstance(name, str)
        }:
            return model
    return None
