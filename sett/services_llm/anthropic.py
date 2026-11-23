"""
SETT Framework: Anthropic (Claude) Adapter
==============================
LLM adapter for Anthropic's Claude models.

Implements the LLMBase interface: swap with any other adapter
without changing the rest of the framework.

Requires, from a source checkout: pip install -e ".[anthropic]"
"""
from __future__ import annotations

import inspect
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


class AnthropicAdapter(LLMBase):
    """
    LLM adapter for Anthropic's Claude models.

    Usage:
        from sett.services_llm.anthropic import AnthropicAdapter

        llm = AnthropicAdapter(
            api_key="your-key",
            model="<model identifier you chose>",
        )
        response = llm.complete("What is SETT?")

    ``model`` is required. SETT ships no default model: the application
    chooses one from the identifiers its provider account offers.

    ``temperature`` is not sent unless the application sets it, at
    construction or per call. Anthropic removed the sampling parameters
    from its Messages API in version ``2026-10-21``, and current SDK
    releases no longer accept them, so a value SETT invented would make
    every request fail. A value the application does set is checked
    against the installed SDK before any request is built: if the SDK's
    ``messages.create`` does not take it, the adapter raises a
    configuration error instead of dropping it. That check reads the SDK's
    signature and nothing else. Whether the provider accepts the value for
    the chosen model is still decided by the provider, and a rejection
    there arrives as an ordinary provider error.

    The SDK's own automatic retries are disabled (``max_retries=0``): one
    adapter call sends one request. Repeating a call is the application's
    decision, made through ``execute_with_resilience()``.
    """

    DEFAULT_MAX_TOKENS = 1024
    DEFAULT_TEMPERATURE: float | None = None
    DEFAULT_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            model: The identifier of the model to use. Required; there
                is no default.
            max_tokens: Maximum tokens in the response.
            temperature: Optional sampling temperature (0.0 to 1.0).
                ``None``, the default, sends no temperature. A value is
                refused at construction when the installed SDK does not
                accept the parameter.
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
                "SETT installs from source; from your checkout run: "
                "pip install -e \".[anthropic]\"",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="anthropic",
                adapter_name=type(self).__name__,
                reason_code="adapter.dependency_missing",
            )

        self._api_key = (api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        if not self._api_key:
            raise SETTLLMAdapterError(
                "Anthropic API key not found. "
                "Pass api_key= or set the ANTHROPIC_API_KEY environment variable.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="anthropic",
                adapter_name=type(self).__name__,
                reason_code="adapter.credentials_missing",
            )

        self._model = required_model(
            model,
            error_type=SETTLLMAdapterError,
            provider="anthropic",
            adapter_name=type(self).__name__,
        )
        self._max_tokens = max_tokens
        self._temperature = (
            None if temperature is None else min(1.0, max(0.0, temperature))
        )
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        # Translate SDK construction failures so callers can catch SETTError
        # without depending on the provider's exception hierarchy.
        try:
            self._client = self._anthropic.Anthropic(
                api_key=self._api_key, max_retries=0
            )
        except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
            raise
        except Exception as error:
            raise normalized_adapter_error(
                error,
                error_type=SETTLLMAdapterError,
                provider="anthropic",
                adapter_name=type(self).__name__,
                operation="construct",
                kind=AdapterErrorKind.CONFIGURATION,
                reason_code="adapter.client_construction_failed",
            ) from error
        if self._temperature is not None:
            self._require_sdk_parameter("temperature", operation="construct")

    def _sdk_accepts(self, parameter: str) -> bool:
        """Whether the installed SDK's ``messages.create`` takes ``parameter``.

        Only the signature is read. A callable whose signature cannot be
        inspected, or one that takes arbitrary keyword arguments, is
        treated as accepting it, and the SDK then decides at call time.
        """
        try:
            parameters = inspect.signature(self._client.messages.create).parameters
        except (TypeError, ValueError):
            return True
        return parameter in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )

    def _require_sdk_parameter(self, parameter: str, *, operation: str) -> None:
        if self._sdk_accepts(parameter):
            return
        raise SETTLLMAdapterError(
            f"The installed anthropic SDK does not accept '{parameter}' on "
            "messages.create. Remove the setting, or install an SDK release "
            "that supports it.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider="anthropic",
            adapter_name=type(self).__name__,
            operation=operation,
            retryable=False,
            reason_code="adapter.configuration_invalid",
        )

    def _sampling_arguments(
        self, kwargs: dict[str, Any], *, operation: str
    ) -> dict[str, Any]:
        """Sampling arguments the application asked for, and no others."""
        temperature = kwargs.get("temperature", self._temperature)
        if temperature is None:
            return {}
        self._require_sdk_parameter("temperature", operation=operation)
        return {"temperature": temperature}

    @staticmethod
    def _visible_text(response: Any) -> str:
        """Join visible text in order; no text blocks means an empty answer."""
        return "".join(block.text for block in response.content if block.type == "text")

    @property
    def model_name(self) -> str:
        """The Claude model this adapter calls."""
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
            sampling = self._sampling_arguments(kwargs, operation="complete")
            response_received = False
            try:
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=kwargs.get("max_tokens", self._max_tokens),
                    system=system or "You are a helpful assistant.",
                    **sampling,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=lifecycle.effective_timeout(self._timeout_seconds),
                )
                response_received = True
                lifecycle.throw_if_cancelled()
                return self._visible_text(resp)
            except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTLLMAdapterError,
                    provider="anthropic",
                    adapter_name=type(self).__name__,
                    operation="complete",
                    kind=(AdapterErrorKind.INVALID_RESPONSE if response_received else None),
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
            sampling = self._sampling_arguments(kwargs, operation="chat")
            response_received = False
            try:
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=kwargs.get("max_tokens", self._max_tokens),
                    system=system or "You are a helpful assistant.",
                    **sampling,
                    messages=messages,
                    timeout=lifecycle.effective_timeout(self._timeout_seconds),
                )
                response_received = True
                lifecycle.throw_if_cancelled()
                return self._visible_text(resp)
            except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTLLMAdapterError,
                    provider="anthropic",
                    adapter_name=type(self).__name__,
                    operation="chat",
                    kind=(AdapterErrorKind.INVALID_RESPONSE if response_received else None),
                ) from error

    def __repr__(self) -> str:
        return f"AnthropicAdapter(model={self._model!r})"
