"""
SETT Framework: Ollama Adapter
=============================
LLM adapter for an application-configured Ollama server, using Python's
standard library. No optional Python SDK is required.

Install Ollama and pull the model chosen by the application before use.
The default endpoint is localhost; a custom URL can select a remote server.
Model availability, resource requirements and access to that server are
the application's responsibility.
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
    normalized_adapter_error,
    required_model,
)
from sett.core_ruler.lifecycle import (
    pop_lifecycle_kwargs,
    validate_duration_seconds,
)
from sett.exceptions import SETTError, SETTLLMAdapterError
from sett.services_llm.base import LLMBase


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(request, *, timeout):
    """Use a per-call opener without changing process-global HTTP behavior."""
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


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
            "Ollama base_url is not a valid URL.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider="ollama",
            adapter_name="OllamaAdapter",
            reason_code="adapter.invalid_base_url",
        ) from e

    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise SETTLLMAdapterError(
            "Ollama base_url must be an absolute http(s) URL "
            "without credentials, query or fragment, for example "
            f"'{OllamaAdapter.DEFAULT_BASE_URL}'.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider="ollama",
            adapter_name="OllamaAdapter",
            reason_code="adapter.invalid_base_url",
        )
    return trimmed


class OllamaAdapter(LLMBase):
    """
    LLM adapter for an explicitly selected Ollama model and server.

    Usage:
        import os
        from sett.services_llm.ollama import OllamaAdapter

        llm = OllamaAdapter(model=os.environ["SETT_LLM_MODEL"])
        response = llm.complete("What is SETT?")

    Before first use:
        1. Install Ollama: https://ollama.com/download
        2. Pull the model selected in SETT_LLM_MODEL with ollama pull.
        3. Make sure Ollama is running (it usually starts automatically
           as a background service after installation).

    Raises:
        SETTLLMAdapterError: at construction, when ``base_url`` is not an
            absolute http(s) URL naming a host; and from ``complete()`` /
            ``chat()`` for every way the request itself can fail.
    """

    DEFAULT_BASE_URL = "http://localhost:11434"
    DEFAULT_TEMPERATURE = 0.75
    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        *,
        max_response_bytes: int = 16 * 1024 * 1024,
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
            max_response_bytes: Positive response byte limit, 16 MiB by default.
                Oversized responses and HTTP redirects are rejected. Base URLs
                must not contain user information, a query or a fragment.
        """
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise SETTLLMAdapterError(
                "Ollama max_response_bytes must be a positive integer.",
                kind=AdapterErrorKind.CONFIGURATION,
                provider="ollama", reason_code="adapter.configuration_invalid",
            )
        self._max_response_bytes = max_response_bytes
        self._model = required_model(
            model, error_type=SETTLLMAdapterError, provider="ollama",
            adapter_name=type(self).__name__,
        )
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
            text = data.get("response", "")
            if not isinstance(text, str):
                raise self._invalid_response(
                    "Ollama returned a response field that is not text."
                )
            return text

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
            message = data.get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else None
            if not isinstance(content, str):
                raise self._invalid_response(
                    "Ollama returned a message whose content is not text."
                )
            return content

    def _invalid_response(self, message: str) -> SETTLLMAdapterError:
        """One shape for every response this adapter cannot use.

        An absent field keeps its established meaning, an empty answer. A
        field that is present with the wrong type, or a body that is not a
        JSON object, is a provider response this adapter does not
        understand, and repeating the same request would not change that.
        """
        return SETTLLMAdapterError(
            message,
            kind=AdapterErrorKind.INVALID_RESPONSE,
            provider="ollama",
            adapter_name=type(self).__name__,
            operation="request",
            retryable=False,
            reason_code="adapter.invalid_response",
        )

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
        own setting and whatever time the surrounding run has left. It
        bounds each socket operation, not the whole call: a server that
        keeps sending data slowly can hold the call open longer.

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
            with _open_request(request, timeout=effective_timeout) as resp:
                response = resp.read(self._max_response_bytes + 1)
                if len(response) > self._max_response_bytes:
                    raise self._invalid_response(
                        "Ollama response exceeds the configured byte limit."
                    )
                data = json.loads(response.decode("utf-8"))
                if not isinstance(data, dict):
                    raise self._invalid_response(
                        "Ollama returned JSON that is not an object."
                    )
                return data
        except SETTError:
            raise
        except urllib.error.HTTPError as e:
            # HTTPError owns a response stream even when no body is read.
            # Close it on every exit while retaining the original cause.
            with e:
                raise normalized_adapter_error(
                    e,
                    error_type=SETTLLMAdapterError,
                    provider="ollama",
                    adapter_name=type(self).__name__,
                    operation="request",
                    status_code=e.code,
                ) from e
        except urllib.error.URLError as e:
            raise SETTLLMAdapterError(
                "Could not reach Ollama. "
                "Is Ollama installed and running? "
                "Try 'ollama serve' or check that the Ollama app is open.",
                kind=AdapterErrorKind.UNAVAILABLE,
                provider="ollama",
                adapter_name=type(self).__name__,
                operation="request",
                retryable=True,
                reason_code="adapter.provider_unavailable",
            ) from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
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
                "Ollama request failed.",
                kind=AdapterErrorKind.UNKNOWN,
                provider="ollama",
                adapter_name=type(self).__name__,
                operation="request",
                reason_code="adapter.unknown",
            ) from e

    def __repr__(self) -> str:
        return f"OllamaAdapter(model={self._model!r}, base_url={self._base_url!r})"
