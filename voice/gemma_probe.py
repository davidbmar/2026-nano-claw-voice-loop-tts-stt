"""Forward transcribed speech to gemma4 on the M5 and record the exchange.

This is the whole of transcribe mode's outbound side. It exists as its own
module for the same reason the provider adapters do: exactly one file may know
how to talk to the local inference host, so a change of host, port or API shape
has one place to happen.

Nothing here synthesizes speech, and nothing here may. Transcribe mode's defining
property is that it never talks back — see `is_transcribe_mode` in
`flow_session.py` and the dispatch in `server.py`.

Why the native ``/api/generate`` endpoint rather than the OpenAI-compat ``/v1``
shim the rest of the stack uses: the native API returns the eval counters and
per-phase nanosecond timings (``prompt_eval_count``, ``eval_duration``, …), and
it accepts ``think``. Both matter when the transcript is a research stimulus
rather than a chat turn — the counters say how much of the context the model
actually consumed, which is the first thing that goes unnoticed when a probe
silently truncates.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("gemma-probe")

DEFAULT_MODEL = "gemma4:26b"
DEFAULT_BASE = "http://host.docker.internal:11434"
DEFAULT_TIMEOUT_S = 120.0


def _env(name: str) -> str:
    """An environment value, treating whitespace-only as unset.

    run.sh forwards a fixed list of `-e VAR="$VAR"` flags, so a variable that is
    unset on the host arrives in the container as an EMPTY STRING rather than
    absent. Every read here has to treat "" as "not configured" or the defaults
    below never apply — that exact confusion took the container down on
    2026-07-29 (see tests/config-env-numbers.test.ts).
    """

    return (os.environ.get(name) or "").strip()


def probe_base_url() -> str:
    """Root URL of the inference host, without the OpenAI-compat suffix.

    Reuses NANO_CLAW_OLLAMA_BASE so the probe and the dropdown's ollama/* models
    always point at the same box — two variables would drift, and a probe
    pointed at a different host than the one being inspected is worse than no
    probe at all. That variable carries a `/v1` suffix for the compat shim;
    strip it, because the native endpoints sit at the root.
    """

    raw = _env("NANO_CLAW_TRANSCRIBE_BASE") or _env("NANO_CLAW_OLLAMA_BASE") or DEFAULT_BASE
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def probe_model() -> str:
    """Which gemma4 the transcript is fed to."""

    return _env("NANO_CLAW_TRANSCRIBE_MODEL") or DEFAULT_MODEL


def probe_thinking_enabled() -> bool:
    """Whether gemma4 may emit a reasoning trace before its answer.

    Off by default, matching the voice path. Turn it on with
    NANO_CLAW_TRANSCRIBE_THINK=1 when the trace is itself the object of study —
    `record_exchange` keeps it in a separate `thinking` field either way, so a
    trace never contaminates the `response` text.
    """

    return _env("NANO_CLAW_TRANSCRIBE_THINK") in {"1", "true", "yes", "on"}


def capture_path() -> Path:
    """Where exchanges are appended as JSONL, one object per utterance."""

    configured = _env("NANO_CLAW_TRANSCRIBE_LOG")
    if configured:
        return Path(configured)
    memory_dir = _env("NANO_CLAW_MEMORY_DIR") or "/app/data/memory"
    return Path(memory_dir) / "transcribe" / "jspace.jsonl"


def build_prompt(text: str) -> str:
    """What gemma4 actually receives.

    Deliberately the bare transcript. A probe measuring how the model represents
    speech must not prepend an instruction, because the wrapper would then be
    part of what is being measured — and the wrapper's tokens would sit at the
    start of the context, exactly where a prefix's influence is strongest.

    If a task framing is ever wanted, it belongs here, in one visible place,
    rather than distributed through the caller.
    """

    return text


async def send_to_gemma(text: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """POST one transcript to gemma4 and return the parsed native response.

    Raises on transport or HTTP failure; the caller decides how loud that is.
    A probe that silently swallowed a dead host would produce a capture file
    full of gaps that look like the model choosing to say nothing.
    """

    url = f"{probe_base_url()}/api/generate"
    payload: dict[str, Any] = {
        "model": probe_model(),
        "prompt": build_prompt(text),
        "stream": False,
        "think": probe_thinking_enabled(),
    }

    started = time.monotonic()
    if client is None:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as owned:
            resp = await owned.post(url, json=payload)
    else:
        resp = await client.post(url, json=payload, timeout=DEFAULT_TIMEOUT_S)
    resp.raise_for_status()
    body = resp.json()
    body["_client_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return body


def record_exchange(transcript: str, result: dict[str, Any] | None, error: str | None = None) -> None:
    """Append one utterance and its outcome to the capture file.

    Best-effort by design: a research capture must never be the reason a live
    session drops a turn. Failures are logged and swallowed.

    Failed exchanges are written too, with `error` set. A capture that only
    contained successes would silently misrepresent the run — the gaps are data.
    """

    result = result or {}
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "transcript": transcript,
        "model": probe_model(),
        "host": probe_base_url(),
        "response": result.get("response"),
        # Kept separate from `response` so a reasoning trace never silently
        # becomes part of the measured output.
        "thinking": result.get("thinking"),
        "error": error,
        # The native API's counters. `prompt_eval_count` is the one to watch:
        # if it stops rising with longer transcripts, the context is truncating.
        "prompt_eval_count": result.get("prompt_eval_count"),
        "eval_count": result.get("eval_count"),
        "total_duration_ns": result.get("total_duration"),
        "eval_duration_ns": result.get("eval_duration"),
        "client_elapsed_ms": result.get("_client_elapsed_ms"),
    }
    try:
        path = capture_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Could not append to the transcribe capture file")
