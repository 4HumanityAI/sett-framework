"""How an execution ended, as data rather than as a boolean.

``ExecutionContext`` says which execution something belongs to.
``ExecutionControl`` says whether it may continue. This module says how it
turned out, and it deliberately distinguishes outcomes that a boolean
collapses into one.

"It did not succeed" is not a useful answer. A run that was cancelled by
the user, a run that ran out of time, a run that hit a bug and a run the
``EthicalFilter`` refused are four different events with four different
correct responses, and only one of them (a timeout) is usually worth
retrying. An ethical rejection in particular must never be reported as a
technical failure: the whole point of the filter is that refusing is a
correct outcome, not a malfunction.

One status is deliberately absent: there is no ``SKIPPED``. A unit that
never ran because something it depended on failed was not cancelled and
did not fail on its own; it never became reachable. That is a property
of the dependency graph, not of this unit's execution, and it is carried
by ``SETTDependencyError`` and by the pipeline's own ``SkipCause``. Adding
``SKIPPED`` here is what would let a causal skip be silently reported as a
caller's cancellation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar

from sett.core_ruler.lifecycle import validate_duration_seconds
from sett.exceptions import (
    SETTDependencyError,
    SETTError,
    SETTValidationError,
)

T = TypeVar("T")


class ExecutionStatus(str, Enum):
    """The terminal outcome of one execution unit."""

    SUCCESS = "success"
    """The whole unit finished and produced a valid result."""

    PARTIAL = "partial"
    """Some of the work finished and some did not.

    Three agents answered and a fourth ran out of its deadline; two
    pipeline stages completed and a third was cancelled. Reporting that
    as either success or failure throws away the part that did finish.
    """

    CANCELLED = "cancelled"
    """Somebody explicitly asked for this to stop."""

    TIMED_OUT = "timed_out"
    """A deadline was exceeded.

    Kept apart from CANCELLED even though both stop the work: nobody
    requested a timeout, and the two call for different responses.
    """

    FAILED = "failed"
    """A technical error, a configuration error, or a component raised."""

    REJECTED = "rejected"
    """The EthicalFilter blocked the operation.

    Never folded into FAILED: the filter refusing is the system working,
    not breaking.
    """


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """The structured record of how one execution unit ended.

    ``started_at`` / ``finished_at`` are wall-clock timestamps, meant for
    reporting and correlation with the outside world. ``duration_seconds``
    comes from the monotonic clock instead, because a wall-clock
    subtraction across a clock adjustment reports a duration that never
    happened. The two are recorded separately on purpose rather than one
    being derived from the other.
    """

    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime
    reason_code: str | None = None
    completed_units: int = 0
    total_units: int | None = None
    attempt_count: int = 1
    cleanup_errors: tuple[str, ...] = field(default_factory=tuple)
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        # isinstance, not membership in a set of the members: because
        # ExecutionStatus mixes in str, its members hash as their own
        # value, so `"success" in set(ExecutionStatus)` is True and a raw
        # string would slip through a containment check unvalidated.
        if not isinstance(self.status, ExecutionStatus):
            raise SETTValidationError(
                "ExecutionState.status must be an ExecutionStatus member, "
                f"got {type(self.status).__name__}."
            )
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise SETTValidationError(f"{name} must be a datetime.")
            if value.tzinfo is None or value.utcoffset() is None:
                raise SETTValidationError(f"{name} must be timezone-aware.")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.completed_units < 0:
            raise SETTValidationError("completed_units must not be negative.")
        if self.total_units is not None:
            if self.total_units < 0:
                raise SETTValidationError("total_units must not be negative.")
            if self.completed_units > self.total_units:
                raise SETTValidationError(
                    "completed_units cannot exceed total_units."
                )
        if self.attempt_count < 1:
            raise SETTValidationError("attempt_count must be at least 1.")
        if self.duration_seconds is not None:
            # The last numeric field of this dataclass to get a guard, and
            # the reason it needed one: every sibling above validates, so
            # a NaN or a negative duration was the one nonsensical value
            # this type would accept and then carry into a report as if it
            # were measured. It cannot arise from the framework itself because
            # it comes from the monotonic clock. However, ExecutionState is a
            # public dataclass an application may build directly.
            object.__setattr__(
                self,
                "duration_seconds",
                validate_duration_seconds(
                    self.duration_seconds,
                    name="duration_seconds",
                    allow_zero=True,
                ),
            )
        object.__setattr__(
            self, "cleanup_errors", tuple(self.cleanup_errors)
        )

    @property
    def cleanup_failed(self) -> bool:
        """Whether any cleanup hook raised while unwinding.

        A cleanup failure never changes ``status``: a run that timed out
        and then failed to delete a temporary file timed out.
        """
        return bool(self.cleanup_errors)


@dataclass(frozen=True, slots=True)
class ExecutionResult(Generic[T]):
    """A terminal outcome together with its value or its cause.

    ``exception`` holds the *actual* exception instance that ended the
    execution, not a description of it. That is what lets ``unwrap()``
    re-raise the real, specific type, which is what keeps existing
    callers working the day an entry point starts producing
    ``ExecutionResult`` internally: code written as
    ``except SETTEthicalFilterRejectedError`` must keep catching exactly
    that, not a generic wrapper.
    """

    state: ExecutionState
    trace_id: str
    run_id: str | None = None
    value: T | None = None
    error_type: str | None = None
    exception: BaseException | None = None

    @property
    def status(self) -> ExecutionStatus:
        """How this execution ended."""
        return self.state.status

    @property
    def succeeded(self) -> bool:
        """Whether the work completed and produced its value."""
        return self.state.status is ExecutionStatus.SUCCESS

    @property
    def cancelled(self) -> bool:
        """Whether somebody asked for this to stop."""
        return self.state.status is ExecutionStatus.CANCELLED

    @property
    def timed_out(self) -> bool:
        """Whether it ran out of the time it was given; nobody asked."""
        return self.state.status is ExecutionStatus.TIMED_OUT

    @property
    def rejected(self) -> bool:
        """Whether the EthicalFilter blocked it, which is not a failure."""
        return self.state.status is ExecutionStatus.REJECTED

    @property
    def partial(self) -> bool:
        """Whether some work finished before the rest was stopped."""
        return self.state.status is ExecutionStatus.PARTIAL

    @property
    def reason_code(self) -> str | None:
        """The stable machine-readable reason, when there is one."""
        return self.state.reason_code

    def unwrap(self) -> T | None:
        """Return the value, or re-raise the exception that prevented it.

        Raises:
            BaseException: The original exception, unchanged, whenever one
                was recorded. Preserving the concrete type is the whole
                contract here.
            SETTError: For a terminal state that never had a concrete
                exception behind it, reachable for a PARTIAL result whose
                completed portion is real but whose caller asked for all
                or nothing. Still a framework error rather than a bare
                ``RuntimeError``, so ``except SETTError`` remains a
                complete net around anything SETT raises.
        """
        if self.succeeded:
            return self.value
        if self.exception is not None:
            raise self.exception
        raise SETTError(
            f"Execution ended as {self.state.status.value}: "
            f"{self.state.reason_code or 'no reason recorded'}"
        )

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def success(
        cls,
        value: T,
        *,
        state: ExecutionState,
        trace_id: str,
        run_id: str | None = None,
    ) -> ExecutionResult[T]:
        """Build a successful result; refuses a non-SUCCESS state."""
        if state.status is not ExecutionStatus.SUCCESS:
            raise SETTValidationError(
                "ExecutionResult.success() requires a SUCCESS state."
            )
        return cls(state=state, trace_id=trace_id, run_id=run_id, value=value)

    @classmethod
    def failure(
        cls,
        exception: BaseException,
        *,
        state: ExecutionState,
        trace_id: str,
        run_id: str | None = None,
        value: T | None = None,
    ) -> ExecutionResult[T]:
        """Build a non-successful result around the real exception."""
        if state.status is ExecutionStatus.SUCCESS:
            raise SETTValidationError(
                "A SUCCESS state cannot carry a terminating exception."
            )
        return cls(
            state=state,
            trace_id=trace_id,
            run_id=run_id,
            value=value,
            error_type=type(exception).__name__,
            exception=exception,
        )

    @classmethod
    def not_executed(
        cls,
        *,
        state: ExecutionState,
        trace_id: str,
        run_id: str | None = None,
        failed_dependency_run_id: str | None = None,
        message: str | None = None,
    ) -> ExecutionResult[T]:
        """Build the result of a unit that never became reachable.

        Carries a ``SETTDependencyError``, so unwrapping it names the
        dependency that failed instead of implying this unit was cancelled
        or that it failed on its own.
        """
        error = SETTDependencyError(
            message or (state.reason_code or "execution.dependency_failed"),
            run_id=run_id,
            failed_dependency_run_id=failed_dependency_run_id,
        )
        return cls.failure(
            error, state=state, trace_id=trace_id, run_id=run_id
        )
