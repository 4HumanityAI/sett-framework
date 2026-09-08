"""Durable TraceRecorder behavior without changing the in-memory default."""
from __future__ import annotations

import pytest

from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.execution_context import ExecutionContext
from sett.exceptions import (
    SETTConfigurationError,
    SETTPersistenceError,
    SETTValidationError,
)
from sett.persistence import (
    DurabilityLevel,
    InMemoryEvidenceStream,
    StreamEntry,
    decode_trace_evidence_frame,
)


def _context() -> ExecutionContext:
    return ExecutionContext()


def test_default_recorder_remains_in_memory_and_verifiable() -> None:
    recorder = TraceRecorder()
    context = _context()
    recorder.record(
        context,
        kind="trace.started",
        component_type="trace",
        component_name="root",
        status="started",
    )
    recorder.record(
        context,
        kind="trace.completed",
        component_type="trace",
        component_name="root",
        status="completed",
    )
    assert recorder.verify(context.trace_id)


def test_durable_recorder_appends_exact_frames_and_flushes_terminal_anchor() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = _context()
    start = recorder.record(
        context,
        kind="trace.started",
        component_type="trace",
        component_name="root",
        status="started",
    )
    terminal = recorder.record(
        context,
        kind="trace.completed",
        component_type="trace",
        component_name="root",
        status="completed",
        cause_id=start.event_id,
    )

    entries = stream.read()
    assert len(entries) == 2
    assert entries[0].write_id != entries[1].write_id
    first_frame = decode_trace_evidence_frame(entries[0].payload)
    second_frame = decode_trace_evidence_frame(entries[1].payload)
    assert first_frame.payload["event_id"] == start.event_id
    assert second_frame.payload["event_id"] == terminal.event_id
    assert first_frame.entry_hash.hex() == start.entry_hash
    assert recorder.verify(context.trace_id)


class _FailingAnchorFlushStream(InMemoryEvidenceStream):
    def __init__(self, *, backend_id: str, stream_id: str) -> None:
        super().__init__(backend_id=backend_id, stream_id=stream_id)
        self._fail_next_flush = False

    def append(self, *, write_id: str, payload: bytes, expected_head):
        receipt = super().append(
            write_id=write_id,
            payload=payload,
            expected_head=expected_head,
        )
        frame = decode_trace_evidence_frame(payload)
        if frame.payload["kind"] == "pipeline.started":
            self._fail_next_flush = True
        return receipt

    def flush(self, *, through):
        if self._fail_next_flush:
            self._fail_next_flush = False
            raise SETTPersistenceError(
                "The anchor confirmation was lost.",
                reason_code="audit.evidence_write_failed",
                backend_id="backend.demo",
            )
        return super().flush(through=through)


def test_accepted_anchor_requires_recovery_when_flush_fails() -> None:
    stream = _FailingAnchorFlushStream(
        backend_id="backend.demo",
        stream_id="evidence.main",
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = _context()

    with pytest.raises(SETTPersistenceError):
        recorder.record(
            context,
            kind="pipeline.started",
            component_type="pipeline",
            component_name="demo",
            status="started",
            attributes={"stage_count": 1},
        )

    assert recorder.get_trace(context.trace_id) == ()
    with pytest.raises(SETTPersistenceError) as blocked:
        recorder.record(
            context,
            kind="route.selected",
            component_type="route",
            component_name="demo",
            status="completed",
        )
    assert blocked.value.reason_code == "audit.evidence_recovery_required"

    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    resumed = TraceRecorder.resume(
        report,
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    following = resumed.record(
        context,
        kind="route.selected",
        component_type="route",
        component_name="demo",
        status="completed",
    )
    refreshed = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )

    assert following.sequence == 2
    assert following.previous_hash == report.last_durable_hash
    assert report.chain_integrity
    assert report.last_durable_sequence == 1
    assert refreshed.chain_integrity
    assert refreshed.last_durable_sequence == 2


class _UnknownAppendOutcomeStream(InMemoryEvidenceStream):
    def __init__(self, *, backend_id: str, stream_id: str) -> None:
        super().__init__(backend_id=backend_id, stream_id=stream_id)
        self._reject_next_append = True

    def append(self, *, write_id: str, payload: bytes, expected_head):
        if self._reject_next_append:
            self._reject_next_append = False
            raise SETTPersistenceError(
                "The append outcome is unknown.",
                reason_code="audit.evidence_write_failed",
                backend_id="backend.demo",
            )
        return super().append(
            write_id=write_id,
            payload=payload,
            expected_head=expected_head,
        )


def test_append_without_receipt_requires_recovery_before_more_events() -> None:
    stream = _UnknownAppendOutcomeStream(
        backend_id="backend.demo",
        stream_id="evidence.main",
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = _context()

    with pytest.raises(SETTPersistenceError):
        recorder.record(
            context,
            kind="route.selected",
            component_type="route",
            component_name="demo",
            status="completed",
        )
    with pytest.raises(SETTPersistenceError) as blocked:
        recorder.record(
            context,
            kind="route.selected",
            component_type="route",
            component_name="demo",
            status="completed",
        )

    assert blocked.value.reason_code == "audit.evidence_recovery_required"
    assert stream.read() == ()
    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    resumed = TraceRecorder.resume(
        report,
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    event = resumed.record(
        context,
        kind="route.selected",
        component_type="route",
        component_name="demo",
        status="completed",
    )
    assert event.sequence == 1


def test_durable_recorder_requires_explicit_satisfiable_configuration() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    with pytest.raises(SETTConfigurationError):
        TraceRecorder(evidence_stream=stream)
    with pytest.raises(SETTConfigurationError):
        TraceRecorder(
            evidence_stream=stream,
            namespace_id="demo.namespace",
            evidence_durability=DurabilityLevel.POWER_LOSS,
        )


def test_nonempty_stream_requires_explicit_recovery() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    stream.append(
        write_id="existing",
        payload=b"existing",
        expected_head=stream.head(),
    )
    with pytest.raises(SETTConfigurationError, match="recovery"):
        TraceRecorder(
            evidence_stream=stream,
            namespace_id="demo.namespace",
            evidence_durability=DurabilityLevel.IN_MEMORY,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "k" * 127),
        ("component_type", "c" * 127),
        ("component_name", "n" * 1023),
        ("status", "s" * 63),
    ],
)
def test_trace_fields_fail_before_opening_a_boundary(field: str, value: str) -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    arguments = {
        "kind": "trace.started",
        "component_type": "trace",
        "component_name": "root",
        "status": "started",
    }
    arguments[field] = value
    with pytest.raises(SETTValidationError):
        recorder.record(_context(), **arguments)
    assert recorder.event_count == 0


def test_reason_code_cardinality_is_bounded() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    with pytest.raises(SETTValidationError):
        recorder.record(
            _context(),
            kind="trace.started",
            component_type="trace",
            component_name="root",
            status="started",
            reason_codes=tuple(f"reason.{index}" for index in range(33)),
        )
    assert recorder.event_count == 0


def test_ephemeral_recorder_preserves_the_previous_unbounded_vocabulary() -> None:
    recorder = TraceRecorder()
    context = _context()
    component_name = "n" * 40_000
    reasons = tuple(f"reason.{index}" for index in range(33))
    recorder.record(
        context,
        kind="trace.started",
        component_type="trace",
        component_name=component_name,
        status="started",
        reason_codes=reasons,
    )
    recorder.record(
        context,
        kind="trace.completed",
        component_type="trace",
        component_name=component_name,
        status="completed",
        reason_codes=reasons,
    )

    assert recorder.verify(context.trace_id)


def test_invalid_terminal_metadata_uses_an_infallible_durable_fallback() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = _context()
    authorized = recorder.record(
        context,
        kind="handler.authorized",
        component_type="handler",
        component_name="demo",
        status="authorized",
    )
    started = recorder.record(
        context,
        kind="handler.started",
        component_type="handler",
        component_name="demo",
        status="started",
        cause_id=authorized.event_id,
    )

    terminal = recorder.record(
        context,
        kind="handler.cancelled",
        component_type="rejected-" + ("c" * 2_000),
        component_name="rejected-" + ("n" * 2_000),
        status="rejected-" + ("s" * 2_000),
        reason_codes=tuple(f"reason.{index}" for index in range(33)),
        attributes={"discarded": "x" * 17_000},
    )

    assert terminal.kind == "handler.cancelled"
    assert terminal.component_type == "audit"
    assert terminal.component_name == "terminal_metadata_fallback"
    assert terminal.status == "metadata_rejected"
    assert terminal.reason_codes == ("audit.terminal_metadata_rejected",)
    assert terminal.cause_id == started.event_id
    assert terminal.attributes == {
        "discarded_attribute_count": 1,
        "discarded_reason_code_count": 33,
    }
    assert recorder.verify(context.trace_id)

    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    recovered = report.traces[0]
    assert recovered.trace_completeness
    assert not recovered.terminal_metadata_complete
    assert recovered.terminal_metadata_fallback_event_ids == (terminal.event_id,)
    assert "audit.terminal_metadata_rejected" in report.diagnostics


def test_restore_is_pure_and_separates_integrity_from_completeness() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    pipeline = _context()
    stage = pipeline.derive()
    recorder.record(
        pipeline,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="demo",
        status="started",
        attributes={"stage_count": 1},
    )
    started = recorder.record(
        stage,
        kind="pipeline.stage_started",
        component_type="pipeline_stage",
        component_name="stage-0",
        status="started",
        attributes={"index": 0},
    )
    before = stream.read()

    first = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    second = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )

    assert stream.read() == before
    assert first == second
    assert first.chain_integrity
    assert first.last_durable_sequence == 2
    assert first.last_durable_hash == started.entry_hash
    assert first.observed_evidence_schemas == ("sett.trace-json.v1",)
    assert first.requires_reconciliation
    recovered = first.traces[0]
    assert not recovered.trace_completeness
    assert not recovered.run_completeness
    assert recovered.reconstructed_status is None
    assert recovered.completed_units == 0
    assert {boundary.start_kind for boundary in recovered.unresolved_boundaries} == {
        "pipeline.started",
        "pipeline.stage_started",
    }


def test_all_durable_stages_reconstruct_success_without_closing_boundary() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    pipeline = _context()
    stage = pipeline.derive()
    recorder.record(
        pipeline,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="demo",
        status="started",
        attributes={"stage_count": 1},
    )
    start = recorder.record(
        stage,
        kind="pipeline.stage_started",
        component_type="pipeline_stage",
        component_name="stage-0",
        status="started",
        attributes={"index": 0},
    )
    recorder.record(
        stage,
        kind="pipeline.stage_completed",
        component_type="pipeline_stage",
        component_name="stage-0",
        status="completed",
        cause_id=start.event_id,
        attributes={"index": 0},
    )

    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    recovered = report.traces[0]
    assert recovered.reconstructed_status.value == "success"
    assert recovered.reconstruction_reason == "recovery.all_stages_completed"
    assert recovered.completed_units == 1
    assert recovered.stages[0].descriptor is None
    assert (
        recovered.stages[0].descriptor_reason
        == "recovery.stage_state_not_recorded"
    )
    assert not recovered.trace_completeness
    assert recovered.requires_reconciliation
    assert tuple(
        boundary.start_kind for boundary in recovered.unresolved_boundaries
    ) == ("pipeline.started",)


def test_resume_continues_from_verified_head_without_replaying_local_events() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    pipeline = _context()
    start = recorder.record(
        pipeline,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="demo",
        status="started",
        attributes={"stage_count": 0},
    )
    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    resumed = TraceRecorder.resume(
        report,
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    terminal = resumed.record(
        pipeline,
        kind="pipeline.completed",
        component_type="pipeline",
        component_name="demo",
        status="completed",
        cause_id=start.event_id,
    )

    assert terminal.sequence == 2
    assert terminal.previous_hash == report.last_durable_hash
    assert resumed.verify(pipeline.trace_id)
    assert len(stream.read()) == 2


class _CorruptingEvidenceStream:
    def __init__(self, wrapped: InMemoryEvidenceStream) -> None:
        self._wrapped = wrapped

    def head(self):
        return self._wrapped.head()

    def append(self, **kwargs):
        return self._wrapped.append(**kwargs)

    def read(self, *, after=None):
        entries = list(self._wrapped.read(after=after))
        if entries:
            final = entries[-1]
            entries[-1] = StreamEntry(
                write_id=final.write_id,
                position=final.position,
                payload=final.payload[:-1] + bytes([final.payload[-1] ^ 1]),
            )
        return tuple(entries)

    def flush(self, *, through):
        return self._wrapped.flush(through=through)

    def durability(self):
        return self._wrapped.durability()

    def health(self):
        return self._wrapped.health()


def test_corrupt_tail_reports_verified_prefix_but_cannot_resume() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = _context()
    first = recorder.record(
        context,
        kind="trace.started",
        component_type="trace",
        component_name="root",
        status="started",
    )
    recorder.record(
        context,
        kind="trace.completed",
        component_type="trace",
        component_name="root",
        status="completed",
        cause_id=first.event_id,
    )
    corrupting = _CorruptingEvidenceStream(stream)

    report = TraceRecorder.restore(
        evidence_stream=corrupting,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    assert not report.chain_integrity
    assert report.last_durable_sequence == 1
    assert report.last_durable_hash == first.entry_hash
    assert "evidence.hash_mismatch" in report.diagnostics
    with pytest.raises(SETTConfigurationError, match="chain integrity"):
        TraceRecorder.resume(
            report,
            evidence_stream=corrupting,
            namespace_id="demo.namespace",
            evidence_durability=DurabilityLevel.IN_MEMORY,
        )


def test_recovery_reports_each_trace_without_global_completeness_contamination() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    closed = _context()
    open_pipeline = _context()
    closed_start = recorder.record(
        closed,
        kind="trace.started",
        component_type="trace",
        component_name="closed",
        status="started",
    )
    recorder.record(
        open_pipeline,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="open",
        status="started",
        attributes={"stage_count": 1},
    )
    recorder.record(
        closed,
        kind="trace.completed",
        component_type="trace",
        component_name="closed",
        status="completed",
        cause_id=closed_start.event_id,
    )

    assert not recorder.verify(closed.trace_id)
    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="demo.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    by_id = {trace.trace_id: trace for trace in report.traces}
    assert report.chain_integrity
    assert by_id[closed.trace_id].trace_completeness
    assert not by_id[closed.trace_id].requires_reconciliation
    assert not by_id[open_pipeline.trace_id].trace_completeness
    assert by_id[open_pipeline.trace_id].requires_reconciliation


def test_recovery_rejects_evidence_copied_under_another_namespace() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.demo", stream_id="evidence.main"
    )
    recorder = TraceRecorder(
        evidence_stream=stream,
        namespace_id="source.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = _context()
    recorder.record(
        context,
        kind="pipeline.started",
        component_type="pipeline",
        component_name="demo",
        status="started",
        attributes={"stage_count": 0},
    )

    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id="destination.namespace",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    assert not report.chain_integrity
    assert report.events == ()
    assert report.diagnostics == (
        "audit.evidence_write_identity_mismatch",
        "audit.chain_integrity_failed",
    )
