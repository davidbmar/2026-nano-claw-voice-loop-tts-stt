"""Provider seam for goal-region supervisor completions.

This module mirrors riff's ``riff.region_providers`` implementation for the
nano-claw phone test bed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol


class SupervisorProvider(Protocol):
    """Minimal transport contract consumed by ``GoalRegionRunner``."""

    provider: str
    model: str

    def complete(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int,
    ) -> tuple[str, str | None]:
        """Return raw response text and a normalized stop reason."""


@dataclass
class AnthropicProvider:
    """The existing Anthropic messages API request behind the provider seam."""

    model: str
    client: object | None = None
    provider: str = field(default="anthropic", init=False)
    api_key_env: str = field(default="ANTHROPIC_API_KEY", init=False)

    @property
    def provider_name(self) -> str:
        return self.provider

    def complete(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int,
    ) -> tuple[str, str | None]:
        request: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": messages,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        }
        # Preserve the supervisor's existing latency knob exactly. The caller
        # simulator uses ``complete_text`` and intentionally does not add it.
        if os.environ.get("SCHED_EVAL_THINKING", "").strip() == "disabled":
            request["thinking"] = {"type": "disabled"}
        return self._send(request)

    def complete_text(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int,
    ) -> tuple[str, str | None]:
        """Make the eval caller's existing plain-text Anthropic request."""
        request = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": messages,
        }
        response = self.ensure_client().messages.create(**request)
        return _anthropic_plain_text(response), _anthropic_stop_reason(response)

    def _send(self, request: dict) -> tuple[str, str | None]:
        client = self.ensure_client()
        response = client.messages.create(**request)
        return _anthropic_response_text(response), _anthropic_stop_reason(response)

    def ensure_client(self):
        """Construct the SDK client lazily when one was not injected."""
        if self.client is None:
            import anthropic

            self.client = anthropic.Anthropic()
        return self.client


@dataclass
class OpenAICompatProvider:
    """OpenAI-compatible chat completions over the Python standard library."""

    provider: str
    model: str
    base_url: str
    api_key_env: str
    _api_key: str = field(repr=False)
    timeout_s: float = 30.0

    @property
    def provider_name(self) -> str:
        return self.provider

    def complete(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int,
    ) -> tuple[str, str | None]:
        schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        schema_system = (
            f"{system}\n\nRespond with a single JSON object matching this schema: "
            f"{schema_json}"
        )
        if self.provider == "xai":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "region_turn",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
        return self._chat_completion(
            system=schema_system,
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def complete_text(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int,
    ) -> tuple[str, str | None]:
        """Make a plain-text request for the evaluation's caller model."""
        return self._chat_completion(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            response_format=None,
        )

    def _chat_completion(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        response_format: dict | None,
    ) -> tuple[str, str | None]:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *messages,
            ],
        }
        if response_format is not None:
            payload["response_format"] = response_format
        # GPT-OSS models route answers into a reasoning channel by default;
        # low effort keeps the JSON in `content` (and keeps turns fast).
        if "gpt-oss" in self.model:
            payload["reasoning_effort"] = "low"
        # A real User-Agent matters: Groq's edge 403s urllib's default UA.
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "nano-claw-goal-region/1.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        raw_response = None
        # One polite retry on 429: free-tier providers (Groq) rate-limit
        # bursts; honoring Retry-After turns a dead scenario into a slow turn.
        for attempt in range(2):
            try:
                response = urllib.request.urlopen(request, timeout=self.timeout_s)
                try:
                    raw_response = response.read()
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 0:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(float(retry_after), 30.0) if retry_after else 2.0
                    except ValueError:
                        delay = 2.0
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"{self.provider} supervisor request failed with HTTP {exc.code}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(
                    f"{self.provider} supervisor request failed: {exc}"
                ) from exc

        try:
            response_payload = json.loads(raw_response.decode("utf-8"))
            choice = response_payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(
                f"{self.provider} supervisor returned an invalid chat completion"
            ) from exc
        if not isinstance(content, str):
            raise RuntimeError(
                f"{self.provider} supervisor returned non-text message content"
            )
        if finish_reason == "length":
            finish_reason = "max_tokens"
        if not isinstance(finish_reason, str):
            finish_reason = None
        return content, finish_reason


_OPENAI_COMPAT_SPECS = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "qwen": (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
    ),
    "local": ("http://localhost:11434/v1", "LOCAL_LLM_API_KEY"),
    # Groq model IDs contain their own slashes (openai/gpt-oss-20b), so
    # routing must split only on the first separator.
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    # Marketplace router — model ids keep their own slashes
    # (openrouter/openai/gpt-oss-20b); ":nitro" suffix routes to the
    # fastest live backend.
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


def resolve_supervisor(model: str) -> SupervisorProvider:
    """Resolve a prefixed model to its supervisor transport.

    Bare model names retain the Anthropic messages API. A slash opts into the
    explicit provider-prefix convention, so unknown prefixes fail closed.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("supervisor model must be a non-empty string")
    requested_model = model.strip()
    if "/" not in requested_model:
        return AnthropicProvider(model=requested_model)

    prefix, provider_model = requested_model.split("/", 1)
    spec = _OPENAI_COMPAT_SPECS.get(prefix)
    if spec is None:
        raise ValueError(f"unknown supervisor provider prefix: {prefix}")
    if not provider_model.strip():
        raise ValueError(f"{prefix}/ requires a provider model name")
    base_url, api_key_env = spec
    if prefix == "local":
        base_url = (
            os.environ.get("LOCAL_LLM_BASE_URL", "").strip()
            or "http://localhost:11434/v1"
        )
    api_key = os.environ.get(api_key_env, "").strip()
    if prefix != "local" and not api_key:
        raise ValueError(
            f"{api_key_env} is required for {prefix}/ supervisor models"
        )
    return OpenAICompatProvider(
        provider=prefix,
        model=provider_model.strip(),
        base_url=base_url,
        api_key_env=api_key_env,
        _api_key=api_key,
    )


def _anthropic_response_text(response) -> str:
    content = _anthropic_content(response)
    for block in content:
        text = (
            block.get("text")
            if isinstance(block, dict)
            else getattr(block, "text", None)
        )
        if not isinstance(text, str):
            continue
        # The old parser stopped on malformed JSON, skipped valid non-object
        # JSON blocks, and accepted the first object. Preserve that selection
        # behavior while returning the provider contract's raw text.
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if isinstance(parsed, dict):
            return text
    return ""


def _anthropic_plain_text(response) -> str:
    for block in _anthropic_content(response):
        text = (
            block.get("text")
            if isinstance(block, dict)
            else getattr(block, "text", None)
        )
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _anthropic_content(response):
    if isinstance(response, dict):
        return response.get("content", [])
    return response.content


def _anthropic_stop_reason(response) -> str | None:
    if isinstance(response, dict):
        value = response.get("stop_reason")
    else:
        value = getattr(response, "stop_reason", None)
    return value if isinstance(value, str) else None


# Descriptive aliases keep the implementation names discoverable without
# widening the transport contract itself.
AnthropicSupervisorProvider = AnthropicProvider
OpenAICompatSupervisorProvider = OpenAICompatProvider
