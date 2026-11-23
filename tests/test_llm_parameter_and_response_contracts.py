"""Offline request, response and diagnostic contracts; no provider credentials."""
from __future__ import annotations

import importlib
import json
import sys
import traceback
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from sett.exceptions import SETTLLMAdapterError


def _install_sdk(monkeypatch, provider, create):
    built = []

    def client(**kwargs):
        built.append(kwargs)
        if provider == "openai":
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        if provider == "anthropic":
            return SimpleNamespace(messages=SimpleNamespace(create=create))
        return SimpleNamespace(models=SimpleNamespace(generate_content=create))

    if provider == "gemini":
        module = ModuleType("google.genai")
        module.Client = client
        google = ModuleType("google")
        google.genai = module
        monkeypatch.setitem(sys.modules, "google", google)
        monkeypatch.setitem(sys.modules, "google.genai", module)
    else:
        module = ModuleType(provider)
        setattr(module, "OpenAI" if provider == "openai" else "Anthropic", client)
        monkeypatch.setitem(sys.modules, provider, module)
    adapter_type = getattr(
        importlib.import_module("sett.services_llm." + provider),
        {"openai": "OpenAIAdapter", "anthropic": "AnthropicAdapter", "gemini": "GeminiAdapter"}[provider],
    )
    return adapter_type, built


def _call(adapter, operation, **kwargs):
    if operation == "complete":
        return adapter.complete("hello", **kwargs)
    return adapter.chat([{"role": "user", "content": "hello"}], **kwargs)


def _openai_response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))])


@pytest.mark.parametrize("operation", ["complete", "chat"])
def test_openai_explicit_completion_budget_omits_unsupported_parameters(monkeypatch, operation):
    calls = []

    def create(*, model, max_completion_tokens, messages, timeout):
        calls.append((model, max_completion_tokens, messages, timeout))
        return _openai_response()

    adapter_type, built = _install_sdk(monkeypatch, "openai", create)
    adapter = adapter_type(api_key="synthetic", model="chosen-model", max_completion_tokens=4096, temperature=None)
    assert _call(adapter, operation) == "answer"
    assert len(calls) == 1
    assert calls[0][0:2] == ("chosen-model", 4096)
    assert calls[0][2][-1] == {"role": "user", "content": "hello"}
    assert calls[0][3] == 60.0
    assert built == [{"api_key": "synthetic", "max_retries": 0}]


@pytest.mark.parametrize("operation", ["complete", "chat"])
def test_openai_legacy_defaults_remain_unchanged(monkeypatch, operation):
    calls = []

    def create(*, model, max_tokens, temperature, messages, timeout):
        calls.append((max_tokens, temperature))
        return _openai_response()

    adapter_type, _ = _install_sdk(monkeypatch, "openai", create)
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    assert _call(adapter, operation) == "answer"
    assert calls == [(1024, 0.75)]


@pytest.mark.parametrize("operation", ["complete", "chat"])
def test_openai_call_overrides_do_not_mutate_instance_defaults(monkeypatch, operation):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return _openai_response()

    adapter_type, _ = _install_sdk(monkeypatch, "openai", create)
    adapter = adapter_type(api_key="synthetic", model="chosen-model", max_completion_tokens=500, temperature=None)
    _call(adapter, operation, max_tokens=23, temperature=0.2)
    _call(adapter, operation)
    _call(adapter, operation, max_completion_tokens=77)
    assert calls[0]["max_tokens"] == 23 and calls[0]["temperature"] == 0.2
    assert "max_completion_tokens" not in calls[0]
    assert calls[1]["max_completion_tokens"] == 500 and "temperature" not in calls[1]
    assert "max_tokens" not in calls[1]
    assert calls[2]["max_completion_tokens"] == 77 and "max_tokens" not in calls[2]


@pytest.mark.parametrize("operation", ["complete", "chat"])
def test_openai_per_call_modern_settings_replace_legacy_defaults(monkeypatch, operation):
    calls = []

    def create(*, model, max_completion_tokens, messages, timeout):
        calls.append(max_completion_tokens)
        return _openai_response()

    adapter_type, _ = _install_sdk(monkeypatch, "openai", create)
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    assert _call(adapter, operation, max_completion_tokens=73, temperature=None) == "answer"
    assert calls == [73]


@pytest.mark.parametrize("settings", [
    {"max_tokens": 2, "max_completion_tokens": 3},
    {"max_tokens": 0}, {"max_tokens": True},
    {"max_completion_tokens": -1}, {"max_completion_tokens": 1.5},
])
def test_openai_refuses_invalid_token_settings_before_client_construction(monkeypatch, settings):
    adapter_type, built = _install_sdk(monkeypatch, "openai", lambda **kwargs: _openai_response())
    with pytest.raises(SETTLLMAdapterError) as caught:
        adapter_type(api_key="synthetic", model="chosen-model", **settings)
    assert caught.value.reason_code == "adapter.configuration_invalid"
    assert built == []


@pytest.mark.parametrize("operation", ["complete", "chat"])
def test_openai_conflicting_call_limits_do_not_reach_provider(monkeypatch, operation):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return _openai_response()

    adapter_type, _ = _install_sdk(monkeypatch, "openai", create)
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    with pytest.raises(SETTLLMAdapterError) as caught:
        _call(adapter, operation, max_tokens=2, max_completion_tokens=3)
    assert caught.value.reason_code == "adapter.configuration_invalid"
    assert caught.value.operation == operation
    assert calls == []


@pytest.mark.parametrize("operation", ["complete", "chat"])
@pytest.mark.parametrize("blocks,expected", [
    ([SimpleNamespace(type="text", text="one")], "one"),
    ([SimpleNamespace(type="thinking", thinking="private"), SimpleNamespace(type="text", text="one")], "one"),
    ([SimpleNamespace(type="text", text="one"), SimpleNamespace(type="redacted_thinking", data="private"), SimpleNamespace(type="text", text="two")], "onetwo"),
    ([SimpleNamespace(type="thinking", thinking="private")], ""),
    ([SimpleNamespace(type="tool_use", input={"text": "not an answer"})], ""),
    ([], ""),
])
def test_anthropic_returns_only_visible_text_in_order(monkeypatch, operation, blocks, expected):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=blocks)

    adapter_type, _ = _install_sdk(monkeypatch, "anthropic", create)
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    assert _call(adapter, operation) == expected
    assert len(calls) == 1
    assert "temperature" not in calls[0]


@pytest.mark.parametrize("operation", ["complete", "chat"])
@pytest.mark.parametrize("block", [SimpleNamespace(type="text", text=None), SimpleNamespace(type="text"), SimpleNamespace(text="missing type")])
def test_anthropic_malformed_text_is_a_normalized_response_error(monkeypatch, operation, block):
    adapter_type, _ = _install_sdk(monkeypatch, "anthropic", lambda **kwargs: SimpleNamespace(content=[block]))
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    with pytest.raises(SETTLLMAdapterError) as caught:
        _call(adapter, operation)
    assert caught.value.reason_code == "adapter.invalid_response"


@pytest.mark.parametrize("explicit,google_key,gemini_key,expected", [
    (" explicit ", "google", "gemini", "explicit"),
    (None, " google ", "gemini", "google"),
    (None, None, " gemini ", "gemini"),
    ("", "", "gemini", "gemini"),
    ("   ", "google", "gemini", None),
    (None, "   ", "gemini", None),
    (None, None, "   ", None),
    (None, None, None, None),
])
def test_gemini_credential_precedence_and_blank_handling(monkeypatch, explicit, google_key, gemini_key, expected):
    for name, value in (("GOOGLE_API_KEY", google_key), ("GEMINI_API_KEY", gemini_key)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    adapter_type, built = _install_sdk(monkeypatch, "gemini", lambda **kwargs: SimpleNamespace(text="answer"))
    if expected is None:
        with pytest.raises(SETTLLMAdapterError) as caught:
            adapter_type(api_key=explicit, model="chosen-model")
        assert caught.value.reason_code == "adapter.credentials_missing"
        assert built == []
    else:
        adapter = adapter_type(api_key=explicit, model="chosen-model")
        assert built == [{"api_key": expected}]
        assert adapter.complete("hello") == "answer"
        assert adapter.chat([{"role": "user", "content": "hello"}]) == "answer"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("operation", ["complete", "chat"])
def test_safe_error_views_exclude_provider_secret_but_traceback_remains_sensitive(monkeypatch, provider, operation):
    secret = "SYNTHETIC_SECRET_NOT_A_CREDENTIAL"
    original = RuntimeError(secret)
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise original

    adapter_type, _ = _install_sdk(monkeypatch, provider, fail)
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    with pytest.raises(SETTLLMAdapterError) as caught:
        _call(adapter, operation)
    error = caught.value
    assert len(calls) == 1
    assert secret not in str(error)
    assert secret not in json.dumps(error.safe_view())
    assert error.__cause__ is original
    assert secret in "".join(traceback.format_exception(error))


def test_documented_ollama_configuration_runs_without_network(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs/adapters.md").read_text(encoding="utf-8")
    snippet = document.split("```python\n")[2].split("```", 1)[0]
    monkeypatch.setenv("SETT_LLM_MODEL", "synthetic-model")
    namespace = {}
    exec(compile(snippet, "docs/adapters.md", "exec"), namespace)
    from sett.services_llm.ollama import OllamaAdapter

    assert isinstance(namespace["llm"], OllamaAdapter)
    assert namespace["llm"].model_name == "synthetic-model"


def test_documented_safe_logging_omits_provider_traceback(monkeypatch, caplog):
    def fail(**kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_TEXT")

    adapter_type, _ = _install_sdk(monkeypatch, "openai", fail)
    adapter = adapter_type(api_key="synthetic", model="chosen-model")
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs/security_model.md").read_text(encoding="utf-8")
    snippet = document.split("## Adapter credentials and diagnostic output", 1)[1].split("```python\n", 1)[1].split("```", 1)[0]
    namespace = {}
    exec(compile(snippet, "docs/security_model.md", "exec"), namespace)
    assert namespace["complete_with_safe_failure_logging"](adapter, "hello") is None
    assert "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in caplog.text
    assert any("Adapter operation failed" in record.message for record in caplog.records)
    assert all(not record.exc_info for record in caplog.records)
