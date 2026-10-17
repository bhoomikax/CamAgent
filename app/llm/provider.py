"""LLM provider interface and the Anthropic-backed implementation.

The intent parser depends only on `LLMProvider`, so tests can substitute a
fake provider and never make network calls. The Anthropic SDK is imported
lazily so the rest of CamAgent works without it installed.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Protocol

DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ENV_VAR = "CAMAGENT_MODEL"


class LLMProviderError(RuntimeError):
    """Raised when the provider cannot return a usable completion."""


class LLMProvider(Protocol):
    """Anything that can turn a system prompt and user text into raw text."""

    def complete(
        self,
        system: str,
        prompt: str,
        response_schema: Mapping[str, Any] | None = None,
    ) -> str: ...


class AnthropicProvider:
    """Call Claude through the official Anthropic SDK.

    Credentials are resolved by the SDK (``ANTHROPIC_API_KEY`` and friends).
    The model defaults to ``claude-opus-5-5`` and can be overridden with the
    ``CAMAGENT_MODEL`` environment variable or the ``model`` argument.
    """

    def __init__(
        self,
        model: str | None = None,
        client: Any = None,
        max_tokens: int = 1024,
    ) -> None:
        self.model = model or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMProviderError(
                    "the 'anthropic' package is required for AnthropicProvider; "
                    "install it with 'pip install anthropic'"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def complete(
        self,
        system: str,
        prompt: str,
        response_schema: Mapping[str, Any] | None = None,
    ) -> str:
        client = self._get_client()
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            # Intent classification is a short, simple task.
            "output_config": {"effort": "low"},
            # On a safety refusal, let the API retry on a fallback model.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if response_schema is not None:
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": dict(response_schema),
            }

        try:
            response = client.beta.messages.create(**request)
        except Exception as exc:  # SDK errors vary by version; surface uniformly.
            raise LLMProviderError(f"LLM request failed: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMProviderError("LLM declined to answer the request")
        if response.stop_reason == "max_tokens":
            raise LLMProviderError("LLM response was truncated")

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise LLMProviderError("LLM returned an empty response")
        return text
