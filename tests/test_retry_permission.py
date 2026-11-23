"""Retry permission is separate from how a failure is classified.

An adapter error marked ``retryable`` describes the failure. Repeating an
effect needs the application to name the error type in ``retry_on``, and a
policy that can repeat an effect needs an idempotency key before the first
attempt. A provider-supplied delay is honoured only inside the budget the
policy adopted.
"""
from __future__ import annotations

import pytest

from sett import EthicalFilter, SETTExecutor, SETTOrchestrator
from sett.core_ruler import resilience
from sett.core_ruler.action import Action
from sett.core_ruler.adapter import AdapterErrorKind
from sett.core_ruler.execution_result import ExecutionStatus
from sett.core_ruler.idempotency import IdempotencyStatus
from sett.core_ruler.resilience import (
    CircuitBreaker,
    CircuitBreakerPolicy,
    ErrorClass,
    RetryPolicy,
    classify_error,
    execute_with_resilience,
    is_retryable,
    retry_delay_seconds,
)
from sett.exceptions import SETTAdapterError
from sett.execution_ruler import executor as executor_module


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """A regression in this module must fail at once, never sleep.

    Several cases hand the framework a provider delay of a full day. If
    the control under test stopped working, an unpatched wait would hang
    the suite instead of failing it, so every wait is refused here. A test
    that expects a wait installs its own recorder over this.
    """

    def refuse(seconds, *args):
        raise AssertionError(f"unexpected real wait of {seconds} seconds")

    monkeypatch.setattr(resilience.time, "sleep", refuse)
    monkeypatch.setattr(resilience, "cooperative_sleep", refuse)
    monkeypatch.setattr(executor_module, "cooperative_sleep", refuse)


def _executor() -> SETTExecutor:
    orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    return executor


def _transient(**extra) -> SETTAdapterError:
    return SETTAdapterError(
        "provider unavailable",
        kind=AdapterErrorKind.UNAVAILABLE,
        retryable=True,
        **extra,
    )


def _failing_handler(runs: list, make_error):
    def handler(payload):
        runs.append(dict(payload))
        raise make_error()

    return handler


@pytest.mark.parametrize("key", [None, "operation-1"])
def test_max_attempts_alone_never_repeats_a_transient_adapter_error(key):
    executor = _executor()
    runs: list = []
    executor.register_handler("charge", _failing_handler(runs, _transient))
    policy = RetryPolicy(max_attempts=3)

    result = executor.submit_controlled(
        Action("charge", {"amount": 1}, idempotency_key=key), retry_policy=policy
    )

    assert policy.retries_enabled is False
    assert len(runs) == 1
    assert result.status is ExecutionStatus.FAILED
    assert result.state.attempt_count == 1
    if key is not None:
        record = executor.idempotency_store.get(key)
        assert record.status is IdempotencyStatus.FAILED_FINAL
        assert record.attempt_count == 1


def test_enabled_retries_without_a_key_are_refused_before_the_handler():
    executor = _executor()
    runs: list = []
    executor.register_handler("charge", _failing_handler(runs, _transient))
    policy = RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,))

    result = executor.submit_controlled(Action("charge", {}), retry_policy=policy)

    assert policy.retries_enabled is True
    assert runs == []
    assert result.status is ExecutionStatus.FAILED
    assert result.state.reason_code == "retry.key_required"


def test_enabled_retries_with_a_key_repeat_only_the_listed_error():
    executor = _executor()
    runs: list = []

    def handler(payload):
        runs.append(dict(payload))
        if len(runs) < 3:
            raise _transient()
        return "done"

    executor.register_handler("charge", handler)
    policy = RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,))

    result = executor.submit_controlled(
        Action("charge", {}, idempotency_key="operation-2"), retry_policy=policy
    )

    assert len(runs) == 3
    assert result.status is ExecutionStatus.SUCCESS
    assert result.state.attempt_count == 3
    record = executor.idempotency_store.get("operation-2")
    assert record.status is IdempotencyStatus.SUCCEEDED
    assert record.attempt_count == 3


def test_an_error_the_policy_does_not_list_is_not_repeated():
    executor = _executor()
    runs: list = []
    executor.register_handler("charge", _failing_handler(runs, _transient))
    policy = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))

    result = executor.submit_controlled(
        Action("charge", {}, idempotency_key="operation-3"), retry_policy=policy
    )

    assert len(runs) == 1
    assert result.status is ExecutionStatus.FAILED


def test_an_ambiguous_provider_timeout_is_not_repeated_even_when_listed():
    executor = _executor()
    runs: list = []
    executor.register_handler(
        "charge",
        _failing_handler(
            runs,
            lambda: SETTAdapterError(
                "no answer", kind=AdapterErrorKind.PROVIDER_TIMEOUT, retryable=None
            ),
        ),
    )
    policy = RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,))

    result = executor.submit_controlled(
        Action("charge", {}, idempotency_key="operation-4"), retry_policy=policy
    )

    assert len(runs) == 1
    assert result.status is ExecutionStatus.FAILED
    assert result.state.reason_code == "action.outcome_unknown"


def test_classification_is_unchanged_and_is_not_permission():
    error = _transient()
    silent = RetryPolicy(max_attempts=5)
    listing = RetryPolicy(max_attempts=5, retry_on=(SETTAdapterError,))

    # What the circuit breaker consumes: the failure is transient either way.
    assert classify_error(error) is ErrorClass.TRANSIENT
    assert classify_error(error, silent) is ErrorClass.TRANSIENT
    # What a retry loop consumes: only the application's own statement counts.
    assert is_retryable(error, silent) is False
    assert is_retryable(error, listing) is True
    not_retryable = SETTAdapterError(
        "bad key", kind=AdapterErrorKind.AUTHENTICATION, retryable=False
    )
    assert is_retryable(not_retryable, listing) is False


def test_the_circuit_breaker_still_counts_transient_adapter_failures():
    breaker = CircuitBreaker(CircuitBreakerPolicy(failure_threshold=2))
    assert breaker.record_failure(_transient()) is False
    assert breaker.record_failure(_transient()) is True


def test_adapter_calls_are_not_repeated_without_a_listed_error(monkeypatch):
    monkeypatch.setattr(resilience.time, "sleep", lambda seconds: None)
    calls: list = []

    def call():
        calls.append(1)
        raise _transient()

    with pytest.raises(SETTAdapterError):
        execute_with_resilience(
            call,
            retry_policy=RetryPolicy(max_attempts=3),
            operation_is_idempotent=True,
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (None, 1.0),
        (0, 1.0),
        (0.5, 1.0),
        (7, 7.0),
        (30.0, 30.0),
        (30.01, None),
        (86400.0, None),
        (-3, 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        ("7", 1.0),
        (True, 1.0),
    ],
)
def test_provider_delay_stays_inside_the_policy_budget(retry_after, expected):
    policy = RetryPolicy(
        max_attempts=2,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=30.0,
        retry_on=(SETTAdapterError,),
    )
    error = _transient()
    error.retry_after_seconds = retry_after

    assert retry_delay_seconds(error, policy, 1) == expected


def _retry_once(monkeypatch, retry_after):
    slept: list = []
    monkeypatch.setattr(resilience.time, "sleep", slept.append)
    calls: list = []

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise _transient(retry_after_seconds=retry_after)
        return "ok"

    policy = RetryPolicy(
        max_attempts=2,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=30.0,
        retry_on=(SETTAdapterError,),
    )
    return call, policy, calls, slept


def test_a_provider_delay_inside_the_budget_is_waited_in_full(monkeypatch):
    call, policy, calls, slept = _retry_once(monkeypatch, 7.0)

    assert execute_with_resilience(
        call, retry_policy=policy, operation_is_idempotent=True
    ) == "ok"
    assert len(calls) == 2
    assert slept == [7.0]


def test_a_provider_delay_above_the_budget_ends_the_call_without_waiting(monkeypatch):
    call, policy, calls, slept = _retry_once(monkeypatch, 86400.0)

    with pytest.raises(SETTAdapterError) as caught:
        execute_with_resilience(call, retry_policy=policy, operation_is_idempotent=True)
    assert caught.value.retry_after_seconds == 86400.0
    assert len(calls) == 1
    assert slept == []


def test_an_effect_is_not_repeated_when_the_provider_delay_exceeds_the_budget():
    executor = _executor()
    runs: list = []
    executor.register_handler(
        "charge",
        _failing_handler(runs, lambda: _transient(retry_after_seconds=86400.0)),
    )
    policy = RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,))

    result = executor.submit_controlled(
        Action("charge", {}, idempotency_key="operation-5"), retry_policy=policy
    )

    assert len(runs) == 1
    assert result.status is ExecutionStatus.FAILED
    assert result.state.reason_code == "retry.provider_delay_exceeded"
    # The key stays open for a later, deliberate resubmission.
    record = executor.idempotency_store.get("operation-5")
    assert record.status is IdempotencyStatus.FAILED_RETRYABLE


def test_an_effect_waits_at_least_the_provider_delay_before_repeating(monkeypatch):
    waited: list = []
    monkeypatch.setattr(
        executor_module,
        "cooperative_sleep",
        lambda seconds, token: waited.append(seconds),
    )
    executor = _executor()
    runs: list = []

    def handler(payload):
        runs.append(1)
        if len(runs) == 1:
            raise _transient(retry_after_seconds=4.0)
        return "done"

    executor.register_handler("charge", handler)
    policy = RetryPolicy(max_attempts=2, retry_on=(SETTAdapterError,))

    result = executor.submit_controlled(
        Action("charge", {}, idempotency_key="operation-6"), retry_policy=policy
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert len(runs) == 2
    assert waited == [4.0]
