"""
F01 regression (defensive audit #2): the official cloud LLM adapters must
honour lifecycle the same way the local ones already do.

Before this fix, OpenAIAdapter/AnthropicAdapter/GeminiAdapter never called
``pop_lifecycle_kwargs()`` or ``throw_if_cancelled()`` at all: a call made
under an already-cancelled ambient control still reached the provider
client. Only OllamaAdapter (plain HTTP, no SDK dependency) implemented the
pattern this file now proves for the other three.

None of ``openai``, ``anthropic`` or ``google-generativeai`` are installed
in this environment (an optional extra each), so these adapters have no
real end-to-end coverage anywhere in this suite - exactly what both audits
flagged. What is verified here, with hand-built provider doubles injected
into ``sys.modules``, is the one thing entirely within SETT's own control:
that the framework's lifecycle kwargs are popped, checked, and forwarded
as the SDK's own ``timeout``/``request_options`` argument before any
provider call, and that an already-cancelled control stops the adapter
before it ever constructs a client call. It does not and cannot prove the
real SDK accepts these arguments the way the double does.
"""
from __future__ import annotations

import sys
import types

import pytest

from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import (
    ExecutionControl,
    ExecutionControlRegistry,
    lifecycle_scope,
)
from sett.exceptions import (
    DEFAULT_PROVIDER_ERROR_MAX_LENGTH,
    SETTCancelledError,
    SETTLLMAdapterError,
    bound_provider_error_message,
)


@pytest.fixture
def registry() -> ExecutionControlRegistry:
    return ExecutionControlRegistry()


def _control(registry, **kwargs) -> ExecutionControl:
    return ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry, **kwargs
    )


# ── OpenAI ───────────────────────────────────────────────────────────────


class _FakeOpenAIClient:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.calls: list[dict] = []
        calls = self.calls

        class _Message:
            content = "hi"

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        class _Completions:
            def create(self_inner, **kwargs):
                calls.append(kwargs)
                return _Response()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def fake_openai(monkeypatch):
    module = types.ModuleType("openai")
    module.OpenAI = _FakeOpenAIClient
    monkeypatch.setitem(sys.modules, "openai", module)
    return module


def _openai_adapter(fake_openai, **kwargs):
    from sett.services_llm.openai import OpenAIAdapter

    return OpenAIAdapter(api_key="sk-test", **kwargs)


def test_openai_forwards_the_effective_timeout(fake_openai, registry):
    adapter = _openai_adapter(fake_openai)
    with _control(registry, timeout_seconds=2.0) as control:
        with lifecycle_scope(control):
            adapter.complete("hello")

    call = adapter._client.calls[0]
    assert call["timeout"] is not None
    assert call["timeout"] <= 2.0


def test_openai_does_not_call_the_provider_once_cancelled(fake_openai, registry):
    adapter = _openai_adapter(fake_openai)
    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel(reason="execution.user_stopped")
            with pytest.raises(SETTCancelledError):
                adapter.complete("hello")

    assert adapter._client.calls == []


def test_openai_chat_also_checks_cancellation_first(fake_openai, registry):
    adapter = _openai_adapter(fake_openai)
    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel()
            with pytest.raises(SETTCancelledError):
                adapter.chat([{"role": "user", "content": "hi"}])

    assert adapter._client.calls == []


def test_openai_works_unchanged_with_no_control_at_all(fake_openai):
    adapter = _openai_adapter(fake_openai)
    assert adapter.complete("hello") == "hi"
    assert adapter._client.calls[0]["timeout"] == adapter._timeout_seconds


def test_openai_strips_whitespace_from_an_explicit_api_key(fake_openai):
    from sett.services_llm.openai import OpenAIAdapter

    adapter = OpenAIAdapter(api_key="  sk-test  ")
    assert adapter._api_key == "sk-test"


def test_openai_a_provider_error_still_becomes_an_adapter_error(
    fake_openai, registry
):
    def boom(**kwargs):
        raise RuntimeError("provider blew up")

    adapter = _openai_adapter(fake_openai)
    adapter._client.chat.completions.create = boom

    with pytest.raises(SETTLLMAdapterError):
        adapter.complete("hello")


def test_openai_bounds_an_oversized_provider_error_message(fake_openai):
    """F29 regression (defensive audit #2): the provider's raw
    ``str(e)`` used to be embedded in the adapter error with no limit.
    The full original stays reachable as __cause__ for real debugging;
    only the adapter's own message string is bounded."""
    huge_message = "provider payload " * 50
    assert len(huge_message) > DEFAULT_PROVIDER_ERROR_MAX_LENGTH

    def boom(**kwargs):
        raise RuntimeError(huge_message)

    adapter = _openai_adapter(fake_openai)
    adapter._client.chat.completions.create = boom

    with pytest.raises(SETTLLMAdapterError) as exc_info:
        adapter.complete("hello")

    assert len(str(exc_info.value)) < len(huge_message)
    assert str(exc_info.value.__cause__) == huge_message


# ── Anthropic ────────────────────────────────────────────────────────────


class _FakeAnthropicClient:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.calls: list[dict] = []
        calls = self.calls

        class _Block:
            text = "hi"

        class _Response:
            content = [_Block()]

        class _Messages:
            def create(self_inner, **kwargs):
                calls.append(kwargs)
                return _Response()

        self.messages = _Messages()


@pytest.fixture
def fake_anthropic(monkeypatch):
    module = types.ModuleType("anthropic")
    module.Anthropic = _FakeAnthropicClient
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


def _anthropic_adapter(fake_anthropic, **kwargs):
    from sett.services_llm.anthropic import AnthropicAdapter

    return AnthropicAdapter(api_key="sk-ant-test", **kwargs)


def test_anthropic_forwards_the_effective_timeout(fake_anthropic, registry):
    adapter = _anthropic_adapter(fake_anthropic)
    with _control(registry, timeout_seconds=2.0) as control:
        with lifecycle_scope(control):
            adapter.complete("hello")

    call = adapter._client.calls[0]
    assert call["timeout"] is not None
    assert call["timeout"] <= 2.0


def test_anthropic_does_not_call_the_provider_once_cancelled(
    fake_anthropic, registry
):
    adapter = _anthropic_adapter(fake_anthropic)
    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel(reason="execution.user_stopped")
            with pytest.raises(SETTCancelledError):
                adapter.chat([{"role": "user", "content": "hi"}])

    assert adapter._client.calls == []


def test_anthropic_works_unchanged_with_no_control_at_all(fake_anthropic):
    adapter = _anthropic_adapter(fake_anthropic)
    assert adapter.complete("hello") == "hi"
    assert adapter._client.calls[0]["timeout"] == adapter._timeout_seconds


def test_anthropic_strips_whitespace_from_an_explicit_api_key(fake_anthropic):
    from sett.services_llm.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="  sk-ant-test  ")
    assert adapter._api_key == "sk-ant-test"


def test_anthropic_bounds_an_oversized_provider_error_message(fake_anthropic):
    """F29 regression (defensive audit #2), Anthropic side."""
    huge_message = "provider payload " * 50

    def boom(**kwargs):
        raise RuntimeError(huge_message)

    adapter = _anthropic_adapter(fake_anthropic)
    adapter._client.messages.create = boom

    with pytest.raises(SETTLLMAdapterError) as exc_info:
        adapter.complete("hello")

    assert len(str(exc_info.value)) < len(huge_message)
    assert str(exc_info.value.__cause__) == huge_message


# ── Gemini ───────────────────────────────────────────────────────────────


class _FakeGeminiModel:
    def __init__(self, model_name=None):
        self.model_name = model_name
        self.calls: list[dict] = []

    def generate_content(self, prompt, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(text="hi")

    def start_chat(self, history=None):
        model = self

        class _Session:
            def send_message(self_inner, message, **kwargs):
                model.calls.append(kwargs)
                return types.SimpleNamespace(text="hi")

        return _Session()


@pytest.fixture
def fake_gemini(monkeypatch):
    genai_module = types.ModuleType("google.generativeai")
    genai_module.configure = lambda **kwargs: None
    genai_module.GenerativeModel = _FakeGeminiModel
    genai_module.GenerationConfig = lambda **kwargs: kwargs

    google_pkg = types.ModuleType("google")
    google_pkg.generativeai = genai_module

    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_module)
    return genai_module


def _gemini_adapter(fake_gemini, **kwargs):
    from sett.services_llm.gemini import GeminiAdapter

    return GeminiAdapter(api_key="google-test", **kwargs)


def test_gemini_forwards_the_effective_timeout_via_request_options(
    fake_gemini, registry
):
    adapter = _gemini_adapter(fake_gemini)
    with _control(registry, timeout_seconds=2.0) as control:
        with lifecycle_scope(control):
            adapter.complete("hello")

    call = adapter._model.calls[0]
    assert call["request_options"]["timeout"] is not None
    assert call["request_options"]["timeout"] <= 2.0


def test_gemini_does_not_call_the_provider_once_cancelled(fake_gemini, registry):
    adapter = _gemini_adapter(fake_gemini)
    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel(reason="execution.user_stopped")
            with pytest.raises(SETTCancelledError):
                adapter.complete("hello")

    assert adapter._model.calls == []


def test_gemini_chat_does_not_call_the_provider_once_cancelled(
    fake_gemini, registry
):
    adapter = _gemini_adapter(fake_gemini)
    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel()
            with pytest.raises(SETTCancelledError):
                adapter.chat([{"role": "user", "content": "hi"}])

    assert adapter._model.calls == []


def test_gemini_works_unchanged_with_no_control_at_all(fake_gemini):
    adapter = _gemini_adapter(fake_gemini)
    assert adapter.complete("hello") == "hi"
    assert (
        adapter._model.calls[0]["request_options"]["timeout"]
        == adapter._timeout_seconds
    )


def test_gemini_strips_whitespace_from_an_explicit_api_key(fake_gemini):
    from sett.services_llm.gemini import GeminiAdapter

    adapter = GeminiAdapter(api_key="  google-test  ")
    assert adapter._api_key == "google-test"


def test_gemini_bounds_an_oversized_provider_error_message(fake_gemini):
    """F29 regression (defensive audit #2), Gemini side."""
    huge_message = "provider payload " * 50

    def boom(prompt, **kwargs):
        raise RuntimeError(huge_message)

    adapter = _gemini_adapter(fake_gemini)
    adapter._model.generate_content = boom

    with pytest.raises(SETTLLMAdapterError) as exc_info:
        adapter.complete("hello")

    assert len(str(exc_info.value)) < len(huge_message)
    assert str(exc_info.value.__cause__) == huge_message


# ── bound_provider_error_message() itself ────────────────────────────────


def test_bound_provider_error_message_leaves_short_messages_unchanged():
    error = RuntimeError("connection refused")
    assert bound_provider_error_message(error) == "connection refused"


def test_bound_provider_error_message_truncates_long_messages():
    huge_message = "x" * (DEFAULT_PROVIDER_ERROR_MAX_LENGTH + 100)
    error = RuntimeError(huge_message)

    result = bound_provider_error_message(error)

    assert len(result) < len(huge_message)
    assert result.endswith("... [truncated]")
    assert result.startswith("x" * DEFAULT_PROVIDER_ERROR_MAX_LENGTH)


def test_bound_provider_error_message_respects_a_custom_max_length():
    error = RuntimeError("0123456789")
    assert bound_provider_error_message(error, max_length=5) == (
        "01234... [truncated]"
    )
