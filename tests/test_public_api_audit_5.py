"""
Public API audit #5 regressions.

Three findings, one section each:

API-01  Provider SDK exceptions escaped adapter constructors untranslated,
        so ``except SETTError`` around "build my adapters" did not hold.
API-02  The CHANGELOG promised ``validate_duration_seconds()`` covered
        durations "everywhere", and the five adapters that keep their own
        ``timeout_seconds`` were the everywhere it did not cover.
API-03  Transitioning an unknown identity is a silent no-op in the
        idempotency store and in the control registry. That is the
        intended contract, it was simply never written down; these tests
        fix the current behaviour so a later change has to be deliberate.

The provider doubles follow ``test_llm_cloud_adapters_v012.py``: none of
``openai``, ``anthropic`` or ``google-generativeai`` is installed here, so
each is stood up as a module in ``sys.modules``. What that proves is what
SETT controls - the translation at the boundary - not that the real SDK
raises exactly this way.
"""
from __future__ import annotations

import sys
import types

import pytest

from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.idempotency import ActionAttempt, InMemoryIdempotencyStore
from sett.core_ruler.lifecycle import (
    ExecutionControl,
    ExecutionControlRegistry,
)
from sett.exceptions import (
    SETTCancelledError,
    SETTError,
    SETTLLMAdapterError,
    SETTServiceAdapterError,
    SETTValidationError,
)


class _Exploding:
    """A provider client whose constructor refuses to be built."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("provider client refused to start")


# ── API-01: constructors translate whatever the SDK raises ───────────────


def test_openai_constructor_failure_becomes_a_sett_error(monkeypatch):
    module = types.ModuleType("openai")
    module.OpenAI = _Exploding
    monkeypatch.setitem(sys.modules, "openai", module)

    from sett.services_llm.openai import OpenAIAdapter

    with pytest.raises(SETTLLMAdapterError) as exc_info:
        OpenAIAdapter(api_key="sk-test")
    assert isinstance(exc_info.value, SETTError)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_anthropic_constructor_failure_becomes_a_sett_error(monkeypatch):
    module = types.ModuleType("anthropic")
    module.Anthropic = _Exploding
    monkeypatch.setitem(sys.modules, "anthropic", module)

    from sett.services_llm.anthropic import AnthropicAdapter

    with pytest.raises(SETTLLMAdapterError) as exc_info:
        AnthropicAdapter(api_key="sk-ant-test")
    assert isinstance(exc_info.value, SETTError)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


@pytest.mark.parametrize("failing_call", ["configure", "GenerativeModel"])
def test_gemini_constructor_failure_becomes_a_sett_error(
    monkeypatch, failing_call
):
    """Gemini builds in two steps; either one failing must translate."""

    def _explode(*args, **kwargs):
        raise RuntimeError("provider client refused to start")

    genai_module = types.ModuleType("google.generativeai")
    genai_module.configure = (
        _explode if failing_call == "configure" else (lambda **kwargs: None)
    )
    genai_module.GenerativeModel = (
        _Exploding if failing_call == "GenerativeModel" else (
            lambda **kwargs: types.SimpleNamespace()
        )
    )
    google_pkg = types.ModuleType("google")
    google_pkg.generativeai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_module)

    from sett.services_llm.gemini import GeminiAdapter

    with pytest.raises(SETTLLMAdapterError) as exc_info:
        GeminiAdapter(api_key="google-test")
    assert isinstance(exc_info.value, SETTError)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_google_client_construction_failure_is_translated():
    """
    Not a DefaultCredentialsError, so the previous version re-raised it
    untranslated: "build a Google client" had one documented failure type
    and an open-ended set of undocumented ones.
    """
    from sett.services_google_auth import build_google_client

    def factory(**kwargs):
        raise RuntimeError("the client library exploded for another reason")

    with pytest.raises(SETTServiceAdapterError) as exc_info:
        build_google_client(factory, credentials=None, adapter_name="Test")
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_google_client_still_names_missing_credentials_specifically():
    """The specific message survives the generic translation."""
    from sett.services_google_auth import build_google_client

    class DefaultCredentialsError(Exception):
        pass

    def factory(**kwargs):
        raise DefaultCredentialsError("could not automatically determine")

    with pytest.raises(SETTServiceAdapterError) as exc_info:
        build_google_client(factory, credentials=None, adapter_name="Test")
    assert "credentials not found" in str(exc_info.value)


def test_google_client_lets_lifecycle_exceptions_through():
    """A cancellation is not the provider failing; it keeps its type."""
    from sett.services_google_auth import build_google_client

    def factory(**kwargs):
        raise SETTCancelledError("somebody asked for this to stop")

    with pytest.raises(SETTCancelledError):
        build_google_client(factory, credentials=None, adapter_name="Test")


# ── API-02: every adapter's own timeout goes through the validator ───────


@pytest.fixture
def every_provider_module(monkeypatch):
    """Stand up all three SDK doubles at once, none of them failing."""
    openai_module = types.ModuleType("openai")
    openai_module.OpenAI = lambda **kwargs: types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "openai", openai_module)

    anthropic_module = types.ModuleType("anthropic")
    anthropic_module.Anthropic = lambda **kwargs: types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_module)

    genai_module = types.ModuleType("google.generativeai")
    genai_module.configure = lambda **kwargs: None
    genai_module.GenerativeModel = lambda **kwargs: types.SimpleNamespace()
    google_pkg = types.ModuleType("google")
    google_pkg.generativeai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_module)


def _build(adapter_name: str, **kwargs):
    if adapter_name == "openai":
        from sett.services_llm.openai import OpenAIAdapter

        return OpenAIAdapter(api_key="sk-test", **kwargs)
    if adapter_name == "anthropic":
        from sett.services_llm.anthropic import AnthropicAdapter

        return AnthropicAdapter(api_key="sk-ant-test", **kwargs)
    if adapter_name == "gemini":
        from sett.services_llm.gemini import GeminiAdapter

        return GeminiAdapter(api_key="google-test", **kwargs)
    if adapter_name == "ollama":
        from sett.services_llm.ollama import OllamaAdapter

        return OllamaAdapter(**kwargs)
    if adapter_name == "elevenlabs":
        pytest.importorskip("requests")
        from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter

        return ElevenLabsTTSAdapter(api_key="eleven-test", **kwargs)
    raise AssertionError(f"unknown adapter {adapter_name!r}")


ADAPTERS = ["openai", "anthropic", "gemini", "ollama", "elevenlabs"]


@pytest.mark.parametrize("adapter_name", ADAPTERS)
@pytest.mark.parametrize(
    "bad_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -5.0, True, "30", None],
)
def test_adapter_refuses_an_unusable_timeout(
    every_provider_module, adapter_name, bad_timeout
):
    """
    NaN is the one that matters: it compares False against every bound, so
    an adapter holding one has a timeout that is never reached. Zero is
    refused for the reason ``validate_duration_seconds`` documents - the
    honest spelling of "already expired" is not a duration.
    """
    with pytest.raises(SETTValidationError):
        _build(adapter_name, timeout_seconds=bad_timeout)


@pytest.mark.parametrize("adapter_name", ADAPTERS)
def test_adapter_keeps_a_usable_timeout_as_a_float(
    every_provider_module, adapter_name
):
    adapter = _build(adapter_name, timeout_seconds=12)
    assert adapter._timeout_seconds == 12.0
    assert isinstance(adapter._timeout_seconds, float)


@pytest.mark.parametrize("adapter_name", ADAPTERS)
def test_adapter_default_timeout_is_itself_valid(
    every_provider_module, adapter_name
):
    """A default that the validator would refuse is a bug shipped by us."""
    adapter = _build(adapter_name)
    assert adapter._timeout_seconds > 0


# ── API-03: an unknown identity is a no-op, on purpose ───────────────────


REAL_KEY = "real-key"


def _reserved_store() -> InMemoryIdempotencyStore:
    """A store holding one real reservation, to prove it stays untouched."""
    store = InMemoryIdempotencyStore()
    reservation = store.reserve(
        key=REAL_KEY,
        fingerprint="fingerprint-a",
        trace_id="trace-a",
    )
    assert reservation.acquired
    return store


def test_marking_an_unknown_key_in_progress_is_a_no_op():
    store = _reserved_store()
    before = store.get(REAL_KEY)

    store.mark_in_progress(key="never-reserved")

    assert store.get("never-reserved") is None
    assert store.get(REAL_KEY) == before


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("mark_succeeded", {"digest": "a-digest"}),
        ("mark_failed", {"retryable": True}),
    ],
)
def test_completing_an_unknown_key_is_a_no_op(method, kwargs):
    store = _reserved_store()
    before = store.get(REAL_KEY)

    getattr(store, method)(key="never-reserved", **kwargs)

    assert store.get("never-reserved") is None
    assert store.get(REAL_KEY) == before


def test_completing_an_unknown_attempt_is_a_no_op():
    store = _reserved_store()
    store.record_attempt(
        ActionAttempt(
            attempt_id="attempt-1",
            action_id="action-1",
            idempotency_key=REAL_KEY,
            attempt_number=1,
            trace_id="trace-a",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    attempts_before = store.attempts_for(REAL_KEY)

    store.complete_attempt(
        "attempt-that-never-started",
        finished_at="2026-01-01T00:00:01+00:00",
        status="succeeded",
    )

    assert store.attempts_for(REAL_KEY) == attempts_before


def test_releasing_an_unknown_run_id_is_a_no_op():
    """
    Same shape as ``snapshot()`` and ``cancel_subtree()``, which already
    declare what they do with a run_id the registry never saw.
    """
    registry = ExecutionControlRegistry()
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry
    )
    with control:
        run_id = control.context.run_id
        assert registry.is_registered(run_id)

        registry.release("a-run-id-that-never-existed")

        assert registry.control_for(run_id) is control
        assert registry.live_run_ids == (run_id,)


# ── API-07: the docstrings stay there ────────────────────────────────────


def _public_definitions_without_docstring() -> list[str]:
    """Walk the shipped package and report undocumented public callables.

    Private classes (a leading underscore on the class itself) are out of
    scope: they are implementation, and their own class docstring is where
    the explanation belongs. Dunder methods other than ``__init__`` are
    out too - ``__repr__`` means what Python says it means.
    """
    import ast
    import pathlib

    package_root = pathlib.Path(__file__).resolve().parent.parent / "sett"
    missing: list[str] = []

    for path in sorted(package_root.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(package_root.parent)

        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                if ast.get_docstring(node) is None:
                    missing.append(f"{relative}:{node.lineno} class {node.name}")
                for item in node.body:
                    if not isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        continue
                    name = item.name
                    if name.startswith("_") and name != "__init__":
                        continue
                    if ast.get_docstring(item) is None:
                        missing.append(
                            f"{relative}:{item.lineno} {node.name}.{name}"
                        )

        for item in module.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith("_"):
                    continue
                if ast.get_docstring(item) is None:
                    missing.append(f"{relative}:{item.lineno} {item.name}()")

    return missing


def test_every_public_definition_carries_a_docstring():
    """
    A guard rather than a habit, for the reason every other guard in this
    suite exists: "remember to write one" is a rule that holds until the
    first hurried change. Public API audit #5 counted dozens of public
    getters, factories and constructors with nothing said about them, and
    a list fixed by hand once refills the same way it filled the first
    time.

    A one-line docstring is enough. This does not check quality, only that
    somebody was asked to say what the thing is.
    """
    missing = _public_definitions_without_docstring()
    assert not missing, (
        f"{len(missing)} public definitions have no docstring:\n"
        + "\n".join(missing)
    )


def test_public_init_methods_declare_their_return_type():
    """``-> None`` on ``__init__``, so a type checker reads the whole file."""
    import ast
    import pathlib

    package_root = pathlib.Path(__file__).resolve().parent.parent / "sett"
    missing: list[str] = []

    for path in sorted(package_root.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(package_root.parent)
        for node in ast.walk(module):
            if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                continue
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "__init__"
                    and item.returns is None
                ):
                    missing.append(f"{relative}:{item.lineno} {node.name}")

    assert not missing, (
        "__init__ without an explicit '-> None':\n" + "\n".join(missing)
    )


# ── API-04: the reference documents what the package exports ─────────────


def test_every_exported_symbol_appears_in_the_api_reference():
    """
    Public API audit #5 found twelve names in ``sett.__all__`` with zero
    mentions in ``docs/api_reference.md`` - not thin coverage, none at
    all. Exporting a name is a promise to support it; a reader who cannot
    find out what it does has been given a promise about nothing.

    The check is presence, not quality: a symbol somebody remembered to
    write about will not silently fall out of the document, and a symbol
    added to ``__all__`` cannot ship undocumented.
    """
    import pathlib

    import sett

    reference = (
        pathlib.Path(__file__).resolve().parent.parent
        / "docs"
        / "api_reference.md"
    ).read_text(encoding="utf-8")

    undocumented = [name for name in sett.__all__ if name not in reference]
    assert not undocumented, (
        f"{len(undocumented)} exported symbols are not mentioned in "
        f"docs/api_reference.md:\n" + "\n".join(sorted(undocumented))
    )
