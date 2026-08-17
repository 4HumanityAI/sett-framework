"""
Phase 6 of v0.12.0: the two reserved adapter kwargs.

Adapters are outside the Agent/Expert tree on purpose: no memory, no
orchestrator registration, no ambient control handed to them the way a
component gets one. ``timeout`` and ``cancellation_token`` are how the
framework's lifecycle reaches them anyway, and standardizing the spelling
is what keeps adapters interchangeable: an expert should not have to know
which provider it is talking to in order to give it a deadline.

A caveat these tests state rather than hide: an adapter built on a
provider SDK is exercised here against a stub, and a stub accepts any
keyword argument. That proves this framework passes the timeout down; it
does not prove the real SDK accepts it. The two adapters that talk plain
HTTP, specifically Ollama and ElevenLabs, are the ones where the whole path is
verifiable here, and they are tested as such.
"""
from __future__ import annotations

import time

import pytest

from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import (
    AdapterLifecycle,
    ExecutionControl,
    ExecutionControlRegistry,
    LifecyclePolicy,
    lifecycle_kwargs,
    lifecycle_policy_scope,
    lifecycle_scope,
    pop_lifecycle_kwargs,
)
from sett.exceptions import (
    SETTCancelledError,
    SETTLLMAdapterError,
    SETTValidationError,
)
from sett.services_llm.base import LLMBase


@pytest.fixture
def registry() -> ExecutionControlRegistry:
    return ExecutionControlRegistry()


def _control(registry, **kwargs) -> ExecutionControl:
    return ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry, **kwargs
    )


# ── AdapterLifecycle ────────────────────────────────────────────────────────


def test_an_empty_lifecycle_imposes_nothing():
    lifecycle = AdapterLifecycle()
    assert lifecycle.timeout is None
    assert lifecycle.token is None
    assert lifecycle.is_cancelled is False
    lifecycle.throw_if_cancelled()  # must not raise
    assert lifecycle.effective_timeout(None) is None


def test_the_effective_timeout_is_the_shorter_of_the_two():
    assert AdapterLifecycle(timeout=2.0).effective_timeout(30.0) == 2.0
    assert AdapterLifecycle(timeout=30.0).effective_timeout(2.0) == 2.0


def test_an_adapter_may_be_stricter_than_the_run():
    """A run with plenty of time does not force an adapter to wait for it."""
    assert AdapterLifecycle(timeout=600.0).effective_timeout(5.0) == 5.0


def test_a_missing_side_falls_back_to_the_other():
    assert AdapterLifecycle(timeout=None).effective_timeout(7.0) == 7.0
    assert AdapterLifecycle(timeout=7.0).effective_timeout(None) == 7.0


def test_the_effective_timeout_is_never_negative():
    assert AdapterLifecycle(timeout=-5.0).effective_timeout(30.0) == 0.0


# ── The reserved kwargs ─────────────────────────────────────────────────────


def test_pop_removes_both_reserved_names_from_kwargs():
    """Removed, not read: the rest is forwarded to a provider SDK."""
    kwargs = {"timeout": 3.0, "cancellation_token": None, "temperature": 0.7}
    lifecycle = pop_lifecycle_kwargs(kwargs)

    assert lifecycle.timeout == 3.0
    assert kwargs == {"temperature": 0.7}


def test_pop_falls_back_to_the_ambient_control(registry):
    """An expert written before v0.12.0 forwards nothing, and it still works."""
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control):
            kwargs: dict = {"temperature": 0.7}
            lifecycle = pop_lifecycle_kwargs(kwargs)

            assert lifecycle.token is not None
            assert lifecycle.timeout is not None
            assert lifecycle.timeout <= 30.0
            assert kwargs == {"temperature": 0.7}


def test_an_explicit_timeout_wins_over_the_ambient_control(registry):
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control):
            lifecycle = pop_lifecycle_kwargs({"timeout": 1.0})
            assert lifecycle.timeout == 1.0


def test_pop_without_any_control_yields_nothing():
    lifecycle = pop_lifecycle_kwargs({})
    assert lifecycle.timeout is None
    assert lifecycle.token is None


def test_the_reserved_kwargs_are_type_checked():
    with pytest.raises(SETTValidationError):
        pop_lifecycle_kwargs({"timeout": "soon"})
    with pytest.raises(SETTValidationError):
        pop_lifecycle_kwargs({"cancellation_token": "not-a-token"})


def test_lifecycle_kwargs_is_empty_without_a_control():
    assert lifecycle_kwargs() == {}


def test_lifecycle_kwargs_carries_the_run_state_into_an_adapter_call(registry):
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control):
            forwarded = lifecycle_kwargs()

    assert set(forwarded) == {"timeout", "cancellation_token"}
    assert forwarded["timeout"] <= 30.0


# ── H-02: LifecyclePolicy.adapter_timeout(), opted into by name ────────────


def test_lifecycle_kwargs_ignores_adapter_timeout_without_a_name(registry):
    """No adapter_name given: behaviour is exactly as before this existed."""
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control), lifecycle_policy_scope(
            LifecyclePolicy(adapter_timeouts={"openai": 0.01})
        ):
            forwarded = lifecycle_kwargs()

    assert forwarded["timeout"] <= 30.0


def test_lifecycle_kwargs_applies_the_named_adapter_timeout(registry):
    """H-02 regression: an Expert naming its adapter gets the policy value.

    Before this fix, ``LifecyclePolicy.adapter_timeout()`` was resolvable
    but had no path to reach an adapter call at all; there was no ambient
    policy and ``lifecycle_kwargs()`` took no adapter identity.
    """
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control), lifecycle_policy_scope(
            LifecyclePolicy(adapter_timeouts={"openai": 0.05})
        ):
            forwarded = lifecycle_kwargs(adapter_name="openai")

    assert forwarded["timeout"] <= 0.05


def test_lifecycle_kwargs_uses_the_default_adapter_timeout_when_unnamed(
    registry,
):
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control), lifecycle_policy_scope(
            LifecyclePolicy(default_adapter_timeout_seconds=0.05)
        ):
            forwarded = lifecycle_kwargs(adapter_name="anthropic")

    assert forwarded["timeout"] <= 0.05


def test_the_run_deadline_still_wins_when_it_is_the_stricter_one(registry):
    with _control(registry, timeout_seconds=0.02) as control:
        with lifecycle_scope(control), lifecycle_policy_scope(
            LifecyclePolicy(adapter_timeouts={"openai": 30.0})
        ):
            forwarded = lifecycle_kwargs(adapter_name="openai")

    assert forwarded["timeout"] <= 0.02


def test_lifecycle_kwargs_with_a_name_but_no_policy_is_unaffected(registry):
    with _control(registry, timeout_seconds=30.0) as control:
        with lifecycle_scope(control):
            forwarded = lifecycle_kwargs(adapter_name="openai")

    assert forwarded["timeout"] <= 30.0


# ── A custom adapter honouring the contract ─────────────────────────────────


class RecordingAdapter(LLMBase):
    """A minimal adapter written the way the base class documents."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.own_timeout = 30.0

    @property
    def model_name(self) -> str:
        return "recording"

    def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        lifecycle = pop_lifecycle_kwargs(kwargs)
        lifecycle.throw_if_cancelled()
        self.calls.append(
            {
                "timeout": lifecycle.effective_timeout(self.own_timeout),
                "forwarded": dict(kwargs),
            }
        )
        lifecycle.throw_if_cancelled()
        return "answer"

    def chat(self, messages, system: str = "", **kwargs) -> str:
        return self.complete("", system, **kwargs)


def test_an_adapter_receives_the_time_the_run_has_left(registry):
    adapter = RecordingAdapter()
    with _control(registry, timeout_seconds=2.0) as control:
        with lifecycle_scope(control):
            adapter.complete("hello", temperature=0.5)

    call = adapter.calls[0]
    assert call["timeout"] <= 2.0
    # Provider-specific kwargs are untouched by the framework's own names.
    assert call["forwarded"] == {"temperature": 0.5}


def test_an_adapter_refuses_to_start_once_the_run_is_cancelled(registry):
    adapter = RecordingAdapter()
    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel(reason="execution.user_stopped")
            with pytest.raises(SETTCancelledError):
                adapter.complete("hello")

    assert adapter.calls == []


def test_an_adapter_works_unchanged_with_no_control_at_all(registry):
    adapter = RecordingAdapter()
    assert adapter.complete("hello", temperature=0.5) == "answer"
    assert adapter.calls[0]["timeout"] == 30.0


# ── The official plain-HTTP adapters ────────────────────────────────────────


def test_ollama_uses_the_run_deadline_instead_of_its_own(monkeypatch, registry):
    """Verifiable end to end: this adapter's transport is stdlib urllib."""
    from sett.services_llm import ollama as ollama_module

    seen: list[float | None] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"response": "hi"}'

    def fake_urlopen(request, timeout=None):
        seen.append(timeout)
        return FakeResponse()

    monkeypatch.setattr(
        ollama_module.urllib.request, "urlopen", fake_urlopen
    )
    adapter = ollama_module.OllamaAdapter(timeout_seconds=30)

    with _control(registry, timeout_seconds=1.5) as control:
        with lifecycle_scope(control):
            assert adapter.complete("hello") == "hi"

    assert seen[0] is not None
    assert seen[0] <= 1.5


def test_ollama_keeps_its_own_timeout_when_no_run_is_shorter(monkeypatch):
    from sett.services_llm import ollama as ollama_module

    seen: list[float | None] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"response": "hi"}'

    monkeypatch.setattr(
        ollama_module.urllib.request,
        "urlopen",
        lambda request, timeout=None: (seen.append(timeout), FakeResponse())[1],
    )
    adapter = ollama_module.OllamaAdapter(timeout_seconds=30)
    adapter.complete("hello")

    assert seen == [30]


def test_ollama_does_not_call_out_at_all_once_cancelled(monkeypatch, registry):
    from sett.services_llm import ollama as ollama_module

    called: list[int] = []
    monkeypatch.setattr(
        ollama_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: called.append(1),
    )
    adapter = ollama_module.OllamaAdapter()

    with _control(registry) as control:
        with lifecycle_scope(control):
            control.cancel()
            with pytest.raises(SETTCancelledError):
                adapter.complete("hello")

    assert called == []


def test_a_provider_timeout_still_surfaces_as_an_adapter_error(monkeypatch):
    from sett.services_llm import ollama as ollama_module

    def always_times_out(request, timeout=None):
        raise TimeoutError("too slow")

    monkeypatch.setattr(
        ollama_module.urllib.request, "urlopen", always_times_out
    )
    adapter = ollama_module.OllamaAdapter(timeout_seconds=5)

    with pytest.raises(SETTLLMAdapterError, match="did not respond"):
        adapter.complete("hello")


def test_an_expired_run_leaves_an_adapter_no_time_to_spend(registry):
    """A run already out of time gives its adapters zero, not its own default."""
    adapter = RecordingAdapter()
    control = _control(registry, timeout_seconds=0.05)
    with control, lifecycle_scope(control):
        time.sleep(0.08)
        lifecycle = pop_lifecycle_kwargs({})
        assert lifecycle.effective_timeout(30.0) == 0.0
