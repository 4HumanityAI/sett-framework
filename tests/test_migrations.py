"""Copy-on-write stage migration dry-runs, execution, and lineage guards."""
from __future__ import annotations

import json

import pytest

from sett.audit_ruler.integrity import NoDigestPolicy, Sha256IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.agent import SETTAgent
from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.orchestrator import SETTOrchestrator
from sett.exceptions import SETTPersistenceError
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryEvidenceStream,
    InMemoryStateRecordStore,
    IntegrityStatus,
    MigrationPlan,
    MigrationRemedyOwner,
    PersistenceBackend,
    PersistencePolicy,
    decode_state_envelope,
    decode_trace_evidence_frame,
    derive_migration_write_id,
    dry_run_stage_migration,
    execute_stage_migration,
)


class _Agent(SETTAgent):
    def __init__(self) -> None:
        super().__init__(name="agent", domain="demo")

    def process(self, input_data):
        return {"value": input_data["value"]}


def _policy(integrity=None) -> PersistencePolicy:
    return PersistencePolicy(
        snapshot_durability=DurabilityLevel.IN_MEMORY,
        evidence_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=integrity or Sha256IntegrityPolicy(),
        audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )


def _source(integrity=None, *, evidence=None):
    stream = evidence or InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="migration.demo"
    )
    backend = PersistenceBackend(
        namespace_id="demo.namespace",
        evidence=stream,
        state=InMemoryStateRecordStore(backend_id="backend.demo"),
    )
    policy = _policy(integrity)
    orchestrator = SETTOrchestrator(
        persistence_backend=backend,
        persistence_policy=policy,
    )
    orchestrator.register_agent(_Agent())
    orchestrator.run_pipeline_controlled(["demo"], input_data={"value": 1})
    report = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    return backend, policy, report


def _transform(payload: bytes) -> bytes:
    value = json.loads(payload.decode("utf-8"))
    value["migrated"] = True
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _plan(
    *,
    accepted=frozenset({IntegrityStatus.VERIFIED}),
    source_version: int = 1,
    target_version: int = 2,
) -> MigrationPlan:
    return MigrationPlan(
        plan_id=f"demo.stage.v{source_version}-to-v{target_version}",
        plan_version=1,
        source_schema_id="sett.pipeline-stage-output",
        source_schema_version=source_version,
        source_codec=f"sett.json.v{source_version}",
        target_schema_id="sett.pipeline-stage-output",
        target_schema_version=target_version,
        target_codec=f"sett.json.v{target_version}",
        target_content_type="application/json",
        transform=_transform,
        accepted_integrity_statuses=accepted,
    )


def test_migration_write_identity_covers_the_complete_target_route() -> None:
    arguments = {
        "namespace_id": "demo.namespace",
        "source_state_ref": "state-1",
        "target_envelope_version": 1,
        "target_schema_id": "demo.output",
        "target_schema_version": 2,
        "target_codec": "demo.json.v2",
        "target_content_type": "application/json",
    }
    first = derive_migration_write_id(**arguments)
    assert derive_migration_write_id(**arguments) == first
    assert derive_migration_write_id(
        **{**arguments, "target_schema_version": 3}
    ) != first
    assert derive_migration_write_id(
        **{**arguments, "target_content_type": "application/cbor"}
    ) != first


def test_dry_run_is_read_only_and_manifest_order_follows_evidence() -> None:
    backend, policy, report = _source()
    before = len(backend.state._records)

    dry_run = dry_run_stage_migration(
        _plan(),
        report,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )

    assert dry_run.ready
    assert len(dry_run.assessments) == 1
    assert len(dry_run.manifest.entries) == 1
    assert dry_run.manifest.entries[0].authoritative_event_id == (
        dry_run.assessments[0].stage.completion_event_id
    )
    assert dry_run.counts_by_integrity_status == {IntegrityStatus.VERIFIED: 1}
    assert dry_run.counts_by_remedy_owner == {MigrationRemedyOwner.NONE: 1}
    assert len(backend.state._records) == before


def test_execution_is_copy_on_write_and_records_a_durable_linear_link() -> None:
    backend, policy, report = _source()
    dry_run = dry_run_stage_migration(
        _plan(),
        report,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    source_ref = dry_run.manifest.entries[0].source_state_ref
    source_bytes = backend.state.get(state_ref=source_ref).payload

    result = execute_stage_migration(
        dry_run,
        backend=backend,
        policy=policy,
        context=ExecutionContext.create(),
    )

    assert len(result.records) == 1
    destination_ref = result.records[0].destination_state_ref
    assert destination_ref != source_ref
    assert backend.state.get(state_ref=source_ref).payload == source_bytes
    destination = decode_state_envelope(
        backend.state.get(state_ref=destination_ref).payload
    )
    assert destination.schema_version == 2
    assert destination.codec == "sett.json.v2"
    assert json.loads(destination.payload_bytes) == {
        "migrated": True,
        "value": 1,
    }
    refreshed = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    migration_events = [
        event for event in refreshed.events if event.kind.startswith("persistence.")
    ]
    assert [event.kind for event in migration_events] == [
        "persistence.migration_started",
        "persistence.state_migrated",
        "persistence.migration_completed",
    ]
    assert refreshed.chain_integrity
    assert not refreshed.requires_reconciliation
    recovered_stage = next(
        stage
        for trace in refreshed.traces
        for stage in trace.stages
        if stage.completion_event_id
        == dry_run.manifest.entries[0].authoritative_event_id
    )
    assert recovered_stage.descriptor is not None
    assert recovered_stage.descriptor.schema_version == 2
    assert recovered_stage.authoritative_event_id == (
        result.records[0].evidence_event_id
    )
    assert recovered_stage.migration_event_ids == (
        result.records[0].evidence_event_id,
    )

    repeated = dry_run_stage_migration(
        _plan(),
        refreshed,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert repeated.manifest.entries == ()
    with pytest.raises(SETTPersistenceError) as stale:
        execute_stage_migration(
            dry_run,
            backend=backend,
            policy=policy,
            context=ExecutionContext.create(),
        )
    assert stale.value.reason_code == "migration.manifest_stale"


def test_explicit_no_digest_requires_explicit_plan_acceptance() -> None:
    backend, policy, report = _source(NoDigestPolicy())

    blocked = dry_run_stage_migration(
        _plan(),
        report,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert not blocked.ready
    assert blocked.assessments[0].integrity_status is (
        IntegrityStatus.EXPLICITLY_DISABLED
    )
    assert blocked.assessments[0].reason_code == (
        "migration.integrity_status_not_accepted"
    )
    assert blocked.counts_by_remedy_owner == {
        MigrationRemedyOwner.PLAN_CONFIGURATION: 1
    }
    with pytest.raises(Exception, match="blocked sources"):
        execute_stage_migration(
            blocked,
            backend=backend,
            policy=policy,
            context=ExecutionContext.create(),
        )

    accepted = dry_run_stage_migration(
        _plan(accepted=frozenset({IntegrityStatus.EXPLICITLY_DISABLED})),
        report,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert accepted.ready


def test_transitive_trust_uses_the_weakest_lineage_link() -> None:
    backend, no_digest_policy, report = _source(NoDigestPolicy())
    strong_policy = _policy(Sha256IntegrityPolicy())
    first = dry_run_stage_migration(
        _plan(accepted=frozenset({IntegrityStatus.EXPLICITLY_DISABLED})),
        report,
        backend=backend,
        policy=no_digest_policy,
        historical_policies={
            no_digest_policy.integrity_policy.policy_id: (
                no_digest_policy.integrity_policy
            )
        },
    )
    execute_stage_migration(
        first,
        backend=backend,
        policy=strong_policy,
        context=ExecutionContext.create(),
    )
    after_first = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=strong_policy.evidence_durability,
    )
    second_plan = _plan(source_version=2, target_version=3)

    blocked = dry_run_stage_migration(
        second_plan,
        after_first,
        backend=backend,
        policy=strong_policy,
        historical_policies={
            no_digest_policy.integrity_policy.policy_id: (
                no_digest_policy.integrity_policy
            ),
            strong_policy.integrity_policy.policy_id: strong_policy.integrity_policy,
        },
    )

    assert len(blocked.assessments) == 1
    assert blocked.assessments[0].integrity_status is (
        IntegrityStatus.EXPLICITLY_DISABLED
    )
    assert not blocked.ready
    accepted = dry_run_stage_migration(
        _plan(
            accepted=frozenset({IntegrityStatus.EXPLICITLY_DISABLED}),
            source_version=2,
            target_version=3,
        ),
        after_first,
        backend=backend,
        policy=strong_policy,
        historical_policies={
            no_digest_policy.integrity_policy.policy_id: (
                no_digest_policy.integrity_policy
            ),
            strong_policy.integrity_policy.policy_id: strong_policy.integrity_policy,
        },
    )
    assert accepted.ready


def test_two_destinations_from_one_historical_head_are_a_lineage_conflict() -> None:
    backend, policy, report = _source()
    dry_run = dry_run_stage_migration(
        _plan(),
        report,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    migration_context = ExecutionContext.create()
    execute_stage_migration(
        dry_run,
        backend=backend,
        policy=policy,
        context=migration_context,
    )
    current = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    first_link = next(
        event
        for event in current.events
        if event.kind == "persistence.state_migrated"
    )
    first_destination = backend.state.get(
        state_ref=first_link.attributes["destination_state_ref"]
    )
    branch_receipt = backend.state.put(
        write_id="test.branch.destination",
        payload=first_destination.payload,
    )
    branch_attributes = dict(first_link.attributes)
    branch_attributes["destination_state_ref"] = branch_receipt.state_ref
    recorder = TraceRecorder.resume(
        current,
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    recorder.record(
        migration_context,
        kind="persistence.state_migrated",
        component_type="persistence",
        component_name="demo.branch",
        status="migrated",
        cause_id=first_link.cause_id,
        reason_codes=("migration.state_migrated",),
        attributes=branch_attributes,
    )
    branched = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    assert "audit.migration_lineage_conflict" in branched.diagnostics
    assert any(
        stage.lineage_conflict
        for trace in branched.traces
        for stage in trace.stages
    )
    assert branched.requires_reconciliation

    with pytest.raises(SETTPersistenceError) as conflict:
        dry_run_stage_migration(
            _plan(source_version=2, target_version=3),
            branched,
            backend=backend,
            policy=policy,
            historical_policies={
                policy.integrity_policy.policy_id: policy.integrity_policy
            },
        )

    assert conflict.value.reason_code == "migration.lineage_conflict"


class _LoseFirstMigrationLink(InMemoryEvidenceStream):
    def __init__(self) -> None:
        super().__init__(
            backend_id="backend.demo",
            stream_id="migration.demo",
        )
        self._lose_link = True

    def append(self, *, write_id: str, payload: bytes, expected_head):
        frame = decode_trace_evidence_frame(payload)
        if (
            self._lose_link
            and frame.payload["kind"] == "persistence.state_migrated"
        ):
            self._lose_link = False
            raise SETTPersistenceError(
                "The migration link was lost.",
                reason_code="audit.evidence_write_failed",
                backend_id="backend.demo",
            )
        return super().append(
            write_id=write_id,
            payload=payload,
            expected_head=expected_head,
        )


def test_lost_destination_evidence_reports_orphan_and_replays_write() -> None:
    backend, policy, report = _source(evidence=_LoseFirstMigrationLink())
    dry_run = dry_run_stage_migration(
        _plan(),
        report,
        backend=backend,
        policy=policy,
        historical_policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    before = len(backend.state._records)

    with pytest.raises(SETTPersistenceError):
        execute_stage_migration(
            dry_run,
            backend=backend,
            policy=policy,
            context=ExecutionContext.create(),
        )

    assert len(backend.state._records) == before + 1
    failed_report = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    assert not any(
        event.kind == "persistence.migration_failed"
        for event in failed_report.events
    )
    assert any(
        boundary.start_kind == "persistence.migration_started"
        for trace in failed_report.traces
        for boundary in trace.unresolved_boundaries
    )

    result = execute_stage_migration(
        dry_run,
        backend=backend,
        policy=policy,
        context=ExecutionContext.create(),
    )

    assert len(result.records) == 1
    assert len(backend.state._records) == before + 1
