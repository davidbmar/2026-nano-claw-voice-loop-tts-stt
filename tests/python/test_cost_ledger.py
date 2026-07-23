from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from voice import cost_ledger, metrics_db, phone, region_providers, server
from voice.region_providers import AnthropicProvider, OpenAICompatProvider


def _conn():
    path = os.path.join(tempfile.mkdtemp(), "costs.db")
    conn = metrics_db.init_db(path)
    assert conn is not None
    assert cost_ledger.ensure_schema(conn)
    return conn


def test_ledger_write_read_roundtrip_and_idempotence():
    conn = _conn()
    entries = [
        cost_ledger.LedgerEntry("telephony", 3.5, "minutes", 0.007),
        cost_ledger.LedgerEntry("stt", 90.0, "audio_seconds", 0.00001),
    ]

    assert cost_ledger.write_call(
        conn, "call-1", "Acme", "scheduler", entries, ts=1234.5
    )
    assert cost_ledger.write_call(
        conn, "call-1", "Acme", "scheduler", entries, ts=9999.0
    )

    rows = cost_ledger.read_entries(conn, "call-1")
    assert len(rows) == 2
    assert rows[0] == {
        "call_id": "call-1",
        "ts": 1234.5,
        "business": "Acme",
        "flow": "scheduler",
        "component": "telephony",
        "units": 3.5,
        "unit_kind": "minutes",
        "usd_per_unit_snapshot": 0.007,
    }
    assert [row[1] for row in conn.execute("PRAGMA table_info(cost_ledger)")] == [
        "call_id",
        "ts",
        "business",
        "flow",
        "component",
        "units",
        "unit_kind",
        "usd_per_unit_snapshot",
    ]


def test_aggregation_math_business_flows_and_customer_statistics():
    conn = _conn()
    caller = "+1 (555) 123-4567"
    metrics_db.record_call_start(conn, "call-a", caller, "+15125550100", "node-a")
    metrics_db.record_call_start(conn, "call-b", caller, "+15125550100", "node-a")

    assert cost_ledger.write_call(
        conn,
        "call-a",
        "Acme",
        "scheduler",
        [
            cost_ledger.LedgerEntry("telephony", 2, "minutes", 0.007),
            cost_ledger.LedgerEntry("scheduler_llm", 1000, "input_tokens", 0.000001),
            cost_ledger.LedgerEntry("scheduler_llm", 100, "output_tokens", 0.000005),
            cost_ledger.LedgerEntry("stt", 60, "audio_seconds", 0.00001),
            cost_ledger.LedgerEntry("tts", 500, "characters", 0.000001),
            cost_ledger.LedgerEntry("infra", 2, "call_minutes", 0.002),
        ],
    )
    assert cost_ledger.write_call(
        conn,
        "call-b",
        "Acme",
        "conversation",
        [
            cost_ledger.LedgerEntry("telephony", 4, "minutes", 0.007),
            cost_ledger.LedgerEntry("infra", 4, "call_minutes", 0.002),
        ],
    )

    report = cost_ledger.build_report(conn, hash_salt="test-salt")

    # Variable: .014 + .001 + .0005 + .0006 + .0005 + .004 + .028 + .008
    assert report["totals"]["variableUsd"] == pytest.approx(0.0566)
    assert report["totals"]["fixedUsd"] == pytest.approx(1.0)
    assert report["totals"]["usd"] == pytest.approx(1.0566)
    assert report["totals"]["calls"] == 2
    assert report["totals"]["minutes"] == 6
    assert report["byComponent"]["telephony"]["perMin"] == pytest.approx(0.007)

    business = report["businesses"][0]
    assert business["name"] == "Acme"
    assert business["calls"] == 2
    assert business["customers"] == 1
    assert business["callsPerCustomer"] == 2
    assert (business["minMin"], business["medMin"], business["maxMin"]) == (2, 3, 4)
    assert {flow["flow"]: flow["share"] for flow in business["flows"]} == {
        "conversation": pytest.approx(4 / 6),
        "scheduler": pytest.approx(2 / 6),
    }

    assert len(report["customers"]) == 1
    customer = report["customers"][0]
    assert customer["calls"] == 2
    assert customer["totalMin"] == 6
    assert caller not in json.dumps(report)


def test_malformed_pricing_falls_back_without_crashing(tmp_path):
    pricing = tmp_path / "pricing.json"
    pricing.write_text("{ definitely not json", encoding="utf-8")
    conn = _conn()

    assert cost_ledger.load_pricing(pricing) is None
    report = cost_ledger.build_report(conn, pricing_path=pricing)

    assert report["status"] == "pricing_unavailable"
    assert report["message"] == "pricing unavailable"
    assert report["pricing"]["available"] is False
    assert report["models"] == []


def test_caller_hash_is_stable_salted_and_never_contains_number():
    caller = "+15551234567"
    first = cost_ledger.hash_caller(caller, salt="one")
    same = cost_ledger.hash_caller("+1 (555) 123-4567", salt="one")
    other_salt = cost_ledger.hash_caller(caller, salt="two")

    assert first == same
    assert first != other_salt
    assert first.startswith("cust_")
    assert caller not in first


def test_anthropic_provider_accumulates_retry_safe_usage():
    response = {
        "content": [{"type": "text", "text": '{"ok": true}'}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
        },
    }

    class Messages:
        def create(self, **request):
            return response

    class Client:
        messages = Messages()

    provider = AnthropicProvider("claude-haiku-4-5", client=Client())
    provider.complete("system", [], {"type": "object"}, 100)
    provider.complete("system", [], {"type": "object"}, 100)

    assert provider.drain_usage() == {
        "prompt": 280.0,
        "completion": 40.0,
        "cacheRead": 60.0,
        "cacheWrite": 20.0,
    }
    assert provider.drain_usage() == {}


def test_openai_compatible_provider_captures_cached_usage(monkeypatch):
    payload = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    }

    class Response:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def close(self):
            return None

    monkeypatch.setattr(region_providers.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    provider = OpenAICompatProvider(
        provider="test",
        model="model",
        base_url="https://example.test/v1",
        api_key_env="TEST_KEY",
        _api_key="key",
    )

    assert provider.complete("system", [], {}, 100) == ("ok", "stop")
    assert provider.drain_usage() == {
        "prompt": 100.0,
        "completion": 25.0,
        "cacheRead": 40.0,
        "cacheWrite": 0.0,
    }


def test_empty_database_api_and_cost_page_render(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(server, "METRICS", conn)
    monkeypatch.setattr(phone, "_metrics_conn", None)
    monkeypatch.setenv("NANO_CLAW_PHONE", "0")

    app = server.create_app()
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/api/costs" in paths
    assert "/costs" in paths

    response = asyncio.run(server.costs_handler(None))
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["status"] == "awaiting_call_data"
    assert payload["totals"]["calls"] == 0
    assert len(payload["models"]) == 6

    response = asyncio.run(server.costs_page_handler(None))
    assert response.status == 200
    html = Path(response._path).read_text(encoding="utf-8")
    assert "Voice AI Cost Console" in html
    assert 'fetch("/api/costs"' in html
    assert "Awaiting call data" in html


def test_prepared_speechchunk_is_billed_without_crashing(monkeypatch):
    # Regression: in prepared-speech mode the synthesis unit is a SpeechChunk,
    # not a str. The billing wrapper called len(sentence) and crashed with
    # "object of type 'SpeechChunk' has no len()", killing phone TTS -> silence.
    import types
    import asyncio
    from voice import cost_ledger
    from voice.speech_preparer import SpeechChunk

    billed = []
    monkeypatch.setattr(cost_ledger, "add_units",
                        lambda call_id, kind, amount, unit: billed.append((kind, amount)))

    class BasePhoneCall:
        async def _synthesize_sentence(self, sentence):
            return b"pcm"

    fake_phone = types.SimpleNamespace(
        PhoneCall=BasePhoneCall,
        PROCESSING_CUE_SENTINEL="\0cue\0",
        phone_rate=lambda: 8000,
    )
    cost_ledger._phone_conn_getter = None
    cost_ledger.install_phone_tracking(fake_phone, lambda: None)

    # Build the tracked call without its network-touching __init__.
    call = fake_phone.PhoneCall.__new__(fake_phone.PhoneCall)
    call.call_id = "cc-bill"

    chunk = SpeechChunk(chunk_id="c1", sequence=0, text="Hello there.",
                        kind="statement", estimated_duration_ms=500,
                        pause_after_ms=140, is_final=True)
    result = asyncio.new_event_loop().run_until_complete(call._synthesize_sentence(chunk))
    assert result == b"pcm"
    assert billed == [(cost_ledger.TTS, len("Hello there."))]
