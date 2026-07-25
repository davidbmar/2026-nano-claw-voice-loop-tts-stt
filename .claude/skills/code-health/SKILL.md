---
name: code-health
description: Run the nano-claw code-health analyzer — silent-exception audit, long-function/big-file report, debt markers, trend history. Use at the start of every cleanup-loop iteration to pick the next target, and after changes to prove the trend improved.
---

# code-health

The measurement half of the cleanup loop. One command:

```bash
python3 scripts/code_health.py          # human summary
python3 scripts/code_health.py --json   # full snapshot to stdout
```

Outputs (both git-tracked so trends survive across sessions):

- `docs/metrics/code-health.json` — full current snapshot (per-file detail)
- `docs/metrics/code-health-history.jsonl` — one totals line per run; diff
  the last two lines to prove a cleanup moved the numbers

## How to use it in a loop iteration

1. Run it. Read the summary blocks in priority order:
   **silent excepts** (failures vanishing without a trace — fix these first)
   → **swallowed catches** → **longest functions** → **biggest files** →
   debt markers.
2. Pick ONE bounded target. Prefer the smallest change that deletes a
   whole finding (add logging to a silent handler, split one function).
3. After the change: full test suites
   (`PYTHONPATH=/opt/homebrew/lib/python3.14/site-packages .venv-test/bin/python -m pytest tests/python -q`
   and `npx vitest run`), then re-run this analyzer and confirm the totals
   moved the right way before committing.

## Reading caveats

- TS/JS analysis is heuristic (regex catch-block scan); Python analysis is
  AST-accurate. A "swallowed catch" hit in JS deserves a manual read before
  filing — comments mentioning `log` inside the block will suppress the hit.
- Runtime latency metrics are a different tool: `curl -s localhost:9090/api/metrics`
  (per-turn stt/ttft/tts/e2e) — pair code health with runtime health when
  choosing targets.
