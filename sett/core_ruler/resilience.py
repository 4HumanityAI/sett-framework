"""Opt-in retry and circuit breaking for effects and external adapters.

The default is one attempt. Retries are opt-in and narrow, because the
question a retry answers is not "did this fail" but "is it safe to do it
twice", and those have different answers far more often than retry
libraries suggest.

Two rules do the real work here.

Retrying requires knowing what failed. An error the framework has never
seen before is not retried: nothing is known about whether it left an
effect behind, and repeating an unknown effect is worse than reporting a
failure. So a policy lists the error types it is willing to retry, and
everything else fails once.

Some errors are never retried, whatever a policy says. An ethical
rejection is the clearest: retrying one would be an attempt to get past
the filter by asking again, which is the one thing this framework is built
to prevent. Cancellation and deadlines are next: somebody asked for the
work to stop, or it ran out of time, and trying again ignores both. These
are enforced below the policy, so no configuration can turn them on.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import TypeVar

from sett.core_ruler.adapter import AdapterErrorKind, record_adapter_event
from sett.core_ruler.lifecycle import (
    CancellationToken,
    cooperative_sleep,
    current_execution_control,
    validate_duration_seconds,
)
from sett.exceptions import (
    SETTAdapterError,
    SETTAuthorizationError,
    SETTCancelledError,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTIdempotencyConflictError,
    SETTTimeoutError,
    SETTValidationError,
)

# Errors that are never retried automatically, regardless of policy.
#
# Each is here for its own reason, not as a general blocklist: a rejection
# must not be re-asked, a cancellation and a deadline were explicit stops,
# a fingerprint conflict means the key describes something else, and
# configuration and validation errors will fail identically every time
# while doing nothing but waste the caller's remaining deadline.
_NEVER_RETRYABLE: tuple[type[BaseException], ...] = (
    SETTEthicalFilterRejectedError,
    SETTCancelledError,
    SETTTimeoutError,
    SETTIdempotencyConflictError,
    SETTConfigurationError,
    SETTValidationError,
    # An authorization block is decided again only by a new submission.
    SETTAuthorizationError,
)


class ErrorClass(str, Enum):
    """How a failed attempt should be treated."""

    TRANSIENT = "transient"
    """Failed for a reason that may not recur; safe to attempt again."""

    PERMANENT = "permanent"
    """Failed for a reason that will recur; attempting again is waste."""

    AMBIGUOUS = "ambiguous"
    """Nobody knows whether the effect happened.

    The dangerous case, and the reason this class exists separately from
    PERMANENT: a request that timed out after being sent may well have
    succeeded on the other side. Treated as not retryable and reported as
    ``action.outcome_unknown``, because "I don't know" is an honest
    terminal state and a second charge is not.
    """


class CircuitState(str, Enum):
    """Whether an adapter call may proceed through a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    """When transient failures open a circuit and when one probe may retry."""

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.failure_threshold, int)
            or isinstance(self.failure_threshold, bool)
            or self.failure_threshold < 1
        ):
            raise SETTValidationError("failure_threshold must be an int of at least 1.")
        object.__setattr__(
            self,
            "recovery_timeout_seconds",
            validate_duration_seconds(
                self.recovery_timeout_seconds,
                name="recovery_timeout_seconds",
            ),
        )


class CircuitBreaker:
    """A per-instance, thread-safe circuit with one half-open probe.

    The breaker consumes ``classify_error()`` below.  It deliberately has
    no provider-specific error table of its own, so retry and circuit state
    cannot disagree about whether a failure is transient.
    """

    def __init__(
        self,
        policy: CircuitBreakerPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a closed breaker; no calls or background work occur."""
        self.policy = policy or CircuitBreakerPolicy()
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probe_active = False
        self._lock = RLock()

    @property
    def state(self) -> CircuitState:
        """Current state, advancing OPEN to HALF_OPEN after the cooldown."""
        with self._lock:
            self._advance_if_ready()
            return self._state

    @property
    def consecutive_failures(self) -> int:
        """Transient failures accumulated since the last success."""
        with self._lock:
            return self._consecutive_failures

    def _advance_if_ready(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at
            >= self.policy.recovery_timeout_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_probe_active = False

    def before_call(
        self,
        *,
        adapter_name: str | None = None,
        operation: str | None = None,
    ) -> None:
        """Reserve permission for a call or raise without contacting a provider."""
        with self._lock:
            self._advance_if_ready()
            if self._state is CircuitState.OPEN:
                raise SETTAdapterError(
                    "The adapter circuit is open; the provider was not called.",
                    kind=AdapterErrorKind.UNAVAILABLE,
                    adapter_name=adapter_name,
                    operation=operation,
                    retryable=False,
                    reason_code="adapter.circuit_open",
                )
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_probe_active:
                    raise SETTAdapterError(
                        "The adapter circuit is half-open and its probe is already in flight.",
                        kind=AdapterErrorKind.UNAVAILABLE,
                        adapter_name=adapter_name,
                        operation=operation,
                        retryable=False,
                        reason_code="adapter.circuit_probe_in_flight",
                    )
                self._half_open_probe_active = True

    def record_success(self) -> bool:
        """Close the circuit; return whether this changed its state."""
        with self._lock:
            changed = self._state is not CircuitState.CLOSED
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_probe_active = False
            return changed

    def record_failure(self, error: BaseException) -> bool:
        """Count only transient failures; return whether the circuit opened."""
        classification = classify_error(error)
        with self._lock:
            self._half_open_probe_active = False
            if classification is not ErrorClass.TRANSIENT:
                return False
            self._consecutive_failures += 1
            if (
                self._state is CircuitState.HALF_OPEN
                or self._consecutive_failures >= self.policy.failure_threshold
            ):
                changed = self._state is not CircuitState.OPEN
                self._state = CircuitState.OPEN
                if changed:
                    # The cooldown starts at the OPEN transition. A call that
                    # was already in flight may fail after another call opened
                    # the circuit, but that late result must not move the
                    # recovery window. A failed HALF_OPEN probe is a new
                    # transition and therefore starts a fresh cooldown.
                    self._opened_at = self._clock()
                return changed
            return False


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many attempts an action gets, and how long between them.

    ``max_attempts=1`` and an empty ``retry_on`` are the defaults on
    purpose: an application that has not said which failures are safe to
    repeat has not said anything that would justify repeating an effect on
    its behalf.
    """

    max_attempts: int = 1
    initial_backoff_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    retry_on: tuple[type[BaseException], ...] = field(default_factory=tuple)
    """Error types this policy is willing to attempt again.

    Empty, the default, means nothing is retried, so raising
    ``max_attempts`` alone never causes a retry. That is deliberate: the
    number of attempts and the decision that repeating is safe are two
    separate statements, and only the second one is about correctness.
    """

    def __post_init__(self) -> None:
        # int, not just "whole number": range(1, max_attempts + 1), which is
        # what the attempt loop actually uses, raises a bare
        # TypeError for a float bound rather than failing where the value
        # was given. bool is technically an int subclass but never a
        # meaningful attempt count, so it is excluded explicitly.
        if not isinstance(self.max_attempts, int) or isinstance(
            self.max_attempts, bool
        ):
            raise SETTValidationError("max_attempts must be an int.")
        if self.max_attempts < 1:
            raise SETTValidationError("max_attempts must be at least 1.")
        for name in (
            "initial_backoff_seconds",
            "backoff_multiplier",
            "max_backoff_seconds",
        ):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                validate_duration_seconds(value, name=name, allow_zero=True),
            )
        if self.backoff_multiplier < 1.0 and self.initial_backoff_seconds:
            raise SETTValidationError(
                "backoff_multiplier must be at least 1.0; a shrinking "
                "backoff would attempt faster the longer a failure lasts."
            )
        retry_on = tuple(self.retry_on)
        for entry in retry_on:
            if not isinstance(entry, type) or not issubclass(
                entry, BaseException
            ):
                raise SETTValidationError(
                    "retry_on must contain exception types."
                )
        object.__setattr__(self, "retry_on", retry_on)

    def backoff_for(self, attempt_number: int) -> float:
        """Seconds to wait before attempt ``attempt_number + 1``."""
        if attempt_number < 1 or not self.initial_backoff_seconds:
            return 0.0
        delay = self.initial_backoff_seconds * (
            self.backoff_multiplier ** (attempt_number - 1)
        )
        return min(delay, self.max_backoff_seconds)

    @property
    def retries_enabled(self) -> bool:
        """Whether this policy can actually retry: attempts AND ``retry_on``."""
        return self.max_attempts > 1 and bool(self.retry_on)


def classify_error(
    error: BaseException, policy: RetryPolicy | None = None
) -> ErrorClass:
    """Decide how a failed attempt should be treated.

    A deadline reached while an external request was in flight is reported
    as AMBIGUOUS rather than PERMANENT: the request may already have
    arrived. The distinction is what stops a timed-out payment from being
    charged a second time.
    """
    if isinstance(error, SETTTimeoutError):
        return ErrorClass.AMBIGUOUS
    if isinstance(error, _NEVER_RETRYABLE):
        return ErrorClass.PERMANENT
    if isinstance(error, SETTAdapterError):
        if error.kind is AdapterErrorKind.PROVIDER_TIMEOUT:
            return ErrorClass.AMBIGUOUS
        if error.retryable is True:
            return ErrorClass.TRANSIENT
        if error.retryable is False:
            return ErrorClass.PERMANENT
    if policy is not None and policy.retry_on and isinstance(
        error, policy.retry_on
    ):
        return ErrorClass.TRANSIENT
    # Unknown failures are not retried. Nothing is known about whether
    # they left an effect behind, and guessing is what produces duplicates.
    return ErrorClass.PERMANENT


def is_retryable(error: BaseException, policy: RetryPolicy) -> bool:
    """Whether this error may be attempted again under this policy.

    The never-retryable list is checked first and cannot be overridden by
    a policy: listing ``SETTEthicalFilterRejectedError`` in ``retry_on``
    does not make a rejection retryable, it just does not do anything.

    Classification and permission are separate. ``classify_error()``
    describes the failure, and an adapter that marks its own error as
    ``retryable`` is describing it too. Neither is permission to repeat an
    operation: that comes only from the application naming the error type
    in ``retry_on``. With an empty ``retry_on`` nothing is retryable.
    """
    if isinstance(error, _NEVER_RETRYABLE):
        return False
    if not policy.retry_on or not isinstance(error, policy.retry_on):
        return False
    return classify_error(error, policy) is ErrorClass.TRANSIENT


def retry_delay_seconds(
    error: BaseException, policy: RetryPolicy, attempt_number: int
) -> float | None:
    """Seconds to wait before the next attempt, or ``None`` to stop.

    A provider may ask for a pause through ``retry_after_seconds``. The
    request is honoured only inside the budget the application adopted:
    the wait is never shorter than the provider asked for, and when the
    provider asks for more than ``max_backoff_seconds`` the answer is
    ``None``, because waiting longer would exceed the policy and retrying
    sooner would ignore the provider. A missing, negative, non-finite or
    non-numeric value is not a provider request and leaves the policy's
    own backoff in place.
    """
    delay = policy.backoff_for(attempt_number)
    retry_after = getattr(error, "retry_after_seconds", None)
    if (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, (int, float))
        or not math.isfinite(retry_after)
        or retry_after <= 0
    ):
        return delay
    if retry_after > policy.max_backoff_seconds:
        return None
    return max(delay, float(retry_after))


T = TypeVar("T")


def execute_with_resilience(
    call: Callable[[], T],
    *,
    adapter: object | None = None,
    operation: str = "call",
    retry_policy: RetryPolicy | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    operation_is_idempotent: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> T:
    """Run a provider call with explicitly opted-in retry and circuit state.

    ``operation_is_idempotent`` is a separate required statement.  A policy
    naming a transient error says the failure may pass; it does not prove a
    possibly billable request is safe to send twice.
    """
    policy = retry_policy or RetryPolicy()
    control = current_execution_control()
    token = cancellation_token or (None if control is None else control.token)
    adapter_name = None if adapter is None else type(adapter).__name__

    for attempt in range(1, policy.max_attempts + 1):
        if token is not None:
            token.throw_if_cancelled()
        state_before = None if circuit_breaker is None else circuit_breaker.state
        if circuit_breaker is not None:
            circuit_breaker.before_call(
                adapter_name=adapter_name,
                operation=operation,
            )
        try:
            result = call()
        except BaseException as error:
            opened = (
                False
                if circuit_breaker is None
                else circuit_breaker.record_failure(error)
            )
            if opened and adapter is not None:
                record_adapter_event(
                    adapter,
                    kind="adapter.circuit_opened",
                    operation=operation,
                    status="open",
                    reason_codes=("adapter.transient_failure_threshold",),
                )
            if (
                attempt >= policy.max_attempts
                or not operation_is_idempotent
                or not is_retryable(error, policy)
            ):
                raise
            delay = retry_delay_seconds(error, policy, attempt)
            if delay is None:
                raise
            if control is not None:
                remaining = control.remaining_seconds()
                if remaining is not None and delay >= remaining:
                    raise
            if adapter is not None:
                record_adapter_event(
                    adapter,
                    kind="adapter.retry_scheduled",
                    operation=operation,
                    status="scheduled",
                    reason_codes=("adapter.retry_opted_in",),
                    attributes={"attempt": attempt + 1, "delay_seconds": delay},
                )
            if delay:
                if token is not None:
                    cooperative_sleep(delay, token)
                else:
                    time.sleep(delay)
            continue
        else:
            closed = (
                False
                if circuit_breaker is None
                else circuit_breaker.record_success()
            )
            if closed and adapter is not None and state_before is not CircuitState.CLOSED:
                record_adapter_event(
                    adapter,
                    kind="adapter.circuit_closed",
                    operation=operation,
                    status="closed",
                    reason_codes=("adapter.probe_succeeded",),
                )
            return result
    raise AssertionError("unreachable")


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerPolicy",
    "CircuitState",
    "ErrorClass",
    "RetryPolicy",
    "classify_error",
    "execute_with_resilience",
    "is_retryable",
]
