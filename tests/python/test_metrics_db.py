import os, tempfile
from voice import metrics_db as m


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "t.db")


def test_init_seeds_prices_and_is_idempotent():
    p = _tmp()
    c1 = m.init_db(p); assert c1 is not None
    n1 = c1.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert n1 == len(m.SEED_PRICES)
    c2 = m.init_db(p)  # again — must not duplicate
    n2 = c2.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert n2 == len(m.SEED_PRICES)


def test_estimate_cost_math_and_unpriced():
    c = m.init_db(_tmp())
    # gemini flash-lite 0.10 in / 0.40 out per 1M
    cost = m.estimate_cost(c, "gemini/gemini-flash-lite-latest", 1_000_000, 1_000_000)
    assert abs(cost - (0.10 + 0.40)) < 1e-9
    assert m.estimate_cost(c, "no/such-model", 100, 100) is None


def test_record_and_recent_and_aggregates():
    c = m.init_db(_tmp())
    m.record_turn(c, {"ts": "2026-07-15T10:00:00", "model": "gemini/gemini-flash-lite-latest",
                      "provider": "gemini", "llm_ttft_ms": 300, "tok_per_sec": 50.0,
                      "e2e_ms": 600, "tokens_in": 4800, "tokens_out": 7200, "est_cost_usd": 0.003,
                      "asked_text": "hi", "said_text": "hello"})
    m.record_turn(c, {"ts": "2026-07-15T10:01:00", "model": "gemini/gemini-flash-lite-latest",
                      "provider": "gemini", "llm_ttft_ms": 500, "tok_per_sec": 40.0, "e2e_ms": 800})
    r = m.recent(c)
    assert len(r) == 2 and r[0]["said_text"] in ("hello", None)
    agg = m.aggregates(c)
    row = next(a for a in agg if a["model"] == "gemini/gemini-flash-lite-latest")
    assert row["n"] == 2
    assert abs(row["avg_ttft_ms"] - 400) < 1e-6  # (300+500)/2


def test_record_turn_is_best_effort():
    m.record_turn(None, {"model": "x"})  # no conn → no-op, no raise
    c = m.init_db(_tmp())
    m.record_turn(c, {"unknown_column": 1})  # bad rec → swallowed, no raise
