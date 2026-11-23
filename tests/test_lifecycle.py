"""
Cancellation, deadlines, cleanup, and the control tree.

Covers the lifecycle rows of the plan's test matrix (24.1 CancellationToken,
24.2 Deadlines) plus the three things that only exist because of the
hierarchy decision taken before implementation started:

* an ``ExecutionControl`` resolves subtree cancellation against the
  ``ExecutionContext`` tree that already exists, instead of maintaining a
  parallel one;
* a cancellation tree is not a dependency graph, and the exception types
  keep that distinction at the type level;
* ``__enter__()`` settles terminal state itself, because Python never calls
  ``__exit__()`` when ``__enter__()`` raises.
"""
from __future__ import annotations

import threading
import time

import pytest

from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import (
    CancellationSource,
    CancellationToken,
    ControlPhase,
    Deadline,
    ExecutionControl,
    ExecutionControlRegistry,
    LifecyclePolicy,
    cooperative_sleep,
    current_execution_control,
    lifecycle_scope,
)
from sett.exceptions import (
    SETTCancelledError,
    SETTConfigurationError,
    SETTDependencyError,
    SETTError,
    SETTTimeoutError,
    SETTValidationError,
)


@pytest.fixture
def registry() -> ExecutionControlRegistry:
    """An isolated registry per test: the module-level default is shared."""
    return ExecutionControlRegistry()


def _root(registry: ExecutionControlRegistry, **kwargs) -> ExecutionControl:
    return ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry, **kwargs
    )


# ── 24.1 CancellationToken ──────────────────────────────────────────────────


def test_token_of_a_running_control_is_not_cancelled(registry):
    with _root(registry) as control:
        assert control.token.is_cancelled is False
        assert control.token.snapshot().cancelled is False
        control.checkpoint()  # must not raise


def test_cancel_once_reports_true(registry):
    with _root(registry) as control:
        assert control.cancel() is True
        assert control.is_cancelled is True


def test_cancel_twice_reports_false_and_keeps_the_first_reason(registry):
    with _root(registry) as control:
        assert control.cancel(reason="execution.first_reason") is True
        assert control.cancel(reason="execution.second_reason") is False
        # The first cause of a cancellation is the true one: a later
        # duplicate request must not rewrite why the run stopped.
        assert control.token.snapshot().reason == "execution.first_reason"


def test_throw_if_cancelled_carries_run_id_reason_and_cause(registry):
    with _root(registry) as control:
        control.cancel(
            reason="execution.barge_in", cause_event_id="event-42"
        )
        with pytest.raises(SETTCancelledError) as excinfo:
            control.token.throw_if_cancelled()

    error = excinfo.value
    assert error.run_id == control.run_id
    assert error.reason == "execution.barge_in"
    assert error.cause_event_id == "event-42"


def test_cancellation_is_observed_from_several_threads(registry):
    observations: list[bool] = []
    barrier = threading.Barrier(4)

    with _root(registry) as control:
        token = control.token

        def observe() -> None:
            barrier.wait(timeout=5)
            try:
                token.wait_or_raise(5.0)
            except SETTCancelledError:
                observations.append(True)
            else:
                observations.append(False)

        threads = [threading.Thread(target=observe) for _ in range(3)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        time.sleep(0.05)
        control.cancel()
        for thread in threads:
            thread.join(timeout=5)

    assert observations == [True, True, True]


def test_cancellation_callbacks_run_in_lifo_order(registry):
    calls: list[str] = []
    with _root(registry) as control:
        control.token.on_cancel(lambda: calls.append("first"))
        control.token.on_cancel(lambda: calls.append("second"))
        control.token.on_cancel(lambda: calls.append("third"))
        control.cancel()

    assert calls == ["third", "second", "first"]


def test_a_faulty_callback_does_not_stop_the_others_or_the_cancellation(
    registry,
):
    calls: list[str] = []

    def broken() -> None:
        raise RuntimeError("observer is broken")

    with _root(registry) as control:
        control.token.on_cancel(lambda: calls.append("registered_first"))
        control.token.on_cancel(broken)
        control.token.on_cancel(lambda: calls.append("registered_last"))

        assert control.cancel() is True
        assert control.is_cancelled is True

    assert calls == ["registered_last", "registered_first"]


def test_a_callback_registered_after_cancellation_fires_immediately(registry):
    calls: list[str] = []
    with _root(registry) as control:
        control.cancel()
        control.token.on_cancel(lambda: calls.append("late"))

    assert calls == ["late"]


def test_wait_or_raise_returns_false_on_a_plain_timeout(registry):
    with _root(registry) as control:
        assert control.token.wait_or_raise(0.05) is False


def test_wait_or_raise_raises_when_cancelled_during_the_wait(registry):
    with _root(registry) as control:
        timer = threading.Timer(0.05, control.cancel)
        timer.start()
        try:
            with pytest.raises(SETTCancelledError):
                control.token.wait_or_raise(5.0)
        finally:
            timer.cancel()


def test_cooperative_sleep_uses_only_the_public_token_api(registry):
    with _root(registry) as control:
        started = time.monotonic()
        timer = threading.Timer(0.05, control.cancel)
        timer.start()
        try:
            with pytest.raises(SETTCancelledError):
                cooperative_sleep(5.0, control.token)
        finally:
            timer.cancel()
        # Ended early because of the cancellation, not after the full wait.
        assert time.monotonic() - started < 2.0


def test_a_token_cannot_cancel_anything(registry):
    with _root(registry) as control:
        token = control.token
        assert not hasattr(token, "cancel")
        assert isinstance(token, CancellationToken)
        assert isinstance(control.source, CancellationSource)
        assert control.source.token.run_id == token.run_id


def test_cancel_reason_must_be_a_non_empty_string(registry):
    with _root(registry) as control, pytest.raises(SETTValidationError):
        control.cancel(reason="   ")


# ── The control tree resolves against ExecutionContext ──────────────────────


def test_cancelling_a_child_leaves_the_root_and_siblings_running(registry):
    with _root(registry) as root, root.child() as first, root.child() as second:
        assert first.cancel() is True

        assert first.is_cancelled is True
        assert second.is_cancelled is False
        assert root.is_cancelled is False
        second.checkpoint()  # a sibling keeps going
        root.checkpoint()


def test_cancelling_the_root_cancels_every_registered_descendant(registry):
    with _root(registry) as root, root.child() as branch, branch.child() as leaf:
        assert root.cancel(reason="execution.session_ended") is True

        for control in (root, branch, leaf):
            assert control.is_cancelled is True
            with pytest.raises(SETTCancelledError):
                control.checkpoint()
        # The leaf can report which branch was cancelled, not just
        # that it was.
        assert leaf.token.snapshot().cancelled_run_id == root.run_id
        assert (
            leaf.token.snapshot().reason == "execution.session_ended"
        )


def test_a_control_registered_after_an_ancestor_was_cancelled_inherits_it(
    registry,
):
    """The late-registration window the downward walk alone cannot cover."""
    with _root(registry) as root, root.child() as branch:
        branch.cancel(reason="execution.branch_cancelled")

        # Built and entered only now, well after the cancellation.
        late = branch.child()
        with pytest.raises(SETTCancelledError) as excinfo:
            late.__enter__()

    assert excinfo.value.reason == "execution.branch_cancelled"


def test_cancellation_is_inherited_through_several_generations(registry):
    with (
        _root(registry) as root,
        root.child() as generation_one,
        generation_one.child() as two,
        two.child() as three,
    ):
        root.cancel()
        assert three.is_cancelled is True


def test_a_control_uses_the_run_id_that_appears_in_its_own_context(registry):
    """The trace identifier and the cancellation handle are the same thing."""
    context = ExecutionContext.create()
    with ExecutionControl.create_root(context, registry=registry) as control:
        assert control.run_id == context.run_id
        assert control.trace_id == context.trace_id
        assert control.parent_id is context.parent_id

        child_context = context.derive()
        with control.child(context=child_context) as child:
            assert child.run_id == child_context.run_id
            assert child.parent_id == context.run_id
            assert child.trace_id == context.trace_id

            # Cancelling by the identifier a trace event would expose.
            assert (
                registry.cancel_subtree(child_context.run_id) is True
            )
            assert child.is_cancelled is True
            assert control.is_cancelled is False


def test_a_child_control_must_be_built_on_a_derived_context(registry):
    with _root(registry) as root:
        unrelated = ExecutionContext.create()
        with pytest.raises(SETTValidationError):
            root.child(context=unrelated)


def test_two_controls_cannot_share_one_run_id(registry):
    context = ExecutionContext.create()
    with ExecutionControl.create_root(context, registry=registry):
        duplicate = ExecutionControl(context, registry=registry)
        with pytest.raises(SETTConfigurationError):
            duplicate.__enter__()


def test_cancelling_an_unknown_run_id_reports_false(registry):
    assert registry.cancel_subtree("no-such-run") is False
    assert registry.snapshot("no-such-run").cancelled is False


def test_the_registry_does_not_leak_controls(registry):
    with _root(registry) as root:
        with root.child() as branch, branch.child():
            assert len(registry.live_run_ids) == 3
        assert registry.live_run_ids == (root.run_id,)
    assert registry.live_run_ids == ()


def test_a_released_parent_is_retained_while_its_subtree_is_alive(registry):
    """Dropping a node early would break the chain its children resolve on."""
    root = _root(registry)
    root.__enter__()
    child = root.child()
    child.__enter__()

    root.__exit__(None, None, None)
    # Released, but still reachable: the child still resolves through it.
    assert root.run_id in registry.live_run_ids
    assert child.is_cancelled is False

    child.__exit__(None, None, None)


def test_a_retained_run_id_cannot_be_re_registered(registry):
    """Regression: a retained run_id cannot be registered again.

    A released-but-retained node (kept alive only because its subtree has
    not finished evicting) used to be silently overwritten by a fresh
    registration under the same run_id, because the guard only checked
    ``not existing.released``. That breaks the one invariant this
    registry exists to hold: exactly one control per run_id, ever, while
    anything about that run_id is still live.
    """
    root = _root(registry)
    root.__enter__()
    child = root.child()
    child.__enter__()

    root.__exit__(None, None, None)
    assert root.run_id in registry.live_run_ids  # retained, not evicted

    impostor = ExecutionControl(root.context, registry=registry)
    with pytest.raises(SETTConfigurationError):
        impostor.__enter__()

    child.__exit__(None, None, None)
    assert registry.live_run_ids == ()
    assert registry.live_run_ids == ()


# ── 24.2 Deadlines ──────────────────────────────────────────────────────────


def test_a_deadline_without_a_timeout_is_unbounded(registry):
    deadline = Deadline.after(None)
    assert deadline.is_unbounded is True
    assert deadline.expired is False
    assert deadline.remaining() is None
    deadline.throw_if_expired()  # must not raise


def test_a_root_deadline_expires_and_raises_a_timeout(registry):
    with _root(registry, timeout_seconds=0.05) as control:
        assert control.remaining_seconds() is not None
        time.sleep(0.08)
        with pytest.raises(SETTTimeoutError) as excinfo:
            control.checkpoint()

    assert excinfo.value.run_id == control.run_id
    assert excinfo.value.deadline is control.deadline


def test_a_child_timeout_shorter_than_the_parent_wins(registry):
    with (
        _root(registry, timeout_seconds=30.0) as root,
        root.child(timeout_seconds=0.05) as child,
    ):
        assert child.remaining_seconds() < 1.0
        assert root.remaining_seconds() > 10.0
        time.sleep(0.08)
        with pytest.raises(SETTTimeoutError):
            child.checkpoint()
        # The parent still has time; only the child ran out.
        root.checkpoint()


def test_a_child_timeout_longer_than_the_parent_cannot_extend_it(registry):
    with (
        _root(registry, timeout_seconds=0.2) as root,
        root.child(timeout_seconds=600.0) as child,
    ):
        remaining = child.remaining_seconds()
        assert remaining is not None and remaining <= 0.2


def test_a_child_of_an_unbounded_parent_may_set_its_own_timeout(registry):
    with _root(registry) as root:
        assert root.remaining_seconds() is None
        with root.child(timeout_seconds=5.0) as child:
            assert child.remaining_seconds() is not None


def test_remaining_is_never_negative():
    deadline = Deadline.after(0.01)
    time.sleep(0.05)
    assert deadline.remaining() == 0.0
    assert deadline.expired is True


def test_deadlines_are_measured_with_the_monotonic_clock(monkeypatch):
    """A system-clock adjustment must not grant or revoke time."""
    fake_now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    deadline = Deadline.after(5.0)
    assert deadline.expired is False
    assert deadline.remaining() == 5.0

    fake_now[0] += 4.0
    assert deadline.expired is False
    assert deadline.remaining() == 1.0

    fake_now[0] += 2.0
    assert deadline.expired is True
    assert deadline.remaining() == 0.0


def test_a_non_positive_timeout_is_rejected():
    with pytest.raises(SETTValidationError):
        Deadline.after(0)
    with pytest.raises(SETTValidationError):
        Deadline.after(-1.0)
    with pytest.raises(SETTValidationError):
        Deadline.after(True)


def test_nan_and_infinite_timeouts_are_rejected():
    """Regression: NaN and infinite timeouts are rejected.

    NaN is the dangerous one: every comparison against it is False, so
    `expired` (`now >= expires_at`) would never become True and a NaN
    "deadline" would silently never expire. Infinity is refused too; the
    honest spelling of "no deadline" is `seconds=None`.
    """
    with pytest.raises(SETTValidationError):
        Deadline.after(float("nan"))
    with pytest.raises(SETTValidationError):
        Deadline.after(float("inf"))
    with pytest.raises(SETTValidationError):
        Deadline.after(float("-inf"))


def test_cancellation_is_reported_before_an_expired_deadline(registry):
    """Both stop the run; the caller's explicit instruction is the reason."""
    with _root(registry, timeout_seconds=0.05) as control:
        control.cancel()
        time.sleep(0.08)
        with pytest.raises(SETTCancelledError):
            control.checkpoint()


# ── Cleanup hooks ───────────────────────────────────────────────────────────


def test_cleanup_hooks_run_lifo_on_normal_exit(registry):
    calls: list[str] = []
    with _root(registry) as control:
        control.register_cleanup(lambda: calls.append("opened_first"))
        control.register_cleanup(lambda: calls.append("opened_second"))

    assert calls == ["opened_second", "opened_first"]


def test_cleanup_hooks_run_when_the_body_raises(registry):
    calls: list[str] = []
    with pytest.raises(ValueError), _root(registry) as control:
        control.register_cleanup(lambda: calls.append("released"))
        raise ValueError("body failed")

    assert calls == ["released"]


def test_a_failing_cleanup_hook_does_not_replace_the_reason(registry):
    def broken() -> None:
        raise OSError("could not delete the temporary file")

    calls: list[str] = []
    with pytest.raises(SETTTimeoutError), _root(
        registry, timeout_seconds=0.05
    ) as control:
        control.register_cleanup(lambda: calls.append("ran_anyway"))
        control.register_cleanup(broken)
        time.sleep(0.08)
        control.checkpoint()

    # The run timed out; the OSError is recorded, never raised in its place.
    assert control.cleanup_errors == ("OSError",)
    assert calls == ["ran_anyway"]


def test_cleanup_hooks_cannot_be_registered_after_the_scope_finished(registry):
    with _root(registry) as control:
        pass
    with pytest.raises(SETTConfigurationError):
        control.register_cleanup(lambda: None)


# ── The __enter__ / __exit__ correction ─────────────────────────────────────


def test_a_control_whose_entry_checkpoint_fails_still_settles_itself(registry):
    """Python never calls __exit__() when __enter__() raises."""
    calls: list[str] = []
    control = _root(registry, timeout_seconds=0.05)
    control.register_cleanup(lambda: calls.append("outer"))
    control.register_cleanup(lambda: calls.append("inner"))
    time.sleep(0.08)

    with pytest.raises(SETTTimeoutError), control:
        pytest.fail("the body must never run")

    assert control.phase is ControlPhase.FINISHED
    assert isinstance(control.terminal_error, SETTTimeoutError)
    assert calls == ["inner", "outer"]
    assert registry.live_run_ids == ()


def test_a_control_cancelled_before_entry_settles_itself_too(registry):
    with _root(registry) as root:
        child = root.child()
        root.cancel()
        with pytest.raises(SETTCancelledError):
            child.__enter__()

        assert child.phase is ControlPhase.FINISHED
        assert isinstance(child.terminal_error, SETTCancelledError)
        assert child.run_id not in registry.live_run_ids


def test_a_control_cannot_be_entered_twice(registry):
    with _root(registry) as control, pytest.raises(SETTConfigurationError):
        control.__enter__()


def test_exiting_twice_is_harmless(registry):
    control = _root(registry)
    control.__enter__()
    control.__exit__(None, None, None)
    finished_at = control.finished_at
    control.__exit__(None, None, None)
    assert control.finished_at == finished_at


def test_a_control_records_its_own_duration(registry):
    with _root(registry) as control:
        time.sleep(0.02)
    assert control.started_at is not None
    assert control.finished_at is not None
    assert control.elapsed_seconds >= 0.02


# ── A cancellation tree is not a dependency graph ───────────────────────────


def test_the_three_stop_reasons_are_distinct_types():
    assert issubclass(SETTCancelledError, SETTError)
    assert issubclass(SETTTimeoutError, SETTError)
    assert issubclass(SETTDependencyError, SETTError)

    for left, right in (
        (SETTCancelledError, SETTTimeoutError),
        (SETTCancelledError, SETTDependencyError),
        (SETTTimeoutError, SETTDependencyError),
    ):
        assert not issubclass(left, right)
        assert not issubclass(right, left)


def test_a_timeout_is_not_caught_as_a_cancellation(registry):
    with _root(registry, timeout_seconds=0.05) as control:
        time.sleep(0.08)
        with pytest.raises(SETTTimeoutError):
            try:
                control.checkpoint()
            except SETTCancelledError:  # pragma: no cover - must not happen
                pytest.fail("a deadline was reported as a cancellation")


def test_a_dependency_failure_names_the_dependency_not_a_canceller():
    error = SETTDependencyError(
        "stage.not_executed",
        run_id="run-b",
        failed_dependency_run_id="run-a",
    )
    assert error.run_id == "run-b"
    assert error.failed_dependency_run_id == "run-a"
    assert not isinstance(error, SETTCancelledError)


# ── Ambient propagation ─────────────────────────────────────────────────────


def test_there_is_no_ambient_control_by_default():
    assert current_execution_control() is None


def test_lifecycle_scope_binds_and_restores_the_active_control(registry):
    with _root(registry) as control:
        assert current_execution_control() is None
        with lifecycle_scope(control) as bound:
            assert bound is control
            assert current_execution_control() is control
        assert current_execution_control() is None


def test_lifecycle_scope_restores_the_outer_control(registry):
    with _root(registry) as root, lifecycle_scope(root):
        with root.child() as child, lifecycle_scope(child):
            assert current_execution_control() is child
        assert current_execution_control() is root


def test_lifecycle_scope_restores_even_when_the_body_raises(registry):
    with _root(registry) as control:
        with pytest.raises(ValueError), lifecycle_scope(control):
            raise ValueError("boom")
        assert current_execution_control() is None


# ── LifecyclePolicy ─────────────────────────────────────────────────────────


def test_an_empty_policy_imposes_no_timeout_anywhere():
    policy = LifecyclePolicy()
    assert policy.run_timeout_seconds is None
    assert policy.stage_timeout("health") is None
    assert policy.agent_timeout("health") is None
    assert policy.adapter_timeout("ollama") is None
    assert policy.handler_timeout("send_sms") is None


def test_a_specific_timeout_overrides_its_level_default():
    policy = LifecyclePolicy(
        default_handler_timeout_seconds=5.0,
        handler_timeouts={"charge_payment": 12.0},
    )
    assert policy.handler_timeout("charge_payment") == 12.0
    assert policy.handler_timeout("send_notification") == 5.0


def test_policy_timeout_tables_are_read_only():
    policy = LifecyclePolicy(stage_timeouts={"health": 8.0})
    with pytest.raises(TypeError):
        policy.stage_timeouts["health"] = 1.0


def test_policy_rejects_non_positive_timeouts():
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(run_timeout_seconds=0)
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(stage_timeouts={"health": -1.0})
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(handler_timeouts={"": 1.0})


def test_policy_rejects_nan_and_infinite_timeouts():
    """Regression, for LifecyclePolicy's own fields: NaN and infinity are rejected."""
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(run_timeout_seconds=float("nan"))
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(default_agent_timeout_seconds=float("inf"))
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(agent_timeouts={"health": float("nan")})
    with pytest.raises(SETTValidationError):
        LifecyclePolicy(handler_timeouts={"charge": float("inf")})
