"""
SETT Framework: Ollama (local) Adapter
==============================
LLM adapter for locally-running models via Ollama (https://ollama.com).

No API key, no cloud, no cost: the model runs entirely on your own
machine. Requires ONLY that Ollama itself be installed and running
(`ollama serve`, usually started automatically) with at least one model
pulled (e.g. `ollama pull qwen3:1.7b`).

Unlike AnthropicAdapter/OpenAIAdapter/GeminiAdapter, this adapter has
NO extra pip dependency at all: it talks to Ollama's local REST API
using only Python's standard library (urllib). If you can run
`ollama pull <model>` and `ollama serve`, this adapter works with
nothing else to install on the Python side.

Recommended low-resource models (verified good picks as of 2026 for
machines without a dedicated GPU):
    - qwen3:1.7b    : lightest, ~4GB RAM, strong multilingual support
    - phi4-mini     : 3.8B, MIT license, built for CPU-only machines

Implements the LLMBase interface: fully interchangeable with the
cloud adapters. This is what makes SETT's principle 4 ("LLM as engine,
not architecture") concrete: swapping AnthropicAdapter for
OllamaAdapter changes zero lines anywhere else in your system.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterKind,
    CapabilitySupport,
    ConnectivityRequirement,
    adapter_call_scope,
)
from sett.core_ruler.lifecycle import (
    pop_lifecycle_kwargs,
    validate_duration_seconds,
)
from sett.exceptions import SETTError, SETTLLMAdapterError
from sett.services_llm.base import LLMBase


def _validated_base_url(base_url: str) -> str:
    """
    Accept only an absolute ``http``/``https`` URL that names a host.

    A wrong ``base_url`` is a configuration mistake, not a request that
    failed, so it is refused here rather than on the first completion.
    Refusing it late is worse than it sounds: the first completion is
    often reached through a fallback path (see ``PhrasingExpert``), and a
    URL error raised there arrives as a bare ``ValueError`` from urllib -
    a type nobody downstream is watching for.

    Returns the URL with any trailing slash removed, ready to have a path
    appended.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise SETTLLMAdapterError(
            "Ollama base_url must be a non-empty string, for example "
            f"'{OllamaAdapter.DEFAULT_BASE_URL}'.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider="ollama",
            adapter_name="OllamaAdapter",
            reason_code="adapter.invalid_base_url",
        )

    trimmed = base_url.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(trimmed)
        # Read on purpose and discarded: urllib parses the port only when
        # asked, so this is what turns a bad one into an error here
        # instead of on the first request.
        _ = parts.port
    except ValueError as e:
        # urllib reports these as plain ValueError: 'Invalid IPv6 URL',
        # 'Port could not be cast to integer value'.
        raise SETTLLMAdapterError(
            f"Ollama base_url {base_url!r} is not a valid URL: {e}",
            kind=AdapterErrorKind.CONFIGURATION,
            provider="ollama",
            adapter_name="OllamaAdapter",
            reason_code="adapter.invalid_base_url",
        ) from e

    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SETTLLMAdapterError(
            f"Ollama base_url {base_url!r} must be an absolute http(s) URL "
            f"including the host, for example "
            f"'{OllamaAdapter.DEFAULT_BASE_URL}'.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider="ollama",
            adapter_name="OllamaAdapter",
            reason_code="adapter.invalid_base_url",
        )
    return trimmed


class OllamaAdapter(LLMBase):
    """
    LLM adapter for locally-running Ollama models. No API key, no cost.

    Usage:
        from sett.services_llm.ollama import OllamaAdapter

        llm = OllamaAdapter(model="qwen3:1.7b")
        response = llm.complete("What is SETT?")

    Before first use:
        1. Install Ollama: https://ollama.com/download
        2. Pull a model:   ollama pull qwen3:1.7b
        3. Make sure Ollama is running (it usually starts automatically
           as a background service after installation).

    Raises:
        SETTLLMAdapterError: at construction, when ``base_url`` is not an
            absolute http(s) URL naming a host; and from ``complete()`` /
            ``chat()`` for every way the request itself can fail.
    """

    DEFAULT_MODEL = "qwen3:1.7b"
    DEFAULT_BASE_URL = "http://localhost:11434"
    DEFAULT_TEMPERATURE = 0.75
    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            model: The Ollama model tag to use (must already be pulled
                   locally: run `ollama pull <model>` first).
            base_url: Where Ollama's local server is listening. Default
                      is correct for almost everyone; only change this
                      if you moved Ollama to a different port/host. Must
                      be an absolute http(s) URL naming a host; anything
                      else is refused here rather than on the first call.
            temperature: Sampling temperature (0.0 to 1.0).
            timeout_seconds: How long to wait for a response before
                giving up. Local CPU inference can be slow: raise this
                if you see timeouts on a low-resource machine.
        """
        self._model = model
        self._base_url = _validated_base_url(base_url)
        self._temperature = min(1.0, max(0.0, temperature))
        self._timeout_seconds = validate_duration_seconds(
            timeout_seconds, name="timeout_seconds"
        )

    @property
    def model_name(self) -> str:
        """The Ollama model tag this adapter calls."""
        return self._model

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Capabilities implemented by the synchronous local-service client."""
        return AdapterCapabilities(
            AdapterKind.LLM,
            streaming=CapabilitySupport.NO,
            voice_cloning=CapabilitySupport.NO,
            partial_results=CapabilitySupport.NO,
            connectivity=ConnectivityRequirement.LOCAL_SERVICE,
            input_formats=("text",),
            output_formats=("text",),
            metadata={"operations": ("complete", "chat")},
        )

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """One-shot completion: no conversation history."""
        with adapter_call_scope(self, "complete"):
            lifecycle = pop_lifecycle_kwargs(kwargs)
            lifecycle.throw_if_cancelled()
            payload: dict[str, Any] = {
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": kwargs.get("temperature", self._temperature)},
            }
            if system:
                payload["system"] = system

            data = self._post(
                "/api/generate",
                payload,
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
            )
            lifecycle.throw_if_cancelled()
            return data.get("response", "")

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

            payload: dict[str, Any] = {
                "model": self._model,
                "messages": full_messages,
                "stream": False,
                "options": {"temperature": kwargs.get("temperature", self._temperature)},
            }

            data = self._post(
                "/api/chat",
                payload,
                timeout=lifecycle.effective_timeout(self._timeout_seconds),
            )
            lifecycle.throw_if_cancelled()
            return data.get("message", {}).get("content", "")

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        POST JSON to the local Ollama server using only the standard
        library: no extra pip dependency required for this adapter.

        ``timeout`` is the effective one, namely the shorter of this adapter's
        own setting and whatever time the surrounding run has left, so a
        local inference call cannot outlive the turn that asked for it.

        Every way this can fail leaves as ``SETTLLMAdapterError``, which
        is the adapter contract callers depend on (``PhrasingExpert``
        falls back to plain text on exactly that type). The frequent
        failures are named individually because their messages say what
        to do about them; the final clause exists because enumerating
        urllib's failure modes has already proven incomplete; a
        malformed URL surfaces as ``ValueError`` and a host with a space
        in it as ``http.client.InvalidURL``, neither of which is a
        ``URLError``. SETT exceptions pass through untouched so that a
        cancellation or a deadline keeps its own type and is not reported
        as a provider failure.
        """
        effective_timeout = (
            self._timeout_seconds if timeout is None else timeout
        )
        url = f"{self._base_url}{path}"
        try:
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=effective_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except SETTError:
            raise
        except urllib.error.URLError as e:
            raise SETTLLMAdapterError(
                f"Could not reach Ollama at {self._base_url}. "
                f"Is Ollama installed and running? "
                f"Try 'ollama serve' or check that the Ollama app is open. "
                f"Original error: {e}",
                kind=AdapterErrorKind.UNAVAILABLE,
                provider="ollama",
                adapter_name=type(self).__name__,
                operation="request",
                retryable=True,
                reason_code="adapter.provider_unavailable",
            ) from e
        except json.JSONDecodeError as e:
            raise SETTLLMAdapterError(
                "Ollama returned a response that wasn't valid JSON.",
                kind=AdapterErrorKind.INVALID_RESPONSE,
                provider="ollama",
                adapter_name=type(self).__name__,
                operation="request",
                retryable=False,
                reason_code="adapter.invalid_response",
            ) from e
        except TimeoutError as e:
            raise SETTLLMAdapterError(
                f"Ollama did not respond within {effective_timeout}s. "
                f"Local CPU inference can be slow on the first call (the "
                f"model needs to load into memory): try again, or raise "
                f"timeout_seconds if this keeps happening.",
                kind=AdapterErrorKind.PROVIDER_TIMEOUT,
                provider="ollama",
                adapter_name=type(self).__name__,
                operation="request",
                retryable=None,
                reason_code="adapter.provider_timeout",
            ) from e
        except Exception as e:
            raise SETTLLMAdapterError(
                f"Ollama request to {url} failed.",
                kind=AdapterErrorKind.UNKNOWN,
                provider="ollama",
                adapter_name=type(self).__name__,
                operation="request",
                reason_code="adapter.unknown",
            ) from e

    def __repr__(self) -> str:
        return f"OllamaAdapter(model={self._model!r}, base_url={self._base_url!r})"
