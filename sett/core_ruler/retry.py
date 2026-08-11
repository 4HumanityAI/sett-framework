"""When a failed external effect may be attempted again, and when it may not.

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
to prevent. Cancellation and deadlines are next - somebody asked for the
work to stop, or it ran out of time, and trying again ignores both. These
are enforced below the policy, so no configuration can turn them on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sett.core_ruler.lifecycle import validate_duration_seconds
from sett.exceptions import (
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

    Empty - the default - means nothing is retried, so raising
    ``max_attempts`` alone never causes a retry. That is deliberate: the
    number of attempts and the decision that repeating is safe are two
    separate statements, and only the second one is about correctness.
    """

    def __post_init__(self) -> None:
        # int, not just "whole number": range(1, max_attempts + 1) - what
        # the attempt loop actually does with this - raises a bare
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
    """
    if isinstance(error, _NEVER_RETRYABLE):
        return False
    return classify_error(error, policy) is ErrorClass.TRANSIENT
