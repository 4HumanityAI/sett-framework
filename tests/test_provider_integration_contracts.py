"""Provider integration contracts that a permissive mock cannot check.

The fakes here have explicit call signatures, like a real SDK, so an
argument the SDK does not accept is refused instead of being swallowed.
The Ollama cases use a real HTTP server on the loopback interface, so the
header and body types are the ones the standard library really delivers.
"""
from __future__ import annotations

import sys
import threading
import types
import urllib.error
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sett.core_ruler.adapter import (
    AdapterErrorKind,
    AdapterHealthStatus,
    provider_retry_after,
)
from sett.core_ruler.phrasing_expert import PhrasingExpert
from sett.exceptions import SETTLLMAdapterError
from sett.services_llm import ollama

# ── Anthropic and OpenAI: strict fake SDKs ──────────────────────────────


def _message(text: str):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])


class _MessagesWithTemperature:
    """The call shape of an SDK release that still takes ``temperature``."""

    def __init__(self, calls: list):
        self._calls = calls

    def create(self, *, model, max_tokens, messages, system=None, temperature=None, timeout=None):
        self._calls.append(
            {"model": model, "max_tokens": max_tokens, "messages": messages,
             "system": system, "temperature": temperature, "timeout": timeout}
        )
        return _message("with-temperature")


class _MessagesWithoutTemperature:
    """The call shape of an SDK release that removed the sampling parameters."""

    def __init__(self, calls: list):
        self._calls = calls

    def create(self, *, model, max_tokens, messages, system=None, timeout=None):
        self._calls.append(
            {"model": model, "max_tokens": max_tokens, "messages": messages,
             "system": system, "timeout": timeout}
        )
        return _message("without-temperature")


def _install_anthropic(monkeypatch, messages_type):
    calls: list = []
    constructed: list = []

    class Client:
        def __init__(self, *, api_key, max_retries):
            constructed.append({"api_key": api_key, "max_retries": max_retries})
            self.messages = messages_type(calls)

    module = types.ModuleType("anthropic")
    module.Anthropic = Client
    monkeypatch.setitem(sys.modules, "anthropic", module)
    from sett.services_llm.anthropic import AnthropicAdapter

    return AnthropicAdapter, calls, constructed


@pytest.mark.parametrize("messages_type", [_MessagesWithTemperature, _MessagesWithoutTemperature])
def test_anthropic_sends_no_sampling_parameter_by_default(monkeypatch, messages_type):
    adapter_type, calls, _ = _install_anthropic(monkeypatch, messages_type)
    adapter = adapter_type(api_key="synthetic", model="synthetic-model")

    adapter.complete("hello")
    adapter.chat([{"role": "user", "content": "hello"}])

    assert len(calls) == 2
    for call in calls:
        assert call.get("temperature") is None
        assert call["model"] == "synthetic-model"
        assert call["max_tokens"] == adapter_type.DEFAULT_MAX_TOKENS


def test_anthropic_sends_an_explicit_temperature_when_the_sdk_takes_it(monkeypatch):
    adapter_type, calls, _ = _install_anthropic(monkeypatch, _MessagesWithTemperature)

    configured = adapter_type(api_key="synthetic", model="synthetic-model", temperature=0.2)
    configured.complete("hello")
    configured.chat([{"role": "user", "content": "hello"}], temperature=0.9)
    default = adapter_type(api_key="synthetic", model="synthetic-model")
    default.complete("hello", temperature=0.4)

    assert [call["temperature"] for call in calls] == [0.2, 0.9, 0.4]


def test_anthropic_refuses_an_explicit_temperature_the_sdk_does_not_take(monkeypatch):
    adapter_type, calls, _ = _install_anthropic(monkeypatch, _MessagesWithoutTemperature)

    with pytest.raises(SETTLLMAdapterError) as at_construction:
        adapter_type(api_key="synthetic", model="synthetic-model", temperature=0.2)
    adapter = adapter_type(api_key="synthetic", model="synthetic-model")
    with pytest.raises(SETTLLMAdapterError) as per_complete:
        adapter.complete("hello", temperature=0.2)
    with pytest.raises(SETTLLMAdapterError) as per_chat:
        adapter.chat([{"role": "user", "content": "hello"}], temperature=0.2)

    for caught in (at_construction, per_complete, per_chat):
        assert caught.value.kind is AdapterErrorKind.CONFIGURATION
        assert caught.value.reason_code == "adapter.configuration_invalid"
        assert caught.value.retryable is False
    # Refused before any request was built, and never silently dropped.
    assert calls == []


def test_a_type_error_raised_inside_the_sdk_is_not_reported_as_configuration(monkeypatch):
    class Messages:
        def __init__(self, calls):
            self._calls = calls

        def create(self, *, model, max_tokens, messages, system=None, timeout=None):
            self._calls.append(model)
            raise TypeError("raised by the SDK while handling the request")

    adapter_type, calls, _ = _install_anthropic(monkeypatch, Messages)
    adapter = adapter_type(api_key="synthetic", model="synthetic-model")

    with pytest.raises(SETTLLMAdapterError) as caught:
        adapter.complete("hello")

    assert calls == ["synthetic-model"]
    assert caught.value.kind is AdapterErrorKind.UNKNOWN
    assert caught.value.reason_code != "adapter.configuration_invalid"
    assert isinstance(caught.value.__cause__, TypeError)


def test_anthropic_and_openai_clients_are_built_without_sdk_retries(monkeypatch):
    adapter_type, _, constructed = _install_anthropic(monkeypatch, _MessagesWithoutTemperature)
    adapter_type(api_key="synthetic", model="synthetic-model")
    assert constructed == [{"api_key": "synthetic", "max_retries": 0}]

    built: list = []

    class OpenAIClient:
        def __init__(self, *, api_key, max_retries):
            built.append({"api_key": api_key, "max_retries": max_retries})

    module = types.ModuleType("openai")
    module.OpenAI = OpenAIClient
    monkeypatch.setitem(sys.modules, "openai", module)
    from sett.services_llm.openai import OpenAIAdapter

    OpenAIAdapter(api_key="synthetic", model="synthetic-model")
    assert built == [{"api_key": "synthetic", "max_retries": 0}]


# ── Ollama: a real loopback server ──────────────────────────────────────


@contextmanager
def _server(status: int, body: bytes, headers: dict | None = None):
    hits: list = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _adapter(url: str) -> ollama.OllamaAdapter:
    return ollama.OllamaAdapter(base_url=url, model="synthetic-model")


@pytest.mark.parametrize(
    ("body", "operation"),
    [
        (b"[]", "complete"),
        (b'"just text"', "complete"),
        (b"null", "complete"),
        (b"7", "chat"),
        (b'{"response": 123}', "complete"),
        (b'{"response": null}', "complete"),
        (b'{"response": ["a"]}', "complete"),
        (b'{"message": "not an object"}', "chat"),
        (b'{"message": null}', "chat"),
        (b'{"message": {"content": 5}}', "chat"),
        (b'{"message": {"content": null}}', "chat"),
        (b'{"message": {"content": ["a"]}}', "chat"),
        (b"\xff\xfe\xfa", "complete"),
        (b"{not json", "chat"),
    ],
)
def test_ollama_reports_an_unusable_response_as_invalid(body, operation):
    with _server(200, body) as (url, hits):
        adapter = _adapter(url)
        with pytest.raises(SETTLLMAdapterError) as caught:
            if operation == "complete":
                adapter.complete("hello")
            else:
                adapter.chat([{"role": "user", "content": "hello"}])

    assert len(hits) == 1
    assert caught.value.kind is AdapterErrorKind.INVALID_RESPONSE
    assert caught.value.reason_code == "adapter.invalid_response"
    assert caught.value.retryable is False
    # The failure is observed like any other adapter failure.
    assert adapter.health.status is AdapterHealthStatus.UNKNOWN
    assert adapter.health.reason_codes == ("adapter.invalid_response",)
    assert adapter.health.error is not None


@pytest.mark.parametrize(
    ("body", "operation", "expected"),
    [
        (b'{"response": "hello"}', "complete", "hello"),
        (b'{"response": ""}', "complete", ""),
        (b"{}", "complete", ""),
        (b'{"message": {"content": "hi"}}', "chat", "hi"),
        (b'{"message": {}}', "chat", ""),
        (b"{}", "chat", ""),
    ],
)
def test_ollama_accepts_text_and_keeps_absent_fields_as_empty(body, operation, expected):
    with _server(200, body) as (url, _):
        adapter = _adapter(url)
        if operation == "complete":
            result = adapter.complete("hello")
        else:
            result = adapter.chat([{"role": "user", "content": "hello"}])

    assert result == expected
    assert isinstance(result, str)
    assert adapter.health.status is AdapterHealthStatus.HEALTHY


class _Greeting(PhrasingExpert):
    def determine_facts(self, context):
        return {"name": "synthetic"}

    def build_prompt(self, facts, context):
        return "Greet the user."

    def fallback_text(self, facts, context):
        return "Hello."


@pytest.mark.parametrize("body", [b"[]", b'{"response": 123}', b"\xff\xfe"])
def test_a_phrasing_expert_falls_back_on_an_unusable_ollama_response(body):
    with _server(200, body) as (url, hits):
        result = _Greeting(name="greeting", llm=_adapter(url)).resolve({})

    assert len(hits) == 1
    assert "Hello." in result.values()


def test_ollama_reads_retry_after_from_a_real_http_response():
    with (
        _server(429, b"{}", {"Retry-After": "7"}) as (url, _),
        pytest.raises(SETTLLMAdapterError) as caught,
    ):
        _adapter(url).complete("hello")

    error = caught.value
    assert error.kind is AdapterErrorKind.QUOTA
    assert error.status_code == 429
    assert error.retry_after_seconds == 7.0
    cause = error.__cause__
    assert isinstance(cause, urllib.error.HTTPError)
    assert isinstance(cause.headers, Message)
    assert "Retry-After" not in str(error)


def _error_with(headers) -> Exception:
    error = RuntimeError("provider text that must stay private")
    error.headers = headers
    return error


def _message_headers(value: str) -> Message:
    headers = Message()
    headers["Retry-After"] = value
    return headers


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (_message_headers("3"), 3.0),
        (_message_headers("0"), 0.0),
        (_message_headers("2.5"), 2.5),
        (_message_headers("-1"), None),
        (_message_headers("nan"), None),
        (_message_headers("inf"), None),
        (_message_headers("Wed, 21 Oct 2026 07:28:00 GMT"), None),
        (Message(), None),
        ({"Retry-After": "4"}, 4.0),
        ({"retry-after": "4"}, 4.0),
        ({}, None),
        (None, None),
        ("Retry-After: 9", None),
        (object(), None),
    ],
)
def test_retry_after_is_read_from_mappings_and_message_headers_only(headers, expected):
    assert provider_retry_after(_error_with(headers)) == expected
