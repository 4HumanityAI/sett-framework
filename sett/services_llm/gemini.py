"""
SETT Framework: Google Gemini Adapter
==============================
LLM adapter for Google's Gemini models, built on the Google Gen AI SDK
(the ``google-genai`` package, imported as ``from google import genai``).

Implements the LLMBase interface: fully interchangeable with
AnthropicAdapter or OpenAIAdapter.

Requires, from a source checkout: pip install -e ".[gemini]"
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


def _status_code(error: BaseException) -> int | None:
    """The HTTP status the SDK attached to ``error``, if it is a number.

    The SDK's API errors carry it as ``code``; ``status`` on the same
    object is a text label, which the shared reader rightly ignores.
    """
    code = getattr(error, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


class GeminiAdapter(LLMBase):
    """
    LLM adapter for Google's Gemini models.

    Usage:
        from sett.services_llm.gemini import GeminiAdapter

        llm = GeminiAdapter(
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
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            api_key: Google AI API key. Falls back to GOOGLE_API_KEY, then
                GEMINI_API_KEY. A whitespace-only selected value is rejected.
            model: The identifier of the model to use. Required; there
                is no default.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 to 1.0).
            timeout_seconds: This adapter's own default timeout, in effect
                when no ambient run gives it a stricter one (see
                ``lifecycle.AdapterLifecycle.effective_timeout``).
        """
        try:
            from google import genai
            self._genai = genai
        except ImportError:
            raise SETTLLMAdapterError(
                "The 'google-genai' package is required to use GeminiAdapter. "
                "From a source checkout run: pip install -e \".[gemini]\". "
                "For an installed wheel, install its declared google-genai dependency.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="google_gemini",
                adapter_name=type(self).__name__,
                reason_code="adapter.dependency_missing",
            )

        self._api_key = (
            api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        if not self._api_key:
            raise SETTLLMAdapterError(
                "Google API key not found. "
                "Pass api_key= or set GOOGLE_API_KEY or GEMINI_API_KEY.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="google_gemini",
                adapter_name=type(self).__name__,
                reason_code="adapter.credentials_missing",
            )

        self._model_name = required_model(
            model,
            error_type=SETTLLMAdapterError,
            provider="google_gemini",
            adapter_name=type(self).__name__,
        )
        self._max_tokens = max_tokens
        self._temperature = min(1.0, max(0.0, temperature))

        # Translate SDK construction failures into the framework error family
        # while preserving structured cancellation and timeout errors.
        try:
            self._client = self._genai.Client(api_key=self._api_key)
        except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
            raise
        except Exception as error:
            raise normalized_adapter_error(
                error,
                error_type=SETTLLMAdapterError,
                provider="google_gemini",
                adapter_name=type(self).__name__,
                operation="construct",
                kind=AdapterErrorKind.CONFIGURATION,
                reason_code="adapter.client_construction_failed",
            ) from error

    @property
    def model_name(self) -> str:
        """The Gemini model this adapter calls."""
        return self._model_name

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

    def _request_config(
        self,
        system: str,
        timeout_seconds: float | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the per-request configuration the SDK accepts as a dict.

        The SDK takes its request timeout in milliseconds, as a whole
        number, inside ``http_options``.
        """
        config: dict[str, Any] = {
            "max_output_tokens": kwargs.get("max_tokens", self._max_tokens),
            "temperature": kwargs.get("temperature", self._temperature),
        }
        if system:
            config["system_instruction"] = system
        if timeout_seconds is not None:
            config["http_options"] = {
                "timeout": max(1, int(timeout_seconds * 1000))
            }
        return config

    def _generate(
        self,
        operation: str,
        contents: Any,
        system: str,
        kwargs: dict[str, Any],
    ) -> str:
        """One request to the provider, with SETT's lifecycle and errors."""
        with adapter_call_scope(self, operation):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            response_received = False
            try:
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=contents() if callable(contents) else contents,
                    config=self._request_config(
                        system,
                        lifecycle.effective_timeout(self._timeout_seconds),
                        kwargs,
                    ),
                )
                response_received = True
                lifecycle.throw_if_cancelled()
                return response.text if response.text else ""
            except (SETTCancelledError, SETTTimeoutError, SETTAdapterError):
                raise
            except Exception as error:
                raise normalized_adapter_error(
                    error,
                    error_type=SETTLLMAdapterError,
                    provider="google_gemini",
                    adapter_name=type(self).__name__,
                    operation=operation,
                    kind=(AdapterErrorKind.INVALID_RESPONSE if response_received else None),
                    status_code=_status_code(error),
                ) from error

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """One-shot completion: no conversation history."""
        return self._generate("complete", prompt, system, kwargs)

    def chat(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        **kwargs: Any,
    ) -> str:
        """
        Multi-turn conversation.
        Converts SETT's role format to Gemini's format.
        """

        def contents() -> list[dict[str, Any]]:
            # Gemini uses "user" and "model" roles (not "assistant"). The
            # whole conversation travels in one stateless request.
            return [
                {
                    "role": "model" if message["role"] == "assistant" else "user",
                    "parts": [{"text": message["content"]}],
                }
                for message in messages
            ]

        return self._generate("chat", contents, system, kwargs)

    def __repr__(self) -> str:
        return f"GeminiAdapter(model={self._model_name!r})"
