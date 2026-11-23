"""Trace roots and recovery reports preserve valid evidence and failures."""
from dataclasses import replace

import pytest

from sett import Action, EthicalFilter, SETTExecutor, SETTOrchestrator
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.execution_context import ExecutionContext
from sett.exceptions import SETTConfigurationError
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryEvidenceStream,
    PersistencePolicy,
)
from sett.persistence.sqlite import SQLitePersistenceBackend


@pytest.mark.parametrize("method", ["submit", "submit_controlled"])
def test_fresh_execution_roots_do_not_poison_following_durable_traces(tmp_path, method):
    path = tmp_path / "evidence.sqlite3"
    backend = SQLitePersistenceBackend.open(path, namespace_id="test", backend_id="test")
    level = backend.evidence.durability().maximum_durability
    policy = PersistencePolicy(
        snapshot_durability=level, evidence_durability=level,
        integrity_policy=Sha256IntegrityPolicy(),
        audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )
    orch = SETTOrchestrator(
        ethical_filter=EthicalFilter(), persistence_backend=backend,
        persistence_policy=policy,
    )
    executor, effects = SETTExecutor(), []
    executor.register_handler("test.effect", lambda p: effects.append(p["number"]))
    orch.register_executor(executor)
    roots = [ExecutionContext.create() for _ in range(3)]
    for number, root in enumerate(roots):
        result = getattr(executor, method)(
            Action("test.effect", {"number": number}), execution_context=root,
        )
        if method == "submit_controlled":
            assert result.status.value == "success"
    assert effects == [0, 1, 2]
    assert orch.verify_traces()
    before = backend.evidence.read()
    backend.close()
    reopened = SQLitePersistenceBackend.open(path, namespace_id="test", backend_id="test")
    try:
        report = TraceRecorder.restore(
            evidence_stream=reopened.evidence, namespace_id="test", evidence_durability=level,
        )
        assert report.chain_integrity
        assert not report.requires_reconciliation
        assert len(report.events) == len(before)
        if method == "submit":
            assert {e.run_id for e in report.events if e.kind == "action.proposed"} == {
                root.run_id for root in roots
            }
        else:
            # Each controlled attempt has its own child context. Its parent
            # is the caller's recorded root, not an invented intermediate run.
            proposed = [e for e in report.events if e.kind == "action.proposed"]
            assert len(proposed) == 3
            assert {e.parent_id for e in proposed} == {root.run_id for root in roots}
            assert {root.run_id for root in roots} <= {e.run_id for e in report.events}
        TraceRecorder.resume(
            report, evidence_stream=reopened.evidence,
            namespace_id="test", evidence_durability=level,
        )
        assert effects == [0, 1, 2]
        assert reopened.evidence.read() == before
        assert replace(report, chain_integrity=False).requires_reconciliation
    finally:
        reopened.close()


@pytest.mark.parametrize("level", ["in_memory", None, 0])
def test_trace_recorder_and_restore_require_durability_enum(level):
    stream = InMemoryEvidenceStream(backend_id="test", stream_id="test")
    for method in (TraceRecorder, TraceRecorder.restore):
        with pytest.raises(SETTConfigurationError):
            method(evidence_stream=stream, namespace_id="test", evidence_durability=level)
    TraceRecorder(
        evidence_stream=stream, namespace_id="test", evidence_durability=DurabilityLevel.IN_MEMORY,
    )
