"""Retry and circuit-breaking contracts."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from sett import (
    AdapterErrorKind,
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitState,
    RetryPolicy,
    SETTAdapterError,
    SETTCancelledError,
    execute_with_resilience,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def transient():
    return SETTAdapterError(
        "down",
        kind=AdapterErrorKind.UNAVAILABLE,
        retryable=True,
        reason_code="adapter.provider_unavailable",
    )


def test_circuit_starts_closed_and_opens_only_at_threshold():
    clock = Clock()
    breaker = CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=2, recovery_timeout_seconds=5),
        clock=clock,
    )
    assert breaker.state is CircuitState.CLOSED
    assert breaker.record_failure(transient()) is False
    assert breaker.state is CircuitState.CLOSED
    assert breaker.record_failure(transient()) is True
    assert breaker.state is CircuitState.OPEN


def test_open_circuit_fails_without_calling_provider():
    breaker = CircuitBreaker(CircuitBreakerPolicy(failure_threshold=1))
    breaker.record_failure(transient())
    called = []
    with pytest.raises(SETTAdapterError) as info:
        execute_with_resilience(
            lambda: called.append(1),
            circuit_breaker=breaker,
        )
    assert called == []
    assert info.value.reason_code == "adapter.circuit_open"


def test_half_open_allows_one_probe_and_success_closes():
    clock = Clock()
    breaker = CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=1, recovery_timeout_seconds=5),
        clock=clock,
    )
    breaker.record_failure(transient())
    clock.now = 5
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.before_call()
    with pytest.raises(SETTAdapterError):
        breaker.before_call()
    assert breaker.record_success() is True
    assert breaker.state is CircuitState.CLOSED


def test_authentication_does_not_open_the_circuit():
    breaker = CircuitBreaker(CircuitBreakerPolicy(failure_threshold=1))
    error = SETTAdapterError(
        "bad key",
        kind=AdapterErrorKind.AUTHENTICATION,
        retryable=False,
    )
    assert breaker.record_failure(error) is False
    assert breaker.state is CircuitState.CLOSED


def test_concurrent_failures_are_counted_once_and_open_exactly_once():
    breaker = CircuitBreaker(CircuitBreakerPolicy(failure_threshold=2_000))

    def record_batch() -> int:
        return sum(breaker.record_failure(transient()) for _ in range(250))

    with ThreadPoolExecutor(max_workers=8) as pool:
        openings = sum(pool.map(lambda _: record_batch(), range(8)))

    assert breaker.consecutive_failures == 2_000
    assert breaker.state is CircuitState.OPEN
    assert openings == 1


def test_late_failure_does_not_postpone_open_circuit_recovery():
    clock = Clock()
    breaker = CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=1, recovery_timeout_seconds=5),
        clock=clock,
    )

    assert breaker.record_failure(transient()) is True
    clock.now = 4
    assert breaker.record_failure(transient()) is False
    clock.now = 5

    assert breaker.consecutive_failures == 2
    assert breaker.state is CircuitState.HALF_OPEN


def test_retries_are_off_by_default():
    attempts = []

    def fail():
        attempts.append(1)
        raise transient()

    with pytest.raises(SETTAdapterError):
        execute_with_resilience(fail)
    assert len(attempts) == 1


def test_policy_still_does_not_retry_without_idempotency_statement():
    attempts = []

    def fail():
        attempts.append(1)
        raise transient()

    with pytest.raises(SETTAdapterError):
        execute_with_resilience(
            fail,
            retry_policy=RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,)),
        )
    assert len(attempts) == 1


def test_opted_in_idempotent_operation_retries_transient_error():
    attempts = []

    def eventually():
        attempts.append(1)
        if len(attempts) < 3:
            raise transient()
        return "ok"

    assert execute_with_resilience(
        eventually,
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,)),
        operation_is_idempotent=True,
    ) == "ok"
    assert len(attempts) == 3


def test_provider_timeout_is_ambiguous_even_if_marked_retryable():
    attempts = []

    def fail():
        attempts.append(1)
        raise SETTAdapterError(
            "remote timeout",
            kind=AdapterErrorKind.PROVIDER_TIMEOUT,
            retryable=True,
        )

    with pytest.raises(SETTAdapterError):
        execute_with_resilience(
            fail,
            retry_policy=RetryPolicy(max_attempts=3, retry_on=(SETTAdapterError,)),
            operation_is_idempotent=True,
        )
    assert len(attempts) == 1


def test_cancellation_never_opens_or_retries():
    breaker = CircuitBreaker(CircuitBreakerPolicy(failure_threshold=1))
    attempts = []

    def stop():
        attempts.append(1)
        raise SETTCancelledError("stop")

    with pytest.raises(SETTCancelledError):
        execute_with_resilience(
            stop,
            retry_policy=RetryPolicy(max_attempts=3, retry_on=(Exception,)),
            circuit_breaker=breaker,
            operation_is_idempotent=True,
        )
    assert attempts == [1]
    assert breaker.state is CircuitState.CLOSED
