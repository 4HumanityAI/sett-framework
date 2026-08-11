"""
SETT Framework: Google Gemini Adapter
==============================
LLM adapter for Google's Gemini models.

Implements the LLMBase interface: fully interchangeable with
AnthropicAdapter or OpenAIAdapter.

Requires: pip install sett-framework[gemini]
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


class GeminiAdapter(LLMBase):
    """
    LLM adapter for Google's Gemini models.

    Usage:
        from sett.services_llm.gemini import GeminiAdapter

        llm = GeminiAdapter(
            api_key="your-key",
            model="gemini-1.5-flash"
        )
        response = llm.complete("What is SETT?")
    """

    DEFAULT_MODEL = "gemini-1.5-flash"
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
            api_key: Google AI API key. Falls back to GOOGLE_API_KEY env var.
            model: The Gemini model to use.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 to 1.0).
            timeout_seconds: This adapter's own default timeout, in effect
                when no ambient run gives it a stricter one (see
                ``lifecycle.AdapterLifecycle.effective_timeout``).
        """
        try:
            import google.generativeai as genai
            self._genai = genai
        except ImportError:
            raise SETTLLMAdapterError(
                "The 'google-generativeai' package is required to use GeminiAdapter. "
                "Install it with: pip install sett-framework[gemini]"
            )

        self._api_key = (api_key or os.getenv("GOOGLE_API_KEY", "")).strip()
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )
        if not self._api_key:
            raise SETTLLMAdapterError(
                "Google API key not found. "
                "Pass api_key= or set the GOOGLE_API_KEY environment variable."
            )

        self._model_name = model
        self._max_tokens = max_tokens
        self._temperature = min(1.0, max(0.0, temperature))

        # Wrapped because whatever the SDK raises here is a failure of this
        # adapter, and Convention #13 says a caller holding
        # ``except SETTError`` should not have to also know the provider
        # library's exception hierarchy to catch it.
        try:
            self._genai.configure(api_key=self._api_key)
            self._model = self._genai.GenerativeModel(
                model_name=self._model_name
            )
        except Exception as error:
            raise SETTLLMAdapterError(
                f"The 'google-generativeai' client could not be "
                f"constructed: {type(error).__name__}: {error}"
            ) from error

    @property
    def model_name(self) -> str:
        """The Gemini model this adapter calls."""
        return self._model_name

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """One-shot completion: no conversation history."""
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            full_prompt = f"{system}\n\n{prompt}".strip() if system else prompt
            config = self._genai.GenerationConfig(
                max_output_tokens=kwargs.get("max_tokens", self._max_tokens),
                temperature=kwargs.get("temperature", self._temperature),
            )
            response = self._model.generate_content(
                full_prompt,
                generation_config=config,
                request_options={
                    "timeout": lifecycle.effective_timeout(
                        self._timeout_seconds
                    )
                },
            )
        except Exception as e:
            raise SETTLLMAdapterError(
                f"Gemini API error during complete(): "
                f"{bound_provider_error_message(e)}"
            ) from e
        lifecycle.throw_if_cancelled()
        return response.text if response.text else ""

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
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        try:
            # Gemini uses "user" and "model" roles (not "assistant")
            gemini_history = []
            for msg in messages[:-1]:  # all but the last message
                role = "model" if msg["role"] == "assistant" else "user"
                gemini_history.append({
                    "role": role,
                    "parts": [msg["content"]],
                })

            config = self._genai.GenerationConfig(
                max_output_tokens=kwargs.get("max_tokens", self._max_tokens),
                temperature=kwargs.get("temperature", self._temperature),
            )

            chat_session = self._model.start_chat(history=gemini_history)

            # Last message is the current user turn
            last_msg = messages[-1]["content"] if messages else ""
            if system:
                last_msg = f"{system}\n\n{last_msg}"

            response = chat_session.send_message(
                last_msg,
                generation_config=config,
                request_options={
                    "timeout": lifecycle.effective_timeout(
                        self._timeout_seconds
                    )
                },
            )
        except Exception as e:
            raise SETTLLMAdapterError(
                f"Gemini API error during chat(): "
                f"{bound_provider_error_message(e)}"
            ) from e
        lifecycle.throw_if_cancelled()
        return response.text if response.text else ""

    def __repr__(self) -> str:
        return f"GeminiAdapter(model={self._model_name!r})"
