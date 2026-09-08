"""Durable private-memory mutations and evidence-only descriptors.

Persisted state lands in the configured backend as strict JSON, without
separate permissions or encryption. ``PrivateMemory`` names an
architectural position, not a storage guarantee; see
``docs/security_model.md`` for who protects the resulting bytes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sett.audit_ruler.integrity import (
    IntegrityPolicy,
    digest_from_trace_value,
    digest_to_trace_value,
)
from sett.exceptions import (
    SETTPersistenceError,
    SETTPersistenceFormatError,
    SETTValidationError,
)
from sett.persistence.contracts import (
    DurabilityLevel,
    StateRecordStore,
    satisfies_durability,
)
from sett.persistence.formats import (
    PrivateMemoryMutationEnvelope,
    PrivateMemoryOperation,
    decode_private_memory_mutation,
    encode_private_memory_mutation,
)
from sett.persistence.identity import (
    PRIVATE_MEMORY_WRITE_ID_VERSION,
    derive_private_memory_write_id,
)
from sett.persistence.state import IntegrityStatus

PRIVATE_MEMORY_SCHEMA_ID = "sett.private-memory-mutation"
PRIVATE_MEMORY_SCHEMA_VERSION = 1
PRIVATE_MEMORY_CODEC = "sett.json.v1"
PRIVATE_MEMORY_CONTENT_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class PrivateMemoryMutationDescriptor:
    """JSON-safe evidence that identifies one durable memory mutation."""

    memory_id: str
    mutation_event_id: str
    state_ref: str
    state_digest: str | None
    digest_algorithm: str
    digest_policy_id: str
    schema_id: str
    schema_version: int
    codec: str
    content_type: str
    backend_id: str
    achieved_durability: DurabilityLevel
    write_id_version: int = PRIVATE_MEMORY_WRITE_ID_VERSION

    def __post_init__(self) -> None:
        for name in (
            "memory_id",
            "mutation_event_id",
            "state_ref",
            "digest_algorithm",
            "digest_policy_id",
            "schema_id",
            "codec",
            "content_type",
            "backend_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise SETTValidationError(
                    f"PrivateMemoryMutationDescriptor.{name} cannot be empty."
                )
        digest_from_trace_value(self.state_digest)
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise SETTValidationError("schema_version must be a positive integer.")
        if not isinstance(self.achieved_durability, DurabilityLevel):
            raise SETTValidationError(
                "achieved_durability must be a DurabilityLevel."
            )
        if self.write_id_version != PRIVATE_MEMORY_WRITE_ID_VERSION:
            raise SETTValidationError(
                "Unsupported private-memory write_id version."
            )

    def trace_attributes(self) -> dict[str, Any]:
        """Return the complete content-free evidence view."""
        return {
            "memory_id": self.memory_id,
            "state_ref": self.state_ref,
            "state_digest": self.state_digest,
            "digest_algorithm": self.digest_algorithm,
            "digest_policy_id": self.digest_policy_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "codec": self.codec,
            "content_type": self.content_type,
            "state_backend_id": self.backend_id,
            "snapshot_durability": self.achieved_durability.value,
            "write_id_version": self.write_id_version,
        }


def _encode_value(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise SETTPersistenceError(
            "Private-memory value is not representable as strict JSON.",
            reason_code="private_memory.value_not_serializable",
        ) from error


def persist_private_memory_mutation(
    *,
    operation: PrivateMemoryOperation,
    key: str | None,
    value: Any,
    store: StateRecordStore,
    namespace_id: str,
    memory_id: str,
    trace_id: str,
    run_id: str,
    mutation_event_id: str,
    integrity_policy: IntegrityPolicy,
    required_durability: DurabilityLevel,
    write_id_version: int = PRIVATE_MEMORY_WRITE_ID_VERSION,
) -> PrivateMemoryMutationDescriptor:
    """Persist one mutation before any corresponding in-memory change."""
    if not isinstance(operation, PrivateMemoryOperation):
        raise SETTValidationError("operation must be a PrivateMemoryOperation.")
    if operation is PrivateMemoryOperation.WRITE:
        if not isinstance(key, str) or not key:
            raise SETTValidationError("A private-memory write requires a key.")
        payload = _encode_value(value)
    else:
        if key is not None:
            raise SETTValidationError("A private-memory clear cannot carry a key.")
        payload = None
    envelope = PrivateMemoryMutationEnvelope(
        digest_algorithm=integrity_policy.algorithm,
        digest_policy_id=integrity_policy.policy_id,
        namespace_id=namespace_id,
        memory_id=memory_id,
        trace_id=trace_id,
        run_id=run_id,
        mutation_event_id=mutation_event_id,
        operation=operation,
        key=key,
        schema_id=PRIVATE_MEMORY_SCHEMA_ID,
        schema_version=PRIVATE_MEMORY_SCHEMA_VERSION,
        codec=PRIVATE_MEMORY_CODEC,
        content_type=PRIVATE_MEMORY_CONTENT_TYPE,
        payload_bytes=payload,
    )
    frame = encode_private_memory_mutation(envelope)
    digest = integrity_policy.digest(frame)
    write_id = derive_private_memory_write_id(
        namespace_id=namespace_id,
        mutation_event_id=mutation_event_id,
        version=write_id_version,
    )
    receipt = store.put(write_id=write_id, payload=frame)
    if not satisfies_durability(
        receipt.achieved_durability, required_durability
    ):
        raise SETTPersistenceError(
            "Private-memory mutation did not reach required durability.",
            reason_code="private_memory.snapshot_durability_unsatisfied",
            backend_id=receipt.backend_id,
        )
    return PrivateMemoryMutationDescriptor(
        memory_id=memory_id,
        mutation_event_id=mutation_event_id,
        state_ref=receipt.state_ref,
        state_digest=digest_to_trace_value(digest),
        digest_algorithm=integrity_policy.algorithm,
        digest_policy_id=integrity_policy.policy_id,
        schema_id=PRIVATE_MEMORY_SCHEMA_ID,
        schema_version=PRIVATE_MEMORY_SCHEMA_VERSION,
        codec=PRIVATE_MEMORY_CODEC,
        content_type=PRIVATE_MEMORY_CONTENT_TYPE,
        backend_id=receipt.backend_id,
        achieved_durability=receipt.achieved_durability,
        write_id_version=write_id_version,
    )


@dataclass(frozen=True, slots=True)
class RestoredPrivateMemoryState:
    """A fully verified replay result, separate from the live memory object."""

    memory_id: str
    values: dict[str, Any]
    history: tuple[dict[str, Any], ...]
    mutation_count: int


def private_memory_descriptor_from_event(
    event: Any,
) -> PrivateMemoryMutationDescriptor:
    """Parse content-free mutation evidence into its strict descriptor."""
    attributes = event.attributes
    try:
        durability = DurabilityLevel(attributes["snapshot_durability"])
        return PrivateMemoryMutationDescriptor(
            memory_id=attributes["memory_id"],
            mutation_event_id=event.cause_id,
            state_ref=attributes["state_ref"],
            state_digest=attributes["state_digest"],
            digest_algorithm=attributes["digest_algorithm"],
            digest_policy_id=attributes["digest_policy_id"],
            schema_id=attributes["schema_id"],
            schema_version=attributes["schema_version"],
            codec=attributes["codec"],
            content_type=attributes["content_type"],
            backend_id=attributes["state_backend_id"],
            achieved_durability=durability,
            write_id_version=attributes["write_id_version"],
        )
    except (KeyError, TypeError, ValueError, SETTValidationError) as error:
        raise SETTPersistenceFormatError(
            "Private-memory evidence metadata is incomplete or invalid.",
            reason_code="private_memory.evidence_metadata_invalid",
        ) from error


@dataclass(frozen=True, slots=True)
class _PrivateMemoryMutationHead:
    descriptor: PrivateMemoryMutationDescriptor
    authoritative_event_id: str
    authoritative_sequence: int
    root_sequence: int
    root_event_id: str
    trace_id: str
    run_id: str
    lineage_integrity_status: IntegrityStatus | None = None


def _private_memory_mutation_heads(
    report: Any,
    *,
    memory_id: str,
) -> tuple[_PrivateMemoryMutationHead, ...]:
    """Resolve linear copy-on-write heads while retaining mutation order."""
    heads: dict[str, _PrivateMemoryMutationHead] = {}
    root_by_current_event: dict[str, str] = {}
    historical: dict[str, str] = {}
    for event in report.events:
        if (
            event.kind == "private_memory.mutation_persisted"
            and event.attributes.get("memory_id") == memory_id
        ):
            descriptor = private_memory_descriptor_from_event(event)
            root = event.event_id
            heads[root] = _PrivateMemoryMutationHead(
                descriptor=descriptor,
                authoritative_event_id=event.event_id,
                authoritative_sequence=event.sequence,
                root_sequence=event.sequence,
                root_event_id=event.event_id,
                trace_id=event.trace_id,
                run_id=event.run_id,
            )
            root_by_current_event[event.event_id] = root
            historical[event.event_id] = root
            continue
        if (
            event.kind != "persistence.state_migrated"
            or event.attributes.get("artifact_kind") != "private_memory"
            or event.attributes.get("memory_id") != memory_id
        ):
            continue
        source_event_id = event.attributes.get("source_authoritative_event_id")
        root = root_by_current_event.pop(source_event_id, None)
        if root is None:
            reason = (
                "migration.lineage_conflict"
                if source_event_id in historical
                else "migration.lineage_invalid"
            )
            raise SETTPersistenceError(
                "Private-memory migration lineage is not linear.",
                reason_code=reason,
                backend_id=report.backend_id,
            )
        source = heads[root]
        attributes = event.attributes
        try:
            descriptor = PrivateMemoryMutationDescriptor(
                memory_id=memory_id,
                mutation_event_id=source.descriptor.mutation_event_id,
                state_ref=attributes["destination_state_ref"],
                state_digest=attributes["destination_state_digest"],
                digest_algorithm=attributes["destination_digest_algorithm"],
                digest_policy_id=attributes["destination_digest_policy_id"],
                schema_id=attributes["destination_schema_id"],
                schema_version=attributes["destination_schema_version"],
                codec=attributes["destination_codec"],
                content_type=attributes["destination_content_type"],
                backend_id=attributes["state_backend_id"],
                achieved_durability=DurabilityLevel(
                    attributes["snapshot_durability"]
                ),
                write_id_version=attributes["write_id_version"],
            )
            lineage_integrity = IntegrityStatus(
                attributes["source_integrity_status"]
            )
        except (KeyError, TypeError, ValueError, SETTValidationError) as error:
            raise SETTPersistenceError(
                "Private-memory migration evidence is incomplete or invalid.",
                reason_code="migration.lineage_invalid",
                backend_id=report.backend_id,
            ) from error
        heads[root] = _PrivateMemoryMutationHead(
            descriptor=descriptor,
            authoritative_event_id=event.event_id,
            authoritative_sequence=event.sequence,
            root_sequence=source.root_sequence,
            root_event_id=source.root_event_id,
            trace_id=source.trace_id,
            run_id=source.run_id,
            lineage_integrity_status=lineage_integrity,
        )
        root_by_current_event[event.event_id] = root
        historical[event.event_id] = root
    return tuple(sorted(heads.values(), key=lambda item: item.root_sequence))


def restore_private_memory_state(
    report: Any,
    *,
    memory_id: str,
    store: StateRecordStore,
    namespace_id: str,
    policies: Mapping[str, IntegrityPolicy],
) -> RestoredPrivateMemoryState:
    """Replay only evidence-confirmed mutations into a detached state view."""
    from sett.audit_ruler.trace import RecoveryReport

    if not isinstance(report, RecoveryReport) or not report.chain_integrity:
        raise SETTPersistenceError(
            "Private-memory restoration requires an intact RecoveryReport.",
            reason_code="private_memory.recovery_report_invalid",
        )
    if report.namespace_id != namespace_id:
        raise SETTPersistenceError(
            "Recovery report and requested namespace disagree.",
            reason_code="private_memory.namespace_mismatch",
            backend_id=report.backend_id,
        )
    starts = {
        event.event_id: event
        for event in report.events
        if event.kind == "private_memory.mutation_started"
    }
    values: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    mutation_count = 0
    events = {event.event_id: event for event in report.events}
    for head in _private_memory_mutation_heads(report, memory_id=memory_id):
        descriptor = head.descriptor
        root_event = events[head.root_event_id]
        started = starts.get(descriptor.mutation_event_id)
        if (
            started is None
            or started.trace_id != head.trace_id
            or started.run_id != head.run_id
            or started.attributes.get("memory_id") != memory_id
        ):
            raise SETTPersistenceError(
                "Private-memory confirmation has no matching durable intent.",
                reason_code="private_memory.intent_mismatch",
            )
        try:
            record = store.get(state_ref=descriptor.state_ref)
        except Exception as error:
            if isinstance(error, SETTPersistenceError):
                raise
            raise SETTPersistenceError(
                "A confirmed private-memory mutation record is missing.",
                reason_code="private_memory.record_missing",
                backend_id=descriptor.backend_id,
            ) from error
        policy = policies.get(descriptor.digest_policy_id)
        if policy is None:
            raise SETTPersistenceError(
                "The historical private-memory integrity policy is unavailable.",
                reason_code="private_memory.integrity_policy_unavailable",
                backend_id=descriptor.backend_id,
            )
        expected_digest = digest_from_trace_value(descriptor.state_digest)
        if (
            policy.algorithm != descriptor.digest_algorithm
            or not policy.verify(record.payload, expected_digest)
        ):
            raise SETTPersistenceError(
                "Private-memory mutation integrity verification failed.",
                reason_code="private_memory.integrity_mismatch",
                backend_id=descriptor.backend_id,
            )
        envelope = decode_private_memory_mutation(record.payload)
        if (
            envelope.digest_algorithm != descriptor.digest_algorithm
            or envelope.digest_policy_id != descriptor.digest_policy_id
            or envelope.namespace_id != namespace_id
            or envelope.memory_id != memory_id
            or envelope.trace_id != head.trace_id
            or envelope.run_id != head.run_id
            or envelope.mutation_event_id != descriptor.mutation_event_id
            or envelope.schema_id != descriptor.schema_id
            or envelope.schema_version != descriptor.schema_version
            or envelope.codec != descriptor.codec
            or envelope.content_type != descriptor.content_type
        ):
            raise SETTPersistenceError(
                "Private-memory evidence contradicts its authenticated envelope.",
                reason_code="private_memory.evidence_metadata_mismatch",
                backend_id=descriptor.backend_id,
            )
        started_operation = started.attributes.get("operation")
        if started_operation != envelope.operation.name.lower():
            raise SETTPersistenceError(
                "Private-memory intent and authenticated operation disagree.",
                reason_code="private_memory.operation_mismatch",
                backend_id=descriptor.backend_id,
            )
        if envelope.operation is PrivateMemoryOperation.WRITE:
            assert envelope.key is not None
            assert envelope.payload_bytes is not None
            try:
                value = json.loads(envelope.payload_bytes.decode("utf-8", "strict"))
            except (UnicodeError, ValueError) as error:
                raise SETTPersistenceError(
                    "Private-memory mutation payload could not be decoded.",
                    reason_code="private_memory.decode_failed",
                    backend_id=descriptor.backend_id,
                ) from error
            values[envelope.key] = value
            history.append(
                {
                    "timestamp": root_event.timestamp.isoformat(),
                    "action": "write",
                    "key": envelope.key,
                }
            )
        else:
            values.clear()
            history.append(
                {
                    "timestamp": root_event.timestamp.isoformat(),
                    "action": "clear",
                    "key": None,
                }
            )
        mutation_count += 1
    return RestoredPrivateMemoryState(
        memory_id=memory_id,
        values=values,
        history=tuple(history),
        mutation_count=mutation_count,
    )
