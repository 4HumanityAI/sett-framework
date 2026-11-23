"""Structured, tamper-evident, privacy-preserving execution traces."""
from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sett.audit_ruler.trace_limits import validate_trace_event_fields
from sett.core_ruler.execution_context import ExecutionContext, is_sensitive_key
from sett.exceptions import (
    SETTConfigurationError,
    SETTPersistenceError,
    SETTValidationError,
)
from sett.persistence.contracts import (
    DurabilityLevel,
    EvidenceStream,
    satisfies_durability,
)
from sett.persistence.formats import (
    TRACE_CANONICALIZATION_ID,
    TRACE_HASH_ALGORITHM_ID,
    StreamPosition,
    decode_trace_evidence_frame,
    encode_trace_evidence_frame,
)
from sett.persistence.identity import derive_evidence_write_id
from sett.persistence.state import StageStateDescriptor

if TYPE_CHECKING:
    from sett.core_ruler.execution_result import ExecutionStatus

_GENESIS_HASH = "0" * 64
TraceExporter = Callable[[dict[str, Any]], None]

# Terminal kinds that may close each instrumented start boundary.
# Pairing and single-terminal validation use this shared mapping.
# Cancellation and timeout remain distinct from failure.
_TERMINAL_BY_START: dict[str, frozenset[str]] = {
    "trace.started": frozenset(
        {
            "trace.completed",
            "trace.failed",
            "trace.cancelled",
            "trace.timed_out",
        }
    ),
    "pipeline.started": frozenset(
        {
            "pipeline.completed",
            "pipeline.rejected",
            "pipeline.failed",
            "pipeline.cancelled",
            "pipeline.timed_out",
            "pipeline.partial",
        }
    ),
    "pipeline.stage_started": frozenset(
        {
            "pipeline.stage_completed",
            "pipeline.stage_rejected",
            "pipeline.stage_skipped",
            "pipeline.stage_failed",
            "pipeline.stage_cancelled",
            "pipeline.stage_timed_out",
        }
    ),
    "broadcast.started": frozenset(
        {
            "broadcast.completed",
            "broadcast.failed",
            "broadcast.cancelled",
            "broadcast.timed_out",
        }
    ),
    "route.selected": frozenset(
        {
            "route.completed",
            "route.failed",
            "route.cancelled",
            "route.timed_out",
        }
    ),
    "agent.started": frozenset(
        {
            "agent.completed",
            "agent.failed",
            "agent.cancelled",
            "agent.timed_out",
        }
    ),
    "expert.started": frozenset(
        {
            "expert.completed",
            "expert.failed",
            "expert.cancelled",
            "expert.timed_out",
        }
    ),
    "handler.authorized": frozenset({"handler.started", "handler.blocked"}),
    "handler.started": frozenset(
        {
            "handler.entry_blocked",
            "handler.completed",
            "handler.failed",
            "handler.cancelled",
            "handler.timed_out",
        }
    ),
    "handler.attempt_started": frozenset(
        {"handler.attempt_completed", "handler.attempt_failed"}
    ),
    "adapter.call_started": frozenset(
        {"adapter.call_completed", "adapter.call_failed"}
    ),
    "persistence.migration_started": frozenset(
        {"persistence.migration_completed", "persistence.migration_failed"}
    ),
}
_START_BY_TERMINAL = {
    terminal_kind: start_kind
    for start_kind, terminal_kinds in _TERMINAL_BY_START.items()
    for terminal_kind in terminal_kinds
}
assert len(_START_BY_TERMINAL) == sum(map(len, _TERMINAL_BY_START.values()))
_CANONICAL_TERMINAL_KIND = {kind: kind for kind in _START_BY_TERMINAL}
_TERMINAL_METADATA_FALLBACK_COMPONENT_TYPE = "audit"
_TERMINAL_METADATA_FALLBACK_COMPONENT_NAME = "terminal_metadata_fallback"
_TERMINAL_METADATA_FALLBACK_STATUS = "metadata_rejected"
_TERMINAL_METADATA_FALLBACK_REASON = "audit.terminal_metadata_rejected"

TRACE_TERMINAL_KINDS: frozenset[str] = _TERMINAL_BY_START["trace.started"]
"""Kinds that close a whole trace. Derived, never written out twice."""

# Events that annotate a run without opening or closing a boundary.
# Keep their names here with the paired event vocabulary.
CANCEL_REQUESTED_KIND = "execution.cancel_requested"
DEADLINE_EXCEEDED_KIND = "execution.deadline_exceeded"
CLEANUP_STARTED_KIND = "execution.cleanup_started"
CLEANUP_COMPLETED_KIND = "execution.cleanup_completed"
CLEANUP_FAILED_KIND = "execution.cleanup_failed"
_DURABLE_ANCHOR_KINDS = frozenset(
    {
        "pipeline.started",
        "persistence.migration_started",
        "private_memory.mutation_started",
        "private_memory.mutation_persisted",
        "handler.authorized",
        "handler.checkpoint_passed",
        "handler.checkpoint_blocked",
        "handler.checkpoint_missed",
        "persistence.checkpoint_requested",
    }
).union(*(terminals for terminals in _TERMINAL_BY_START.values()))
_MAX_ATTRIBUTE_KEYS = 32
_MAX_ATTRIBUTE_DEPTH = 8
_MAX_ATTRIBUTE_BYTES = 16 * 1024
# Reuse the execution-context definition of sensitive keys.


def _freeze_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise SETTValidationError(
            f"Trace attributes exceed maximum depth {_MAX_ATTRIBUTE_DEPTH}."
        )
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SETTValidationError(
                "Trace attribute numbers must be finite JSON values."
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise SETTValidationError(
                    "Trace attribute keys must be non-empty strings."
                )
            if is_sensitive_key(key):
                raise SETTValidationError(
                    f"Trace attribute key {key!r} is sensitive and cannot "
                    "be recorded."
                )
            result[key] = _freeze_value(item, depth + 1)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item, depth + 1) for item in value)
    raise SETTValidationError(
        f"Trace attributes must be JSON-safe; got {type(value).__name__}."
    )


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _freeze_attributes(
    attributes: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    raw = dict(attributes or {})
    if len(raw) > _MAX_ATTRIBUTE_KEYS:
        raise SETTValidationError(
            f"Trace attributes accept at most {_MAX_ATTRIBUTE_KEYS} keys."
        )
    frozen = _freeze_value(raw)
    assert isinstance(frozen, Mapping)
    encoded = json.dumps(
        _thaw_value(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_ATTRIBUTE_BYTES:
        raise SETTValidationError(
            f"Trace attributes exceed {_MAX_ATTRIBUTE_BYTES} bytes."
        )
    return frozen


def _canonical_bytes(entry: Mapping[str, Any]) -> bytes:
    payload = {
        key: _thaw_value(value)
        for key, value in entry.items()
        if key != "entry_hash"
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One immutable operation in a causal execution trace."""

    event_id: str
    trace_id: str
    run_id: str
    parent_id: str | None
    cause_id: str | None
    timestamp: datetime
    sequence: int
    kind: str
    component_type: str
    component_name: str
    status: str
    reason_codes: tuple[str, ...]
    attributes: Mapping[str, Any]
    previous_hash: str
    entry_hash: str
    canonicalization_id: str = TRACE_CANONICALIZATION_ID
    hash_algorithm_id: str = TRACE_HASH_ALGORITHM_ID

    def safe_view(self) -> dict[str, Any]:
        """This event as a dict, with attribute values reduced to shape."""
        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "cause_id": self.cause_id,
            "timestamp": self.timestamp.isoformat(),
            "sequence": self.sequence,
            "kind": self.kind,
            "component_type": self.component_type,
            "component_name": self.component_name,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "attributes": _thaw_value(self.attributes),
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
            "canonicalization_id": self.canonicalization_id,
            "hash_algorithm_id": self.hash_algorithm_id,
        }


class BoundaryDispositionBasis(str, Enum):
    """The source and evidentiary weight of a retrospective claim."""

    PROVIDER_RECONCILIATION = "provider_reconciliation"
    PROCESS_OBSERVATION = "process_observation"
    OPERATOR_ATTESTATION = "operator_attestation"
    ABANDONED = "abandoned"


class BoundaryClaimedOutcome(str, Enum):
    """A terminal outcome claimed after the original boundary stayed open."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class BoundaryDisposition:
    """One append-only explanation attached to an open boundary."""

    event_id: str
    basis: BoundaryDispositionBasis
    claimed_outcome: BoundaryClaimedOutcome | None
    occurred_at_claimed: datetime | None
    evidence_refs: tuple[str, ...]
    idempotency_key_digest: str | None
    supersedes_event_id: str | None
    recorded_at: datetime


class BoundaryDispositionStatus(str, Enum):
    """Whether an open boundary has one unambiguous active disposition."""

    MISSING = "missing"
    DOCUMENTED = "documented"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class UnresolvedBoundary:
    """One instrumented start with no durable terminal event.

    ``trace_id`` is deliberately absent: the owning ``RecoveredTrace`` is
    the single source of truth for it.
    """

    start_event_id: str
    run_id: str
    start_kind: str
    component_type: str
    component_name: str
    sequence: int
    dispositions: tuple[BoundaryDisposition, ...] = ()

    @property
    def active_dispositions(self) -> tuple[BoundaryDisposition, ...]:
        """Return dispositions not superseded by another valid disposition."""
        superseded = {
            disposition.supersedes_event_id
            for disposition in self.dispositions
            if disposition.supersedes_event_id is not None
        }
        return tuple(
            disposition
            for disposition in self.dispositions
            if disposition.event_id not in superseded
        )

    @property
    def disposition_status(self) -> BoundaryDispositionStatus:
        """Classify the active disposition set without closing the boundary."""
        active_count = len(self.active_dispositions)
        if active_count == 0:
            return BoundaryDispositionStatus.MISSING
        if active_count == 1:
            return BoundaryDispositionStatus.DOCUMENTED
        return BoundaryDispositionStatus.CONFLICT

    @property
    def requires_reconciliation(self) -> bool:
        """Whether this open boundary lacks one unambiguous disposition."""
        return self.disposition_status is not BoundaryDispositionStatus.DOCUMENTED


@dataclass(frozen=True, slots=True)
class RecoveredRun:
    """Lifecycle completeness reconstructed for one run in a trace."""

    run_id: str
    parent_id: str | None
    unresolved_boundaries: tuple[UnresolvedBoundary, ...]
    lifecycle_complete: bool


@dataclass(frozen=True, slots=True)
class RecoveredStage:
    """One completed stage reconstructed from durable evidence only."""

    index: int
    run_id: str
    completion_event_id: str
    descriptor: StageStateDescriptor | None
    descriptor_reason: str
    authoritative_event_id: str | None = None
    migration_event_ids: tuple[str, ...] = ()
    lineage_integrity_status: str | None = None
    lineage_conflict: bool = False


@dataclass(frozen=True, slots=True)
class RecoveredTrace:
    """Evidence-only reconstruction of one trace; never materialized state."""

    trace_id: str
    runs: tuple[RecoveredRun, ...]
    unresolved_boundaries: tuple[UnresolvedBoundary, ...]
    reconstructed_status: ExecutionStatus | None
    reconstruction_reason: str
    total_units: int | None
    stages: tuple[RecoveredStage, ...]
    trace_completeness: bool
    terminal_metadata_fallback_event_ids: tuple[str, ...]
    event_count: int

    @property
    def reconstructed_outcome(self) -> ExecutionStatus | None:
        """Compatibility wording for the reconstructed terminal status."""
        return self.reconstructed_status

    @property
    def completed_units(self) -> int:
        """Count completed stages from the evidence instead of storing it."""
        return len(self.stages)

    @property
    def completed_stage_indexes(self) -> tuple[int, ...]:
        """Return indexes derived from the recovered stage descriptors."""
        return tuple(stage.index for stage in self.stages)

    @property
    def run_completeness(self) -> bool:
        """Whether every observed run has no unresolved boundary."""
        return all(run.lifecycle_complete for run in self.runs)

    @property
    def requires_reconciliation(self) -> bool:
        """Whether any boundary or migrated stage has no unique resolution."""
        return any(
            boundary.requires_reconciliation
            for boundary in self.unresolved_boundaries
        ) or any(stage.lineage_conflict for stage in self.stages)

    @property
    def terminal_metadata_complete(self) -> bool:
        """Whether every terminal retained its requested metadata."""

        return not self.terminal_metadata_fallback_event_ids


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Pure reconstruction of one durable evidence prefix.

    The report contains evidence and structural conclusions only. It never
    materializes stage outputs or mutates the stream it describes. Per-trace
    conclusions are diagnostic when ``chain_integrity`` is false.
    """

    namespace_id: str
    backend_id: str
    stream_id: str
    chain_integrity: bool
    traces: tuple[RecoveredTrace, ...]
    last_durable_sequence: int
    last_durable_hash: str
    last_durable_position: StreamPosition | None
    observed_evidence_schemas: tuple[str, ...]
    diagnostics: tuple[str, ...]
    events: tuple[TraceEvent, ...]
    event_preimages: tuple[bytes, ...]

    @property
    def requires_reconciliation(self) -> bool:
        """Whether chain integrity or a recovered trace needs reconciliation."""
        return not self.chain_integrity or any(
            trace.requires_reconciliation for trace in self.traces
        )


def _verify_chain_integrity(
    events: tuple[TraceEvent, ...],
    preimages: tuple[bytes, ...],
) -> bool:
    """Verify hashes, sequence, causal identity, and run ownership only."""
    if len(preimages) != len(events):
        return False
    expected_previous = _GENESIS_HASH
    known_events: dict[str, TraceEvent] = {}
    seen_runs: dict[str, set[str]] = {}
    root_runs: dict[str, set[str]] = {}
    run_owners: dict[str, str] = {}
    run_parents: dict[tuple[str, str], str | None] = {}
    for index, (event, preimage) in enumerate(
        zip(events, preimages, strict=True), start=1
    ):
        view = event.safe_view()
        if event.sequence != index or event.previous_hash != expected_previous:
            return False
        payload = {key: value for key, value in view.items() if key != "entry_hash"}
        try:
            recovered_payload = json.loads(preimage.decode("utf-8"))
        except (UnicodeError, ValueError):
            return False
        if recovered_payload != payload:
            return False
        if sha256(preimage).hexdigest() != event.entry_hash:
            return False
        if event.event_id in known_events:
            return False
        if event.cause_id is not None:
            cause = known_events.get(event.cause_id)
            if cause is None or cause.trace_id != event.trace_id:
                return False

        owner = run_owners.setdefault(event.run_id, event.trace_id)
        if owner != event.trace_id:
            return False
        run_key = (event.trace_id, event.run_id)
        if run_key in run_parents:
            if run_parents[run_key] != event.parent_id:
                return False
        else:
            run_parents[run_key] = event.parent_id

        trace_runs = seen_runs.setdefault(event.trace_id, set())
        roots = root_runs.setdefault(event.trace_id, set())
        if event.parent_id is None:
            roots.add(event.run_id)
            if len(roots) > 1:
                return False
        elif event.parent_id == event.run_id or event.parent_id not in trace_runs:
            return False

        trace_runs.add(event.run_id)
        known_events[event.event_id] = event
        expected_previous = event.entry_hash
    return True


_DURABLE_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "trace_id",
        "run_id",
        "parent_id",
        "cause_id",
        "timestamp",
        "sequence",
        "kind",
        "component_type",
        "component_name",
        "status",
        "reason_codes",
        "attributes",
        "previous_hash",
        "canonicalization_id",
        "hash_algorithm_id",
    }
)


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise SETTValidationError(
            f"Recovered trace field {field!r} must be a non-empty string."
        )
    return value


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise SETTValidationError(
            f"Recovered trace field {field!r} must be a string or null."
        )
    return value


def _event_from_durable_payload(
    payload: Mapping[str, Any],
    *,
    entry_hash: bytes,
) -> TraceEvent:
    """Rebuild an event from authenticated fields without reserializing it."""
    if set(payload) != _DURABLE_EVENT_FIELDS:
        raise SETTValidationError(
            "Recovered trace evidence has missing or unexpected event fields."
        )
    if payload.get("canonicalization_id") != TRACE_CANONICALIZATION_ID:
        raise SETTValidationError(
            "Recovered trace evidence has an unsupported canonicalization."
        )
    if payload.get("hash_algorithm_id") != TRACE_HASH_ALGORITHM_ID:
        raise SETTValidationError(
            "Recovered trace evidence has an unsupported hash algorithm."
        )
    raw_timestamp = _required_text(payload, "timestamp")
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError as error:
        raise SETTValidationError(
            "Recovered trace timestamp is not valid ISO 8601."
        ) from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SETTValidationError(
            "Recovered trace timestamp must be timezone-aware."
        )
    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise SETTValidationError(
            "Recovered trace sequence must be a positive integer."
        )
    raw_reasons = payload.get("reason_codes")
    if not isinstance(raw_reasons, list) or not all(
        isinstance(reason, str) for reason in raw_reasons
    ):
        raise SETTValidationError(
            "Recovered trace reason_codes must be a list of strings."
        )
    raw_attributes = payload.get("attributes")
    if not isinstance(raw_attributes, Mapping):
        raise SETTValidationError(
            "Recovered trace attributes must be a JSON object."
        )
    kind = _required_text(payload, "kind")
    component_type = _required_text(payload, "component_type")
    component_name = _required_text(payload, "component_name")
    status = _required_text(payload, "status")
    reasons = validate_trace_event_fields(
        kind=kind,
        component_type=component_type,
        component_name=component_name,
        status=status,
        reason_codes=raw_reasons,
    )
    attributes = _freeze_attributes(raw_attributes)
    return TraceEvent(
        event_id=_required_text(payload, "event_id"),
        trace_id=_required_text(payload, "trace_id"),
        run_id=_required_text(payload, "run_id"),
        parent_id=_optional_text(payload, "parent_id"),
        cause_id=_optional_text(payload, "cause_id"),
        timestamp=timestamp.astimezone(timezone.utc),
        sequence=sequence,
        kind=kind,
        component_type=component_type,
        component_name=component_name,
        status=status,
        reason_codes=reasons,
        attributes=attributes,
        previous_hash=_required_text(payload, "previous_hash"),
        entry_hash=entry_hash.hex(),
        canonicalization_id=TRACE_CANONICALIZATION_ID,
        hash_algorithm_id=TRACE_HASH_ALGORITHM_ID,
    )


_DISPOSITION_ATTRIBUTE_FIELDS = frozenset(
    {
        "basis",
        "claimed_outcome",
        "occurred_at_claimed",
        "evidence_refs",
        "idempotency_key_digest",
        "supersedes_event_id",
    }
)


def _disposition_from_event(
    event: TraceEvent,
    *,
    prior_event_ids: frozenset[str],
) -> BoundaryDisposition | None:
    if event.kind != "recovery.boundary_disposition_recorded":
        return None
    attributes = event.attributes
    if set(attributes) != _DISPOSITION_ATTRIBUTE_FIELDS:
        return None
    try:
        basis = BoundaryDispositionBasis(attributes["basis"])
        raw_outcome = attributes["claimed_outcome"]
        claimed_outcome = (
            None
            if raw_outcome is None
            else BoundaryClaimedOutcome(raw_outcome)
        )
        raw_occurred = attributes["occurred_at_claimed"]
        occurred_at = (
            None
            if raw_occurred is None
            else datetime.fromisoformat(raw_occurred)
        )
        if occurred_at is not None:
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                return None
            occurred_at = occurred_at.astimezone(timezone.utc)
        raw_refs = attributes["evidence_refs"]
        if not isinstance(raw_refs, tuple) or not all(
            isinstance(reference, str) and reference
            for reference in raw_refs
        ):
            return None
        digest = attributes["idempotency_key_digest"]
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest.lower() != digest
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return None
        supersedes = attributes["supersedes_event_id"]
        if supersedes is not None and (
            not isinstance(supersedes, str) or supersedes not in prior_event_ids
        ):
            return None
    except (TypeError, ValueError):
        return None
    if basis is BoundaryDispositionBasis.ABANDONED:
        if claimed_outcome is not None or occurred_at is not None:
            return None
    elif claimed_outcome is None:
        return None
    if basis in {
        BoundaryDispositionBasis.PROVIDER_RECONCILIATION,
        BoundaryDispositionBasis.OPERATOR_ATTESTATION,
    } and not raw_refs:
        return None
    return BoundaryDisposition(
        event_id=event.event_id,
        basis=basis,
        claimed_outcome=claimed_outcome,
        occurred_at_claimed=occurred_at,
        evidence_refs=raw_refs,
        idempotency_key_digest=digest,
        supersedes_event_id=supersedes,
        recorded_at=event.timestamp,
    )


def _open_boundaries(events: tuple[TraceEvent, ...]) -> tuple[UnresolvedBoundary, ...]:
    """Return starts left open after pairing terminals in evidence order."""
    open_by_run_and_kind: dict[tuple[str, str], list[TraceEvent]] = {}
    terminal_owner = {
        terminal: start
        for start, terminals in _TERMINAL_BY_START.items()
        for terminal in terminals
    }
    for event in events:
        # An event can end one boundary and start the next (``handler.started``
        # ends ``handler.authorized``); it closes the first before opening its own.
        start_kind = terminal_owner.get(event.kind)
        if start_kind is not None:
            pending = open_by_run_and_kind.get((event.run_id, start_kind))
            if pending:
                pending.pop(0)
        if event.kind in _TERMINAL_BY_START:
            open_by_run_and_kind.setdefault((event.run_id, event.kind), []).append(
                event
            )
    boundaries = tuple(
        UnresolvedBoundary(
            start_event_id=event.event_id,
            run_id=event.run_id,
            start_kind=event.kind,
            component_type=event.component_type,
            component_name=event.component_name,
            sequence=event.sequence,
        )
        for pending in open_by_run_and_kind.values()
        for event in pending
    )
    enriched: list[UnresolvedBoundary] = []
    for boundary in boundaries:
        dispositions: list[BoundaryDisposition] = []
        for event in events:
            if event.cause_id != boundary.start_event_id:
                continue
            disposition = _disposition_from_event(
                event,
                prior_event_ids=frozenset(
                    item.event_id for item in dispositions
                ),
            )
            if disposition is not None:
                dispositions.append(disposition)
        enriched.append(
            replace(boundary, dispositions=tuple(dispositions))
        )
    return tuple(enriched)


def _run_lifecycle_complete(events: tuple[TraceEvent, ...]) -> bool:
    """Check starts and terminals for one run, including orphan terminals."""
    for start_kind, terminal_kinds in _TERMINAL_BY_START.items():
        open_boundaries = 0
        for event in events:
            if event.kind == start_kind:
                open_boundaries += 1
            elif event.kind in terminal_kinds:
                if open_boundaries == 0:
                    return False
                open_boundaries -= 1
        if open_boundaries:
            return False
    return True


_PIPELINE_TERMINAL_STATUS = {
    "pipeline.completed": "success",
    "pipeline.partial": "partial",
    "pipeline.rejected": "rejected",
    "pipeline.failed": "failed",
    "pipeline.cancelled": "cancelled",
    "pipeline.timed_out": "timed_out",
}
_TRACE_TERMINAL_STATUS = {
    "trace.completed": "success",
    "trace.failed": "failed",
    "trace.cancelled": "cancelled",
    "trace.timed_out": "timed_out",
}


_STAGE_DESCRIPTOR_FIELDS = frozenset(
    {
        "state_ref",
        "state_digest",
        "digest_algorithm",
        "digest_policy_id",
        "schema_id",
        "schema_version",
        "codec",
        "content_type",
        "state_backend_id",
        "snapshot_durability",
        "write_id_version",
    }
)


def _recovered_stage(event: TraceEvent) -> RecoveredStage | None:
    raw_index = event.attributes.get("index")
    if (
        isinstance(raw_index, bool)
        or not isinstance(raw_index, int)
        or raw_index < 0
    ):
        return None
    present = _STAGE_DESCRIPTOR_FIELDS.intersection(event.attributes)
    if not present:
        return RecoveredStage(
            index=raw_index,
            run_id=event.run_id,
            completion_event_id=event.event_id,
            descriptor=None,
            descriptor_reason="recovery.stage_state_not_recorded",
            authoritative_event_id=event.event_id,
        )
    if present != _STAGE_DESCRIPTOR_FIELDS:
        return RecoveredStage(
            index=raw_index,
            run_id=event.run_id,
            completion_event_id=event.event_id,
            descriptor=None,
            descriptor_reason="recovery.stage_descriptor_incomplete",
            authoritative_event_id=event.event_id,
        )
    attributes = event.attributes
    try:
        state_digest = attributes["state_digest"]
        if state_digest is not None and not isinstance(state_digest, str):
            raise TypeError
        descriptor = StageStateDescriptor(
            stage_index=raw_index,
            run_id=event.run_id,
            state_ref=attributes["state_ref"],
            state_digest=state_digest,
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
    except (TypeError, ValueError, SETTValidationError):
        return RecoveredStage(
            index=raw_index,
            run_id=event.run_id,
            completion_event_id=event.event_id,
            descriptor=None,
            descriptor_reason="recovery.stage_descriptor_invalid",
            authoritative_event_id=event.event_id,
        )
    return RecoveredStage(
        index=raw_index,
        run_id=event.run_id,
        completion_event_id=event.event_id,
        descriptor=descriptor,
        descriptor_reason="recovery.stage_descriptor_recorded",
        authoritative_event_id=event.event_id,
    )


def _migration_descriptor(
    event: TraceEvent,
    source: RecoveredStage,
) -> StageStateDescriptor:
    attributes = event.attributes
    return StageStateDescriptor(
        stage_index=source.index,
        run_id=source.run_id,
        state_ref=attributes["destination_state_ref"],
        state_digest=attributes["destination_state_digest"],
        digest_algorithm=attributes["destination_digest_algorithm"],
        digest_policy_id=attributes["destination_digest_policy_id"],
        schema_id=attributes["destination_schema_id"],
        schema_version=attributes["destination_schema_version"],
        codec=attributes["destination_codec"],
        content_type=attributes["destination_content_type"],
        backend_id=attributes["state_backend_id"],
        achieved_durability=DurabilityLevel(attributes["snapshot_durability"]),
        write_id_version=attributes["write_id_version"],
    )


def _apply_migration_lineages(
    traces: tuple[RecoveredTrace, ...],
    events: tuple[TraceEvent, ...],
) -> tuple[tuple[RecoveredTrace, ...], tuple[str, ...]]:
    """Resolve stage heads globally because migrations use their own traces."""
    mutable_stages = [list(trace.stages) for trace in traces]
    current: dict[str, tuple[int, int]] = {}
    historical: dict[str, tuple[int, int]] = {}
    for trace_index, trace in enumerate(traces):
        for stage_index, stage in enumerate(trace.stages):
            head = stage.authoritative_event_id or stage.completion_event_id
            current[head] = (trace_index, stage_index)
            historical[head] = (trace_index, stage_index)
    diagnostics: list[str] = []
    for event in events:
        if (
            event.kind != "persistence.state_migrated"
            or event.attributes.get("artifact_kind") != "stage_output"
        ):
            continue
        source_event_id = event.attributes.get("source_authoritative_event_id")
        location = current.pop(source_event_id, None)
        if location is None:
            prior = historical.get(source_event_id)
            if prior is None:
                diagnostics.append("audit.migration_lineage_invalid")
                continue
            trace_index, stage_index = prior
            existing = mutable_stages[trace_index][stage_index]
            for current_event_id, current_location in tuple(current.items()):
                if current_location == prior:
                    current.pop(current_event_id)
            mutable_stages[trace_index][stage_index] = replace(
                existing,
                descriptor=None,
                descriptor_reason="recovery.stage_migration_lineage_conflict",
                migration_event_ids=tuple(
                    (*existing.migration_event_ids, event.event_id)
                ),
                lineage_conflict=True,
            )
            diagnostics.append("audit.migration_lineage_conflict")
            continue
        trace_index, stage_index = location
        source = mutable_stages[trace_index][stage_index]
        try:
            descriptor = _migration_descriptor(event, source)
            integrity_status = event.attributes["source_integrity_status"]
            if not isinstance(integrity_status, str) or not integrity_status:
                raise TypeError
        except (KeyError, TypeError, ValueError, SETTValidationError):
            mutable_stages[trace_index][stage_index] = replace(
                source,
                descriptor=None,
                descriptor_reason="recovery.stage_migration_descriptor_invalid",
                migration_event_ids=tuple(
                    (*source.migration_event_ids, event.event_id)
                ),
                lineage_conflict=True,
            )
            diagnostics.append("audit.migration_lineage_invalid")
            historical[event.event_id] = (trace_index, stage_index)
            continue
        migrated = replace(
            source,
            descriptor=descriptor,
            descriptor_reason="recovery.stage_migrated",
            authoritative_event_id=event.event_id,
            migration_event_ids=tuple(
                (*source.migration_event_ids, event.event_id)
            ),
            lineage_integrity_status=integrity_status,
        )
        mutable_stages[trace_index][stage_index] = migrated
        current[event.event_id] = (trace_index, stage_index)
        historical[event.event_id] = (trace_index, stage_index)
    return (
        tuple(
            replace(trace, stages=tuple(mutable_stages[index]))
            for index, trace in enumerate(traces)
        ),
        tuple(dict.fromkeys(diagnostics)),
    )


def _reconstructed_trace(events: tuple[TraceEvent, ...]) -> RecoveredTrace:
    # Imported lazily because lifecycle imports trace event constants. Keeping
    # this dependency out of module initialization preserves audit_ruler as a
    # leaf while returning the framework's existing public status type.
    from sett.core_ruler.execution_result import ExecutionStatus

    trace_id = events[0].trace_id
    unresolved = _open_boundaries(events)
    run_ids = tuple(dict.fromkeys(event.run_id for event in events))
    runs = tuple(
        RecoveredRun(
            run_id=run_id,
            parent_id=next(event.parent_id for event in events if event.run_id == run_id),
            unresolved_boundaries=tuple(
                boundary for boundary in unresolved if boundary.run_id == run_id
            ),
            lifecycle_complete=_run_lifecycle_complete(
                tuple(event for event in events if event.run_id == run_id)
            ),
        )
        for run_id in run_ids
    )
    stages = tuple(
        sorted(
            (
                stage
                for event in events
                if event.kind == "pipeline.stage_completed"
                and (stage := _recovered_stage(event)) is not None
            ),
            key=lambda stage: stage.index,
        )
    )
    completed_indexes = tuple(stage.index for stage in stages)
    pipeline_starts = tuple(
        event for event in events if event.kind == "pipeline.started"
    )
    total_units: int | None = None
    status: ExecutionStatus | None = None
    reason = "recovery.outcome_not_inferable"
    if len(pipeline_starts) == 1:
        raw_total = pipeline_starts[0].attributes.get("stage_count")
        if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0:
            total_units = raw_total
        terminals = tuple(
            event
            for event in events
            if event.kind in _PIPELINE_TERMINAL_STATUS
            and event.run_id == pipeline_starts[0].run_id
        )
        if len(terminals) == 1:
            status = ExecutionStatus(_PIPELINE_TERMINAL_STATUS[terminals[0].kind])
            reason = "recovery.pipeline_terminal_observed"
        elif total_units is not None and len(completed_indexes) == total_units:
            status = ExecutionStatus.SUCCESS
            reason = "recovery.all_stages_completed"
        elif completed_indexes:
            status = ExecutionStatus.PARTIAL
            reason = "recovery.pipeline_interrupted_after_progress"
    elif len(pipeline_starts) > 1:
        reason = "recovery.multiple_pipelines_observed"
    else:
        terminals = tuple(
            event for event in events if event.kind in _TRACE_TERMINAL_STATUS
        )
        if len(terminals) == 1:
            status = ExecutionStatus(_TRACE_TERMINAL_STATUS[terminals[0].kind])
            reason = "recovery.trace_terminal_observed"
        else:
            reason = "recovery.no_pipeline_outcome"
    return RecoveredTrace(
        trace_id=trace_id,
        runs=runs,
        unresolved_boundaries=unresolved,
        reconstructed_status=status,
        reconstruction_reason=reason,
        total_units=total_units,
        stages=stages,
        trace_completeness=TraceRecorder._verify_terminals(list(events)),
        terminal_metadata_fallback_event_ids=tuple(
            event.event_id
            for event in events
            if _TERMINAL_METADATA_FALLBACK_REASON in event.reason_codes
        ),
        event_count=len(events),
    )


class TraceRecorder:
    """Thread-safe recorder with optional append-only durable evidence."""

    def __init__(
        self,
        *,
        evidence_stream: EvidenceStream | None = None,
        namespace_id: str | None = None,
        evidence_durability: DurabilityLevel | None = None,
    ) -> None:
        """Start an empty recorder and validate optional persistence."""
        self._events: list[TraceEvent] = []
        self._preimages: list[bytes] = []
        self._exporters: list[TraceExporter] = []
        self._lock = threading.RLock()
        self._evidence_stream = evidence_stream
        self._namespace_id = namespace_id
        self._evidence_durability = evidence_durability
        self._evidence_head = None
        self._evidence_requires_recovery = False
        if evidence_stream is None:
            if namespace_id is not None or evidence_durability is not None:
                raise SETTConfigurationError(
                    "namespace_id and evidence_durability require an evidence_stream."
                )
            return
        if not isinstance(namespace_id, str) or not namespace_id:
            raise SETTConfigurationError(
                "Durable evidence requires an explicit namespace_id."
            )
        if (
            not isinstance(evidence_durability, DurabilityLevel)
            or evidence_durability is DurabilityLevel.UNKNOWN
        ):
            raise SETTConfigurationError(
                "Durable evidence requires an explicit evidence_durability."
            )
        declaration = evidence_stream.durability()
        if not satisfies_durability(
            declaration.maximum_durability, evidence_durability
        ):
            raise SETTConfigurationError(
                "The evidence stream cannot satisfy evidence_durability."
            )
        if tuple(evidence_stream.read()):
            raise SETTConfigurationError(
                "The evidence stream is not empty; explicit recovery is required."
            )
        self._evidence_head = evidence_stream.head()

    @classmethod
    def restore(
        cls,
        *,
        evidence_stream: EvidenceStream,
        namespace_id: str,
        evidence_durability: DurabilityLevel,
    ) -> RecoveryReport:
        """Read and classify a stable durable prefix without writing to it."""
        if not isinstance(evidence_stream, EvidenceStream):
            raise SETTConfigurationError(
                "evidence_stream must implement the EvidenceStream contract."
            )
        if not isinstance(namespace_id, str) or not namespace_id:
            raise SETTConfigurationError(
                "Evidence recovery requires an explicit namespace_id."
            )
        if (
            not isinstance(evidence_durability, DurabilityLevel)
            or evidence_durability is DurabilityLevel.UNKNOWN
        ):
            raise SETTConfigurationError(
                "Evidence recovery requires an explicit evidence_durability."
            )
        declaration = evidence_stream.durability()
        if not satisfies_durability(
            declaration.maximum_durability, evidence_durability
        ):
            raise SETTConfigurationError(
                "The evidence stream cannot satisfy evidence_durability."
            )

        entries = ()
        stable_head = evidence_stream.head()
        for _ in range(3):
            before = evidence_stream.head()
            candidate = tuple(evidence_stream.read())
            after = evidence_stream.head()
            candidate_head = candidate[-1].position if candidate else before
            if before == after and candidate_head == after:
                entries = candidate
                stable_head = after
                break
        else:
            raise SETTPersistenceError(
                "Evidence changed while a stable recovery prefix was being read.",
                reason_code="audit.recovery_snapshot_unstable",
                backend_id=stable_head.backend_id,
            )

        durability = evidence_stream.flush(through=stable_head)
        if not satisfies_durability(
            durability.achieved_durability, evidence_durability
        ):
            raise SETTPersistenceError(
                "The recovered evidence prefix did not reach required durability.",
                reason_code="audit.recovery_durability_unsatisfied",
                backend_id=durability.backend_id,
            )

        recovered_events: list[TraceEvent] = []
        recovered_preimages: list[bytes] = []
        schemas: set[str] = set()
        diagnostics: list[str] = []
        last_position: StreamPosition | None = stable_head if not entries else None
        for entry in entries:
            try:
                frame = decode_trace_evidence_frame(entry.payload)
                event = _event_from_durable_payload(
                    frame.payload,
                    entry_hash=frame.entry_hash,
                )
            except (SETTPersistenceError, SETTValidationError) as error:
                diagnostics.append(
                    getattr(error, "reason_code", "audit.event_reconstruction_failed")
                )
                break
            expected_write_id = derive_evidence_write_id(
                namespace_id=namespace_id,
                event_id=event.event_id,
            )
            if entry.write_id != expected_write_id:
                diagnostics.append("audit.evidence_write_identity_mismatch")
                break
            candidate_events = tuple((*recovered_events, event))
            candidate_preimages = tuple((*recovered_preimages, frame.preimage))
            if not _verify_chain_integrity(candidate_events, candidate_preimages):
                diagnostics.append("audit.chain_integrity_failed")
                break
            recovered_events.append(event)
            recovered_preimages.append(frame.preimage)
            schemas.add(frame.canonicalization_id)
            last_position = entry.position

        chain_integrity = len(recovered_events) == len(entries)
        if not chain_integrity and "audit.chain_integrity_failed" not in diagnostics:
            diagnostics.append("audit.chain_integrity_failed")
        events_tuple = tuple(recovered_events)
        traces = tuple(
            _reconstructed_trace(
                tuple(event for event in events_tuple if event.trace_id == trace_id)
            )
            for trace_id in dict.fromkeys(event.trace_id for event in events_tuple)
        )
        traces, migration_diagnostics = _apply_migration_lineages(
            traces, events_tuple
        )
        diagnostics.extend(migration_diagnostics)
        if any(not trace.terminal_metadata_complete for trace in traces):
            diagnostics.append(_TERMINAL_METADATA_FALLBACK_REASON)
        return RecoveryReport(
            namespace_id=namespace_id,
            backend_id=stable_head.backend_id,
            stream_id=stable_head.stream_id,
            chain_integrity=chain_integrity,
            traces=traces,
            last_durable_sequence=(
                recovered_events[-1].sequence if recovered_events else 0
            ),
            last_durable_hash=(
                recovered_events[-1].entry_hash
                if recovered_events
                else _GENESIS_HASH
            ),
            last_durable_position=last_position,
            observed_evidence_schemas=tuple(sorted(schemas)),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            events=events_tuple,
            event_preimages=tuple(recovered_preimages),
        )

    @classmethod
    def resume(
        cls,
        report: RecoveryReport,
        *,
        evidence_stream: EvidenceStream,
        namespace_id: str,
        evidence_durability: DurabilityLevel,
    ) -> TraceRecorder:
        """Resume only from a complete, unchanged, verified durable prefix."""
        if not isinstance(report, RecoveryReport):
            raise SETTValidationError("report must be a RecoveryReport.")
        if not report.chain_integrity or report.last_durable_position is None:
            raise SETTConfigurationError(
                "Cannot resume from evidence whose chain integrity is not valid."
            )
        if namespace_id != report.namespace_id:
            raise SETTConfigurationError(
                "Recovery namespace does not match the report namespace."
            )
        if not isinstance(evidence_stream, EvidenceStream):
            raise SETTConfigurationError(
                "evidence_stream must implement the EvidenceStream contract."
            )
        declaration = evidence_stream.durability()
        if (
            not isinstance(evidence_durability, DurabilityLevel)
            or evidence_durability is DurabilityLevel.UNKNOWN
            or not satisfies_durability(
                declaration.maximum_durability, evidence_durability
            )
        ):
            raise SETTConfigurationError(
                "The evidence stream cannot satisfy evidence_durability."
            )
        current_head = evidence_stream.head()
        if (
            current_head.backend_id != report.backend_id
            or current_head.stream_id != report.stream_id
            or current_head != report.last_durable_position
        ):
            raise SETTConfigurationError(
                "The evidence stream advanced or does not match the recovery report."
            )
        if not _verify_chain_integrity(report.events, report.event_preimages):
            raise SETTValidationError(
                "Recovery report events do not match their authenticated preimages."
            )
        recorder = cls.__new__(cls)
        recorder._events = list(report.events)
        recorder._preimages = list(report.event_preimages)
        recorder._exporters = []
        recorder._lock = threading.RLock()
        recorder._evidence_stream = evidence_stream
        recorder._namespace_id = namespace_id
        recorder._evidence_durability = evidence_durability
        recorder._evidence_head = current_head
        recorder._evidence_requires_recovery = False
        return recorder

    def register_exporter(self, exporter: TraceExporter) -> None:
        """Add a sink called once per recorded event, best effort."""
        if not callable(exporter):
            raise SETTValidationError("Trace exporter must be callable.")
        with self._lock:
            self._exporters.append(exporter)

    def _open_start_for_terminal(
        self,
        context: ExecutionContext,
        terminal_kind: str,
    ) -> TraceEvent | None:
        start_kind = _START_BY_TERMINAL.get(terminal_kind)
        if start_kind is None:
            return None
        pending: list[TraceEvent] = []
        terminal_kinds = _TERMINAL_BY_START[start_kind]
        for event in self._events:
            if event.trace_id != context.trace_id or event.run_id != context.run_id:
                continue
            if event.kind == start_kind:
                pending.append(event)
            elif event.kind in terminal_kinds and pending:
                pending.pop(0)
        return pending[0] if pending else None

    def record(
        self,
        context: ExecutionContext,
        *,
        kind: str,
        component_type: str,
        component_name: str,
        status: str,
        cause_id: str | None = None,
        reason_codes: tuple[str, ...] | list[str] = (),
        attributes: Mapping[str, Any] | None = None,
        effect_boundary: bool = False,
    ) -> TraceEvent:
        """Commit an event before notifying best-effort exporters."""
        raw_attributes = dict(attributes or {})
        raw_reasons = tuple(reason_codes)
        with self._lock:
            if self._evidence_stream is None:
                safe_attributes = _freeze_attributes(raw_attributes)
                safe_reasons = raw_reasons
            else:
                try:
                    safe_attributes = _freeze_attributes(raw_attributes)
                    safe_reasons = validate_trace_event_fields(
                        kind=kind,
                        component_type=component_type,
                        component_name=component_name,
                        status=status,
                        reason_codes=raw_reasons,
                    )
                except SETTValidationError:
                    start = self._open_start_for_terminal(context, kind)
                    if start is None:
                        raise
                    kind = _CANONICAL_TERMINAL_KIND[kind]
                    component_type = _TERMINAL_METADATA_FALLBACK_COMPONENT_TYPE
                    component_name = _TERMINAL_METADATA_FALLBACK_COMPONENT_NAME
                    status = _TERMINAL_METADATA_FALLBACK_STATUS
                    cause_id = start.event_id
                    safe_reasons = (_TERMINAL_METADATA_FALLBACK_REASON,)
                    safe_attributes = _freeze_attributes(
                        {
                            "discarded_attribute_count": len(raw_attributes),
                            "discarded_reason_code_count": len(raw_reasons),
                        }
                    )
            if self._evidence_requires_recovery:
                raise SETTPersistenceError(
                    "The evidence stream has an unresolved append outcome; "
                    "restore and resume it before recording more events.",
                    reason_code="audit.evidence_recovery_required",
                    backend_id=(
                        None
                        if self._evidence_head is None
                        else self._evidence_head.backend_id
                    ),
                )
            timestamp = datetime.now(timezone.utc)
            event_id = str(uuid4())
            base = {
                "event_id": event_id,
                "trace_id": context.trace_id,
                "run_id": context.run_id,
                "parent_id": context.parent_id,
                "cause_id": cause_id,
                "timestamp": timestamp.isoformat(),
                "sequence": len(self._events) + 1,
                "kind": kind,
                "component_type": component_type,
                "component_name": component_name,
                "status": status,
                "reason_codes": list(safe_reasons),
                "attributes": _thaw_value(safe_attributes),
                "previous_hash": (
                    self._events[-1].entry_hash if self._events else _GENESIS_HASH
                ),
                "canonicalization_id": TRACE_CANONICALIZATION_ID,
                "hash_algorithm_id": TRACE_HASH_ALGORITHM_ID,
            }
            preimage = _canonical_bytes(base)
            base["entry_hash"] = sha256(preimage).hexdigest()
            event = TraceEvent(
                event_id=base["event_id"],
                trace_id=base["trace_id"],
                run_id=base["run_id"],
                parent_id=base["parent_id"],
                cause_id=base["cause_id"],
                timestamp=timestamp,
                sequence=base["sequence"],
                kind=base["kind"],
                component_type=base["component_type"],
                component_name=base["component_name"],
                status=base["status"],
                reason_codes=safe_reasons,
                attributes=safe_attributes,
                previous_hash=base["previous_hash"],
                entry_hash=base["entry_hash"],
                canonicalization_id=base["canonicalization_id"],
                hash_algorithm_id=base["hash_algorithm_id"],
            )
            if self._evidence_stream is not None:
                assert self._namespace_id is not None
                assert self._evidence_head is not None
                frame = encode_trace_evidence_frame(
                    preimage,
                    entry_hash=bytes.fromhex(event.entry_hash),
                )
                write_id = derive_evidence_write_id(
                    namespace_id=self._namespace_id,
                    event_id=event_id,
                )
                try:
                    receipt = self._evidence_stream.append(
                        write_id=write_id,
                        payload=frame,
                        expected_head=self._evidence_head,
                    )
                except Exception as error:
                    self._evidence_requires_recovery = True
                    if isinstance(error, SETTPersistenceError):
                        raise
                    raise SETTPersistenceError(
                        "Trace evidence could not be persisted.",
                        reason_code="audit.evidence_write_failed",
                    ) from error
                self._evidence_head = receipt.position
                if kind in _DURABLE_ANCHOR_KINDS:
                    try:
                        durability = self._evidence_stream.flush(
                            through=receipt.position
                        )
                        assert self._evidence_durability is not None
                        if not satisfies_durability(
                            durability.achieved_durability,
                            self._evidence_durability,
                        ):
                            raise SETTPersistenceError(
                                "Evidence anchor did not reach required durability.",
                                reason_code="audit.anchor_durability_unsatisfied",
                                backend_id=receipt.backend_id,
                            )
                    except Exception as error:
                        self._evidence_requires_recovery = True
                        if isinstance(error, SETTPersistenceError):
                            raise
                        raise SETTPersistenceError(
                            "Trace evidence durability could not be confirmed.",
                            reason_code="audit.evidence_write_failed",
                            backend_id=receipt.backend_id,
                        ) from error
                self._events.append(event)
                self._preimages.append(preimage)
            else:
                self._events.append(event)
                self._preimages.append(preimage)
            exporters = tuple(self._exporters)

        for exporter in exporters:
            try:
                exporter(event.safe_view())
            except Exception as error:
                if effect_boundary:
                    boundary_error = SETTConfigurationError(
                        "A trace exporter failed at a real-world effect boundary; "
                        "the effect was not executed."
                    )
                    boundary_error.trace_event_id = event.event_id
                    raise boundary_error from error
        return event

    def get_trace(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """Every event recorded under one trace, in recorded order."""
        with self._lock:
            return tuple(event for event in self._events if event.trace_id == trace_id)

    def knows_event(self, event_id: str, *, trace_id: str | None = None) -> bool:
        """Whether this recorder holds ``event_id``, optionally in one trace.

        Exists so a caller can safely use an event id that reached it from
        outside, such as a cancellation request carried on an exception,
        as a ``cause_id``. Referencing an event this recorder
        never saw, or one belonging to a different trace, is precisely what
        ``verify()`` rejects, so the honest move is to check first and fall
        back to a local cause rather than to record an unverifiable link.
        """
        if not event_id:
            return False
        with self._lock:
            for event in reversed(self._events):
                if event.event_id != event_id:
                    continue
                return trace_id is None or event.trace_id == trace_id
        return False

    def last_event_id(self, run_id: str) -> str | None:
        """Return the latest event for one execution node."""
        with self._lock:
            for event in reversed(self._events):
                if event.run_id == run_id:
                    return event.event_id
        return None

    def export_trace(
        self,
        trace_id: str,
        *,
        view: str = "sanitized",
    ) -> tuple[dict[str, Any], ...]:
        """One trace as dicts, in the 'summary' or 'sanitized' view."""
        if view not in {"summary", "sanitized"}:
            raise SETTValidationError("Trace view must be 'summary' or 'sanitized'.")
        events = self.get_trace(trace_id)
        if view == "sanitized":
            return tuple(event.safe_view() for event in events)
        return tuple(
            {
                "event_id": event.event_id,
                "trace_id": event.trace_id,
                "run_id": event.run_id,
                "parent_id": event.parent_id,
                "cause_id": event.cause_id,
                "timestamp": event.timestamp.isoformat(),
                "sequence": event.sequence,
                "kind": event.kind,
                "component_type": event.component_type,
                "component_name": event.component_name,
                "status": event.status,
                "reason_codes": list(event.reason_codes),
            }
            for event in events
        )

    def verify(self, trace_id: str | None = None) -> bool:
        """
        Verify this recorder's hash chain and structural integrity.

        The chain is global, over every event this recorder has ever
        recorded; there is no separate chain per trace_id.
        Passing ``trace_id`` only adds a
        precondition, "at least one event with this trace_id exists";
        it does not scope verification to that trace alone. Corruption
        in any event, in any trace, makes ``verify(trace_id=...)``
        return False for every trace_id, not only the corrupted one.

        Args:
            trace_id: If given, additionally require that at least one
                recorded event carries this trace_id. Does not narrow
                what gets verified.

        Returns:
            True if the whole recorder's history is internally
            consistent (and, when trace_id is given, that trace_id was
            seen at all). False otherwise.
        """
        with self._lock:
            events = tuple(self._events)
        if trace_id is not None and not any(
            event.trace_id == trace_id for event in events
        ):
            return False

        with self._lock:
            preimages = tuple(self._preimages)
        if not _verify_chain_integrity(events, preimages):
            return False

        traces = {event.trace_id for event in events}
        for current_trace_id in traces:
            trace_events = [
                event for event in events if event.trace_id == current_trace_id
            ]
            if not self._verify_terminals(trace_events):
                return False
        return True

    @staticmethod
    def _verify_terminals(events: list[TraceEvent]) -> bool:
        """Validate start/terminal pairs for instrumented execution kinds."""
        terminal_by_start = _TERMINAL_BY_START
        events_by_run: dict[str, list[TraceEvent]] = {}
        for event in events:
            events_by_run.setdefault(event.run_id, []).append(event)

        for run_events in events_by_run.values():
            for start_kind, terminal_kinds in terminal_by_start.items():
                open_boundaries = 0
                for event in run_events:
                    if event.kind == start_kind:
                        open_boundaries += 1
                    elif event.kind in terminal_kinds:
                        if open_boundaries == 0:
                            return False
                        open_boundaries -= 1
                if open_boundaries:
                    return False

        trace_starts = [event for event in events if event.kind == "trace.started"]
        if trace_starts:
            if len(trace_starts) != 1:
                return False
            trace_terminals = [
                event for event in events if event.kind in TRACE_TERMINAL_KINDS
            ]
            if len(trace_terminals) != 1:
                return False
            if events[-1].event_id != trace_terminals[0].event_id:
                return False
        return True

    @property
    def durable_position(self) -> StreamPosition | None:
        """Return the position accepted for the latest local durable event."""
        with self._lock:
            return self._evidence_head

    @property
    def event_count(self) -> int:
        """How many events this recorder currently holds."""
        with self._lock:
            return len(self._events)
