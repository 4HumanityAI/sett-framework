"""Opt-in durable PrivateMemory mutation ordering and failure behavior."""
from __future__ import annotations

import json

import pytest

from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.agent import SETTAgent
from sett.core_ruler.execution_context import ExecutionContext, execution_scope
from sett.core_ruler.execution_result import ExecutionStatus
from sett.core_ruler.orchestrator import SETTOrchestrator
from sett.exceptions import SETTConfigurationError, SETTPersistenceError
from sett.memory_ruler.private import PrivateMemory
from sett.persistence import (
    PRIVATE_MEMORY_CODEC,
    PRIVATE_MEMORY_SCHEMA_ID,
    PRIVATE_MEMORY_SCHEMA_VERSION,
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryEvidenceStream,
    InMemoryStateRecordStore,
    IntegrityStatus,
    MigrationPlan,
    PersistenceBackend,
    PersistencePolicy,
    decode_private_memory_mutation,
    decode_trace_evidence_frame,
    dry_run_private_memory_migration,
    execute_private_memory_migration,
)


def _policy() -> PersistencePolicy:
    return PersistencePolicy(
        snapshot_durability=DurabilityLevel.IN_MEMORY,
        evidence_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(),
        audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )


def _configured_memory(
    *, evidence: InMemoryEvidenceStream | None = None
) -> tuple[
    PrivateMemory,
    PersistenceBackend,
    TraceRecorder,
]:
    stream = evidence or InMemoryEvidenceStream(
        backend_id="backend.demo",
        stream_id="private-memory",
    )
    backend = PersistenceBackend(
        namespace_id="demo.namespace",
        evidence=stream,
        state=InMemoryStateRecordStore(backend_id="backend.demo"),
    )
    policy = _policy()
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    memory = PrivateMemory(owner="agent", memory_id="memory.demo")
    memory.configure_persistence(
        backend=backend,
        policy=policy,
        trace_recorder=recorder,
    )
    return memory, backend, recorder


def test_persistent_mutation_requires_real_context_before_state_change() -> None:
    memory, backend, recorder = _configured_memory()

    with pytest.raises(SETTConfigurationError, match="ExecutionContext"):
        memory.write("answer", 42)

    assert memory.get_all() == {}
    assert memory.get_history() == []
    assert recorder.event_count == 0
    assert backend.state.lookup(write_id="absent") is None


def test_configuration_requires_memory_id_and_precedes_first_mutation() -> None:
    _memory, backend, recorder = _configured_memory()
    policy = _policy()
    missing_id = PrivateMemory(owner="agent")
    with pytest.raises(SETTConfigurationError, match="memory_id"):
        missing_id.configure_persistence(
            backend=backend,
            policy=policy,
            trace_recorder=recorder,
        )
    already_used = PrivateMemory(owner="agent", memory_id="memory.used")
    already_used.write("value", 1)
    with pytest.raises(SETTConfigurationError, match="first mutation"):
        already_used.configure_persistence(
            backend=backend,
            policy=policy,
            trace_recorder=recorder,
        )


def test_write_is_durable_before_memory_and_confirmation_contains_no_value() -> None:
    memory, backend, recorder = _configured_memory()
    context = ExecutionContext.create()
    secret_value = {"medical_value": 137, "label": "private"}

    memory.write("profile", secret_value, context=context)

    assert memory.read("profile") == secret_value
    events = recorder.get_trace(context.trace_id)
    assert [event.kind for event in events] == [
        "private_memory.mutation_started",
        "private_memory.mutation_persisted",
    ]
    started, persisted = events
    assert persisted.cause_id == started.event_id
    assert persisted.attributes["memory_id"] == "memory.demo"
    assert "profile" not in repr(persisted.safe_view())
    assert "medical_value" not in repr(persisted.safe_view())
    record = backend.state.get(state_ref=persisted.attributes["state_ref"])
    envelope = decode_private_memory_mutation(record.payload)
    assert envelope.key == "profile"
    assert envelope.payload_bytes is not None
    assert b"medical_value" in envelope.payload_bytes


def test_clear_uses_same_durable_mutation_protocol() -> None:
    memory, backend, recorder = _configured_memory()
    context = ExecutionContext.create()
    memory.write("answer", 42, context=context)

    memory.clear(context=context)

    assert memory.get_all() == {}
    persisted = [
        event
        for event in recorder.get_trace(context.trace_id)
        if event.kind == "private_memory.mutation_persisted"
    ]
    clear_record = backend.state.get(
        state_ref=persisted[-1].attributes["state_ref"]
    )
    envelope = decode_private_memory_mutation(clear_record.payload)
    assert envelope.key is None
    assert envelope.payload_bytes is None


def test_private_memory_migration_is_copy_on_write_and_restorable() -> None:
    memory, backend, _recorder = _configured_memory()
    context = ExecutionContext.create()
    memory.write("first", {"value": 1}, context=context)
    memory.clear(context=context)
    memory.write("final", {"value": 3}, context=context)
    report = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    source_refs = {
        event.attributes["state_ref"]
        for event in report.events
        if event.kind == "private_memory.mutation_persisted"
    }
    source_bytes = {
        state_ref: backend.state.get(state_ref=state_ref).payload
        for state_ref in source_refs
    }
    transform_calls = 0

    def transform(payload: bytes) -> bytes:
        nonlocal transform_calls
        transform_calls += 1
        return json.dumps(
            json.loads(payload), sort_keys=True, separators=(",", ":")
        ).encode()

    plan = MigrationPlan(
        plan_id="private-memory.v1-to-v2",
        plan_version=1,
        source_schema_id=PRIVATE_MEMORY_SCHEMA_ID,
        source_schema_version=PRIVATE_MEMORY_SCHEMA_VERSION,
        source_codec=PRIVATE_MEMORY_CODEC,
        target_schema_id=PRIVATE_MEMORY_SCHEMA_ID,
        target_schema_version=2,
        target_codec="sett.json.v2",
        target_content_type="application/json",
        transform=transform,
        accepted_integrity_statuses=frozenset({IntegrityStatus.VERIFIED}),
    )
    policy = _policy()
    before_orphan = len(backend.state._records)
    backend.state.put(write_id="unreferenced.orphan", payload=b"orphan")
    dry_run = dry_run_private_memory_migration(
        plan,
        report,
        memory_id="memory.demo",
        backend=backend,
        policy=policy,
        historical_policies={
            policy.integrity_policy.policy_id: policy.integrity_policy
        },
    )

    assert dry_run.ready
    assert len(dry_run.assessments) == 3
    assert len(dry_run.manifest.entries) == 3
    assert transform_calls == 2
    assert len(backend.state._records) == before_orphan + 1

    result = execute_private_memory_migration(
        dry_run,
        backend=backend,
        policy=policy,
        context=ExecutionContext.create(),
    )

    assert len(result.records) == 3
    assert transform_calls == 4
    assert all(
        backend.state.get(state_ref=state_ref).payload == payload
        for state_ref, payload in source_bytes.items()
    )
    refreshed = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    links = [
        event
        for event in refreshed.events
        if event.kind == "persistence.state_migrated"
        and event.attributes.get("artifact_kind") == "private_memory"
    ]
    assert len(links) == 3
    assert all(
        event.attributes["destination_schema_version"] == 2 for event in links
    )
    restored = PrivateMemory.restore(
        owner="agent",
        memory_id="memory.demo",
        report=refreshed,
        backend=backend,
        policy=policy,
    )
    assert restored.get_all() == {"final": {"value": 3}}
    assert [item["action"] for item in restored.get_history()] == [
        "write",
        "clear",
        "write",
    ]

    next_dry_run = dry_run_private_memory_migration(
        MigrationPlan(
            plan_id="private-memory.v2-to-v3",
            plan_version=1,
            source_schema_id=PRIVATE_MEMORY_SCHEMA_ID,
            source_schema_version=2,
            source_codec="sett.json.v2",
            target_schema_id=PRIVATE_MEMORY_SCHEMA_ID,
            target_schema_version=3,
            target_codec="sett.json.v3",
            target_content_type="application/json",
            transform=lambda payload: payload,
            accepted_integrity_statuses=frozenset({IntegrityStatus.VERIFIED}),
        ),
        refreshed,
        memory_id="memory.demo",
        backend=backend,
        policy=policy,
        historical_policies={
            policy.integrity_policy.policy_id: policy.integrity_policy
        },
    )
    assert next_dry_run.ready
    assert len(next_dry_run.assessments) == 3


def test_ambient_and_explicit_context_conflict_fails_before_intent() -> None:
    memory, _backend, recorder = _configured_memory()
    ambient = ExecutionContext.create()
    different = ExecutionContext.create()

    with execution_scope(ambient), pytest.raises(
        SETTConfigurationError, match="disagree"
    ):
        memory.write("answer", 42, context=different)

    assert memory.get_all() == {}
    assert recorder.event_count == 0


def test_unserializable_value_never_changes_memory() -> None:
    memory, _backend, recorder = _configured_memory()
    context = ExecutionContext.create()

    with pytest.raises(SETTPersistenceError) as captured:
        memory.write("opaque", object(), context=context)

    assert captured.value.reason_code == "private_memory.value_not_serializable"
    assert memory.get_all() == {}
    assert [event.kind for event in recorder.get_trace(context.trace_id)] == [
        "private_memory.mutation_started"
    ]


class _ConfirmationFailureStream(InMemoryEvidenceStream):
    def __init__(self) -> None:
        super().__init__(
            backend_id="backend.demo",
            stream_id="private-memory",
        )
        self._fail_next_flush = False

    def append(self, *, write_id: str, payload: bytes, expected_head):
        receipt = super().append(
            write_id=write_id,
            payload=payload,
            expected_head=expected_head,
        )
        frame = decode_trace_evidence_frame(payload)
        if frame.payload["kind"] == "private_memory.mutation_persisted":
            self._fail_next_flush = True
        return receipt

    def flush(self, *, through):
        if self._fail_next_flush:
            self._fail_next_flush = False
            raise SETTPersistenceError(
                "Confirmation durability was lost.",
                reason_code="audit.evidence_write_failed",
                backend_id="backend.demo",
            )
        return super().flush(through=through)


def test_failed_confirmation_rolls_back_live_memory_view() -> None:
    memory, _backend, _recorder = _configured_memory(
        evidence=_ConfirmationFailureStream()
    )
    context = ExecutionContext.create()

    with pytest.raises(SETTPersistenceError):
        memory.write("answer", 42, context=context)

    assert memory.get_all() == {}
    assert memory.get_history() == []


def test_ephemeral_memory_keeps_accepting_any_python_value_without_context() -> None:
    memory = PrivateMemory(owner="agent")
    value = object()

    memory.write("opaque", value)

    assert memory.read("opaque") is not value


def test_restore_replays_only_confirmed_mutations_and_can_continue() -> None:
    memory, backend, recorder = _configured_memory()
    first_context = ExecutionContext.create()
    memory.write("first", {"value": 1}, context=first_context)
    memory.write("second", [1, 2], context=first_context)
    memory.clear(context=first_context)
    memory.write("final", {"value": 3}, context=first_context)
    report = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )

    restored = PrivateMemory.restore(
        owner="agent",
        memory_id="memory.demo",
        report=report,
        backend=backend,
        policy=_policy(),
    )

    assert restored.get_all() == {"final": {"value": 3}}
    assert [entry["action"] for entry in restored.get_history()] == [
        "write",
        "write",
        "clear",
        "write",
    ]
    second_context = ExecutionContext.create()
    restored.write("continued", True, context=second_context)
    assert restored.read("continued") is True
    refreshed = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    assert refreshed.last_durable_sequence == recorder.event_count + 2


def test_restore_ignores_intent_without_confirmed_state_reference() -> None:
    memory, backend, _recorder = _configured_memory()
    context = ExecutionContext.create()
    with pytest.raises(SETTPersistenceError):
        memory.write("opaque", object(), context=context)
    report = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )

    restored = PrivateMemory.restore(
        owner="agent",
        memory_id="memory.demo",
        report=report,
        backend=backend,
        policy=_policy(),
    )

    assert restored.get_all() == {}
    assert restored.get_history() == []


class _MemoryAgent(SETTAgent):
    def __init__(self, *, persistent: bool) -> None:
        super().__init__(
            name="memory_agent",
            domain="memory",
            private_memory_id="memory.agent" if persistent else None,
        )

    def process(self, input_data):
        self._private_memory.write("private", input_data["value"])
        return {"accepted": True}


def test_agent_memory_persistence_is_explicit_and_uses_ambient_context() -> None:
    for persistent, expected_mutations in ((False, 0), (True, 1)):
        stream = InMemoryEvidenceStream(
            backend_id=f"backend.{persistent}",
            stream_id=f"stream.{persistent}",
        )
        backend = PersistenceBackend(
            namespace_id=f"namespace.{persistent}",
            evidence=stream,
            state=InMemoryStateRecordStore(
                backend_id=f"backend.{persistent}"
            ),
        )
        orchestrator = SETTOrchestrator(
            persistence_backend=backend,
            persistence_policy=_policy(),
        )
        orchestrator.register_agent(_MemoryAgent(persistent=persistent))

        result = orchestrator.run_pipeline_controlled(
            ["memory"],
            input_data={"value": 7},
        )

        assert result.status is ExecutionStatus.SUCCESS
        report = TraceRecorder.restore(
            evidence_stream=backend.evidence,
            namespace_id=backend.namespace_id,
            evidence_durability=DurabilityLevel.IN_MEMORY,
        )
        assert sum(
            event.kind == "private_memory.mutation_persisted"
            for event in report.events
        ) == expected_mutations
