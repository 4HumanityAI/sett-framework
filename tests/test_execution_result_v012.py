"""
Phase 2 of v0.12.0: structured outcomes, and the additive extension of
StageOutcome.

Two things are being pinned down here.

The first is that ``unwrap()`` re-raises the *real* exception. That is the
single load-bearing property of the whole compatibility plan: the day an
entry point starts producing an ExecutionResult internally, code written
as ``except SETTEthicalFilterRejectedError`` has to keep catching exactly
that, with its structured attributes intact, rather than a generic
wrapper.

The second is that a cancellation tree is not a dependency graph, now
that the pipeline is the one feeding the distinction. A stage that never
ran reports no execution status at all, not CANCELLED, and names the
stage that made it unreachable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sett import (
    EthicalFilter,
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
    RejectionOutcome,
    SETTAgent,
    SETTCancelledError,
    SETTConfigurationError,
    SETTDependencyError,
    SETTError,
    SETTEthicalFilterRejectedError,
    SETTOrchestrator,
    SETTValidationError,
    SkipCause,
    StageOutcome,
)
from sett.ethics_ruler.ethic_kernel.rules import EthicalRuleset

_STARTED = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
_FINISHED = _STARTED + timedelta(seconds=2)


def _state(status: ExecutionStatus, **kwargs) -> ExecutionState:
    return ExecutionState(
        status=status, started_at=_STARTED, finished_at=_FINISHED, **kwargs
    )


# ── ExecutionStatus ─────────────────────────────────────────────────────────


def test_the_six_terminal_statuses_and_no_more():
    assert {status.value for status in ExecutionStatus} == {
        "success",
        "partial",
        "cancelled",
        "timed_out",
        "failed",
        "rejected",
    }


def test_there_is_deliberately_no_skipped_status():
    """A unit that never ran has no execution outcome to report."""
    assert not hasattr(ExecutionStatus, "SKIPPED")
    assert "skipped" not in {status.value for status in ExecutionStatus}


def test_a_rejection_is_not_a_failure():
    assert ExecutionStatus.REJECTED is not ExecutionStatus.FAILED


def test_a_timeout_is_not_a_cancellation():
    assert ExecutionStatus.TIMED_OUT is not ExecutionStatus.CANCELLED


# ── ExecutionState ──────────────────────────────────────────────────────────


def test_a_state_records_progress_and_attempts():
    state = _state(
        ExecutionStatus.PARTIAL,
        reason_code="pipeline.stage_timed_out",
        completed_units=2,
        total_units=4,
        attempt_count=3,
    )
    assert state.completed_units == 2
    assert state.total_units == 4
    assert state.attempt_count == 3
    assert state.cleanup_failed is False


def test_cleanup_errors_are_recorded_without_changing_the_status():
    state = _state(
        ExecutionStatus.TIMED_OUT,
        reason_code="execution.deadline_exceeded",
        cleanup_errors=("OSError",),
    )
    assert state.status is ExecutionStatus.TIMED_OUT
    assert state.cleanup_errors == ("OSError",)
    assert state.cleanup_failed is True


def test_state_rejects_naive_timestamps():
    with pytest.raises(SETTValidationError):
        ExecutionState(
            status=ExecutionStatus.SUCCESS,
            started_at=datetime(2026, 8, 9, 12, 0, 0),
            finished_at=_FINISHED,
        )


def test_state_rejects_impossible_progress():
    with pytest.raises(SETTValidationError):
        _state(ExecutionStatus.PARTIAL, completed_units=5, total_units=4)
    with pytest.raises(SETTValidationError):
        _state(ExecutionStatus.SUCCESS, completed_units=-1)
    with pytest.raises(SETTValidationError):
        _state(ExecutionStatus.SUCCESS, attempt_count=0)


def test_state_rejects_a_status_that_is_not_an_execution_status():
    with pytest.raises(SETTValidationError):
        ExecutionState(
            status="success",  # the string, not the member
            started_at=_STARTED,
            finished_at=_FINISHED,
        )


def test_duration_is_carried_separately_from_the_wall_clock():
    """Wall-clock subtraction across a clock adjustment is not a duration."""
    state = _state(ExecutionStatus.SUCCESS, duration_seconds=0.25)
    wall_clock_delta = (state.finished_at - state.started_at).total_seconds()
    assert wall_clock_delta == 2.0
    assert state.duration_seconds == 0.25


# ── ExecutionResult.unwrap() ────────────────────────────────────────────────


def test_unwrap_returns_the_value_on_success():
    result = ExecutionResult.success(
        {"ok": True}, state=_state(ExecutionStatus.SUCCESS), trace_id="t-1"
    )
    assert result.succeeded is True
    assert result.unwrap() == {"ok": True}


def test_unwrap_reraises_the_real_exception_type_with_its_attributes():
    """The load-bearing property of the whole compatibility plan."""
    original = SETTEthicalFilterRejectedError(
        "blocked by the filter",
        action="charge_payment",
        score=0.91,
        threshold=0.7,
        principle="do no harm",
        reasoning="economic harm detected",
    )
    result = ExecutionResult.failure(
        original,
        state=_state(ExecutionStatus.REJECTED, reason_code="policy.reject"),
        trace_id="t-2",
    )

    with pytest.raises(SETTEthicalFilterRejectedError) as excinfo:
        result.unwrap()

    raised = excinfo.value
    assert raised is original  # the instance, not a copy or a wrapper
    assert raised.action == "charge_payment"
    assert raised.score == 0.91
    assert raised.threshold == 0.7
    assert str(raised) == "blocked by the filter"
    assert result.error_type == "SETTEthicalFilterRejectedError"


@pytest.mark.parametrize(
    "error",
    [
        SETTConfigurationError("no handler registered"),
        SETTCancelledError("execution.cancelled_by_caller"),
        ValueError("a plain Python error from a handler"),
    ],
)
def test_unwrap_preserves_every_exception_type_it_is_given(error):
    result = ExecutionResult.failure(
        error, state=_state(ExecutionStatus.FAILED), trace_id="t-3"
    )
    with pytest.raises(type(error)) as excinfo:
        result.unwrap()
    assert excinfo.value is error


def test_unwrap_without_an_exception_raises_a_framework_error_not_runtime_error():
    result = ExecutionResult(
        state=_state(
            ExecutionStatus.PARTIAL,
            reason_code="pipeline.stage_timed_out",
            completed_units=2,
            total_units=4,
        ),
        trace_id="t-4",
    )
    with pytest.raises(SETTError) as excinfo:
        result.unwrap()
    # `except SETTError` stays a complete net around anything SETT raises.
    assert type(excinfo.value) is SETTError
    assert "partial" in str(excinfo.value)
    assert "pipeline.stage_timed_out" in str(excinfo.value)


def test_status_predicates_report_exactly_one_terminal_shape():
    cancelled = ExecutionResult(
        state=_state(ExecutionStatus.CANCELLED), trace_id="t-5"
    )
    assert cancelled.cancelled is True
    assert cancelled.succeeded is False
    assert cancelled.timed_out is False
    assert cancelled.rejected is False
    assert cancelled.partial is False
    assert cancelled.status is ExecutionStatus.CANCELLED


def test_success_and_failure_factories_refuse_contradictory_states():
    with pytest.raises(SETTValidationError):
        ExecutionResult.success(
            {"v": 1}, state=_state(ExecutionStatus.FAILED), trace_id="t-6"
        )
    with pytest.raises(SETTValidationError):
        ExecutionResult.failure(
            ValueError("x"), state=_state(ExecutionStatus.SUCCESS),
            trace_id="t-6",
        )


def test_a_result_carries_the_run_id_that_can_be_cancelled():
    result = ExecutionResult.success(
        {"ok": True},
        state=_state(ExecutionStatus.SUCCESS),
        trace_id="t-7",
        run_id="run-7",
    )
    assert result.trace_id == "t-7"
    assert result.run_id == "run-7"


# ── not_executed(): the dependency graph, not the cancellation tree ─────────


def test_a_unit_that_never_ran_names_its_failed_dependency():
    result = ExecutionResult.not_executed(
        state=_state(
            ExecutionStatus.FAILED, reason_code="execution.dependency_failed"
        ),
        trace_id="t-8",
        run_id="run-b",
        failed_dependency_run_id="run-a",
    )

    with pytest.raises(SETTDependencyError) as excinfo:
        result.unwrap()

    error = excinfo.value
    assert error.run_id == "run-b"
    assert error.failed_dependency_run_id == "run-a"
    # Nobody cancelled this unit; it never became reachable.
    assert not isinstance(error, SETTCancelledError)
    assert result.cancelled is False


# ── StageOutcome stayed additive ────────────────────────────────────────────


def test_the_new_stage_outcome_fields_all_default_to_none():
    outcome = StageOutcome(domain="health", status="completed")
    assert outcome.execution_status is None
    assert outcome.reason_code is None
    assert outcome.skip_cause is None


def test_positional_construction_of_the_original_four_fields_still_works():
    rejection = RejectionOutcome(
        domain="health",
        action=None,
        score=None,
        threshold=None,
        principle=None,
        reasoning=None,
        message="blocked",
    )
    outcome = StageOutcome("health", "rejected", None, rejection)
    assert outcome.domain == "health"
    assert outcome.status == "rejected"
    assert outcome.output is None
    assert outcome.rejection is rejection


def test_an_outcome_written_the_old_way_still_compares_equal():
    assert StageOutcome(domain="health", status="skipped") == StageOutcome(
        domain="health", status="skipped"
    )
    assert StageOutcome("a", "completed", {"x": 1}) == StageOutcome(
        domain="a", status="completed", output={"x": 1}
    )


# ── The pipeline feeds the distinction ──────────────────────────────────────


class TraceAgent(SETTAgent):
    def __init__(self, domain: str):
        super().__init__(name=f"{domain}_agent", domain=domain)

    def process(self, input_data):
        self._publish_to_universal({f"{self.domain}_done": True})
        return {"trail": input_data.get("trail", []) + [self.domain]}


class SilentAgent(SETTAgent):
    def __init__(self, domain: str):
        super().__init__(name=f"{domain}_agent", domain=domain)

    def process(self, input_data):
        return {"trail": input_data.get("trail", [])}


def _rejected_pipeline():
    """Three stages where the second one is rejected by a strict filter."""
    orchestrator = SETTOrchestrator(
        ethical_filter=EthicalFilter(
            ruleset=EthicalRuleset(
                name="strict_test", reject_threshold=0.1, warn_threshold=0.05
            )
        )
    )
    for agent in (SilentAgent("first"), TraceAgent("second"), TraceAgent("third")):
        orchestrator.register_agent(agent)
    result = orchestrator.run_pipeline(
        ["first", "second", "third"],
        {"trail": []},
        emotional_state="crisis",
    )
    return orchestrator, result


def test_a_completed_stage_reports_a_success_execution_status():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("alpha"))
    result = orchestrator.run_pipeline(["alpha"], {"trail": []})

    stage = result.steps[0]
    assert stage.status == "completed"
    assert stage.execution_status is ExecutionStatus.SUCCESS
    assert stage.skip_cause is None


def test_a_rejected_stage_reports_rejected_and_never_failed():
    _, result = _rejected_pipeline()
    rejected = result.steps[1]

    assert rejected.status == "rejected"
    assert rejected.execution_status is ExecutionStatus.REJECTED
    assert rejected.execution_status is not ExecutionStatus.FAILED
    assert rejected.reason_code == "policy.reject"


def test_a_skipped_stage_reports_no_execution_status_at_all():
    """``execution_status is None`` iff the stage never executed."""
    _, result = _rejected_pipeline()
    skipped = result.steps[2]

    assert skipped.status == "skipped"
    assert skipped.execution_status is None
    # And most importantly: not reported as somebody having cancelled it.
    assert skipped.execution_status is not ExecutionStatus.CANCELLED


def test_a_skipped_stage_names_the_stage_that_made_it_unreachable():
    _, result = _rejected_pipeline()
    skipped = result.steps[2]

    assert isinstance(skipped.skip_cause, SkipCause)
    assert skipped.skip_cause.domain == "second"
    assert skipped.skip_cause.reason_code == "policy.reject"
    assert skipped.reason_code == "pipeline.previous_stage_rejected"


def test_the_skip_cause_run_id_is_the_one_the_trace_recorded():
    """The dependency is named by the same identity its trace events use."""
    orchestrator, result = _rejected_pipeline()
    skip_cause = result.steps[2].skip_cause

    events = orchestrator.get_trace(orchestrator.last_trace_id)
    rejected_events = [
        event
        for event in events
        if event.kind == "pipeline.stage_rejected"
    ]
    assert len(rejected_events) == 1
    assert skip_cause.run_id == rejected_events[0].run_id


def test_the_pre_existing_stage_fields_are_untouched_by_the_extension():
    """Everything test_pipeline.py already asserts still holds, field by field."""
    _, result = _rejected_pipeline()

    assert [stage.status for stage in result.steps] == [
        "completed",
        "rejected",
        "skipped",
    ]
    assert result.completed is False
    assert result.output is None
    assert isinstance(result.rejection, RejectionOutcome)
    assert result.rejection.domain == "second"
    assert result.steps[0].output is not None
    assert result.steps[1].rejection is result.rejection
    assert result.steps[2].output is None
    assert result.steps[2].rejection is None


def test_traces_still_verify_after_the_extension():
    orchestrator, _ = _rejected_pipeline()
    assert orchestrator.verify_traces(orchestrator.last_trace_id) is True
