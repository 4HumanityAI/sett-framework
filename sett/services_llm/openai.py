"""
SETT Framework: OpenAI (GPT) Adapter
==============================
LLM adapter for OpenAI's GPT models.

Implements the LLMBase interface: fully interchangeable with
AnthropicAdapter or GeminiAdapter.

Requires, from a source checkout: pip install -e ".[openai]"
"""
from __future__ import annotations

import os
from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterKind,
    CapabilitySupport,
    ConnectivityRequirement,
    adapter_call_scope,
    normalized_adapter_error,
    required_model,
)
from sett.core_ruler.lifecycle import (
    pop_lifecycle_kwargs,
    validate_duration_seconds,
)
from sett.exceptions import (
    SETTAdapterError,
    SETTCancelledError,
    SETTLLMAdapterError,
    SETTTimeoutError,
)
from sett.services_llm.base import LLMBase


class OpenAIAdapter(LLMBase):
    """
    LLM adapter for OpenAI's GPT models.

    Usage:
        from sett.services_llm.openai import OpenAIAdapter

        llm = OpenAIAdapter(
            api_key="your-key",
            model="<model identifier you chose>",
        )
        response = llm.complete("What is SETT?")

    ``model`` is required. SETT ships no default model: the application
    chooses one from the identifiers its provider account offers.
    """

    DEFAULT_MAX_TOKENS = 1024
    DEFAULT_TEMPERATURE = 0.75
    DEFAULT_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        max_completion_tokens: int | None = None,
    ) -> None:
        """
        Args:
            api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
            model: The identifier of the model to use. Required; there
                is no default.
            max_tokens: Legacy token limit. Mutually exclusive with
                max_completion_tokens. If neither is set, sends max_tokens=1024.
            temperature: Sampling temperature (0.0 to 1.0). None omits it.
            max_completion_tokens: Completion budget, including reasoning
                tokens on models that use them. Select this explicitly when
                required by the model; SETT does not infer model capabilities.
            timeout_seconds: This adapter's own default timeout, in effect
                when no ambient run gives it a stricter one (see
                ``lifecycle.AdapterLifecycle.effective_timeout``).
        """
        try:
            import openai as _openai
            self._openai = _openai
        except ImportError:
            raise SETTLLMAdapterError(
                "The 'openai' package is required to use the OpenAIAdapter. "
                "SETT installs from source; from your checkout run: "
                "pip install -e \".[openai]\"",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="openai",
                adapter_name=type(self).__name__,
                reason_code="adapter.dependency_missing",
            )

        self._api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        if not self._api_key:
            raise SETTLLMAdapterError(
                "OpenAI API key not found. "
                "Pass api_key= or set the OPENAI_API_KEY environment variable.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="openai",
                adapter_name=type(self).__name__,
                reason_code="adapter.credentials_missing",
            )

        self._model = required_model(
            model,
            error_type=SETTLLMAdapterError,
            provider="openai",
            adapter_name=type(self).__name__,
        )
        self._token_limits = self._token_arguments(
            max_tokens, max_completion_tokens, operation="construct"
        )
        self._temperature = (
            None if temperature is None else min(1.0, max(0.0, temperature))
        )
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        # Translate SDK construction failures so callers can catch SETTError
        # without depending on the provider's exception hierarchy.
        try:
            # One adapter call sends one request: the SDK's automatic
            # retries are off, and repeating a call is the application's
            # decision through execute_with_resilience().
            self._client = self._openai.OpenAI(api_key=self._api_key, max_retries=0)
        except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
            raise
        except Exception as error:
            raise normalized_adapter_error(
                error,
                error_type=SETTLLMAdapterError,
                provider="openai",
                adapter_name=type(self).__name__,
                operation="construct",
                kind=AdapterErrorKind.CONFIGURATION,
                reason_code="adapter.client_construction_failed",
            ) from error

    def _token_arguments(
        self, max_tokens: int | None, max_completion_tokens: int | None,
        *, operation: str,
    ) -> dict[str, int]:
        if max_tokens is not None and max_completion_tokens is not None:
            raise SETTLLMAdapterError(
                "Choose either max_tokens or max_completion_tokens, not both.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="openai", adapter_name=type(self).__name__,
                operation=operation, reason_code="adapter.configuration_invalid",
            )
        name = "max_completion_tokens" if max_completion_tokens is not None else "max_tokens"
        value = max_completion_tokens if max_completion_tokens is not None else max_tokens
        if value is None:
            value = self.DEFAULT_MAX_TOKENS
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SETTLLMAdapterError(
                "The token limit must be a positive integer.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="openai", adapter_name=type(self).__name__,
                operation=operation, reason_code="adapter.configuration_invalid",
            )
        return {name: value}

    def _generation_arguments(self, kwargs: dict[str, Any], *, operation: str) -> dict[str, Any]:
        if "max_tokens" in kwargs or "max_completion_tokens" in kwargs:
            arguments = self._token_arguments(
                kwargs.get("max_tokens"), kwargs.get("max_completion_tokens"),
                operation=operation,
            )
        else:
            arguments = dict(self._token_limits)
        temperature = kwargs.get("temperature", self._temperature)
        if temperature is not None:
            arguments["temperature"] = temperature
        return arguments

    @property
    def model_name(self) -> str:
        """The GPT model this adapter calls."""
        return self._model

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Capabilities implemented by this synchronous adapter."""
        return AdapterCapabilities(
            AdapterKind.LLM,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("text",),
            output_formats=("text",),
            metadata={"operations": ("complete", "chat")},
        )

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """One-shot completion: no conversation history."""
        with adapter_call_scope(self, "complete"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            response_received = False
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    **self._generation_arguments(kwargs, operation="complete"),
                    messages=[
                        {"role": "system", "content": system or "You are a helpful assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    timeout=lifecycle.effective_timeout(self._timeout_seconds),
                )
                response_received = True
                lifecycle.throw_if_cancelled()
                return resp.choices[0].message.content or ""
            except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTLLMAdapterError,
                    provider="openai",
                    adapter_name=type(self).__name__,
                    operation="complete",
                    kind=(
                        AdapterErrorKind.INVALID_RESPONSE
                        if response_received
                        else None
                    ),
                ) from error

    def chat(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        **kwargs: Any,
    ) -> str:
        """Multi-turn conversation completion."""
        with adapter_call_scope(self, "chat"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            full_messages = []
            if system:
                full_messages.append({"role": "system", "content": system})
            full_messages.extend(messages)
            response_received = False
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    **self._generation_arguments(kwargs, operation="chat"),
                    messages=full_messages,
                    timeout=lifecycle.effective_timeout(self._timeout_seconds),
                )
                response_received = True
                lifecycle.throw_if_cancelled()
                return resp.choices[0].message.content or ""
            except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTLLMAdapterError,
                    provider="openai",
                    adapter_name=type(self).__name__,
                    operation="chat",
                    kind=(AdapterErrorKind.INVALID_RESPONSE if response_received else None),
                ) from error

    def __repr__(self) -> str:
        return f"OpenAIAdapter(model={self._model!r})"
