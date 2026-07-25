#!/usr/bin/env python3
"""Code-health metrics for nano-claw — the measurement half of the cleanup loop.

Analyzes the Python voice server (AST-accurate) and the TypeScript agent +
console JS (heuristic) for the defect classes that actually bite this repo:
silent exception handlers, oversized functions/files, and tracked debt
markers. Emits a human summary on stdout, a machine-readable snapshot at
docs/metrics/code-health.json, and appends one trend line per run to
docs/metrics/code-health-history.jsonl so successive loop iterations can
prove the trend is improving rather than assert it.

Usage: python3 scripts/code_health.py [--json]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_TARGETS = ["voice"]
TS_TARGETS = ["src"]
JS_TARGETS = ["voice/web"]
SKIP_DIRS = {"node_modules", "dist", ".venv", "__pycache__", "vendor"}
DEBT_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
LONG_FUNCTION_LINES = 60
BIG_FILE_LINES = 800


def iter_files(targets: list[str], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        base = ROOT / target
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in suffixes or not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


# An explicit `health-ok: <reason>` comment inside a handler declares the
# silence intentional (e.g. best-effort cleanup); the analyzer then treats it
# as audited intent rather than a finding. The reason is mandatory culture,
# not syntax — reviewers should reject bare markers.
HEALTH_OK_MARKER = "health-ok"


def handler_is_silent(handler: ast.ExceptHandler, source_lines: list[str]) -> bool:
    """A handler is silent when nothing in its body logs, re-raises, or
    otherwise surfaces the failure. `pass`-only bodies are the classic case."""
    start = handler.lineno - 1
    end = handler.end_lineno or handler.lineno
    if any(HEALTH_OK_MARKER in line for line in source_lines[start:end]):
        return False
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return False
        if isinstance(node, ast.Return) and node.value is not None:
            # Returning an HTTP error response IS surfacing the failure —
            # the caller sees a 400/JSON error, nothing vanished. Returning
            # a bare default value is still silent and still flagged.
            returned = ast.unparse(node.value) if hasattr(ast, "unparse") else ""
            if "Response" in returned or "json_response" in returned:
                return False
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
            if any(tok in name for tok in ("log", "print", "warn", "error", "exception")):
                return False
    return True


def analyze_python(path: Path) -> dict:
    text = path.read_text(errors="replace")
    lines = text.count("\n") + 1
    out = {
        "file": str(path.relative_to(ROOT)),
        "lines": lines,
        "long_functions": [],
        "broad_excepts": 0,
        "silent_excepts": [],
        "debt": len(DEBT_RE.findall(text)),
    }
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        out["parse_error"] = str(error)
        return out
    source_lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > LONG_FUNCTION_LINES:
                out["long_functions"].append(
                    {"name": node.name, "line": node.lineno, "length": length}
                )
        elif isinstance(node, ast.ExceptHandler):
            is_broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")
            )
            if is_broad:
                out["broad_excepts"] += 1
            if handler_is_silent(node, source_lines):
                out["silent_excepts"].append({"line": node.lineno})
    return out


CATCH_RE = re.compile(r"catch\s*(\([^)]*\))?\s*\{")


def catch_block_body(text: str, open_brace: int) -> str:
    depth = 0
    for i in range(open_brace, min(len(text), open_brace + 4000)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace : i + 1]
    return text[open_brace : open_brace + 4000]


def analyze_ts(path: Path) -> dict:
    text = path.read_text(errors="replace")
    lines = text.count("\n") + 1
    swallowed = []
    for match in CATCH_RE.finditer(text):
        body = catch_block_body(text, match.end() - 1)
        if HEALTH_OK_MARKER in body:
            continue
        if not re.search(r"\b(log|logger|console|throw|reject|warn|error)\b", body):
            swallowed.append({"line": text[: match.start()].count("\n") + 1})
    return {
        "file": str(path.relative_to(ROOT)),
        "lines": lines,
        "catches": len(CATCH_RE.findall(text)),
        "swallowed_catches": swallowed,
        "any_types": len(re.findall(r":\s*any\b", text)),
        "debt": len(DEBT_RE.findall(text)),
    }


def main() -> int:
    py = [analyze_python(p) for p in iter_files(PY_TARGETS, (".py",))]
    ts = [analyze_ts(p) for p in iter_files(TS_TARGETS, (".ts",))]
    js = [analyze_ts(p) for p in iter_files(JS_TARGETS, (".js", ".mjs"))]

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": py,
        "typescript": ts,
        "console_js": js,
        "totals": {
            "py_files": len(py),
            "py_lines": sum(f["lines"] for f in py),
            "py_long_functions": sum(len(f["long_functions"]) for f in py),
            "py_broad_excepts": sum(f["broad_excepts"] for f in py),
            "py_silent_excepts": sum(len(f["silent_excepts"]) for f in py),
            "ts_files": len(ts) + len(js),
            "ts_lines": sum(f["lines"] for f in ts + js),
            "ts_swallowed_catches": sum(len(f["swallowed_catches"]) for f in ts + js),
            "ts_any_types": sum(f["any_types"] for f in ts),
            "debt_markers": sum(f["debt"] for f in py + ts + js),
        },
    }

    metrics_dir = ROOT / "docs" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "code-health.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    with open(metrics_dir / "code-health-history.jsonl", "a") as history:
        history.write(
            json.dumps({"ts": snapshot["generated_at"], **snapshot["totals"]}) + "\n"
        )

    if "--json" in sys.argv:
        print(json.dumps(snapshot, indent=2))
        return 0

    t = snapshot["totals"]
    print(f"code-health @ {snapshot['generated_at']}")
    print(
        f"  python:     {t['py_files']} files / {t['py_lines']} lines | "
        f"long fns {t['py_long_functions']} | broad excepts {t['py_broad_excepts']} | "
        f"SILENT excepts {t['py_silent_excepts']}"
    )
    print(
        f"  ts+console: {t['ts_files']} files / {t['ts_lines']} lines | "
        f"swallowed catches {t['ts_swallowed_catches']} | any-types {t['ts_any_types']}"
    )
    print(f"  debt markers (TODO/FIXME/XXX/HACK): {t['debt_markers']}")

    def top(items: list[dict], key, n=5):
        return sorted(items, key=key, reverse=True)[:n]

    print("\n  biggest files:")
    for f in top(py + ts + js, lambda f: f["lines"]):
        flag = "  <-- over budget" if f["lines"] > BIG_FILE_LINES else ""
        print(f"    {f['lines']:6d}  {f['file']}{flag}")
    print("\n  longest python functions:")
    longest = [
        {**fn, "file": f["file"]} for f in py for fn in f["long_functions"]
    ]
    for fn in top(longest, lambda fn: fn["length"]):
        print(f"    {fn['length']:4d}ln  {fn['file']}:{fn['line']}  {fn['name']}")
    silent = [
        {"file": f["file"], **s} for f in py for s in f["silent_excepts"]
    ]
    if silent:
        print("\n  silent python excepts (failure disappears without a trace):")
        for s in silent[:10]:
            print(f"    {s['file']}:{s['line']}")
    swallowed = [
        {"file": f["file"], **s} for f in ts + js for s in f["swallowed_catches"]
    ]
    if swallowed:
        print("\n  swallowed ts/js catches:")
        for s in swallowed[:10]:
            print(f"    {s['file']}:{s['line']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
