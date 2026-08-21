"""
Adversarial audit and backward compatibility.

Two jobs.

The first is section 24.8: proving that a caller who never asked for any
of this sees exactly what v0.11.0 gave them, down to the exception type.
That is the promise the whole version rests on, and "the old tests still
pass" is necessary but not sufficient; the old tests were written before
there was anything to collide with.

The second is deliberately hostile. Every test below the compatibility
section describes something no earlier test in this version anticipated:
races between cancellers, a store that breaks, a handler that submits
another action, a cleanup hook that cancels, an expert that raises a
cancellation nobody requested. A passing suite proves the code does what
its author expected; these exist to find out what it does when somebody
does something its author did not.
"""
from __future__ import annotations

import threading
import time

import pytest

from sett import (
    Action,
    EthicalFilter,
    ExecutionContext,
    ExecutionStatus,
    InMemoryIdempotencyStore,
    PipelineStep,
    RetryPolicy,
    SETTAgent,
    SETTCancelledError,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTExecutor,
    SETTExpert,
    SETTOrchestrator,
    SETTTimeoutError,
    SETTValidationError,
    StageOutcome,
)
from sett.core_ruler.lifecycle import (
    ExecutionControl,
    ExecutionControlRegistry,
    current_execution_control,
)
from sett.ethics_ruler.ethic_kernel.context_analyzer import ContextAnalyzer
from sett.ethics_ruler.ethic_kernel.rules import EthicalRuleset


class TraceAgent(SETTAgent):
    def __init__(self, domain: str):
        super().__init__(name=f"{domain}_agent", domain=domain)
        self.calls = 0

    def process(self, input_data):
        self.calls += 1
        self._publish_to_universal({f"{self.domain}_done": True})
        return {"trail": input_data.get("trail", []) + [self.domain]}


class SilentAgent(SETTAgent):
    def __init__(self, domain: str):
        super().__init__(name=f"{domain}_agent", domain=domain)

    def process(self, input_data):
        return {"trail": input_data.get("trail", [])}


def strict_filter() -> EthicalFilter:
    return EthicalFilter(
        ruleset=EthicalRuleset(
            name="strict_test", reject_threshold=0.1, warn_threshold=0.05
        )
    )


# ── 24.8 Backward compatibility ─────────────────────────────────────────────


def test_process_raises_the_exact_rejection_type_a_legacy_caller_catches():
    orchestrator = SETTOrchestrator(ethical_filter=strict_filter())
    orchestrator.register_agent(TraceAgent("health"))

    try:
        orchestrator.process({}, domain="health", emotional_state="crisis")
    except SETTEthicalFilterRejectedError as error:
        assert error.score is not None
        assert error.threshold is not None
    else:
        pytest.fail("the rejection type a v0.11.0 caller catches did not arrive")


@pytest.mark.parametrize(
    "raised, expected",
    [
        (SETTConfigurationError("misconfigured"), SETTConfigurationError),
        (ValueError("a plain error"), ValueError),
        (KeyError("missing"), KeyError),
    ],
)
def test_process_still_propagates_every_exception_type_unchanged(
    raised, expected
):
    class RaisingAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="raiser", domain="raise")

        def process(self, input_data):
            raise raised

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(RaisingAgent())

    with pytest.raises(expected):
        orchestrator.process({}, domain="raise")


def test_the_legacy_entry_points_return_exactly_what_they_used_to():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.register_agent(TraceAgent("second"))

    routed = orchestrator.process({"trail": []}, domain="first")
    broadcast = orchestrator.process({"trail": []})
    pipeline = orchestrator.run_pipeline(["first", "second"], {"trail": []})

    assert routed == {"trail": ["first"]}
    assert set(broadcast) == {"first", "second"}
    assert pipeline.completed is True
    assert pipeline.output == {"trail": ["first", "second"]}
    assert isinstance(pipeline.steps[0], StageOutcome)


def test_a_legacy_stage_outcome_still_compares_equal_to_itself():
    """Positional construction of the original four fields is untouched."""
    left = StageOutcome("health", "completed", {"x": 1}, None)
    right = StageOutcome(
        domain="health", status="completed", output={"x": 1}, rejection=None
    )
    assert left == right


def test_submit_returns_the_bare_handler_value_not_a_result_object():
    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_handler("noop", lambda payload: {"ok": True})

    assert executor.submit(Action("noop")) == {"ok": True}


def test_an_action_built_through_the_legacy_api_carries_no_idempotency_key():
    action = Action("send_sms", {"to": "someone"}, "comms")
    assert action.idempotency_key is None
    assert action.proposed_by == "comms"


def test_an_orchestrator_built_through_the_legacy_api_needs_no_new_arguments():
    orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
    orchestrator.register_agent(TraceAgent("health"))
    assert orchestrator.process({"trail": []}, domain="health")


# ── Races between cancellers ────────────────────────────────────────────────


def test_only_one_of_many_concurrent_cancellers_reports_the_cancellation():
    registry = ExecutionControlRegistry()
    results: list[bool] = []
    barrier = threading.Barrier(8)

    with ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry
    ) as control:

        def cancel() -> None:
            barrier.wait(timeout=5)
            results.append(control.cancel(reason="execution.race"))

        threads = [threading.Thread(target=cancel) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert results.count(True) == 1
        assert results.count(False) == 7
        assert control.token.snapshot().reason == "execution.race"


def test_cancelling_while_children_are_being_created_reaches_all_of_them():
    """The registration window, hammered from two threads at once."""
    registry = ExecutionControlRegistry()
    observed: list[bool] = []
    stop = threading.Event()

    with ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry
    ) as root:

        def spawn_children() -> None:
            while not stop.is_set():
                child = root.child()
                try:
                    with child:
                        observed.append(child.is_cancelled)
                except SETTCancelledError:
                    observed.append(True)

        spawner = threading.Thread(target=spawn_children)
        spawner.start()
        time.sleep(0.05)
        root.cancel()
        time.sleep(0.05)
        stop.set()
        spawner.join(timeout=5)

    # Once the root was cancelled, no child may ever have observed itself
    # as running: the last observations must all be True.
    assert observed[-1] is True
    assert registry.live_run_ids == ()


def test_a_run_that_already_finished_cannot_be_cancelled_afterwards():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))
    turn = ExecutionContext.create()

    result = orchestrator.process_controlled(
        {"trail": []}, domain="health", execution_context=turn
    )

    assert result.succeeded is True
    assert orchestrator.cancel(turn.run_id) is False


# ── A store that misbehaves ─────────────────────────────────────────────────


class BrokenStore(InMemoryIdempotencyStore):
    """A durable store whose backend is unreachable."""

    def reserve(self, *, key, fingerprint, trace_id):
        raise RuntimeError("the idempotency backend is unreachable")


def test_a_store_that_cannot_reserve_never_lets_the_effect_happen():
    """Fail closed: an unknown idempotency state is not a licence to act."""
    calls: list[int] = []
    orchestrator = SETTOrchestrator()
    executor = SETTExecutor(idempotency_store=BrokenStore())
    orchestrator.register_executor(executor)
    executor.register_handler(
        "charge_payment", lambda payload: calls.append(1) or {"ok": True}
    )

    with pytest.raises(RuntimeError, match="unreachable"):
        executor.submit_controlled(
            Action("charge_payment", {"amount": 10}, idempotency_key="k")
        )

    assert calls == []


def test_a_handler_returning_an_unserializable_value_still_records_a_digest():
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    store = InMemoryIdempotencyStore()
    orchestrator = SETTOrchestrator()
    executor = SETTExecutor(idempotency_store=store)
    orchestrator.register_executor(executor)
    executor.register_handler("charge_payment", lambda payload: Opaque())

    result = executor.submit_controlled(
        Action("charge_payment", {"amount": 10}, idempotency_key="k")
    )

    assert result.succeeded is True
    assert store.get("k").result_digest is not None


# ── Handlers doing unanticipated things ─────────────────────────────────────


def test_a_handler_that_submits_another_action_does_not_corrupt_the_trace():
    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_handler("inner", lambda payload: {"inner": True})
    executor.register_handler(
        "outer",
        lambda payload: {
            "outer": True,
            "nested": executor.submit(Action("inner")),
        },
    )
    root = ExecutionContext.create()
    orchestrator._trace_recorder.record(
        root,
        kind="trace.started",
        component_type="orchestrator",
        component_name="TestHarness",
        status="started",
    )

    result = executor.submit_controlled(
        Action("outer", {}, idempotency_key="outer:1"),
        execution_context=root,
    )

    orchestrator._trace_recorder.record(
        root,
        kind="trace.completed",
        component_type="orchestrator",
        component_name="TestHarness",
        status="completed",
        cause_id=orchestrator._trace_recorder.last_event_id(root.run_id),
    )
    assert result.unwrap() == {"outer": True, "nested": {"inner": True}}
    assert orchestrator.verify_traces(root.trace_id) is True


def test_a_handler_that_cancels_its_own_run_stops_and_is_not_retried():
    calls: list[int] = []

    def self_cancelling(payload, context):
        calls.append(1)
        raise SETTCancelledError("execution.handler_gave_up")

    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge_payment", self_cancelling)
    root = ExecutionContext.create()
    orchestrator._trace_recorder.record(
        root,
        kind="trace.started",
        component_type="orchestrator",
        component_name="TestHarness",
        status="started",
    )

    result = executor.submit_controlled(
        Action("charge_payment", {"amount": 10}, idempotency_key="k"),
        execution_context=root,
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
    )

    orchestrator._trace_recorder.record(
        root,
        kind="trace.cancelled",
        component_type="orchestrator",
        component_name="TestHarness",
        status="cancelled",
        cause_id=orchestrator._trace_recorder.last_event_id(root.run_id),
    )
    assert len(calls) == 1
    assert result.cancelled is True
    kinds = [event.kind for event in orchestrator.get_trace(root.trace_id)]
    # Stopped, not broken: the handler boundary says so.
    assert "handler.cancelled" in kinds
    assert "handler.failed" not in kinds
    assert orchestrator.verify_traces(root.trace_id) is True


def test_a_handler_that_runs_out_of_time_closes_its_boundary_as_timed_out():
    def slow_handler(payload, context):
        raise SETTTimeoutError("the provider stopped answering")

    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge_payment", slow_handler)
    root = ExecutionContext.create()
    orchestrator._trace_recorder.record(
        root,
        kind="trace.started",
        component_type="orchestrator",
        component_name="TestHarness",
        status="started",
    )

    result = executor.submit_controlled(
        Action("charge_payment", {"amount": 10}, idempotency_key="k"),
        execution_context=root,
    )

    orchestrator._trace_recorder.record(
        root,
        kind="trace.timed_out",
        component_type="orchestrator",
        component_name="TestHarness",
        status="timed_out",
        cause_id=orchestrator._trace_recorder.last_event_id(root.run_id),
    )
    assert result.timed_out is True
    assert result.reason_code == "action.outcome_unknown"
    kinds = [event.kind for event in orchestrator.get_trace(root.trace_id)]
    assert "handler.timed_out" in kinds
    assert "handler.failed" not in kinds
    assert orchestrator.verify_traces(root.trace_id) is True


# ── Cleanup hooks doing unanticipated things ────────────────────────────────


def test_a_cleanup_hook_that_cancels_its_own_run_does_not_hang_or_recurse():
    registry = ExecutionControlRegistry()
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry
    )
    with control:
        control.register_cleanup(lambda: control.cancel(reason="cleanup.late"))

    assert control.cleanup_errors == ()
    assert registry.live_run_ids == ()


def test_a_cleanup_hook_registering_another_hook_does_not_loop_forever():
    registry = ExecutionControlRegistry()
    calls: list[str] = []
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry
    )

    def outer() -> None:
        calls.append("outer")
        # Registering during unwinding: the stack was already taken, so
        # this must not be picked up and must not raise on a live scope.
        try:
            control.register_cleanup(lambda: calls.append("never"))
        except SETTConfigurationError:
            calls.append("refused")

    with control:
        control.register_cleanup(outer)

    assert "never" not in calls
    assert registry.live_run_ids == ()


def test_a_cleanup_hook_raising_a_base_exception_is_not_swallowed_silently():
    """KeyboardInterrupt is not an ordinary failure and must not be hidden."""
    registry = ExecutionControlRegistry()
    control = ExecutionControl.create_root(
        ExecutionContext.create(), registry=registry
    )

    def brutal() -> None:
        raise KeyboardInterrupt("user pressed control-c")

    with pytest.raises(KeyboardInterrupt), control:
        control.register_cleanup(brutal)


# ── Components impersonating framework signals ──────────────────────────────


def test_an_expert_raising_a_cancellation_nobody_requested_is_still_reported():
    """SETT reports what it was told, and the trace stays consistent."""

    class LyingExpert(SETTExpert):
        def resolve(self, context):
            raise SETTCancelledError("execution.expert_decided_to_stop")

    class LyingAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="liar", domain="liar")
            self.register_expert(LyingExpert(name="liar_expert"))

        def process(self, input_data):
            return self.get_expert("liar_expert").resolve(input_data)

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(LyingAgent())
    turn = ExecutionContext.create()

    result = orchestrator.process_controlled(
        {}, domain="liar", execution_context=turn
    )

    assert result.cancelled is True
    # Nobody actually cancelled the control, so it is not marked cancelled.
    assert orchestrator.control_registry.live_run_ids == ()
    assert orchestrator.verify_traces(turn.trace_id) is True


def test_an_agent_raising_a_timeout_nobody_reached_is_reported_as_timed_out():
    class ImpatientAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="impatient", domain="impatient")

        def process(self, input_data):
            raise SETTTimeoutError("the provider gave up on us")

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(ImpatientAgent())
    turn = ExecutionContext.create()

    result = orchestrator.process_controlled(
        {}, domain="impatient", execution_context=turn
    )

    assert result.timed_out is True
    assert orchestrator.verify_traces(turn.trace_id) is True


# ── Broadcast and pipelines under cancellation ──────────────────────────────


def test_cancelling_a_broadcast_stops_it_and_leaves_a_verifiable_trace():
    class SlowAgent(SETTAgent):
        def __init__(self, domain):
            super().__init__(name=domain, domain=domain)
            self.started = threading.Event()

        def process(self, input_data):
            self.started.set()
            deadline = time.monotonic() + 5.0
            control = current_execution_control()
            while time.monotonic() < deadline:
                if control is not None:
                    control.checkpoint()
                time.sleep(0.01)
            return {}

    orchestrator = SETTOrchestrator()
    first = SlowAgent("first")
    orchestrator.register_agent(first)
    orchestrator.register_agent(TraceAgent("second"))
    turn = ExecutionContext.create()

    canceller = threading.Thread(
        target=lambda: (
            first.started.wait(timeout=5),
            orchestrator.cancel(turn.run_id),
        )
    )
    canceller.start()
    result = orchestrator.process_controlled({}, execution_context=turn)
    canceller.join(timeout=5)

    assert result.cancelled is True
    kinds = [event.kind for event in orchestrator.get_trace(turn.trace_id)]
    assert "broadcast.cancelled" in kinds
    assert orchestrator.verify_traces(turn.trace_id) is True


def test_a_pipeline_rejected_at_the_first_stage_skips_every_later_stage():
    orchestrator = SETTOrchestrator(ethical_filter=strict_filter())
    orchestrator.register_agent(TraceAgent("first"))
    second = TraceAgent("second")
    third = TraceAgent("third")
    orchestrator.register_agent(second)
    orchestrator.register_agent(third)

    result = orchestrator.run_pipeline_controlled(
        ["first", "second", "third"], {}, emotional_state="crisis"
    )

    assert result.status is ExecutionStatus.REJECTED
    assert result.state.completed_units == 0
    assert [stage.status for stage in result.value.steps] == [
        "rejected",
        "skipped",
        "skipped",
    ]
    assert second.calls == 0 and third.calls == 0
    for stage in result.value.steps[1:]:
        assert stage.skip_cause.domain == "first"


def test_a_transform_raising_is_a_failure_not_a_cancellation():
    def broken_transform(original, previous):
        raise ValueError("the transform is broken")

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.register_agent(TraceAgent("second"))

    result = orchestrator.run_pipeline_controlled(
        ["first", PipelineStep(domain="second", transform=broken_transform)],
        {"trail": []},
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.cancelled is False
    with pytest.raises(ValueError):
        result.unwrap()


# ── The registry under load ─────────────────────────────────────────────────


def test_no_terminal_kind_is_declared_without_ever_being_emitted():
    """Declared-but-unreachable vocabulary is a lie the trace tells.

    This guard exists because the audit found one: ``broadcast.cancelled``
    and ``broadcast.timed_out`` were added to the terminal table in phase 3
    and never emitted, so a cancelled broadcast was still being closed with
    ``broadcast.failed``, reporting the exact difference this version was
    built to make as if it did not exist. Adding a kind to the table and
    forgetting to emit it now fails here instead of in an incident.
    """
    import pathlib

    from sett.audit_ruler.trace import _TERMINAL_BY_START

    package_root = pathlib.Path(__file__).resolve().parents[1] / "sett"
    # The module holding the table is excluded from the scan. Including it
    # is what made the first version of this guard vacuous: every declared
    # kind trivially appeared in the source, because the declaration is
    # itself a string literal. What has to be searched is the emitting
    # code, and trace.py never emits a kind; it receives one.
    declaring_module = package_root / "audit_ruler" / "trace.py"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.rglob("*.py")
        if path != declaring_module
    )
    declared = set(_TERMINAL_BY_START) | {
        kind for kinds in _TERMINAL_BY_START.values() for kind in kinds
    }
    never_emitted = sorted(
        kind for kind in declared if f'"{kind}"' not in source
    )

    assert not never_emitted, (
        "These trace kinds are declared in the terminal table but never "
        f"recorded anywhere in sett/: {never_emitted}"
    )


def test_many_sequential_controlled_runs_leave_nothing_registered():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))

    for _ in range(50):
        orchestrator.process_controlled({"trail": []}, domain="health")

    assert orchestrator.control_registry.live_run_ids == ()


def test_concurrent_controlled_runs_do_not_interfere():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))
    outcomes: list[bool] = []
    barrier = threading.Barrier(6)

    def run_one() -> None:
        barrier.wait(timeout=5)
        outcomes.append(
            orchestrator.process_controlled(
                {"trail": []}, domain="health"
            ).succeeded
        )

    threads = [threading.Thread(target=run_one) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert outcomes == [True] * 6
    assert orchestrator.control_registry.live_run_ids == ()


def test_cancelling_one_concurrent_run_does_not_touch_the_others():
    class SlowAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="slow", domain="slow")
            self.started = threading.Event()

        def process(self, input_data):
            self.started.set()
            deadline = time.monotonic() + 2.0
            control = current_execution_control()
            while time.monotonic() < deadline:
                if control is not None:
                    control.checkpoint()
                time.sleep(0.01)
            return {"finished": True}

    orchestrator = SETTOrchestrator()
    agent = SlowAgent()
    orchestrator.register_agent(agent)

    victim = ExecutionContext.create()
    survivor = ExecutionContext.create()
    results: dict[str, object] = {}

    def run(name, context):
        results[name] = orchestrator.process_controlled(
            {}, domain="slow", execution_context=context
        )

    threads = [
        threading.Thread(target=run, args=("victim", victim)),
        threading.Thread(target=run, args=("survivor", survivor)),
    ]
    for thread in threads:
        thread.start()
    agent.started.wait(timeout=5)
    time.sleep(0.05)
    orchestrator.cancel(victim.run_id)
    for thread in threads:
        thread.join(timeout=10)

    assert results["victim"].cancelled is True
    assert results["survivor"].succeeded is True


# ── Process-level signals must propagate, not become a result ──────────────
#
# D-01 / F04 regression (both audits): KeyboardInterrupt, SystemExit and
# GeneratorExit are not "the run failed"; they mean the process itself is
# being interrupted or torn down. Before this fix, the broad
# `except BaseException` blocks that turn a stopped or failed controlled
# run into a structured ExecutionResult caught these too, so a Ctrl+C
# during a controlled run came back as ExecutionStatus.FAILED instead of
# actually interrupting anything. The narrower `except BaseException` in
# lifecycle.py's ExecutionControl.__enter__() is deliberately unchanged:
# it always re-raises after settling terminal state, never swallows.


def test_keyboard_interrupt_during_process_controlled_is_not_swallowed():
    class InterruptingAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="interrupting", domain="interrupting")

        def process(self, input_data):
            raise KeyboardInterrupt()

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(InterruptingAgent())

    with pytest.raises(KeyboardInterrupt):
        orchestrator.process_controlled({}, domain="interrupting")


def test_system_exit_during_a_controlled_pipeline_is_not_swallowed():
    class InterruptingAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="interrupting", domain="interrupting")

        def process(self, input_data):
            raise SystemExit()

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(InterruptingAgent())

    with pytest.raises(SystemExit):
        orchestrator.run_pipeline_controlled(["interrupting"], {})


def test_keyboard_interrupt_during_a_controlled_handler_is_not_swallowed():
    def interrupting_handler(payload, context):
        raise KeyboardInterrupt()

    orchestrator = SETTOrchestrator()
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    executor.register_controlled_handler("charge_payment", interrupting_handler)

    action = Action(
        action_type="charge_payment",
        payload={"amount": 1},
        proposed_by="billing",
    )
    with pytest.raises(KeyboardInterrupt):
        executor.submit_controlled(action)


def test_a_controlled_run_still_releases_its_control_after_an_interrupt():
    """Cleanup must still happen even though the exception now propagates."""
    class InterruptingAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="interrupting", domain="interrupting")

        def process(self, input_data):
            raise KeyboardInterrupt()

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(InterruptingAgent())

    with pytest.raises(KeyboardInterrupt):
        orchestrator.process_controlled({}, domain="interrupting")

    assert orchestrator.control_registry.live_run_ids == ()


# ---------------------------------------------------------------------------
# Duplicate registration and identifier validation (F05/F06/F07/F08/F21,
# defensive audit #2): a second registration under a name/action_type that
# already has one used to silently overwrite it across handlers, experts,
# and context analyzers, with no signal that anything was lost. The one
# documented, tested exception is SETTOrchestrator.register_agent()'s
# domain-keyed StubDomainAgent swap (see docs/api_reference.md and
# tests/test_stub_agent.py), which is deliberately left untouched here.
# ---------------------------------------------------------------------------

def test_registering_a_second_handler_for_the_same_action_type_is_rejected():
    executor = SETTExecutor()
    executor.register_handler("send_sms", lambda payload: "first")

    with pytest.raises(SETTConfigurationError):
        executor.register_handler("send_sms", lambda payload: "second")


def test_register_controlled_handler_rejects_a_duplicate_of_a_legacy_handler():
    """The two handler maps must not be allowed to drift out of sync:
    an action_type already owned by a legacy handler must not also be
    claimable by a controlled one, and vice versa."""
    executor = SETTExecutor()
    executor.register_handler("send_sms", lambda payload: "legacy")

    with pytest.raises(SETTConfigurationError):
        executor.register_controlled_handler(
            "send_sms", lambda payload, context: "controlled"
        )


def test_unregister_handler_makes_a_deliberate_replacement_possible():
    executor = SETTExecutor()
    executor.register_handler("send_sms", lambda payload: "first")
    executor.unregister_handler("send_sms")
    executor.register_handler("send_sms", lambda payload: "second")

    assert executor.registered_action_types == ["send_sms"]


def test_register_handler_rejects_a_non_callable_handler():
    executor = SETTExecutor()
    with pytest.raises(SETTValidationError):
        executor.register_handler("send_sms", "not-a-function")


def test_register_handler_rejects_an_empty_action_type():
    executor = SETTExecutor()
    with pytest.raises(SETTValidationError):
        executor.register_handler("   ", lambda payload: None)


def test_registering_a_second_expert_under_the_same_name_is_rejected():
    class NamedExpert(SETTExpert):
        def resolve(self, context):
            return {}

    class HostAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="host", domain="host")

        def process(self, input_data):
            return {}

    agent = HostAgent()
    agent.register_expert(NamedExpert(name="scorer"))

    with pytest.raises(SETTConfigurationError):
        agent.register_expert(NamedExpert(name="scorer"))


def test_register_expert_rejects_an_empty_name():
    class NamedExpert(SETTExpert):
        def resolve(self, context):
            return {}

    class HostAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="host", domain="host")

        def process(self, input_data):
            return {}

    agent = HostAgent()
    with pytest.raises(SETTValidationError):
        agent.register_expert(NamedExpert(name="   "))


def test_registering_a_second_analyzer_for_the_same_action_type_is_rejected():
    ethical_filter = EthicalFilter()
    ethical_filter.register_analyzer("confirm_purchase", ContextAnalyzer())

    with pytest.raises(SETTConfigurationError):
        ethical_filter.register_analyzer("confirm_purchase", ContextAnalyzer())


def test_unregister_analyzer_makes_a_deliberate_replacement_possible():
    ethical_filter = EthicalFilter()
    ethical_filter.register_analyzer("confirm_purchase", ContextAnalyzer())
    ethical_filter.unregister_analyzer("confirm_purchase")

    # Re-registering after an explicit unregister must not raise.
    ethical_filter.register_analyzer("confirm_purchase", ContextAnalyzer())


def test_register_analyzer_rejects_a_non_analyzer():
    ethical_filter = EthicalFilter()
    with pytest.raises(SETTValidationError):
        ethical_filter.register_analyzer("confirm_purchase", object())


def test_register_analyzer_rejects_an_empty_action_type():
    ethical_filter = EthicalFilter()
    with pytest.raises(SETTValidationError):
        ethical_filter.register_analyzer("   ", ContextAnalyzer())


def test_an_agent_with_an_empty_name_is_rejected():
    class MinimalAgent(SETTAgent):
        def process(self, input_data):
            return {}

    with pytest.raises(SETTValidationError):
        MinimalAgent(name="", domain="health")


def test_an_agent_with_a_whitespace_only_domain_is_rejected():
    class MinimalAgent(SETTAgent):
        def process(self, input_data):
            return {}

    with pytest.raises(SETTValidationError):
        MinimalAgent(name="HealthAgent", domain="   ")
