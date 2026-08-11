"""
Phase 4 of v0.12.0: the controlled entry points, end to end.

The scenario that motivates the whole version is the last section of this
file: a voice turn is cancelled mid-flight - a barge-in - and the
conversation session it belongs to keeps running. That only works because
a control is addressed by the same ``run_id`` its trace events carry, so
an application can cancel exactly the branch it means to.

Everything before that section establishes the properties that make it
safe: the legacy entry points are untouched and behave identically, a run
without lifecycle management pays nothing, and work that really completed
is never thrown away when a later stage stops.
"""
from __future__ import annotations

import threading
import time

import pytest

from sett import (
    EthicalFilter,
    ExecutionContext,
    ExecutionStatus,
    LifecyclePolicy,
    PipelineStep,
    SETTAgent,
    SETTAgentNotFoundError,
    SETTCancelledError,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTError,
    SETTExpert,
    SETTOrchestrator,
    SETTTimeoutError,
    SkipCause,
)
from sett.core_ruler.lifecycle import current_execution_control
from sett.ethics_ruler.ethic_kernel.rules import EthicalRuleset


# ── Agents used across the file ─────────────────────────────────────────────


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
        self.calls = 0

    def process(self, input_data):
        self.calls += 1
        return {"trail": input_data.get("trail", [])}


class SlowAgent(SETTAgent):
    """Sleeps cooperatively, observing the ambient control as it goes."""

    def __init__(self, domain: str, seconds: float = 5.0):
        super().__init__(name=f"{domain}_agent", domain=domain)
        self.seconds = seconds
        self.started = threading.Event()
        self.finished = False

    def process(self, input_data):
        self.started.set()
        control = current_execution_control()
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            if control is not None:
                control.checkpoint()
            time.sleep(0.01)
        self.finished = True
        return {"trail": input_data.get("trail", []) + [self.domain]}


class ExplodingAgent(SETTAgent):
    def __init__(self, domain: str):
        super().__init__(name=f"{domain}_agent", domain=domain)

    def process(self, input_data):
        raise ValueError("this agent is broken")


def strict_orchestrator(**kwargs) -> SETTOrchestrator:
    return SETTOrchestrator(
        ethical_filter=EthicalFilter(
            ruleset=EthicalRuleset(
                name="strict_test", reject_threshold=0.1, warn_threshold=0.05
            )
        ),
        **kwargs,
    )


# ── The legacy entry points pay nothing and behave identically ──────────────


def test_process_still_returns_the_bare_result_and_raises_as_before():
    orchestrator = strict_orchestrator()
    orchestrator.register_agent(TraceAgent("health"))

    assert orchestrator.process({"trail": []}, domain="health") == {
        "trail": ["health"]
    }
    with pytest.raises(SETTEthicalFilterRejectedError):
        orchestrator.process(
            {"trail": []}, domain="health", emotional_state="crisis"
        )


def test_no_control_is_established_by_the_legacy_path():
    """The checkpoints added at the boundaries are no-ops without one."""
    seen: list[object] = []

    class Probe(SETTExpert):
        def resolve(self, context):
            seen.append(current_execution_control())
            return {}

    class ProbeAgent(SETTAgent):
        def __init__(self):
            super().__init__(name="probe_agent", domain="probe")
            self.register_expert(Probe(name="probe"))

        def process(self, input_data):
            seen.append(current_execution_control())
            self.get_expert("probe").resolve(input_data)
            return {"ok": True}

    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(ProbeAgent())
    orchestrator.process({}, domain="probe")

    assert seen == [None, None]


def test_the_legacy_path_leaves_no_control_registered():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))
    orchestrator.process({"trail": []}, domain="health")
    orchestrator.run_pipeline(["health"], {"trail": []})

    assert orchestrator.control_registry.live_run_ids == ()


def test_an_orchestrator_built_the_old_way_has_no_timeouts():
    orchestrator = SETTOrchestrator()
    policy = orchestrator.lifecycle_policy
    assert policy.run_timeout_seconds is None
    assert policy.stage_timeout("anything") is None


# ── process_controlled() ────────────────────────────────────────────────────


def test_process_controlled_returns_a_successful_result():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))

    result = orchestrator.process_controlled({"trail": []}, domain="health")

    assert result.succeeded is True
    assert result.status is ExecutionStatus.SUCCESS
    assert result.unwrap() == {"trail": ["health"]}
    assert result.trace_id == orchestrator.last_trace_id
    assert result.state.duration_seconds is not None


def test_a_rejection_comes_back_as_rejected_not_failed():
    orchestrator = strict_orchestrator()
    orchestrator.register_agent(TraceAgent("health"))

    result = orchestrator.process_controlled(
        {"trail": []}, domain="health", emotional_state="crisis"
    )

    assert result.status is ExecutionStatus.REJECTED
    assert result.status is not ExecutionStatus.FAILED
    assert result.reason_code == "policy.reject"
    # The real exception survives, with its structured attributes.
    with pytest.raises(SETTEthicalFilterRejectedError) as excinfo:
        result.unwrap()
    assert excinfo.value.score is not None


def test_a_component_exception_comes_back_as_failed_and_unwraps_intact():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(ExplodingAgent("health"))

    result = orchestrator.process_controlled({}, domain="health")

    assert result.status is ExecutionStatus.FAILED
    assert result.error_type == "ValueError"
    with pytest.raises(ValueError, match="this agent is broken"):
        result.unwrap()


def test_an_unknown_domain_comes_back_as_failed():
    orchestrator = SETTOrchestrator()
    result = orchestrator.process_controlled({}, domain="nope")
    assert result.status is ExecutionStatus.FAILED
    with pytest.raises(SETTAgentNotFoundError):
        result.unwrap()


def test_a_run_timeout_ends_the_run_as_timed_out_not_cancelled():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(SlowAgent("health", seconds=5.0))

    result = orchestrator.process_controlled(
        {"trail": []}, domain="health", timeout_seconds=0.05
    )

    assert result.timed_out is True
    assert result.cancelled is False
    assert result.reason_code == "execution.deadline_exceeded"
    with pytest.raises(SETTTimeoutError):
        result.unwrap()


def test_the_lifecycle_policy_supplies_the_run_timeout():
    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(run_timeout_seconds=0.05)
    )
    orchestrator.register_agent(SlowAgent("health", seconds=5.0))

    result = orchestrator.process_controlled({"trail": []}, domain="health")

    assert result.timed_out is True


def test_an_explicit_timeout_overrides_the_policy():
    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(run_timeout_seconds=0.05)
    )
    orchestrator.register_agent(TraceAgent("health"))

    result = orchestrator.process_controlled(
        {"trail": []}, domain="health", timeout_seconds=30.0
    )

    assert result.succeeded is True


def test_the_lifecycle_policy_supplies_a_per_agent_timeout():
    """H-01 regression: agent_timeouts must actually stop a slow agent.

    Before this fix, ``LifecyclePolicy.agent_timeout()`` was resolvable
    but never consulted anywhere in the real execution path: an agent
    given a strict per-domain timeout still inherited only the run's
    overall deadline.
    """
    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(agent_timeouts={"health": 0.05})
    )
    orchestrator.register_agent(SlowAgent("health", seconds=5.0))

    result = orchestrator.process_controlled({"trail": []}, domain="health")

    assert result.timed_out is True


def test_the_default_agent_timeout_applies_when_no_override_matches():
    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(default_agent_timeout_seconds=0.05)
    )
    orchestrator.register_agent(SlowAgent("health", seconds=5.0))

    result = orchestrator.process_controlled({"trail": []}, domain="health")

    assert result.timed_out is True


def test_an_agent_timeout_does_not_affect_a_different_domain():
    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(agent_timeouts={"other": 0.05})
    )
    orchestrator.register_agent(TraceAgent("health"))

    result = orchestrator.process_controlled({"trail": []}, domain="health")

    assert result.succeeded is True


def test_a_controlled_run_releases_its_controls():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))
    orchestrator.process_controlled({"trail": []}, domain="health")

    assert orchestrator.control_registry.live_run_ids == ()


def test_a_controlled_run_produces_a_verifiable_trace():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("health"))
    orchestrator.process_controlled({"trail": []}, domain="health")

    assert orchestrator.verify_traces(orchestrator.last_trace_id) is True


def test_a_timed_out_run_produces_a_verifiable_trace_with_its_own_terminal():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(SlowAgent("health", seconds=5.0))
    orchestrator.process_controlled(
        {"trail": []}, domain="health", timeout_seconds=0.05
    )

    trace_id = orchestrator.last_trace_id
    kinds = [event.kind for event in orchestrator.get_trace(trace_id)]
    assert "trace.timed_out" in kinds
    assert "agent.timed_out" in kinds
    assert "trace.failed" not in kinds
    assert orchestrator.verify_traces(trace_id) is True


# ── run_pipeline_controlled() ───────────────────────────────────────────────


def test_a_controlled_pipeline_completes_like_the_legacy_one():
    orchestrator = SETTOrchestrator()
    for domain in ("first", "second"):
        orchestrator.register_agent(TraceAgent(domain))

    result = orchestrator.run_pipeline_controlled(
        ["first", "second"], {"trail": []}
    )

    assert result.succeeded is True
    pipeline = result.unwrap()
    assert pipeline.completed is True
    assert pipeline.output["trail"] == ["first", "second"]
    assert [stage.status for stage in pipeline.steps] == [
        "completed",
        "completed",
    ]
    assert orchestrator.verify_traces(orchestrator.last_trace_id) is True


def test_a_rejected_controlled_pipeline_reports_rejected():
    orchestrator = strict_orchestrator()
    orchestrator.register_agent(SilentAgent("first"))
    orchestrator.register_agent(TraceAgent("second"))
    orchestrator.register_agent(TraceAgent("third"))

    result = orchestrator.run_pipeline_controlled(
        ["first", "second", "third"], {"trail": []}, emotional_state="crisis"
    )

    assert result.status is ExecutionStatus.REJECTED
    pipeline = result.value
    assert [stage.status for stage in pipeline.steps] == [
        "completed",
        "rejected",
        "skipped",
    ]
    assert pipeline.rejection.domain == "second"
    assert orchestrator.verify_traces(orchestrator.last_trace_id) is True


def test_a_stage_timeout_stops_only_that_stage():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.register_agent(SlowAgent("second", seconds=5.0))
    third = TraceAgent("third")
    orchestrator.register_agent(third)

    result = orchestrator.run_pipeline_controlled(
        [
            PipelineStep(domain="first"),
            PipelineStep(domain="second", timeout_seconds=0.05),
            PipelineStep(domain="third"),
        ],
        {"trail": []},
    )

    pipeline = result.value
    assert [stage.status for stage in pipeline.steps] == [
        "completed",
        "timed_out",
        "skipped",
    ]
    assert third.calls == 0


def test_a_pipeline_stage_agent_is_bound_by_its_own_policy_timeout():
    """H-01 regression, inside a pipeline: the agent inside a stage must
    still respect its own per-domain timeout even when the stage itself
    was given no ``timeout_seconds`` of its own.
    """
    orchestrator = SETTOrchestrator(
        lifecycle_policy=LifecyclePolicy(agent_timeouts={"second": 0.05})
    )
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.register_agent(SlowAgent("second", seconds=5.0))
    third = TraceAgent("third")
    orchestrator.register_agent(third)

    result = orchestrator.run_pipeline_controlled(
        ["first", "second", "third"], {"trail": []}
    )

    pipeline = result.value
    assert [stage.status for stage in pipeline.steps] == [
        "completed",
        "timed_out",
        "skipped",
    ]
    assert third.calls == 0


def test_partial_work_is_reported_as_partial_not_as_a_bare_stop():
    """Two stages really did finish; the result must not throw that away."""
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.register_agent(TraceAgent("second"))
    orchestrator.register_agent(SlowAgent("third", seconds=5.0))
    orchestrator.register_agent(TraceAgent("fourth"))

    result = orchestrator.run_pipeline_controlled(
        [
            PipelineStep(domain="first"),
            PipelineStep(domain="second"),
            PipelineStep(domain="third", timeout_seconds=0.05),
            PipelineStep(domain="fourth"),
        ],
        {"trail": []},
    )

    assert result.status is ExecutionStatus.PARTIAL
    assert result.state.completed_units == 2
    assert result.state.total_units == 4
    # The finished stages' outputs are still reachable without unwrapping.
    assert result.value.steps[1].output["trail"] == ["first", "second"]
    kinds = [event.kind for event in orchestrator.get_trace(
        orchestrator.last_trace_id
    )]
    assert "pipeline.partial" in kinds
    assert orchestrator.verify_traces(orchestrator.last_trace_id) is True


def test_a_genuine_stage_failure_does_not_discard_earlier_completed_work():
    """FAILED is a different status from PARTIAL, but the same promise
    applies: work that really finished is not thrown away just because
    what stopped the pipeline was an exception instead of a cancellation
    or a deadline. Found by an external audit building this exact
    scenario against both stop paths and comparing `result.value`."""
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.register_agent(TraceAgent("second"))
    orchestrator.register_agent(ExplodingAgent("third"))

    result = orchestrator.run_pipeline_controlled(
        [
            PipelineStep(domain="first"),
            PipelineStep(domain="second"),
            PipelineStep(domain="third"),
        ],
        {"trail": []},
    )

    assert result.status is ExecutionStatus.FAILED
    assert isinstance(result.exception, ValueError)
    assert result.state.completed_units == 2
    # The two stages that really completed before the third one raised are
    # still reachable, exactly as they are on the PARTIAL/cancelled path.
    assert result.value is not None
    assert [outcome.status for outcome in result.value.steps] == [
        "completed",
        "completed",
    ]
    assert result.value.steps[1].output["trail"] == ["first", "second"]


def test_a_stage_failure_on_the_very_first_stage_carries_no_partial_value():
    """No stage ever completed, so there is nothing to preserve - `value`
    stays `None` rather than an empty, misleading PipelineResult."""
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(ExplodingAgent("first"))

    result = orchestrator.run_pipeline_controlled(
        [PipelineStep(domain="first")], {"trail": []}
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.value is None


def test_a_pipeline_stopped_before_anything_finished_is_not_partial():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(SlowAgent("first", seconds=5.0))
    orchestrator.register_agent(TraceAgent("second"))

    result = orchestrator.run_pipeline_controlled(
        [PipelineStep(domain="first", timeout_seconds=0.05), "second"],
        {"trail": []},
    )

    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.state.completed_units == 0


def test_stages_after_a_stopped_one_are_skipped_never_cancelled():
    """A cancellation tree is not a dependency graph, at the pipeline level."""
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(SlowAgent("first", seconds=5.0))
    orchestrator.register_agent(TraceAgent("second"))
    orchestrator.register_agent(TraceAgent("third"))

    result = orchestrator.run_pipeline_controlled(
        [
            PipelineStep(domain="first", timeout_seconds=0.05),
            "second",
            "third",
        ],
        {"trail": []},
    )

    stopped, *unreached = result.value.steps
    assert stopped.execution_status is ExecutionStatus.TIMED_OUT
    for stage in unreached:
        assert stage.status == "skipped"
        # Never ran, so it has no execution outcome at all.
        assert stage.execution_status is None
        assert stage.execution_status is not ExecutionStatus.CANCELLED
        assert isinstance(stage.skip_cause, SkipCause)
        assert stage.skip_cause.domain == "first"
        assert stage.skip_cause.reason_code == "execution.deadline_exceeded"


def test_a_stage_timeout_never_extends_the_run_deadline():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(SlowAgent("first", seconds=5.0))

    started = time.monotonic()
    result = orchestrator.run_pipeline_controlled(
        [PipelineStep(domain="first", timeout_seconds=600.0)],
        {"trail": []},
        timeout_seconds=0.05,
    )

    assert result.timed_out is True
    assert time.monotonic() - started < 2.0


def test_an_empty_controlled_pipeline_is_a_configuration_error():
    orchestrator = SETTOrchestrator()
    result = orchestrator.run_pipeline_controlled([], {})

    assert result.status is ExecutionStatus.FAILED
    with pytest.raises(SETTConfigurationError):
        result.unwrap()


def test_an_unknown_domain_fails_before_any_stage_runs():
    orchestrator = SETTOrchestrator()
    first = TraceAgent("first")
    orchestrator.register_agent(first)

    result = orchestrator.run_pipeline_controlled(["first", "nope"], {})

    assert result.status is ExecutionStatus.FAILED
    assert first.calls == 0
    with pytest.raises(SETTAgentNotFoundError):
        result.unwrap()


def test_a_controlled_pipeline_releases_its_controls():
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TraceAgent("first"))
    orchestrator.run_pipeline_controlled(["first"], {"trail": []})

    assert orchestrator.control_registry.live_run_ids == ()


# ── Experts observe cancellation without any subclass knowing ──────────────


def test_an_existing_expert_gets_checkpoints_without_being_modified():
    resolved: list[str] = []

    class PlainExpert(SETTExpert):
        """Written exactly as it would have been for v0.11.0."""

        def resolve(self, context):
            resolved.append(self.name)
            return {"seen": True}

    class TwoExpertAgent(SETTAgent):
        def __init__(self, registry_holder):
            super().__init__(name="two_agent", domain="two")
            self.register_expert(PlainExpert(name="first_expert"))
            self.register_expert(PlainExpert(name="second_expert"))
            self._holder = registry_holder

        def process(self, input_data):
            self.get_expert("first_expert").resolve(input_data)
            # Cancelled between the two experts: the second must not run.
            self._holder["orchestrator"].cancel(self._holder["run_id"])
            self.get_expert("second_expert").resolve(input_data)
            return {"ok": True}

    holder: dict = {}
    orchestrator = SETTOrchestrator()
    orchestrator.register_agent(TwoExpertAgent(holder))
    context = ExecutionContext.create()
    holder["orchestrator"] = orchestrator
    holder["run_id"] = context.run_id

    result = orchestrator.process_controlled(
        {}, domain="two", execution_context=context
    )

    assert result.cancelled is True
    assert resolved == ["first_expert"]
    kinds = [
        event.kind for event in orchestrator.get_trace(context.trace_id)
    ]
    assert "expert.cancelled" in kinds
    assert orchestrator.verify_traces(context.trace_id) is True


# ── The barge-in: cancel one turn, keep the session ─────────────────────────


def test_a_turn_can_be_cancelled_from_another_thread_by_its_run_id():
    orchestrator = SETTOrchestrator()
    agent = SlowAgent("voice", seconds=5.0)
    orchestrator.register_agent(agent)

    turn = ExecutionContext.create()
    canceller = threading.Thread(
        target=lambda: (
            agent.started.wait(timeout=5),
            orchestrator.cancel(turn.run_id, reason="voice.barge_in"),
        )
    )
    canceller.start()

    started_at = time.monotonic()
    result = orchestrator.process_controlled(
        {"trail": []}, domain="voice", execution_context=turn
    )
    canceller.join(timeout=5)

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.reason_code == "voice.barge_in"
    assert agent.finished is False
    assert time.monotonic() - started_at < 2.0
    with pytest.raises(SETTCancelledError) as excinfo:
        result.unwrap()
    assert excinfo.value.reason == "voice.barge_in"


def test_cancelling_one_turn_leaves_the_next_turn_of_the_session_working():
    """The whole point: a barge-in must not take the session down with it."""
    orchestrator = SETTOrchestrator()
    slow = SlowAgent("voice", seconds=5.0)
    orchestrator.register_agent(slow)
    orchestrator.register_agent(TraceAgent("reply"))

    session = ExecutionContext.create()
    first_turn = session.derive()

    canceller = threading.Thread(
        target=lambda: (
            slow.started.wait(timeout=5),
            orchestrator.cancel(first_turn.run_id, reason="voice.barge_in"),
        )
    )
    canceller.start()
    cancelled_turn = orchestrator.process_controlled(
        {"trail": []}, domain="voice", execution_context=first_turn
    )
    canceller.join(timeout=5)

    # A second turn in the same session, started after the barge-in.
    second_turn = session.derive()
    next_turn = orchestrator.process_controlled(
        {"trail": []}, domain="reply", execution_context=second_turn
    )

    assert cancelled_turn.cancelled is True
    assert next_turn.succeeded is True
    assert next_turn.unwrap() == {"trail": ["reply"]}
    assert first_turn.trace_id == second_turn.trace_id == session.trace_id
    assert orchestrator.control_registry.live_run_ids == ()


def test_cancelling_a_run_id_that_is_not_running_reports_false():
    orchestrator = SETTOrchestrator()
    assert orchestrator.cancel("never-existed") is False


def test_the_cancellable_run_id_is_the_one_the_trace_exports():
    """The identifier in an exported event is the handle, not a parallel id."""
    orchestrator = SETTOrchestrator()
    agent = SlowAgent("voice", seconds=5.0)
    orchestrator.register_agent(agent)
    turn = ExecutionContext.create()

    def cancel_using_only_the_exported_trace() -> None:
        agent.started.wait(timeout=5)
        exported = orchestrator.export_trace(turn.trace_id, view="summary")
        root_events = [
            event for event in exported if event["kind"] == "trace.started"
        ]
        orchestrator.cancel(root_events[0]["run_id"], reason="voice.barge_in")

    canceller = threading.Thread(target=cancel_using_only_the_exported_trace)
    canceller.start()
    result = orchestrator.process_controlled(
        {"trail": []}, domain="voice", execution_context=turn
    )
    canceller.join(timeout=5)

    assert result.cancelled is True
    assert result.run_id == turn.run_id


def test_a_cancelled_run_still_verifies_and_carries_its_own_terminal():
    orchestrator = SETTOrchestrator()
    agent = SlowAgent("voice", seconds=5.0)
    orchestrator.register_agent(agent)
    turn = ExecutionContext.create()

    canceller = threading.Thread(
        target=lambda: (
            agent.started.wait(timeout=5),
            orchestrator.cancel(turn.run_id),
        )
    )
    canceller.start()
    orchestrator.process_controlled(
        {"trail": []}, domain="voice", execution_context=turn
    )
    canceller.join(timeout=5)

    kinds = [event.kind for event in orchestrator.get_trace(turn.trace_id)]
    assert "trace.cancelled" in kinds
    assert "agent.cancelled" in kinds
    assert "route.cancelled" in kinds
    assert "execution.cancel_requested" in kinds
    assert "trace.failed" not in kinds
    assert orchestrator.verify_traces(turn.trace_id) is True
