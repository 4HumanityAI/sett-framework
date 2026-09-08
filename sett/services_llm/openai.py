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
            model="gpt-4o"
        )
        response = llm.complete("What is SETT?")
    """

    DEFAULT_MODEL = "gpt-4o"
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
            api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
            model: The GPT model to use.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 to 1.0).
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
            self._client = self._openai.OpenAI(api_key=self._api_key)
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
                    max_tokens=kwargs.get("max_tokens", self._max_tokens),
                    temperature=kwargs.get("temperature", self._temperature),
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
                    max_tokens=kwargs.get("max_tokens", self._max_tokens),
                    temperature=kwargs.get("temperature", self._temperature),
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
