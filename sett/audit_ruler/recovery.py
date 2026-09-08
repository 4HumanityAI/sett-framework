"""Explicit append-only dispositions for unresolved audit boundaries."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sett.audit_ruler.closure import AuditGapToken
from sett.audit_ruler.trace import (
    BoundaryClaimedOutcome,
    BoundaryDispositionBasis,
    RecoveryReport,
    TraceRecorder,
    UnresolvedBoundary,
)
from sett.core_ruler.execution_context import ExecutionContext, is_sensitive_key
from sett.exceptions import (
    SETTConfigurationError,
    SETTPersistenceError,
    SETTValidationError,
)
from sett.persistence.contracts import (
    DurabilityLevel,
    EvidenceStream,
    StreamPosition,
    satisfies_durability,
)

_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class BoundaryDispositionReceipt:
    """Confirmed position and identity of one disposition event."""

    event_id: str
    position: StreamPosition
    achieved_durability: DurabilityLevel
    recorded_at: datetime


def _validated_references(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise SETTValidationError(
            "evidence_refs must be an iterable of opaque references."
        )
    references = tuple(values)
    if len(references) > 16:
        raise SETTValidationError(
            "A boundary disposition accepts at most 16 evidence references."
        )
    for reference in references:
        if (
            not isinstance(reference, str)
            or _REFERENCE.fullmatch(reference) is None
            or is_sensitive_key(reference)
        ):
            raise SETTValidationError(
                "Evidence references must be opaque, non-sensitive identifiers."
            )
    return references


def _owning_trace(
    report: RecoveryReport,
    boundary: UnresolvedBoundary,
):
    owners = tuple(
        trace
        for trace in report.traces
        if any(candidate is boundary for candidate in trace.unresolved_boundaries)
    )
    if len(owners) != 1:
        raise SETTValidationError(
            "boundary must be an UnresolvedBoundary from this RecoveryReport."
        )
    return owners[0]


def _validate_basis_contract(
    *,
    boundary: UnresolvedBoundary,
    basis: BoundaryDispositionBasis,
    claimed_outcome: BoundaryClaimedOutcome | None,
    occurred_at_claimed: datetime | None,
    evidence_refs: tuple[str, ...],
    idempotency_key_digest: str | None,
    audit_gap_token: AuditGapToken | None,
) -> datetime | None:
    if not isinstance(basis, BoundaryDispositionBasis):
        raise SETTValidationError(
            "basis must be a BoundaryDispositionBasis member."
        )
    if claimed_outcome is not None and not isinstance(
        claimed_outcome, BoundaryClaimedOutcome
    ):
        raise SETTValidationError(
            "claimed_outcome must be a BoundaryClaimedOutcome or None."
        )
    if occurred_at_claimed is not None:
        if (
            not isinstance(occurred_at_claimed, datetime)
            or occurred_at_claimed.tzinfo is None
            or occurred_at_claimed.utcoffset() is None
        ):
            raise SETTValidationError(
                "occurred_at_claimed must be timezone-aware or None."
            )
        occurred_at_claimed = occurred_at_claimed.astimezone(timezone.utc)
    if idempotency_key_digest is not None and (
        not isinstance(idempotency_key_digest, str)
        or _LOWER_SHA256.fullmatch(idempotency_key_digest) is None
    ):
        raise SETTValidationError(
            "idempotency_key_digest must be lowercase SHA-256 hexadecimal."
        )
    if basis is BoundaryDispositionBasis.ABANDONED:
        if claimed_outcome is not None or occurred_at_claimed is not None:
            raise SETTValidationError(
                "ABANDONED cannot claim an outcome or occurrence time."
            )
    elif claimed_outcome is None:
        raise SETTValidationError(
            "This disposition basis requires a claimed_outcome."
        )
    if basis in {
        BoundaryDispositionBasis.PROVIDER_RECONCILIATION,
        BoundaryDispositionBasis.OPERATOR_ATTESTATION,
    } and not evidence_refs:
        raise SETTValidationError(
            "This disposition basis requires at least one evidence reference."
        )
    if basis is BoundaryDispositionBasis.PROCESS_OBSERVATION:
        if (
            not isinstance(audit_gap_token, AuditGapToken)
            or audit_gap_token.boundary_event_id != boundary.start_event_id
        ):
            raise SETTValidationError(
                "PROCESS_OBSERVATION requires the matching SETT-issued token."
            )
    elif audit_gap_token is not None:
        raise SETTValidationError(
            "audit_gap_token is valid only for PROCESS_OBSERVATION."
        )
    if (
        basis is not BoundaryDispositionBasis.PROVIDER_RECONCILIATION
        and idempotency_key_digest is not None
    ):
        raise SETTValidationError(
            "idempotency_key_digest belongs only to provider reconciliation."
        )
    return occurred_at_claimed


def record_boundary_disposition(
    report: RecoveryReport,
    boundary: UnresolvedBoundary,
    *,
    evidence_stream: EvidenceStream,
    namespace_id: str,
    evidence_durability: DurabilityLevel,
    basis: BoundaryDispositionBasis,
    claimed_outcome: BoundaryClaimedOutcome | None = None,
    occurred_at_claimed: datetime | None = None,
    evidence_refs: Iterable[str] = (),
    idempotency_key_digest: str | None = None,
    supersedes_event_id: str | None = None,
    audit_gap_token: AuditGapToken | None = None,
) -> BoundaryDispositionReceipt:
    """Append and confirm a disposition without closing its boundary."""
    if not isinstance(report, RecoveryReport) or not report.chain_integrity:
        raise SETTConfigurationError(
            "A disposition requires a valid RecoveryReport."
        )
    trace = _owning_trace(report, boundary)
    references = _validated_references(evidence_refs)
    occurred_at_claimed = _validate_basis_contract(
        boundary=boundary,
        basis=basis,
        claimed_outcome=claimed_outcome,
        occurred_at_claimed=occurred_at_claimed,
        evidence_refs=references,
        idempotency_key_digest=idempotency_key_digest,
        audit_gap_token=audit_gap_token,
    )
    if supersedes_event_id is not None and (
        not isinstance(supersedes_event_id, str)
        or supersedes_event_id
        not in {
            disposition.event_id for disposition in boundary.active_dispositions
        }
    ):
        raise SETTValidationError(
            "supersedes_event_id must name an active disposition on this boundary."
        )
    start = next(
        (
            event
            for event in report.events
            if event.event_id == boundary.start_event_id
            and event.trace_id == trace.trace_id
            and event.run_id == boundary.run_id
        ),
        None,
    )
    if start is None:
        raise SETTValidationError(
            "The boundary start event is absent from the recovery prefix."
        )
    recorder = TraceRecorder.resume(
        report,
        evidence_stream=evidence_stream,
        namespace_id=namespace_id,
        evidence_durability=evidence_durability,
    )
    context = ExecutionContext(
        trace_id=start.trace_id,
        run_id=start.run_id,
        parent_id=start.parent_id,
    )
    event = recorder.record(
        context,
        kind="recovery.boundary_disposition_recorded",
        component_type="recovery",
        component_name="boundary_disposition",
        status="recorded",
        cause_id=boundary.start_event_id,
        reason_codes=("recovery.boundary_disposition",),
        attributes={
            "basis": basis.value,
            "claimed_outcome": (
                None if claimed_outcome is None else claimed_outcome.value
            ),
            "occurred_at_claimed": (
                None
                if occurred_at_claimed is None
                else occurred_at_claimed.isoformat()
            ),
            "evidence_refs": references,
            "idempotency_key_digest": idempotency_key_digest,
            "supersedes_event_id": supersedes_event_id,
        },
    )
    position = recorder.durable_position
    if position is None:
        raise SETTPersistenceError(
            "The disposition event has no durable stream position.",
            reason_code="recovery.disposition_position_missing",
        )
    durability = evidence_stream.flush(through=position)
    if not satisfies_durability(
        durability.achieved_durability, evidence_durability
    ):
        raise SETTPersistenceError(
            "The disposition did not reach required durability.",
            reason_code="recovery.disposition_durability_unsatisfied",
            backend_id=durability.backend_id,
        )
    return BoundaryDispositionReceipt(
        event_id=event.event_id,
        position=position,
        achieved_durability=durability.achieved_durability,
        recorded_at=event.timestamp,
    )
