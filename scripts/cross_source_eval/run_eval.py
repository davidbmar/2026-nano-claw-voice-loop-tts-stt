#!/usr/bin/env python3
"""Run the isolated live cross-source milestone evaluation.

This is intentionally standard-library-only. It builds NanoClaw, starts a
temporary intelligence-platform database and API, ingests the pinned source
manifest from corpus.json, starts a dedicated NanoClaw API, runs each case in a
fresh session with an explicit collection scope, executes the deterministic
malformed-artifact check, and writes a dated scoreboard.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
NANO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_CORPUS = SCRIPT_DIR / "corpus.json"
DEFAULT_SCOREBOARD_DIR = SCRIPT_DIR / "scoreboards"
DEFAULT_DESIGN_DOC = Path.home() / ".claude/plans/riff-nanoqa-unified-knowledge.md"
TRACE_VERSION = "cross-source-eval-v1"
PROVIDER_KEYS = (
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
)


class EvalError(RuntimeError):
    """A configuration or live-stack infrastructure failure."""


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    log_path: Path
    log_file: Any

    def stop(self) -> None:
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.log_file.close()

    def failure_tail(self) -> str:
        if not self.log_path.is_file():
            return ""
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-30:])


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvalError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvalError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_corpus(corpus: dict[str, Any]) -> None:
    meta = corpus.get("_meta")
    sources = corpus.get("sources")
    cases = corpus.get("cases")
    if not isinstance(meta, dict) or meta.get("format_version") != 1:
        raise EvalError("corpus metadata format_version must be 1")
    if not isinstance(sources, list) or not sources:
        raise EvalError("corpus sources must be a non-empty list")
    if not isinstance(cases, list):
        raise EvalError("corpus cases must be a list")
    counts = {"document": 0, "code": 0, "cross_source": 0}
    known_ids: set[str] = set()
    collection_ids = {
        source.get("collection_id")
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("collection_id"), str)
    }
    for case in cases:
        if not isinstance(case, dict):
            raise EvalError("every corpus case must be an object")
        case_id = case.get("id")
        group = case.get("group")
        if not isinstance(case_id, str) or case_id in known_ids:
            raise EvalError(f"invalid or duplicate case id: {case_id!r}")
        known_ids.add(case_id)
        if group not in counts:
            raise EvalError(f"{case_id}: invalid group {group!r}")
        counts[group] += 1
        if case.get("expected_route") not in {"fast", "deep", "registry"}:
            raise EvalError(f"{case_id}: invalid expected_route")
        scope = case.get("scope")
        if not isinstance(scope, list) or not scope:
            raise EvalError(f"{case_id}: scope must be non-empty")
        unknown = [item for item in scope if item not in collection_ids]
        if unknown:
            raise EvalError(f"{case_id}: unknown collection ids {unknown}")
    if counts != {"document": 5, "code": 5, "cross_source": 3}:
        raise EvalError(f"corpus group counts must be 5/5/3, got {counts}")


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse the simple KEY=value subset used by this repo without printing values."""

    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        values[name] = parsed[0] if parsed else ""
    return values


def child_environment(nano_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name, value in parse_dotenv(nano_root / ".env").items():
        env.setdefault(name, value)
    return env


def resolve_model(env: dict[str, str], requested: str | None) -> str:
    if requested:
        return requested
    config_path = Path.home() / ".nano-claw/config.json"
    if config_path.is_file():
        with contextlib.suppress(Exception):
            config = read_json(config_path)
            model = config.get("agents", {}).get("defaults", {}).get("model")
            if isinstance(model, str) and model:
                return model
    defaults = (
        ("DEEPSEEK_API_KEY", "deepseek/deepseek-v4-flash"),
        ("ANTHROPIC_API_KEY", "anthropic/claude-haiku-4-5"),
        ("GEMINI_API_KEY", "gemini/gemini-flash-lite-latest"),
        ("GROQ_API_KEY", "groq/llama-3.3-70b-versatile"),
        ("OPENAI_API_KEY", "openai/gpt-4o-mini"),
        ("OPENROUTER_API_KEY", "openrouter/openai/gpt-4o-mini"),
    )
    for key, model in defaults:
        if env.get(key):
            return model
    raise EvalError(
        "no live model selected and no supported provider credential is available"
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise EvalError(f"{method} {url} returned HTTP {error.code}: {body[:500]}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise EvalError(f"{method} {url} failed: {error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EvalError(f"{method} {url} returned invalid JSON") from error
    if not isinstance(parsed, dict):
        raise EvalError(f"{method} {url} returned a non-object JSON response")
    return parsed


def start_process(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> ManagedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return ManagedProcess(name=name, process=process, log_path=log_path, log_file=log_file)


def wait_for_health(process: ManagedProcess, url: str, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise EvalError(
                f"{process.name} exited with {process.process.returncode}\n"
                f"{process.failure_tail()}"
            )
        try:
            response = json_request("GET", url, timeout=2)
            if response.get("status") in {"ok", "healthy"}:
                return
        except EvalError as error:
            last_error = str(error)
        time.sleep(0.25)
    raise EvalError(
        f"{process.name} did not become ready at {url}: {last_error}\n"
        f"{process.failure_tail()}"
    )


def git_fingerprint(root: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    try:
        commit = git("rev-parse", "HEAD")
        status = git("status", "--porcelain")
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(status)}


def source_documents(
    corpus: dict[str, Any],
    roots: dict[str, Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    provenance: dict[str, Any] = {}
    for raw_source in corpus["sources"]:
        source_id = raw_source["id"]
        collection_id = raw_source["collection_id"]
        root = roots[raw_source["root_arg"]].expanduser().resolve()
        source_entries: list[dict[str, str]] = []
        for requested in raw_source["files"]:
            if requested == "." and root.is_file():
                path = root
                relative = path.name
            else:
                path = root / requested
                relative = requested
            if not path.is_file():
                raise EvalError(f"required eval source is missing: {path}")
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise EvalError(f"eval source is not UTF-8: {path}") from error
            digest = sha256_bytes(raw)
            source_ref = f"eval://{source_id}/{relative}"
            documents.append(
                {
                    "source_id": source_id,
                    "collection_id": collection_id,
                    "relative": relative,
                    "source_ref": source_ref,
                    "title": relative,
                    "text": text,
                    "digest": digest,
                }
            )
            source_entries.append({"path": relative, "sha256": digest})
        provenance[source_id] = {
            "kind": raw_source["kind"],
            "collection_id": collection_id,
            "files": source_entries,
        }
    return documents, provenance


def ingest_documents(
    platform_url: str,
    tenant_id: str,
    documents: list[dict[str, Any]],
) -> None:
    for index, document in enumerate(documents, start=1):
        json_request(
            "POST",
            f"{platform_url}/v1/ingest/text",
            {
                "tenant_id": tenant_id,
                "policy": {
                    "tenant_id": tenant_id,
                    "principal_id": "cross-source-eval",
                    "permissions": ["knowledge:ingest"],
                },
                "source_ref": document["source_ref"],
                "document_key": f"{document['collection_id']}:{document['relative']}",
                "title": document["title"],
                "text": document["text"],
                "collection_ids": [document["collection_id"]],
                "source_kind": "cross_source_eval",
                "metadata": {
                    "eval_source_id": document["source_id"],
                    "path": document["relative"],
                    "sha256": document["digest"],
                },
            },
            timeout=60,
        )
        print(f"  ingested {index:02d}/{len(documents):02d} {document['source_ref']}")


def write_nano_config(home: Path, model: str) -> None:
    config_path = home / ".nano-claw/config.json"
    config = {
        "providers": {},
        "agents": {
            "defaults": {
                "model": model,
                "temperature": 0,
                "maxTokens": 1800,
            },
            "profiles": {
                "intelligence": {
                    "label": "Cross-source evaluation",
                    "systemPrompt": (
                        "Answer only from retrieved evidence or validated analysis. "
                        "Name source files and symbols when the evidence provides them. "
                        "A retrieval miss is not proof of absence. For absence or "
                        "implementation-coverage questions, say exactly that you did not "
                        "find evidence in what's loaded unless the evidence proves absence. "
                        "Keep the answer concise and never expose internal chain-of-thought."
                    ),
                    "knowledgeFiles": [],
                }
            },
        },
        "tools": {"enabled": False, "restrictToWorkspace": True},
        "channels": {},
    }
    write_json(config_path, config)


def chat(
    nano_url: str,
    *,
    message: str,
    session_id: str,
    model: str,
    trace: bool,
    timeout: float,
) -> dict[str, Any]:
    return json_request(
        "POST",
        f"{nano_url}/api/chat",
        {
            "message": message,
            "sessionId": session_id,
            "model": model,
            "profile": "intelligence",
            "responseMode": "text",
            "evalTrace": trace,
        },
        timeout=timeout,
    )


def set_scope(
    nano_url: str,
    *,
    scope: list[str],
    session_id: str,
    model: str,
    timeout: float,
) -> None:
    for index, collection_id in enumerate(scope):
        command = ("load " if index == 0 else "add ") + collection_id
        response = chat(
            nano_url,
            message=command,
            session_id=session_id,
            model=model,
            trace=False,
            timeout=timeout,
        )
        if response.get("type") != "final":
            raise EvalError(f"scope command did not finish: {command}")
        debug_scope = response.get("debug", {}).get("knowledgeScope", {})
        expected = sorted(scope[: index + 1])
        actual = sorted(debug_scope.get("collectionIds", []))
        if actual != expected:
            raise EvalError(
                f"scope command {command!r} selected {actual}, expected {expected}"
            )


def field_value(evidence: dict[str, Any], field: str) -> str:
    key = {
        "source_ref": "sourceRef",
        "section_path": "sectionPath",
    }.get(field, field)
    value = evidence.get(key)
    if isinstance(value, list):
        return " > ".join(str(item) for item in value)
    return value if isinstance(value, str) else ""


def predicate_matches(evidence: list[dict[str, Any]], predicate: dict[str, Any]) -> bool:
    needle = predicate.get("contains")
    field = predicate.get("field")
    if not isinstance(needle, str) or not isinstance(field, str):
        return False
    folded = needle.casefold()
    return any(folded in field_value(item, field).casefold() for item in evidence)


def score_anchors(
    evidence: list[dict[str, Any]], anchors: list[dict[str, Any]]
) -> tuple[float, list[str], list[str]]:
    passed: list[str] = []
    failed: list[str] = []
    for anchor in anchors:
        predicates = anchor.get("matches", [])
        matches = [
            predicate_matches(evidence, predicate)
            for predicate in predicates
            if isinstance(predicate, dict)
        ]
        ok = all(matches) if anchor.get("rule") == "all" else any(matches)
        (passed if ok else failed).append(str(anchor.get("id")))
    return (
        len(passed) / len(anchors) if anchors else 1.0,
        passed,
        failed,
    )


def citation_presence(claims: list[dict[str, Any]]) -> float:
    if not claims:
        return 0.0
    cited = sum(
        1
        for claim in claims
        if claim.get("evidenceIds") or claim.get("citationIds")
    )
    return cited / len(claims)


def coverage_disclaimer_present(response: str) -> bool:
    lowered = re.sub(r"\s+", " ", response.casefold())
    return any(
        phrase in lowered
        for phrase in (
            "didn't find evidence",
            "did not find evidence",
            "no implementation evidence",
            "evidence is missing",
            "evidence remains missing",
            "in what's loaded",
            "in what is loaded",
            "loaded evidence does not establish",
            "loaded sources do not establish",
        )
    )


def source_coverage(
    evidence: list[dict[str, Any]], required_sources: list[str]
) -> tuple[bool, list[str]]:
    missing = []
    refs = [
        item.get("sourceRef", "")
        for item in evidence
        if isinstance(item.get("sourceRef"), str)
    ]
    for source_id in required_sources:
        if not any(f"eval://{source_id}/" in ref for ref in refs):
            missing.append(source_id)
    return not missing, missing


def compact_citations(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in evidence:
        compact.append(
            {
                key: item[key]
                for key in (
                    "evidenceId",
                    "citationId",
                    "title",
                    "sourceRef",
                    "sectionPath",
                    "charStart",
                    "charEnd",
                    "lineStart",
                    "lineEnd",
                    "rank",
                )
                if item.get(key) is not None
            }
        )
    return compact


def evaluate_response(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    debug = response.get("debug")
    trace = debug.get("evalTrace") if isinstance(debug, dict) else None
    if response.get("type") != "final" or not isinstance(trace, dict):
        raise EvalError(f"{case['id']}: final response is missing debug.evalTrace")
    if trace.get("version") != TRACE_VERSION:
        raise EvalError(
            f"{case['id']}: trace version {trace.get('version')!r} != {TRACE_VERSION!r}"
        )
    evidence = [item for item in trace.get("evidence", []) if isinstance(item, dict)]
    claims = [item for item in trace.get("claims", []) if isinstance(item, dict)]
    anchors = [item for item in case.get("anchors", []) if isinstance(item, dict)]
    anchor_rate, passed_anchors, failed_anchors = score_anchors(evidence, anchors)
    claim_rate = citation_presence(claims)
    source_ok, missing_sources = source_coverage(
        evidence,
        [item for item in case.get("required_sources", []) if isinstance(item, str)],
    )
    answer = response.get("response") if isinstance(response.get("response"), str) else ""
    route_ok = trace.get("route") == case["expected_route"]
    coverage_required = bool(case.get("coverage_disclaimer_required"))
    coverage_ok = not coverage_required or coverage_disclaimer_present(answer)
    scope_ok = sorted(trace.get("config", {}).get("collectionIds", [])) == sorted(case["scope"])
    anchor_ok = anchor_rate == 1.0
    citation_ok = claim_rate >= float(case.get("min_citation_presence", 0))
    passed = route_ok and scope_ok and anchor_ok and citation_ok and source_ok and coverage_ok
    return {
        "passed": passed,
        "route": trace.get("route"),
        "expected_route": case["expected_route"],
        "outcome": trace.get("outcome"),
        "error_code": trace.get("errorCode"),
        "scope_ok": scope_ok,
        "metrics": {
            "expected_evidence_hit_rate": round(anchor_rate, 4),
            "citation_presence": round(claim_rate, 4),
            "routing_correctness": 1.0 if route_ok else 0.0,
            "required_source_coverage": 1.0 if source_ok else 0.0,
            "coverage_disclaimer": 1.0 if coverage_ok else 0.0,
        },
        "passed_anchors": passed_anchors,
        "failed_anchors": failed_anchors,
        "missing_sources": missing_sources,
        "claim_count": len(claims),
        "cited_claim_count": sum(
            1 for claim in claims if claim.get("evidenceIds") or claim.get("citationIds")
        ),
        "citations": compact_citations(evidence),
        "response_sha256": sha256_bytes(answer.encode("utf-8")),
        "response_excerpt": answer[:320],
    }


def run_case(
    case: dict[str, Any],
    *,
    nano_url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    session_id = f"voice-{uuid.uuid4().hex}"
    started = time.monotonic()
    try:
        set_scope(
            nano_url,
            scope=case["scope"],
            session_id=session_id,
            model=model,
            timeout=timeout,
        )
        setup_results: list[dict[str, Any]] = []
        for setup_turn in case.get("setup", []):
            setup_response = chat(
                nano_url,
                message=setup_turn,
                session_id=session_id,
                model=model,
                trace=True,
                timeout=timeout,
            )
            setup_trace = setup_response.get("debug", {}).get("evalTrace", {})
            setup_results.append(
                {
                    "route": setup_trace.get("route"),
                    "outcome": setup_trace.get("outcome"),
                    "error_code": setup_trace.get("errorCode"),
                    "response_sha256": sha256_bytes(
                        str(setup_response.get("response", "")).encode("utf-8")
                    ),
                }
            )
            if setup_trace.get("outcome") != "answered":
                result = {
                    "passed": False,
                    "route": setup_trace.get("route"),
                    "expected_route": case["expected_route"],
                    "outcome": "setup_failed",
                    "error_code": setup_trace.get("errorCode"),
                    "metrics": {
                        "expected_evidence_hit_rate": 0.0,
                        "citation_presence": 0.0,
                        "routing_correctness": 0.0,
                        "required_source_coverage": 0.0,
                        "coverage_disclaimer": 0.0,
                    },
                    "setup": setup_results,
                    "citations": [],
                    "response_sha256": None,
                    "response_excerpt": "",
                }
                break
        else:
            response = chat(
                nano_url,
                message=case["question"],
                session_id=session_id,
                model=model,
                trace=True,
                timeout=timeout,
            )
            result = evaluate_response(case, response)
            result["setup"] = setup_results
    finally:
        with contextlib.suppress(EvalError):
            json_request(
                "DELETE",
                f"{nano_url}/api/session",
                {"sessionId": session_id},
                timeout=10,
            )
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    if result["passed"]:
        result["status"] = "pass"
    elif case.get("known_failure"):
        result["status"] = "known_fail"
        result["known_failure"] = case["known_failure"]
    else:
        result["status"] = "fail"
    return result


def run_fault_case(
    *,
    nano_root: Path,
    env: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    command = ["node", str(SCRIPT_DIR / "fault_injection.mjs")]
    completed = subprocess.run(
        command,
        cwd=nano_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    parsed: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        with contextlib.suppress(json.JSONDecodeError):
            candidate = json.loads(line)
            if isinstance(candidate, dict) and candidate.get("id") == "invalid-analysis-artifact":
                parsed = candidate
                break
    if parsed is None:
        return {
            "id": "invalid-analysis-artifact",
            "passed": False,
            "status": "infrastructure_error",
            "detail": completed.stdout[-1000:],
        }
    parsed["status"] = "pass" if parsed.get("passed") else "fail"
    return parsed


def aggregate(case_results: list[dict[str, Any]], fault: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for group in ("document", "code", "cross_source"):
        selected = [item for item in case_results if item["group"] == group]
        metrics = (
            "expected_evidence_hit_rate",
            "citation_presence",
            "routing_correctness",
            "required_source_coverage",
            "coverage_disclaimer",
        )
        groups[group] = {
            "total": len(selected),
            "passed": sum(item["status"] == "pass" for item in selected),
            "known_failed": sum(item["status"] == "known_fail" for item in selected),
            "failed": sum(item["status"] == "fail" for item in selected),
            "infrastructure_errors": sum(
                item["status"] == "infrastructure_error" for item in selected
            ),
            "metrics": {
                metric: round(
                    sum(float(item["metrics"].get(metric, 0)) for item in selected)
                    / max(1, len(selected)),
                    4,
                )
                for metric in metrics
            },
        }
    document_green = (
        groups["document"]["passed"] == groups["document"]["total"]
        and groups["document"]["total"] == 5
    )
    unexpected_failures = [
        item["id"]
        for item in case_results
        if item["status"] in {"fail", "infrastructure_error"}
    ]
    delivery_gate = document_green and not unexpected_failures and bool(fault.get("passed"))
    return {
        "groups": groups,
        "fault_injection": {
            "passed": bool(fault.get("passed")),
            "error_code": fault.get("errorCode"),
        },
        "unexpected_failures": unexpected_failures,
        "delivery_gate_passed": delivery_gate,
    }


def baseline_key(
    *,
    corpus_digest: str,
    implementation_digest: str,
    model: str,
    source_provenance: dict[str, Any],
    repositories: dict[str, Any],
    scoring: dict[str, Any],
    case_ids: list[str],
) -> str:
    return canonical_digest(
        {
            "corpus": corpus_digest,
            "implementation": implementation_digest,
            "model": model,
            "sources": source_provenance,
            "repositories": repositories,
            "scoring": scoring,
            "case_ids": case_ids,
        }
    )


def find_baseline(directory: Path, key: str) -> dict[str, Any] | None:
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json"), reverse=True):
        with contextlib.suppress(EvalError):
            candidate = read_json(path)
            if candidate.get("metadata", {}).get("baseline_key") == key:
                return candidate
    return None


def metric_deltas(
    current: dict[str, Any], baseline: dict[str, Any] | None
) -> dict[str, Any] | None:
    if baseline is None:
        return None
    deltas: dict[str, Any] = {}
    for group, summary in current["groups"].items():
        old = baseline.get("summary", {}).get("groups", {}).get(group, {})
        deltas[group] = {
            metric: round(
                float(value) - float(old.get("metrics", {}).get(metric, 0)),
                4,
            )
            for metric, value in summary["metrics"].items()
        }
    return deltas


def print_case_result(case_id: str, result: dict[str, Any]) -> None:
    metrics = result["metrics"]
    print(
        f"  {case_id}: {result['status'].upper()} "
        f"route={result.get('route')}/{result.get('expected_route')} "
        f"evidence={metrics['expected_evidence_hit_rate']:.2f} "
        f"citations={metrics['citation_presence']:.2f} "
        f"{result['duration_ms']}ms"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--nano-root", type=Path, default=NANO_ROOT)
    parser.add_argument(
        "--platform-root",
        type=Path,
        default=NANO_ROOT.parent / "intelligence-platform",
    )
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--model", help="NanoClaw fast model; defaults to local config")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-run-dir", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=480)
    parser.add_argument("--deep-timeout-ms", type=int, default=420000)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    corpus_path = args.corpus.expanduser().resolve()
    nano_root = args.nano_root.expanduser().resolve()
    platform_root = args.platform_root.expanduser().resolve()
    design_doc = args.design_doc.expanduser().resolve()
    corpus = read_json(corpus_path)
    validate_corpus(corpus)
    roots = {
        "design_doc": design_doc,
        "nano_root": nano_root,
        "platform_root": platform_root,
    }
    documents, source_provenance = source_documents(corpus, roots)
    env = child_environment(nano_root)
    model = resolve_model(env, args.model)
    available_keys = [name for name in PROVIDER_KEYS if env.get(name)]
    if not available_keys and not args.dry_run:
        raise EvalError("no supported provider credential is available for the live run")
    selected_cases = [
        case
        for case in corpus["cases"]
        if not args.case_ids or case["id"] in set(args.case_ids)
    ]
    if args.case_ids:
        missing = sorted(set(args.case_ids) - {case["id"] for case in selected_cases})
        if missing:
            raise EvalError(f"unknown --case ids: {missing}")

    corpus_digest = sha256_bytes(corpus_path.read_bytes())
    implementation_digest = canonical_digest(
        {
            "runner": sha256_bytes(Path(__file__).read_bytes()),
            "fault_injection": sha256_bytes((SCRIPT_DIR / "fault_injection.mjs").read_bytes()),
        }
    )
    repositories = {
        "nano_claw": git_fingerprint(nano_root),
        "intelligence_platform": git_fingerprint(platform_root),
    }
    scoring = corpus["_meta"]["scoring"]
    key = baseline_key(
        corpus_digest=corpus_digest,
        implementation_digest=implementation_digest,
        model=model,
        source_provenance=source_provenance,
        repositories=repositories,
        scoring=scoring,
        case_ids=[case["id"] for case in selected_cases],
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "corpus": str(corpus_path),
                    "corpus_digest": corpus_digest,
                    "implementation_digest": implementation_digest,
                    "cases": len(selected_cases),
                    "documents": len(documents),
                    "model": model,
                    "source_provenance": source_provenance,
                    "baseline_key": key,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not args.skip_build:
        subprocess.run(["npm", "run", "build"], cwd=nano_root, env=env, check=True)

    run_dir = Path(tempfile.mkdtemp(prefix="nano-cross-source-eval-"))
    processes: list[ManagedProcess] = []
    started_at = dt.datetime.now(dt.UTC)
    tenant_id = f"cross-source-eval-{started_at.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    platform_port = free_port()
    nano_port = free_port()
    platform_url = f"http://127.0.0.1:{platform_port}"
    nano_url = f"http://127.0.0.1:{nano_port}"
    try:
        platform_env = dict(env)
        platform_env["INTELLIGENCE_DB_PATH"] = str(run_dir / "intelligence.db")
        platform = start_process(
            "intelligence-platform",
            [
                "uv",
                "run",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(platform_port),
            ],
            cwd=platform_root,
            env=platform_env,
            log_path=run_dir / "platform.log",
        )
        processes.append(platform)
        wait_for_health(platform, f"{platform_url}/healthz")
        print(f"Isolated platform ready: tenant={tenant_id}")
        ingest_documents(platform_url, tenant_id, documents)

        eval_home = run_dir / "home"
        memory_dir = run_dir / "memory"
        write_nano_config(eval_home, model)
        nano_env = dict(env)
        nano_env.update(
            {
                "HOME": str(eval_home),
                "NANO_CLAW_MEMORY_DIR": str(memory_dir),
                "NANO_CLAW_INTELLIGENCE_URL": platform_url,
                "NANO_CLAW_INTELLIGENCE_ENABLED": "1",
                "NANO_CLAW_INTELLIGENCE_TENANT": tenant_id,
                "NANO_CLAW_INTELLIGENCE_COLLECTIONS": ",".join(
                    source["collection_id"] for source in corpus["sources"]
                ),
                "NANO_CLAW_INTELLIGENCE_PROFILE": "intelligence",
                "NANO_CLAW_INTELLIGENCE_GROUNDING": "strict",
                "NANO_CLAW_DEEP_REASONING": "1",
                "NANO_CLAW_DEEP_ROUTING": "auto",
                "NANO_CLAW_DEEP_CONFIRM": scoring["affirmation_policy"],
                "NANO_CLAW_DEEP_TIMEOUT_MS": str(args.deep_timeout_ms),
                "NANO_CLAW_DISABLE_TOOLS": "1",
                "NANO_CLAW_STREAM": "0",
                "NANO_CLAW_EVAL_TRACE": "1",
            }
        )
        nano = start_process(
            "nano-claw",
            ["node", "dist/cli/index.js", "serve", "--port", str(nano_port)],
            cwd=nano_root,
            env=nano_env,
            log_path=run_dir / "nano-claw.log",
        )
        processes.append(nano)
        wait_for_health(nano, f"{nano_url}/api/health")
        print(f"NanoClaw eval API ready: model={model}")

        case_results: list[dict[str, Any]] = []
        for case in selected_cases:
            print(f"\n[{case['group']}] {case['id']}")
            try:
                result = run_case(
                    case,
                    nano_url=nano_url,
                    model=model,
                    timeout=args.timeout_seconds,
                )
            except Exception as error:
                result = {
                    "passed": False,
                    "status": "infrastructure_error",
                    "route": None,
                    "expected_route": case["expected_route"],
                    "outcome": "infrastructure_error",
                    "error_code": type(error).__name__,
                    "detail": str(error),
                    "metrics": {
                        "expected_evidence_hit_rate": 0.0,
                        "citation_presence": 0.0,
                        "routing_correctness": 0.0,
                        "required_source_coverage": 0.0,
                        "coverage_disclaimer": 0.0,
                    },
                    "duration_ms": 0,
                    "citations": [],
                    "response_sha256": None,
                    "response_excerpt": "",
                    "setup": [],
                }
            result["id"] = case["id"]
            result["group"] = case["group"]
            case_results.append(result)
            print_case_result(case["id"], result)

        print("\n[failure_mode] invalid-analysis-artifact")
        fault = run_fault_case(
            nano_root=nano_root,
            env=nano_env,
            timeout=min(args.timeout_seconds, 60),
        )
        print(
            f"  invalid-analysis-artifact: {fault['status'].upper()} "
            f"error={fault.get('errorCode')}"
        )
        summary = aggregate(case_results, fault)
        scoreboard_dir = (args.output.parent if args.output else DEFAULT_SCOREBOARD_DIR).resolve()
        baseline = find_baseline(scoreboard_dir, key)
        deltas = metric_deltas(summary, baseline)
        completed_at = dt.datetime.now(dt.UTC)
        output_path = (
            args.output.expanduser().resolve()
            if args.output
            else scoreboard_dir / f"{completed_at.strftime('%Y-%m-%dT%H%M%SZ')}.json"
        )
        scoreboard = {
            "schema_version": 1,
            "metadata": {
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "corpus_path": str(corpus_path.relative_to(nano_root)),
                "corpus_version": corpus["_meta"]["corpus_version"],
                "corpus_digest": corpus_digest,
                "implementation_digest": implementation_digest,
                "trace_version": TRACE_VERSION,
                "baseline_key": key,
                "baseline_file": (
                    baseline.get("metadata", {}).get("scoreboard_file")
                    if baseline
                    else None
                ),
                "scoreboard_file": output_path.name,
                "tenant_id": tenant_id,
                "database_isolation": "fresh temporary SQLite database",
                "registry_control": (
                    "empty at run start; explicit fresh-analysis setup only for the "
                    "registry case"
                ),
                "model": model,
                "platform_reasoning_provider": platform_env.get(
                    "INTELLIGENCE_REASONING_PROVIDER", "configured-by-platform-env"
                ),
                "platform_reasoning_model": platform_env.get(
                    "INTELLIGENCE_REASONING_MODEL", "configured-by-platform-env"
                ),
                "provider_credentials_present": available_keys,
                "repetitions": scoring["repetitions"],
                "top_k": scoring["top_k"],
                "candidate_pool": scoring["candidate_pool"],
                "affirmation_policy": scoring["affirmation_policy"],
                "repositories": repositories,
                "source_provenance": source_provenance,
                "source_fingerprint": canonical_digest(source_provenance),
                "partial_case_filter": args.case_ids or [],
            },
            "cases": case_results,
            "failure_modes": [fault],
            "summary": summary,
            "deltas_vs_baseline": deltas,
        }
        write_json(output_path, scoreboard)
        print(f"\nScoreboard: {output_path}")
        if deltas is None:
            print("Delta: no comparable baseline")
        else:
            for group, group_deltas in deltas.items():
                rendered = ", ".join(
                    f"{name}={value:+.4f}" for name, value in group_deltas.items()
                )
                print(f"Delta {group}: {rendered}")
        for group, group_summary in summary["groups"].items():
            print(
                f"{group}: {group_summary['passed']}/{group_summary['total']} pass, "
                f"{group_summary['known_failed']} known fail, "
                f"{group_summary['failed']} unexpected fail, "
                f"{group_summary['infrastructure_errors']} infrastructure"
            )
        print(
            "Delivery gate: "
            + ("PASS" if summary["delivery_gate_passed"] else "FAIL")
        )
        return 0 if summary["delivery_gate_passed"] else 1
    finally:
        for process in reversed(processes):
            process.stop()
        if args.keep_run_dir:
            print(f"Run directory retained: {run_dir}")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
