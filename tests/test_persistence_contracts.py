"""Contract behavior for the in-memory persistence reference backend."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from sett.exceptions import (
    SETTConfigurationError,
    SETTIdempotencyConflictError,
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistencePositionError,
)
from sett.persistence import (
    DurabilityLevel,
    InMemoryEvidenceStream,
    InMemoryStateRecordStore,
    PersistenceBackend,
    StreamPosition,
    satisfies_durability,
    validate_backend_declarations,
)
from sett.testing import (
    assert_evidence_stream_contract,
    assert_state_record_store_contract,
)


def test_unknown_is_outside_the_durability_order() -> None:
    assert satisfies_durability(DurabilityLevel.NODE_LOSS, DurabilityLevel.IN_MEMORY)
    assert not satisfies_durability(
        DurabilityLevel.UNKNOWN, DurabilityLevel.IN_MEMORY
    )
    assert not satisfies_durability(
        DurabilityLevel.NODE_LOSS, DurabilityLevel.UNKNOWN
    )


def test_public_persistence_exceptions_have_stable_reexports() -> None:
    import sett
    from sett import persistence

    for name in (
        "SETTAuditClosureError",
        "SETTPersistenceContinuityError",
        "SETTPersistenceError",
        "SETTPersistenceFormatError",
        "SETTPersistencePositionError",
    ):
        assert name in sett.__all__
        if name != "SETTAuditClosureError":
            assert getattr(persistence, name) is getattr(sett, name)


def test_in_memory_persistence_components_are_thread_safe() -> None:
    stream = InMemoryEvidenceStream(
        backend_id="backend.concurrent",
        stream_id="main",
    )
    store = InMemoryStateRecordStore(backend_id="backend.concurrent")

    def write_batch(worker: int) -> None:
        for index in range(100):
            write_id = f"worker-{worker}-item-{index}"
            payload = write_id.encode()
            while True:
                try:
                    stream.append(
                        write_id=write_id,
                        payload=payload,
                        expected_head=stream.head(),
                    )
                    break
                except SETTPersistenceContinuityError:
                    continue
            store.put(write_id=write_id, payload=payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(write_batch, range(8)))

    entries = tuple(stream.read())
    assert len(entries) == 800
    assert len({entry.write_id for entry in entries}) == 800
    assert all(
        store.lookup(write_id=f"worker-{worker}-item-{index}") is not None
        for worker in range(8)
        for index in range(100)
    )


def test_public_contract_kit_accepts_in_memory_references() -> None:
    assert_evidence_stream_contract(
        InMemoryEvidenceStream(
            backend_id="backend.contract",
            stream_id="contract",
        )
    )
    assert_state_record_store_contract(
        InMemoryStateRecordStore(backend_id="backend.contract")
    )


def test_stream_replay_preserves_identity_after_head_advances() -> None:
    stream = InMemoryEvidenceStream(backend_id="backend.demo", stream_id="main")
    genesis = stream.head()
    first = stream.append(write_id="write-1", payload=b"one", expected_head=genesis)
    second = stream.append(
        write_id="write-2", payload=b"two", expected_head=first.position
    )
    replay = stream.append(
        write_id="write-1", payload=b"one", expected_head=genesis
    )

    assert replay.acquired is False
    assert replay.position == first.position
    assert replay.accepted_at == first.accepted_at
    assert stream.head() == second.position
    assert stream.read(after=genesis) == stream.read()
    assert tuple(entry.payload for entry in stream.read(after=first.position)) == (
        b"two",
    )


def test_stream_rejects_changed_replay_and_stale_new_append() -> None:
    stream = InMemoryEvidenceStream(backend_id="backend.demo", stream_id="main")
    genesis = stream.head()
    stream.append(write_id="write-1", payload=b"one", expected_head=genesis)

    with pytest.raises(SETTIdempotencyConflictError):
        stream.append(write_id="write-1", payload=b"changed", expected_head=genesis)
    with pytest.raises(SETTPersistenceContinuityError) as continuity:
        stream.append(write_id="write-2", payload=b"two", expected_head=genesis)
    assert continuity.value.reason_code == "persistence.stream_continuity_conflict"
    assert tuple(entry.write_id for entry in stream.read()) == ("write-1",)


def test_stream_rejects_foreign_and_unknown_positions() -> None:
    stream = InMemoryEvidenceStream(backend_id="backend.demo", stream_id="main")
    foreign = StreamPosition("backend.other", "main", "token")
    with pytest.raises(SETTPersistencePositionError) as backend_error:
        stream.read(after=foreign)
    assert backend_error.value.reason_code == "persistence.position_backend_mismatch"

    unknown = replace(stream.head(), token="unknown")
    with pytest.raises(SETTPersistencePositionError) as unknown_error:
        stream.flush(through=unknown)
    assert unknown_error.value.reason_code == "persistence.position_unknown"


def test_state_store_put_lookup_get_and_conflict() -> None:
    store = InMemoryStateRecordStore(backend_id="backend.demo")
    receipt = store.put(write_id="write-1", payload=b"state")
    replay = store.put(write_id="write-1", payload=b"state")

    assert receipt.acquired is True
    assert replay.acquired is False
    assert replay.state_ref == receipt.state_ref
    assert store.lookup(write_id="write-1") == replay
    assert store.get(state_ref=receipt.state_ref).payload == b"state"
    with pytest.raises(SETTIdempotencyConflictError):
        store.put(write_id="write-1", payload=b"different")
    with pytest.raises(SETTPersistenceError) as missing:
        store.get(state_ref="missing")
    assert missing.value.reason_code == "state.record_missing"


def test_in_memory_backend_fails_policy_preflight_above_its_ceiling() -> None:
    evidence = InMemoryEvidenceStream(backend_id="backend.demo", stream_id="main")
    state = InMemoryStateRecordStore(backend_id="backend.demo")
    backend = PersistenceBackend(
        namespace_id="demo.namespace", evidence=evidence, state=state
    )

    validate_backend_declarations(
        backend,
        snapshot_durability=DurabilityLevel.IN_MEMORY,
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    with pytest.raises(SETTConfigurationError):
        validate_backend_declarations(
            backend,
            snapshot_durability=DurabilityLevel.POWER_LOSS,
            evidence_durability=DurabilityLevel.PROCESS_CRASH,
        )
