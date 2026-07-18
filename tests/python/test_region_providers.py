from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime

import pytest

from scripts.scheduling_eval import run_eval
from voice import region_providers
from voice.goal_region import FreeWindow, GoalRegionRunner, RegionConfig
from voice.region_providers import (
    AnthropicProvider,
    OpenAICompatProvider,
    resolve_supervisor,
)


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class FakeURLopener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("fake urllib transport has no queued response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeHTTPResponse(response)


class FakeAnthropicMessages:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **request):
        self.calls.append(request)
        return self.response


class FakeAnthropicClient:
    def __init__(self, response: dict):
        self.messages = FakeAnthropicMessages(response)


@pytest.mark.parametrize(
    (
        "requested_model",
        "provider_name",
        "wire_model",
        "expected_base_url",
        "key_env",
    ),
    [
        (
            "deepseek/deepseek-chat",
            "deepseek",
            "deepseek-chat",
            "https://api.deepseek.com/v1",
            "DEEPSEEK_API_KEY",
        ),
        (
            "xai/grok-4-1-fast",
            "xai",
            "grok-4-1-fast",
            "https://api.x.ai/v1",
            "XAI_API_KEY",
        ),
        (
            "qwen/qwen-plus",
            "qwen",
            "qwen-plus",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "DASHSCOPE_API_KEY",
        ),
    ],
)
def test_prefix_routing_resolves_openai_compat_provider(
    monkeypatch,
    requested_model,
    provider_name,
    wire_model,
    expected_base_url,
    key_env,
):
    monkeypatch.setenv(key_env, "test-key")

    provider = resolve_supervisor(requested_model)

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.provider == provider_name
    assert provider.model == wire_model
    assert provider.base_url == expected_base_url
    assert provider.api_key_env == key_env
    assert provider.timeout_s == 30.0


def test_bare_model_uses_anthropic_and_unknown_prefix_fails():
    provider = resolve_supervisor("claude-haiku-4-5")

    assert isinstance(provider, AnthropicProvider)
    assert provider.provider == "anthropic"
    assert provider.model == "claude-haiku-4-5"
    assert provider.client is None
    with pytest.raises(ValueError, match="unknown supervisor provider prefix: foo"):
        resolve_supervisor("foo/bar")


@pytest.mark.parametrize(
    ("model", "key_env"),
    [
        ("deepseek/deepseek-chat", "DEEPSEEK_API_KEY"),
        ("xai/grok-4-1-fast", "XAI_API_KEY"),
        ("qwen/qwen-plus", "DASHSCOPE_API_KEY"),
    ],
)
def test_prefixed_provider_requires_its_environment_key(monkeypatch, model, key_env):
    monkeypatch.delenv(key_env, raising=False)

    with pytest.raises(ValueError, match=key_env):
        resolve_supervisor(model)


def _config() -> RegionConfig:
    return RegionConfig(
        goal="Book a valid appointment.",
        persona="You are a concise scheduler.",
        digest="Monday 09:00-12:00 is free.",
        slots={
            "job": {"required": True},
            "slot_start": {"required": True},
            "duration_minutes": {"required": True, "values": [30, 60]},
        },
        escape_phrases=("operator",),
        max_turns=6,
        deadline_s=60.0,
    )


def _windows() -> list[FreeWindow]:
    return [
        FreeWindow(
            datetime(2026, 7, 20, 9, 0),
            datetime(2026, 7, 20, 12, 0),
        )
    ]


def _region_payload() -> dict:
    return {
        "reply": "You are booked.",
        "slot_candidates": {
            "job": "leaking sink",
            "slot_start": "2026-07-20T09:00:00",
            "duration_minutes": 60,
        },
        "exit_candidate": "booked",
        "evidence": "caller accepted Monday at nine",
    }


def _chat_response(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }]
    }


def test_openai_compat_engine_turn_posts_schema_json_and_books(monkeypatch):
    monkeypatch.setenv("SCHED_EVAL_MODEL", "deepseek/deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    transport = FakeURLopener([_chat_response(json.dumps(_region_payload()))])
    monkeypatch.setattr(region_providers.urllib.request, "urlopen", transport)
    runner = GoalRegionRunner(_config(), _windows())

    result = runner.turn("Book my leaking sink visit Monday at nine for an hour.")

    assert result.exit == "booked"
    assert result.reply == ""
    assert result.slots == {
        "job": "leaking sink",
        "slot_start": "2026-07-20T09:00:00",
        "duration_minutes": 60,
    }
    assert result.rejected == []
    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    assert request.full_url == "https://api.deepseek.com/v1/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer deepseek-test-key"
    assert timeout == 30.0
    wire_payload = json.loads(request.data)
    assert wire_payload["model"] == "deepseek-chat"
    assert wire_payload["max_tokens"] == 4096
    assert wire_payload["response_format"] == {"type": "json_object"}
    assert "temperature" not in wire_payload
    assert wire_payload["messages"][1:] == [{
        "role": "user",
        "content": "Book my leaking sink visit Monday at nine for an hour.",
    }]
    system = wire_payload["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("You are a concise scheduler.")
    assert "Respond with a single JSON object matching this schema:" in system[
        "content"
    ]
    assert '"additionalProperties":false' in system["content"]


def test_malformed_openai_content_retries_then_degrades(monkeypatch):
    monkeypatch.setenv("SCHED_EVAL_MODEL", "deepseek/deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    transport = FakeURLopener([
        _chat_response("not json"),
        _chat_response("{still not json"),
    ])
    monkeypatch.setattr(region_providers.urllib.request, "urlopen", transport)
    runner = GoalRegionRunner(_config(), _windows())

    result = runner.turn("Monday morning would be good.")

    assert len(transport.calls) == 2
    assert result.reply == "Sorry — could you say that again?"
    assert result.exit is None
    assert result.slots == {}
    assert result.rejected == ["supervisor: unparseable output (after retry)"]


def test_openai_length_finish_reason_triggers_truncation_retry(monkeypatch):
    monkeypatch.setenv("SCHED_EVAL_MODEL", "xai/grok-4-1-fast")
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    content = json.dumps(_region_payload())
    transport = FakeURLopener([
        _chat_response(content, finish_reason="length"),
        _chat_response(content),
    ])
    monkeypatch.setattr(region_providers.urllib.request, "urlopen", transport)
    runner = GoalRegionRunner(_config(), _windows())

    result = runner.turn("Monday at nine works.")

    assert len(transport.calls) == 2
    assert result.exit == "booked"


def test_openai_network_error_is_a_clear_runtime_error(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    transport = FakeURLopener([urllib.error.URLError("offline")])
    monkeypatch.setattr(region_providers.urllib.request, "urlopen", transport)
    provider = resolve_supervisor("qwen/qwen-plus")

    with pytest.raises(RuntimeError, match=r"qwen supervisor request failed.*offline"):
        provider.complete("system", [], {"type": "object"}, 100)


def test_eval_caller_uses_plain_openai_compat_request(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    transport = FakeURLopener([_chat_response("Tuesday afternoon works.")])
    monkeypatch.setattr(region_providers.urllib.request, "urlopen", transport)

    caller_text = run_eval._caller_completion(
        None,
        model="xai/grok-4-1-fast",
        system="Act as the caller.",
        messages=[{"role": "user", "content": "When works?"}],
        max_tokens=160,
    )

    assert caller_text == "Tuesday afternoon works."
    request, _timeout = transport.calls[0]
    payload = json.loads(request.data)
    assert payload == {
        "model": "grok-4-1-fast",
        "max_tokens": 160,
        "messages": [
            {"role": "system", "content": "Act as the caller."},
            {"role": "user", "content": "When works?"},
        ],
    }


def test_eval_caller_preserves_plain_anthropic_request():
    client = FakeAnthropicClient({
        "content": [{"type": "text", "text": '"Monday morning works."'}],
        "stop_reason": "end_turn",
    })

    caller_text = run_eval._caller_completion(
        client,
        model="claude-haiku-4-5",
        system="Act as the caller.",
        messages=[{"role": "user", "content": "When works?"}],
        max_tokens=160,
    )

    assert caller_text == "Monday morning works."
    assert client.messages.calls == [{
        "model": "claude-haiku-4-5",
        "max_tokens": 160,
        "system": [{
            "type": "text",
            "text": "Act as the caller.",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{"role": "user", "content": "When works?"}],
    }]


def test_eval_results_record_full_and_resolved_models(monkeypatch, tmp_path):
    supervisor_model = "deepseek/deepseek-chat"
    caller_model = "xai/grok-4-1-fast"
    monkeypatch.setenv("SCHED_EVAL_MODEL", supervisor_model)
    monkeypatch.setenv("SCHED_EVAL_CALLER_MODEL", caller_model)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    availability_path = tmp_path / "availability.json"
    results_path = tmp_path / "results.json"
    availability_path.write_text("{}")
    monkeypatch.setattr(run_eval, "AVAILABILITY_PATH", availability_path)
    monkeypatch.setattr(run_eval, "RESULTS_PATH", results_path)
    monkeypatch.setattr(run_eval, "_load_scenarios", lambda: [{"id": "one"}])
    monkeypatch.setattr(
        run_eval,
        "run_scenario",
        lambda client, availability, scenario: {
            "id": scenario["id"],
            "passed": True,
            "supervisor_samples_ms": [],
        },
    )
    monkeypatch.setattr(run_eval, "_print_table", lambda results: None)

    assert run_eval.main() == 0

    output = json.loads(results_path.read_text())
    assert output["supervisor_model"] == supervisor_model
    assert output["supervisor_provider"] == "deepseek"
    assert output["supervisor_resolved_model"] == "deepseek-chat"
    assert output["caller_model"] == caller_model
    assert output["caller_provider"] == "xai"
    assert output["caller_resolved_model"] == "grok-4-1-fast"


def test_eval_prefixed_model_without_its_key_exits_two_before_network():
    env = os.environ.copy()
    env["SCHED_EVAL_MODEL"] = "deepseek/deepseek-chat"
    env["SCHED_EVAL_CALLER_MODEL"] = "deepseek/deepseek-chat"
    env.pop("DEEPSEEK_API_KEY", None)

    completed = subprocess.run(
        [sys.executable, str(run_eval.__file__)],
        cwd=run_eval.ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.count("\n") == 1
    assert "DEEPSEEK_API_KEY" in completed.stderr
