"""Append-only dispositions explain open boundaries without closing them."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sett.audit_ruler import trace as trace_module
from sett.audit_ruler.closure import AuditClosure, AuditGapToken
from sett.audit_ruler.recovery import record_boundary_disposition
from sett.audit_ruler.trace import (
    BoundaryClaimedOutcome,
    BoundaryDispositionBasis,
    BoundaryDispositionStatus,
    TraceRecorder,
)
from sett.core_ruler.execution_context import ExecutionContext
from sett.exceptions import SETTConfigurationError, SETTValidationError
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream


def _open_pipeline():
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo",
        stream_id="evidence.demo",
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = ExecutionContext()
    started = recorder.record(
        context,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="demo_pipeline",
        status="started",
        attributes={"stage_count": 1, "write_id_version": 1},
    )
    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    return stream, context, started, report, report.traces[0].unresolved_boundaries[0]


def _restore(stream):
    return TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )


def _record(report, boundary, stream, **arguments):
    return record_boundary_disposition(
        report,
        boundary,
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
        **arguments,
    )


def test_abandoned_disposition_explains_but_never_closes_boundary() -> None:
    stream, context, started, report, boundary = _open_pipeline()

    receipt = _record(
        report,
        boundary,
        stream,
        basis=BoundaryDispositionBasis.ABANDONED,
    )
    recovered = _restore(stream)
    recovered_boundary = recovered.traces[0].unresolved_boundaries[0]

    assert receipt.position == recovered.last_durable_position
    assert recovered.chain_integrity
    assert not recovered.traces[0].trace_completeness
    assert not recovered.traces[0].run_completeness
    assert not recovered.requires_reconciliation
    assert (
        recovered_boundary.disposition_status
        is BoundaryDispositionStatus.DOCUMENTED
    )
    disposition = recovered_boundary.dispositions[0]
    assert disposition.basis is BoundaryDispositionBasis.ABANDONED
    event = next(
        event for event in recovered.events if event.event_id == receipt.event_id
    )
    assert event.cause_id == started.event_id
    assert "start_event_id" not in event.attributes
    assert "recorded_at" not in event.attributes

    resumed = TraceRecorder.resume(
        recovered,
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    assert not resumed.verify(context.trace_id)


def test_disposition_kind_is_never_a_terminal_kind() -> None:
    terminal_kinds = {
        kind
        for kinds in trace_module._TERMINAL_BY_START.values()
        for kind in kinds
    }
    assert "recovery.boundary_disposition_recorded" not in terminal_kinds


def test_process_observation_requires_the_matching_sett_token() -> None:
    stream, _, _, report, boundary = _open_pipeline()
    closure = AuditClosure.unconfirmed(
        boundary_event_id=boundary.start_event_id,
    )

    with pytest.raises(SETTValidationError, match="token"):
        _record(
            report,
            boundary,
            stream,
            basis=BoundaryDispositionBasis.PROCESS_OBSERVATION,
            claimed_outcome=BoundaryClaimedOutcome.SUCCESS,
        )
    with pytest.raises(SETTValidationError, match="issued by SETT"):
        AuditGapToken("fake", boundary.start_event_id)
    with pytest.raises(AttributeError, match="immutable"):
        closure.gap_token._boundary_event_id = "different"

    _record(
        report,
        boundary,
        stream,
        basis=BoundaryDispositionBasis.PROCESS_OBSERVATION,
        claimed_outcome=BoundaryClaimedOutcome.SUCCESS,
        occurred_at_claimed=datetime.now(timezone.utc),
        audit_gap_token=closure.gap_token,
    )
    recovered = _restore(stream)
    assert not recovered.requires_reconciliation


def test_provider_reconciliation_records_refs_and_only_a_key_digest() -> None:
    stream, _, _, report, boundary = _open_pipeline()
    digest = "ab" * 32

    _record(
        report,
        boundary,
        stream,
        basis=BoundaryDispositionBasis.PROVIDER_RECONCILIATION,
        claimed_outcome=BoundaryClaimedOutcome.SUCCESS,
        evidence_refs=("provider.receipt:123",),
        idempotency_key_digest=digest,
    )
    disposition = _restore(stream).traces[0].unresolved_boundaries[0].dispositions[0]
    assert disposition.evidence_refs == ("provider.receipt:123",)
    assert disposition.idempotency_key_digest == digest


def test_supersession_is_linear_and_a_stale_report_is_rejected() -> None:
    stream, _, _, initial, boundary = _open_pipeline()
    first = _record(
        initial,
        boundary,
        stream,
        basis=BoundaryDispositionBasis.OPERATOR_ATTESTATION,
        claimed_outcome=BoundaryClaimedOutcome.FAILED,
        evidence_refs=("case:123",),
    )
    with pytest.raises(SETTConfigurationError, match="advanced"):
        _record(
            initial,
            boundary,
            stream,
            basis=BoundaryDispositionBasis.ABANDONED,
        )

    second_report = _restore(stream)
    second_boundary = second_report.traces[0].unresolved_boundaries[0]
    second = _record(
        second_report,
        second_boundary,
        stream,
        basis=BoundaryDispositionBasis.PROVIDER_RECONCILIATION,
        claimed_outcome=BoundaryClaimedOutcome.SUCCESS,
        evidence_refs=("provider.receipt:456",),
        supersedes_event_id=first.event_id,
    )
    final = _restore(stream)
    final_boundary = final.traces[0].unresolved_boundaries[0]
    assert tuple(
        disposition.event_id for disposition in final_boundary.active_dispositions
    ) == (second.event_id,)
    assert final_boundary.disposition_status is BoundaryDispositionStatus.DOCUMENTED


def test_two_unsuperseded_dispositions_are_reported_as_conflict() -> None:
    stream, _, _, first_report, first_boundary = _open_pipeline()
    _record(
        first_report,
        first_boundary,
        stream,
        basis=BoundaryDispositionBasis.ABANDONED,
    )
    second_report = _restore(stream)
    second_boundary = second_report.traces[0].unresolved_boundaries[0]
    _record(
        second_report,
        second_boundary,
        stream,
        basis=BoundaryDispositionBasis.OPERATOR_ATTESTATION,
        claimed_outcome=BoundaryClaimedOutcome.FAILED,
        evidence_refs=("case:456",),
    )

    final = _restore(stream)
    boundary = final.traces[0].unresolved_boundaries[0]
    assert boundary.disposition_status is BoundaryDispositionStatus.CONFLICT
    assert final.requires_reconciliation


def test_invalid_basis_fields_fail_before_appending() -> None:
    stream, _, _, report, boundary = _open_pipeline()
    initial_count = len(stream.read())

    with pytest.raises(SETTValidationError, match="ABANDONED"):
        _record(
            report,
            boundary,
            stream,
            basis=BoundaryDispositionBasis.ABANDONED,
            claimed_outcome=BoundaryClaimedOutcome.SUCCESS,
        )
    with pytest.raises(SETTValidationError, match="non-sensitive"):
        _record(
            report,
            boundary,
            stream,
            basis=BoundaryDispositionBasis.OPERATOR_ATTESTATION,
            claimed_outcome=BoundaryClaimedOutcome.SUCCESS,
            evidence_refs=("api-token:secret",),
        )

    assert len(stream.read()) == initial_count
