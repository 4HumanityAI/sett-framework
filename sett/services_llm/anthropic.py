"""
SETT Framework: Anthropic (Claude) Adapter
==============================
LLM adapter for Anthropic's Claude models.

This is the recommended adapter for SETT-based systems.
Implements the LLMBase interface: swap with any other adapter
without changing the rest of the framework.

Requires: pip install sett-framework[anthropic]
"""
from __future__ import annotations
import os
from typing import Any

from sett.core_ruler.lifecycle import (
    pop_lifecycle_kwargs,
    validate_duration_seconds,
)
from sett.services_llm.base import LLMBase
from sett.exceptions import SETTLLMAdapterError, bound_provider_error_message


class AnthropicAdapter(LLMBase):
    """
    LLM adapter for Anthropic's Claude models.

    Usage:
        from sett.services_llm.anthropic import AnthropicAdapter

        llm = AnthropicAdapter(
            api_key="your-key",
            model="claude-sonnet-4-20250514"
        )
        response = llm.complete("What is SETT?")
    """

    DEFAULT_MODEL = "claude-sonnet-4-20250514"
    DEFAULT_MAX_TOKENS = 1024
    DEFAULT_TEMPERATURE = 0.75
    DEFAULT_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            model: The Claude model to use.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 to 1.0).
            timeout_seconds: This adapter's own default timeout, in effect
                when no ambient run gives it a stricter one (see
                ``lifecycle.AdapterLifecycle.effective_timeout``).
        """
        try:
            import anthropic as _anthropic
            self._anthropic = _anthropic
        except ImportError:
            raise SETTLLMAdapterError(
                "The 'anthropic' package is required to use the AnthropicAdapter. "
                "Install it with: pip install sett-framework[anthropic]"
            )

        self._api_key = (api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        if not self._api_key:
            raise SETTLLMAdapterError(
                "Anthropic API key not found. "
                "Pass api_key= or set the ANTHROPIC_API_KEY environment variable."
            )

        self._model = model
        self._max_tokens = max_tokens
        self._temperature = min(1.0, max(0.0, temperature))
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        # Wrapped because whatever the SDK raises here is a failure of this
        # adapter, and Convention #13 says a caller holding
        # ``except SETTError`` should not have to also know the provider
        # library's exception hierarchy to catch it.
        try:
            self._client = self._anthropic.Anthropic(api_key=self._api_key)
        except Exception as error:
            raise SETTLLMAdapterError(
                f"The 'anthropic' client could not be constructed: "
                f"{type(error).__name__}: {error}"
            ) from error

    @property
    def model_name(self) -> str:
        """The Claude model this adapter calls."""
        return self._model

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """One-shot completion: no conversation history."""
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=kwargs.get("max_tokens", self._max_tokens),
                system=system or "You are a helpful assistant.",
                temperature=kwargs.get("temperature", self._temperature),
                messages=[{"role": "user", "content": prompt}],
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
            )
        except Exception as e:
            raise SETTLLMAdapterError(
                f"Anthropic API error during complete(): "
                f"{bound_provider_error_message(e)}"
            ) from e
        lifecycle.throw_if_cancelled()
        return resp.content[0].text if resp.content else ""

    def chat(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        **kwargs: Any,
    ) -> str:
        """Multi-turn conversation completion."""
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=kwargs.get("max_tokens", self._max_tokens),
                system=system or "You are a helpful assistant.",
                temperature=kwargs.get("temperature", self._temperature),
                messages=messages,
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
            )
        except Exception as e:
            raise SETTLLMAdapterError(
                f"Anthropic API error during chat(): "
                f"{bound_provider_error_message(e)}"
            ) from e
        lifecycle.throw_if_cancelled()
        return resp.content[0].text if resp.content else ""

    def __repr__(self) -> str:
        return f"AnthropicAdapter(model={self._model!r})"
