"""
Cancellation and cleanup as causally explained trace events.

Two properties are being pinned down.

The first is that a trace can tell apart the ways an execution stops. A
run that a user cancelled and a run that ran out of time are different
events, and the recorder's terminal verification now accepts both without
accepting one in place of the other.

The second is causal explanation, which is the point of recording any of
this. A cancelled leaf does not just report that it stopped: through
``cause_event_id`` it points back at the exact event where somebody asked
for the stop, several levels up. Without that link, a trace answers "what
happened" but not "who asked", which is the only question worth asking
after the fact.
"""
from __future__ import annotations

import time

import pytest

from sett.audit_ruler.trace import (
    _TERMINAL_BY_START,
    TRACE_TERMINAL_KINDS,
    TraceRecorder,
)
from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import (
    ExecutionControl,
    ExecutionControlRegistry,
)
from sett.exceptions import SETTCancelledError, SETTTimeoutError


@pytest.fixture
def registry() -> ExecutionControlRegistry:
    return ExecutionControlRegistry()


@pytest.fixture
def recorder() -> TraceRecorder:
    return TraceRecorder()


def _kinds(recorder: TraceRecorder, trace_id: str) -> list[str]:
    return [event.kind for event in recorder.get_trace(trace_id)]


def _by_kind(recorder: TraceRecorder, trace_id: str, kind: str):
    return [
        event for event in recorder.get_trace(trace_id) if event.kind == kind
    ]


def _open_root(recorder: TraceRecorder, context: ExecutionContext):
    """Open a trace the way an instrumented boundary does."""
    return recorder.record(
        context,
        kind="trace.started",
        component_type="orchestrator",
        component_name="TestHarness",
        status="started",
    )


# ── The terminal vocabulary ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "start_kind, terminal_kind",
    [
        ("trace.started", "trace.cancelled"),
        ("trace.started", "trace.timed_out"),
        ("pipeline.started", "pipeline.cancelled"),
        ("pipeline.started", "pipeline.timed_out"),
        ("pipeline.started", "pipeline.partial"),
        ("pipeline.stage_started", "pipeline.stage_cancelled"),
        ("pipeline.stage_started", "pipeline.stage_timed_out"),
        ("broadcast.started", "broadcast.cancelled"),
        ("route.selected", "route.cancelled"),
        ("agent.started", "agent.cancelled"),
        ("agent.started", "agent.timed_out"),
        ("expert.started", "expert.cancelled"),
        ("expert.started", "expert.timed_out"),
        ("handler.started", "handler.cancelled"),
        ("handler.started", "handler.timed_out"),
        ("handler.attempt_started", "handler.attempt_completed"),
        ("handler.attempt_started", "handler.attempt_failed"),
    ],
)
def test_every_new_terminal_closes_its_start_kind(start_kind, terminal_kind):
    assert terminal_kind in _TERMINAL_BY_START[start_kind]


def test_the_original_terminals_were_not_replaced():
    """v0.11.0's vocabulary is extended, never narrowed."""
    for start_kind, terminals in {
        "trace.started": {"trace.completed", "trace.failed"},
        "pipeline.started": {
            "pipeline.completed",
            "pipeline.rejected",
            "pipeline.failed",
        },
        "pipeline.stage_started": {
            "pipeline.stage_completed",
            "pipeline.stage_rejected",
            "pipeline.stage_skipped",
            "pipeline.stage_failed",
        },
        "broadcast.started": {"broadcast.completed", "broadcast.failed"},
        "route.selected": {"route.completed", "route.failed"},
        "agent.started": {"agent.completed", "agent.failed"},
        "expert.started": {"expert.completed", "expert.failed"},
        "handler.authorized": {"handler.started", "handler.blocked"},
        "handler.started": {"handler.completed", "handler.failed"},
    }.items():
        assert terminals <= _TERMINAL_BY_START[start_kind]


def test_cancellation_and_timeout_never_share_a_terminal_kind():
    for terminals in _TERMINAL_BY_START.values():
        cancelled = {kind for kind in terminals if kind.endswith(".cancelled")}
        timed_out = {kind for kind in terminals if kind.endswith(".timed_out")}
        assert not (cancelled & timed_out)


def test_the_trace_terminal_set_is_derived_not_duplicated():
    """One mapping feeds both terminal checks, so they cannot drift apart."""
    assert TRACE_TERMINAL_KINDS is _TERMINAL_BY_START["trace.started"]


def test_a_trace_closed_by_cancellation_verifies(recorder):
    context = ExecutionContext.create()
    started = _open_root(recorder, context)
    recorder.record(
        context,
        kind="trace.cancelled",
        component_type="orchestrator",
        component_name="TestHarness",
        status="cancelled",
        cause_id=started.event_id,
        reason_codes=("execution.cancelled_by_caller",),
    )
    assert recorder.verify(context.trace_id) is True


def test_a_trace_closed_by_a_timeout_verifies(recorder):
    context = ExecutionContext.create()
    started = _open_root(recorder, context)
    recorder.record(
        context,
        kind="trace.timed_out",
        component_type="orchestrator",
        component_name="TestHarness",
        status="timed_out",
        cause_id=started.event_id,
        reason_codes=("execution.deadline_exceeded",),
    )
    assert recorder.verify(context.trace_id) is True


def test_a_partial_pipeline_terminal_verifies(recorder):
    context = ExecutionContext.create()
    started = _open_root(recorder, context)
    pipeline_context = context.derive()
    pipeline_started = recorder.record(
        pipeline_context,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="native_pipeline",
        status="started",
        cause_id=started.event_id,
    )
    partial = recorder.record(
        pipeline_context,
        kind="pipeline.partial",
        component_type="pipeline",
        component_name="native_pipeline",
        status="partial",
        cause_id=pipeline_started.event_id,
        attributes={"completed_units": 2, "total_units": 4},
    )
    recorder.record(
        context,
        kind="trace.completed",
        component_type="orchestrator",
        component_name="TestHarness",
        status="completed",
        cause_id=partial.event_id,
    )
    assert recorder.verify(context.trace_id) is True


def test_an_unclosed_boundary_still_fails_verification(recorder):
    """The extension must not have loosened the check into uselessness."""
    context = ExecutionContext.create()
    _open_root(recorder, context)
    assert recorder.verify(context.trace_id) is False


# ── Cancellation is recorded, and explains itself causally ──────────────────


def test_cancelling_records_the_request_before_the_cancellation(
    registry, recorder
):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context, registry=registry, trace_recorder=recorder
    )
    with control:
        control.cancel(reason="execution.barge_in")

    assert "execution.cancel_requested" in _kinds(recorder, context.trace_id)
    requested = _by_kind(recorder, context.trace_id, "execution.cancel_requested")
    assert len(requested) == 1
    assert requested[0].reason_codes == ("execution.barge_in",)
    assert requested[0].run_id == context.run_id


def test_a_repeated_cancel_does_not_record_a_second_request(
    registry, recorder
):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context, registry=registry, trace_recorder=recorder
    )
    with control:
        assert control.cancel() is True
        assert control.cancel() is False

    assert (
        len(_by_kind(recorder, context.trace_id, "execution.cancel_requested"))
        == 1
    )


def test_a_cancelled_leaf_points_back_at_the_request_several_levels_up(
    registry, recorder
):
    """Section 14.1: the cancellation is causally explained, not just recorded."""
    root_context = ExecutionContext.create()
    _open_root(recorder, root_context)
    root = ExecutionControl.create_root(
        root_context, registry=registry, trace_recorder=recorder
    )

    with root:
        branch_context = root_context.derive()
        recorder.record(
            branch_context,
            kind="agent.started",
            component_type="agent",
            component_name="branch",
            status="started",
        )
        with root.child(context=branch_context) as branch:
            leaf_context = branch_context.derive()
            recorder.record(
                leaf_context,
                kind="expert.started",
                component_type="expert",
                component_name="leaf",
                status="started",
            )
            with branch.child(context=leaf_context) as leaf:
                root.cancel(reason="execution.session_ended")

                with pytest.raises(SETTCancelledError) as excinfo:
                    leaf.checkpoint()

    requested = _by_kind(
        recorder, root_context.trace_id, "execution.cancel_requested"
    )[0]
    # The error raised three levels down names the event where somebody
    # asked for the stop, not merely the fact that it stopped.
    assert excinfo.value.cause_event_id == requested.event_id
    assert excinfo.value.run_id == leaf_context.run_id
    assert excinfo.value.reason == "execution.session_ended"


def test_cancelling_one_branch_leaves_the_sibling_uncancelled(
    registry, recorder
):
    root_context = ExecutionContext.create()
    _open_root(recorder, root_context)
    root = ExecutionControl.create_root(
        root_context, registry=registry, trace_recorder=recorder
    )
    with root:
        first_context = root_context.derive()
        second_context = root_context.derive()
        with root.child(context=first_context) as first, root.child(
            context=second_context
        ) as second:
            first.cancel(reason="execution.turn_cancelled")

            assert first.token.snapshot().cancelled_run_id == first_context.run_id
            assert second.token.snapshot().cancelled is False
            second.checkpoint()


# ── The deadline is recorded once ───────────────────────────────────────────


def test_an_expired_deadline_is_recorded_once_no_matter_how_often_checked(
    registry, recorder
):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context,
        timeout_seconds=0.05,
        registry=registry,
        trace_recorder=recorder,
    )
    with pytest.raises(SETTTimeoutError), control:
        time.sleep(0.08)
        for _ in range(5):
            try:
                control.checkpoint()
            except SETTTimeoutError:
                continue
        control.checkpoint()

    events = _by_kind(recorder, context.trace_id, "execution.deadline_exceeded")
    assert len(events) == 1
    assert events[0].reason_codes == ("execution.deadline_exceeded",)


def test_no_deadline_event_is_recorded_when_nothing_expires(registry, recorder):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context, timeout_seconds=30.0, registry=registry, trace_recorder=recorder
    )
    with control:
        control.checkpoint()

    assert "execution.deadline_exceeded" not in _kinds(
        recorder, context.trace_id
    )


# ── Cleanup is recorded, failures included ──────────────────────────────────


def test_cleanup_records_a_started_and_completed_pair(registry, recorder):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context, registry=registry, trace_recorder=recorder
    )
    with control:
        control.register_cleanup(lambda: None)

    kinds = _kinds(recorder, context.trace_id)
    assert "execution.cleanup_started" in kinds
    assert "execution.cleanup_completed" in kinds
    assert "execution.cleanup_failed" not in kinds

    started = _by_kind(recorder, context.trace_id, "execution.cleanup_started")[0]
    completed = _by_kind(
        recorder, context.trace_id, "execution.cleanup_completed"
    )[0]
    assert completed.cause_id == started.event_id


def test_a_scope_with_no_hooks_records_no_cleanup_events(registry, recorder):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context, registry=registry, trace_recorder=recorder
    )
    with control:
        pass

    kinds = _kinds(recorder, context.trace_id)
    assert "execution.cleanup_started" not in kinds
    assert "execution.cleanup_completed" not in kinds


def test_a_failing_hook_records_cleanup_failed_without_hiding_the_reason(
    registry, recorder
):
    def broken() -> None:
        raise OSError("temporary file is gone")

    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context,
        timeout_seconds=0.05,
        registry=registry,
        trace_recorder=recorder,
    )
    with pytest.raises(SETTTimeoutError), control:
        control.register_cleanup(broken)
        time.sleep(0.08)
        control.checkpoint()

    failed = _by_kind(recorder, context.trace_id, "execution.cleanup_failed")
    assert len(failed) == 1
    assert failed[0].reason_codes == ("cleanup.OSError",)
    # The run timed out. The cleanup failure is recorded beside that, not
    # in place of it.
    assert "execution.deadline_exceeded" in _kinds(recorder, context.trace_id)
    assert isinstance(control.terminal_error, SETTTimeoutError)


# ── Tracing never decides whether execution may continue ────────────────────


class BrokenRecorder:
    """A recorder that fails at recording, not at exporting.

    Deliberately not a TraceRecorder with a failing exporter: the real
    recorder already absorbs exporter failures on its own, so that
    arrangement proves nothing about this module. What has to be proven
    here is that a recorder which cannot record at all still cannot
    prevent an execution from stopping.

    ``fail_lookup`` switches between the two distinct failure points a
    recorder has from this module's perspective, including resolving the causal
    parent and writing the event itself, because a try block that only
    covers one of them would leak the other.
    """

    def __init__(self, *, fail_lookup: bool = False) -> None:
        self.attempts: list[str] = []
        self.lookups = 0
        self._fail_lookup = fail_lookup

    def record(self, context, *, kind, **kwargs):
        self.attempts.append(kind)
        raise RuntimeError("the trace backend is unreachable")

    def last_event_id(self, run_id):
        self.lookups += 1
        if self._fail_lookup:
            raise RuntimeError("the trace backend is unreachable")


def test_a_recorder_whose_write_fails_cannot_block_a_cancellation(registry):
    broken = BrokenRecorder()
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry, trace_recorder=broken
    )
    with control:
        # Cancellation is not a real-world effect: an unreachable trace
        # backend must never keep a run alive that somebody stopped.
        assert control.cancel() is True
        assert control.is_cancelled is True

    # It really was asked to write, and really did fail.
    assert broken.attempts == ["execution.cancel_requested"]


def test_a_recorder_whose_lookup_fails_cannot_block_a_cancellation(registry):
    broken = BrokenRecorder(fail_lookup=True)
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry, trace_recorder=broken
    )
    with control:
        assert control.cancel() is True
        assert control.is_cancelled is True

    # It failed before reaching the write, which is the other failure
    # point and must be inside the same guard.
    assert broken.lookups == 1
    assert broken.attempts == []


@pytest.mark.parametrize("fail_lookup", [False, True])
def test_a_recorder_that_cannot_record_cannot_block_cleanup_either(
    registry, fail_lookup
):
    broken = BrokenRecorder(fail_lookup=fail_lookup)
    calls: list[str] = []
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry, trace_recorder=broken
    )
    with control:
        control.register_cleanup(lambda: calls.append("released"))

    assert calls == ["released"]
    assert registry.live_run_ids == ()


def test_a_failing_exporter_never_reaches_the_control(registry, recorder):
    """Complementary to the above: the recorder absorbs exporter failures."""

    def broken_exporter(event: dict) -> None:
        raise RuntimeError("the exporter is down")

    context = ExecutionContext.create()
    _open_root(recorder, context)
    recorder.register_exporter(broken_exporter)
    control = ExecutionControl.create_root(
        context, registry=registry, trace_recorder=recorder
    )
    with control:
        assert control.cancel() is True
        assert control.is_cancelled is True


def test_lifecycle_events_keep_the_recorder_verifiable(registry, recorder):
    context = ExecutionContext.create()
    started = _open_root(recorder, context)
    control = ExecutionControl.create_root(
        context, registry=registry, trace_recorder=recorder
    )
    with control:
        control.register_cleanup(lambda: None)
        control.cancel()
    recorder.record(
        context,
        kind="trace.cancelled",
        component_type="orchestrator",
        component_name="TestHarness",
        status="cancelled",
        cause_id=started.event_id,
    )
    assert recorder.verify(context.trace_id) is True


def test_a_control_without_a_recorder_records_nothing_and_still_works(
    registry, recorder
):
    context = ExecutionContext.create()
    _open_root(recorder, context)
    control = ExecutionControl.create_root(context, registry=registry)
    with control:
        control.register_cleanup(lambda: None)
        assert control.cancel() is True

    # Only the harness event: the control was never attached to it.
    assert _kinds(recorder, context.trace_id) == ["trace.started"]
