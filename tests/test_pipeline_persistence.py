"""Durable pipeline outputs and explicit audit closure behavior."""
from __future__ import annotations

import pytest

from sett.audit_ruler.closure import AuditClosureStatus
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.agent import SETTAgent
from sett.core_ruler.execution_result import ExecutionStatus
from sett.core_ruler.orchestrator import SETTOrchestrator
from sett.exceptions import (
    SETTAuditClosureError,
    SETTConfigurationError,
    SETTPersistenceError,
    SETTValidationError,
)
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryEvidenceStream,
    InMemoryStateRecordStore,
    PersistenceBackend,
    PersistencePolicy,
    StageStateDescriptor,
    decode_trace_evidence_frame,
    materialize_stage_output,
)


class _PipelineAgent(SETTAgent):
    def __init__(self, domain: str = "demo") -> None:
        super().__init__(name=f"{domain}_agent", domain=domain)
        self.call_count = 0

    def process(self, input_data):
        self.call_count += 1
        return {"value": input_data.get("value", 0) + 1}


class _SnapshotRefusedStore(InMemoryStateRecordStore):
    def put(self, *, write_id: str, payload: bytes):
        raise SETTPersistenceError(
            "The test store refused the requested durability.",
            reason_code="state.snapshot_durability_unsatisfied",
            backend_id="backend.demo",
        )


class _LostTerminalConfirmationStream(InMemoryEvidenceStream):
    def __init__(
        self,
        *,
        backend_id: str,
        stream_id: str,
        target_kind: str = "pipeline.stage_completed",
    ) -> None:
        super().__init__(backend_id=backend_id, stream_id=stream_id)
        self._lose_next_flush = False
        self._target_kind = target_kind

    def append(self, *, write_id: str, payload: bytes, expected_head):
        receipt = super().append(
            write_id=write_id,
            payload=payload,
            expected_head=expected_head,
        )
        frame = decode_trace_evidence_frame(payload)
        if frame.payload["kind"] == self._target_kind:
            self._lose_next_flush = True
        return receipt

    def flush(self, *, through):
        if self._lose_next_flush:
            self._lose_next_flush = False
            raise SETTPersistenceError(
                "The terminal confirmation was lost.",
                reason_code="audit.evidence_write_failed",
                backend_id="backend.demo",
            )
        return super().flush(through=through)


def _policy(
    gap: AuditGapPolicy = AuditGapPolicy.RETURN_KNOWN_OUTCOME,
) -> PersistencePolicy:
    return PersistencePolicy(
        snapshot_durability=DurabilityLevel.IN_MEMORY,
        evidence_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(),
        audit_gap_policy=gap,
    )


def _backend(*, evidence=None, state=None) -> PersistenceBackend:
    return PersistenceBackend(
        namespace_id="demo.namespace",
        evidence=evidence
        or InMemoryEvidenceStream(
            backend_id="backend.demo",
            stream_id="evidence.demo",
        ),
        state=state or InMemoryStateRecordStore(backend_id="backend.demo"),
    )


def _orchestrator(
    *,
    backend: PersistenceBackend,
    policy: PersistencePolicy | None = None,
) -> tuple[SETTOrchestrator, _PipelineAgent]:
    orchestrator = SETTOrchestrator(
        persistence_backend=backend,
        persistence_policy=policy or _policy(),
    )
    agent = _PipelineAgent()
    orchestrator.register_agent(agent)
    return orchestrator, agent


def _descriptor_from_event(event) -> StageStateDescriptor:
    attributes = event.attributes
    return StageStateDescriptor(
        stage_index=attributes["index"],
        run_id=event.run_id,
        state_ref=attributes["state_ref"],
        state_digest=attributes["state_digest"],
        digest_algorithm=attributes["digest_algorithm"],
        digest_policy_id=attributes["digest_policy_id"],
        schema_id=attributes["schema_id"],
        schema_version=attributes["schema_version"],
        codec=attributes["codec"],
        content_type=attributes["content_type"],
        backend_id=attributes["state_backend_id"],
        achieved_durability=DurabilityLevel(
            attributes["snapshot_durability"]
        ),
        write_id_version=attributes["write_id_version"],
    )


def test_persistence_configuration_is_paired_and_checked_before_use() -> None:
    backend = _backend()
    with pytest.raises(SETTValidationError, match="together"):
        SETTOrchestrator(persistence_backend=backend)
    with pytest.raises(SETTValidationError, match="together"):
        SETTOrchestrator(persistence_policy=_policy())


def test_plain_pipeline_fails_before_opening_evidence_when_persistent() -> None:
    backend = _backend()
    orchestrator, agent = _orchestrator(backend=backend)

    with pytest.raises(SETTConfigurationError, match="controlled"):
        orchestrator.run_pipeline(["demo"], {"value": 1})

    assert agent.call_count == 0
    assert backend.evidence.read() == ()


def test_non_pipeline_entry_points_fail_before_opening_evidence() -> None:
    backend = _backend()
    orchestrator, agent = _orchestrator(backend=backend)

    with pytest.raises(SETTConfigurationError, match="pipeline"):
        orchestrator.process({"value": 1}, domain="demo")
    with pytest.raises(SETTConfigurationError, match="pipeline"):
        orchestrator.process_controlled({"value": 1}, domain="demo")
    with pytest.raises(SETTConfigurationError, match="pipeline"):
        orchestrator.process_traced({"value": 1}, domain="demo")

    assert agent.call_count == 0
    assert backend.evidence.read() == ()


def test_controlled_pipeline_persists_output_before_completion_evidence() -> None:
    backend = _backend()
    policy = _policy()
    orchestrator, agent = _orchestrator(backend=backend, policy=policy)

    result = orchestrator.run_pipeline_controlled(["demo"], {"value": 1})

    assert agent.call_count == 1
    assert result.status is ExecutionStatus.SUCCESS
    assert result.audit_closure.status is AuditClosureStatus.SATISFIED
    events = orchestrator.get_trace(result.trace_id)
    pipeline_started = next(
        event for event in events if event.kind == "pipeline.started"
    )
    assert pipeline_started.attributes["write_id_version"] == 1
    completed = next(
        event for event in events if event.kind == "pipeline.stage_completed"
    )
    assert "value" not in completed.attributes
    descriptor = _descriptor_from_event(completed)
    report = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    recovered_stage = report.traces[0].stages[0]
    assert recovered_stage.descriptor == descriptor
    assert (
        recovered_stage.descriptor_reason
        == "recovery.stage_descriptor_recorded"
    )
    materialized = materialize_stage_output(
        recovered_stage.descriptor,
        store=backend.state,
        namespace_id=backend.namespace_id,
        trace_id=result.trace_id,
        policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert materialized.value == {"value": 2}

    kinds = [
        decode_trace_evidence_frame(entry.payload).payload["kind"]
        for entry in backend.evidence.read()
    ]
    assert kinds.index("pipeline.stage_completed") < kinds.index(
        "pipeline.completed"
    )


def test_snapshot_refusal_returns_known_success_with_unsatisfied_closure() -> None:
    backend = _backend(state=_SnapshotRefusedStore(backend_id="backend.demo"))
    orchestrator, agent = _orchestrator(backend=backend)

    result = orchestrator.run_pipeline_controlled(["demo"], {"value": 1})

    assert agent.call_count == 1
    assert result.status is ExecutionStatus.SUCCESS
    assert result.value.output == {"value": 2}
    assert result.audit_closure.status is AuditClosureStatus.UNSATISFIED
    assert result.audit_closure.reason_code == "audit.terminal_not_durable"
    kinds = [event.kind for event in orchestrator.get_trace(result.trace_id)]
    assert "pipeline.stage_failed" not in kinds
    assert "pipeline.stage_completed" not in kinds


def test_lost_terminal_confirmation_is_unconfirmed_not_failed() -> None:
    stream = _LostTerminalConfirmationStream(
        backend_id="backend.demo",
        stream_id="evidence.demo",
    )
    backend = _backend(evidence=stream)
    orchestrator, agent = _orchestrator(backend=backend)

    result = orchestrator.run_pipeline_controlled(["demo"], {"value": 1})

    assert agent.call_count == 1
    assert result.status is ExecutionStatus.SUCCESS
    assert result.audit_closure.status is AuditClosureStatus.UNCONFIRMED
    assert (
        result.audit_closure.reason_code
        == "audit.terminal_durability_unconfirmed"
    )
    kinds = [event.kind for event in orchestrator.get_trace(result.trace_id)]
    assert "pipeline.stage_failed" not in kinds


def test_nested_agent_terminal_gap_preserves_the_known_stage_result() -> None:
    stream = _LostTerminalConfirmationStream(
        backend_id="backend.demo",
        stream_id="evidence.demo",
        target_kind="agent.completed",
    )
    backend = _backend(evidence=stream)
    orchestrator, agent = _orchestrator(backend=backend)

    result = orchestrator.run_pipeline_controlled(["demo"], {"value": 1})

    assert agent.call_count == 1
    assert result.status is ExecutionStatus.SUCCESS
    assert result.value.output == {"value": 2}
    assert result.audit_closure.status is AuditClosureStatus.UNCONFIRMED
    kinds = [event.kind for event in orchestrator.get_trace(result.trace_id)]
    assert "pipeline.stage_failed" not in kinds
    assert "agent.failed" not in kinds


def test_escalation_carries_known_outcome_and_is_not_retry_safe() -> None:
    backend = _backend(state=_SnapshotRefusedStore(backend_id="backend.demo"))
    orchestrator, agent = _orchestrator(
        backend=backend,
        policy=_policy(AuditGapPolicy.ESCALATE_KNOWN_OUTCOME),
    )

    with pytest.raises(SETTAuditClosureError) as captured:
        orchestrator.run_pipeline_controlled(["demo"], {"value": 1})

    error = captured.value
    assert agent.call_count == 1
    assert error.retry_safe is False
    assert error.execution_result.status is ExecutionStatus.SUCCESS
    assert error.execution_result.value.output == {"value": 2}
    assert error.audit_closure.status is AuditClosureStatus.UNSATISFIED
