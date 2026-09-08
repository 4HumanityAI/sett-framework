"""Restart and transaction tests for the SQLite reference backend."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.agent import SETTAgent
from sett.core_ruler.orchestrator import SETTOrchestrator
from sett.exceptions import (
    SETTConfigurationError,
    SETTIdempotencyConflictError,
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistencePositionError,
)
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    MaterializationStatus,
    PersistencePolicy,
    SQLitePersistenceBackend,
    StreamPosition,
    materialize_stage_output,
)
from sett.testing import (
    assert_evidence_stream_contract,
    assert_state_record_store_contract,
)


class _SQLitePipelineAgent(SETTAgent):
    def __init__(self) -> None:
        super().__init__(name="sqlite_agent", domain="demo")

    def process(self, input_data):
        return {"value": input_data["value"] + 1}


def _open(
    path: Path,
    *,
    journal_mode: str = "WAL",
    synchronous: str = "FULL",
) -> SQLitePersistenceBackend:
    return SQLitePersistenceBackend.open(
        path,
        namespace_id="demo.namespace",
        backend_id="backend.demo",
        journal_mode=journal_mode,
        synchronous=synchronous,
    )


def test_sqlite_evidence_and_state_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sett.db"
    with _open(path) as backend:
        genesis = backend.evidence.head()
        stream_receipt = backend.evidence.append(
            write_id="event-1", payload=b"evidence", expected_head=genesis
        )
        state_receipt = backend.state.put(write_id="state-1", payload=b"state")
        assert backend.evidence.flush(
            through=stream_receipt.position
        ).achieved_durability is DurabilityLevel.POWER_LOSS

    with _open(path) as reopened:
        assert reopened.evidence.head() == stream_receipt.position
        assert reopened.evidence.read()[0].payload == b"evidence"
        replay = reopened.evidence.append(
            write_id="event-1", payload=b"evidence", expected_head=genesis
        )
        assert replay.acquired is False
        assert replay.position == stream_receipt.position
        assert replay.accepted_at == stream_receipt.accepted_at
        lookup = reopened.state.lookup(write_id="state-1")
        assert lookup is not None
        assert lookup.state_ref == state_receipt.state_ref
        assert reopened.state.get(state_ref=lookup.state_ref).payload == b"state"


def test_sqlite_backend_runs_and_restores_a_controlled_pipeline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pipeline.db"
    policy = PersistencePolicy(
        snapshot_durability=DurabilityLevel.POWER_LOSS,
        evidence_durability=DurabilityLevel.POWER_LOSS,
        integrity_policy=Sha256IntegrityPolicy(),
        audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )
    with _open(path) as backend:
        orchestrator = SETTOrchestrator(
            persistence_backend=backend,
            persistence_policy=policy,
        )
        orchestrator.register_agent(_SQLitePipelineAgent())
        result = orchestrator.run_pipeline_controlled(
            ["demo"],
            input_data={"value": 1},
        )
        assert result.succeeded

    with _open(path) as reopened:
        report = TraceRecorder.restore(
            evidence_stream=reopened.evidence,
            namespace_id=reopened.namespace_id,
            evidence_durability=policy.evidence_durability,
        )
        recovered_trace = report.traces[0]
        descriptor = recovered_trace.stages[0].descriptor
        assert descriptor is not None
        materialized = materialize_stage_output(
            descriptor,
            store=reopened.state,
            namespace_id=reopened.namespace_id,
            trace_id=recovered_trace.trace_id,
            policies={policy.integrity_policy.policy_id: policy.integrity_policy},
        )
        assert materialized.materialization_status is (
            MaterializationStatus.MATERIALIZED
        )
        assert materialized.value == {"value": 2}


def test_public_contract_kit_accepts_sqlite_references(tmp_path: Path) -> None:
    with _open(tmp_path / "contract.db") as backend:
        assert_evidence_stream_contract(backend.evidence)
        assert_state_record_store_contract(backend.state)


def test_sqlite_conflicts_are_atomic(tmp_path: Path) -> None:
    with _open(tmp_path / "sett.db") as backend:
        genesis = backend.evidence.head()
        first = backend.evidence.append(
            write_id="event-1", payload=b"one", expected_head=genesis
        )
        with pytest.raises(SETTIdempotencyConflictError):
            backend.evidence.append(
                write_id="event-1", payload=b"different", expected_head=genesis
            )
        with pytest.raises(SETTPersistenceContinuityError):
            backend.evidence.append(
                write_id="event-2", payload=b"two", expected_head=genesis
            )
        assert backend.evidence.head() == first.position
        assert tuple(entry.write_id for entry in backend.evidence.read()) == (
            "event-1",
        )


def test_sqlite_declaration_uses_effective_pragma_readback(tmp_path: Path) -> None:
    with _open(tmp_path / "full.db", synchronous="FULL") as full:
        assert full.effective_synchronous == "FULL"
        assert full.evidence.durability().maximum_durability is (
            DurabilityLevel.POWER_LOSS
        )
    with _open(tmp_path / "off.db", synchronous="OFF") as off:
        assert off.effective_synchronous == "OFF"
        assert off.evidence.durability().maximum_durability is (
            DurabilityLevel.PROCESS_CRASH
        )
    with _open(
        tmp_path / "rollback-full.db",
        journal_mode="DELETE",
        synchronous="FULL",
    ) as rollback_full:
        assert rollback_full.effective_journal_mode == "DELETE"
        assert rollback_full.evidence.durability().maximum_durability is (
            DurabilityLevel.PROCESS_CRASH
        )
    with _open(
        tmp_path / "rollback-extra.db",
        journal_mode="DELETE",
        synchronous="EXTRA",
    ) as rollback_extra:
        assert rollback_extra.effective_journal_mode == "DELETE"
        assert rollback_extra.evidence.durability().maximum_durability is (
            DurabilityLevel.POWER_LOSS
        )


def test_sqlite_file_is_bound_to_one_backend_and_namespace_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sett.db"
    with _open(path):
        pass
    with pytest.raises(SETTConfigurationError):
        SQLitePersistenceBackend.open(
            path,
            namespace_id="demo.namespace",
            backend_id="backend.other",
        )
    with pytest.raises(SETTConfigurationError):
        SQLitePersistenceBackend.open(
            path,
            namespace_id="other.namespace",
            backend_id="backend.demo",
        )


def test_sqlite_backend_serializes_concurrent_stream_and_state_writes(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "concurrent.db") as backend:

        def write_batch(worker: int) -> None:
            for index in range(25):
                write_id = f"worker-{worker}-item-{index}"
                payload = write_id.encode()
                while True:
                    try:
                        backend.evidence.append(
                            write_id=write_id,
                            payload=payload,
                            expected_head=backend.evidence.head(),
                        )
                        break
                    except SETTPersistenceContinuityError:
                        continue
                backend.state.put(write_id=write_id, payload=payload)

        with ThreadPoolExecutor(max_workers=8) as pool:
            tuple(pool.map(write_batch, range(8)))

        entries = tuple(backend.evidence.read())
        assert len(entries) == 200
        assert len({entry.write_id for entry in entries}) == 200
        assert all(
            backend.state.lookup(
                write_id=f"worker-{worker}-item-{index}"
            )
            is not None
            for worker in range(8)
            for index in range(25)
        )


# ── M-01: native SQLite failures stay inside the persistence hierarchy ──
#
# Defensive and infrastructure audit of v0.14.0, finding M-01. Opening an
# unwritable path raised a bare ``sqlite3.OperationalError``, so an
# application catching ``SETTPersistenceError`` around the framework's own
# reference backend missed the failure entirely. Each test below states one
# half of the fix: infrastructure failures are translated, and the domain
# failures that were already structured are not swallowed by that
# translation.


def test_an_unopenable_path_is_reported_as_a_structured_persistence_error(
    tmp_path: Path,
) -> None:
    unopenable = tmp_path / "missing-directory" / "evidence.sqlite3"

    with pytest.raises(SETTPersistenceError) as raised:
        SQLitePersistenceBackend.open(
            unopenable,
            namespace_id="demo.namespace",
            backend_id="backend.demo",
        )

    assert raised.value.reason_code == "persistence.sqlite.open_failed"
    assert raised.value.backend_id == "backend.demo"
    assert isinstance(raised.value.__cause__, sqlite3.Error)


def test_the_public_message_does_not_carry_the_driver_text_or_the_path(
    tmp_path: Path,
) -> None:
    """The path is diagnostic, not public: it stays on ``__cause__``."""
    unopenable = tmp_path / "missing-directory" / "evidence.sqlite3"

    with pytest.raises(SETTPersistenceError) as raised:
        SQLitePersistenceBackend.open(
            unopenable,
            namespace_id="demo.namespace",
            backend_id="backend.demo",
        )

    message = str(raised.value)
    assert message == "SQLite persistence operation failed."
    assert "missing-directory" not in message
    assert "unable to open" not in message.lower()
    assert str(raised.value.__cause__) != ""


def test_a_failure_after_the_connection_closes_is_normalized_and_attributed(
    tmp_path: Path,
) -> None:
    """Every operation, not only ``open()``, reports inside the hierarchy."""
    backend = _open(tmp_path / "closed.db")
    backend.close()

    for operation, call in (
        ("head_read", lambda: backend.evidence.head()),
        ("read", lambda: backend.evidence.read()),
        ("state_lookup", lambda: backend.state.lookup(write_id="w-1")),
        ("state_read", lambda: backend.state.get(state_ref="state-1")),
    ):
        with pytest.raises(SETTPersistenceError) as raised:
            call()
        assert raised.value.reason_code == f"persistence.sqlite.{operation}_failed"
        assert raised.value.backend_id == "backend.demo"
        assert isinstance(raised.value.__cause__, sqlite3.Error)


def test_normalization_does_not_replace_the_structured_domain_failures(
    tmp_path: Path,
) -> None:
    """M-01 translates infrastructure errors; it must not flatten these."""
    with _open(tmp_path / "domain.db") as backend:
        head = backend.evidence.head()
        backend.evidence.append(write_id="w-1", payload=b"one", expected_head=head)

        with pytest.raises(SETTIdempotencyConflictError):
            backend.evidence.append(
                write_id="w-1", payload=b"different", expected_head=backend.evidence.head()
            )
        with pytest.raises(SETTPersistenceContinuityError):
            backend.evidence.append(write_id="w-2", payload=b"two", expected_head=head)
        with pytest.raises(SETTPersistencePositionError):
            backend.evidence.read(
                after=StreamPosition(
                    "backend.demo", "evidence.main", "9999999999999999"
                )
            )
        with pytest.raises(SETTIdempotencyConflictError):
            backend.state.put(write_id="w-1", payload=b"one")
            backend.state.put(write_id="w-1", payload=b"different")


def test_a_state_record_that_is_absent_keeps_its_existing_reason_code(
    tmp_path: Path,
) -> None:
    """``state.record_missing`` is a fact about content, not infrastructure."""
    with _open(tmp_path / "absent.db") as backend, pytest.raises(
        SETTPersistenceError
    ) as raised:
        backend.state.get(state_ref="state-absent")

    assert raised.value.reason_code == "state.record_missing"
    assert raised.value.__cause__ is None
