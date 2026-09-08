"""Dependency-free contract checks for third-party persistence backends.

The probes mutate the supplied component and therefore require a fresh,
empty test instance. They never inspect implementation internals and can run
under unittest, pytest, or a plain build script.
"""
from __future__ import annotations

from dataclasses import replace

from sett.exceptions import (
    SETTIdempotencyConflictError,
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistencePositionError,
)
from sett.persistence.contracts import (
    DurabilityDeclaration,
    DurabilityReceipt,
    EvidenceStream,
    PersistenceHealth,
    StateRecordReceipt,
    StateRecordStore,
    StreamReceipt,
    satisfies_durability,
)


def _expect(exception_type: type[BaseException], call, message: str) -> None:
    try:
        call()
    except exception_type:
        return
    raise AssertionError(message)


def assert_evidence_stream_contract(stream: EvidenceStream) -> None:
    """Exercise continuity, replay, positions, reads, and passive views."""
    if not isinstance(stream, EvidenceStream):
        raise AssertionError("component does not implement EvidenceStream")
    if tuple(stream.read()):
        raise AssertionError("evidence contract probe requires an empty stream")
    genesis = stream.head()
    if stream.head() != genesis:
        raise AssertionError("empty stream head is not stable")

    first = stream.append(
        write_id="sett.contract.evidence.1",
        payload=b"first",
        expected_head=genesis,
    )
    if not isinstance(first, StreamReceipt) or not first.acquired:
        raise AssertionError("first evidence append did not return an acquired receipt")
    if stream.head() != first.position:
        raise AssertionError("evidence head did not advance to the accepted position")
    replay = stream.append(
        write_id="sett.contract.evidence.1",
        payload=b"first",
        expected_head=genesis,
    )
    if (
        not isinstance(replay, StreamReceipt)
        or replay.acquired
        or replay.position != first.position
        or replay.backend_id != first.backend_id
        or replay.accepted_at != first.accepted_at
        or not satisfies_durability(
            replay.achieved_durability, first.achieved_durability
        )
    ):
        raise AssertionError("identical evidence replay did not preserve its receipt")
    _expect(
        SETTIdempotencyConflictError,
        lambda: stream.append(
            write_id="sett.contract.evidence.1",
            payload=b"changed",
            expected_head=genesis,
        ),
        "changed evidence replay did not raise SETTIdempotencyConflictError",
    )
    _expect(
        SETTPersistenceContinuityError,
        lambda: stream.append(
            write_id="sett.contract.evidence.stale",
            payload=b"stale",
            expected_head=genesis,
        ),
        "stale evidence append did not raise SETTPersistenceContinuityError",
    )

    second = stream.append(
        write_id="sett.contract.evidence.2",
        payload=b"second",
        expected_head=first.position,
    )
    entries = tuple(stream.read())
    if tuple(entry.payload for entry in entries) != (b"first", b"second"):
        raise AssertionError("evidence read did not preserve contiguous order")
    if tuple(stream.read(after=first.position)) != (entries[1],):
        raise AssertionError("evidence suffix read did not start after its position")

    foreign = replace(first.position, backend_id=f"{first.backend_id}.foreign")
    _expect(
        SETTPersistencePositionError,
        lambda: tuple(stream.read(after=foreign)),
        "foreign evidence position did not raise SETTPersistencePositionError",
    )
    foreign_stream = replace(
        first.position,
        stream_id=f"{first.position.stream_id}.foreign",
    )
    _expect(
        SETTPersistencePositionError,
        lambda: tuple(stream.read(after=foreign_stream)),
        "foreign stream position did not raise SETTPersistencePositionError",
    )
    unknown = replace(second.position, token=f"{second.position.token}.unknown")
    _expect(
        SETTPersistencePositionError,
        lambda: stream.flush(through=unknown),
        "unknown evidence position did not raise SETTPersistencePositionError",
    )
    flushed = stream.flush(through=second.position)
    if (
        not isinstance(flushed, DurabilityReceipt)
        or flushed.through != second.position
        or flushed.backend_id != second.backend_id
    ):
        raise AssertionError("evidence flush did not confirm its requested position")
    passive_head = stream.head()
    passive_entries = tuple(stream.read())
    declaration = stream.durability()
    if not isinstance(declaration, DurabilityDeclaration):
        raise AssertionError("evidence durability() returned the wrong type")
    if not isinstance(stream.health(), PersistenceHealth):
        raise AssertionError("evidence health() returned the wrong type")
    if stream.head() != passive_head or tuple(stream.read()) != passive_entries:
        raise AssertionError("evidence durability or health mutated the stream")
    for achieved in (
        first.achieved_durability,
        replay.achieved_durability,
        second.achieved_durability,
        flushed.achieved_durability,
    ):
        if not satisfies_durability(declaration.maximum_durability, achieved):
            raise AssertionError("evidence receipt exceeds declared durability")


def assert_state_record_store_contract(store: StateRecordStore) -> None:
    """Exercise exact-byte replay, random access, and passive views."""
    if not isinstance(store, StateRecordStore):
        raise AssertionError("component does not implement StateRecordStore")
    if store.lookup(write_id="sett.contract.state.missing") is not None:
        raise AssertionError("fresh state store reported an unknown write_id")

    first = store.put(write_id="sett.contract.state.1", payload=b"state")
    if not isinstance(first, StateRecordReceipt) or not first.acquired:
        raise AssertionError("first state put did not return an acquired receipt")
    replay = store.put(write_id="sett.contract.state.1", payload=b"state")
    if (
        not isinstance(replay, StateRecordReceipt)
        or replay.acquired
        or replay.state_ref != first.state_ref
        or replay.backend_id != first.backend_id
        or replay.accepted_at != first.accepted_at
        or not satisfies_durability(
            replay.achieved_durability, first.achieved_durability
        )
    ):
        raise AssertionError("identical state replay did not preserve its receipt")
    _expect(
        SETTIdempotencyConflictError,
        lambda: store.put(
            write_id="sett.contract.state.1",
            payload=b"changed",
        ),
        "changed state replay did not raise SETTIdempotencyConflictError",
    )
    lookup = store.lookup(write_id="sett.contract.state.1")
    if (
        lookup is None
        or lookup.state_ref != first.state_ref
        or lookup.backend_id != first.backend_id
        or lookup.accepted_at != first.accepted_at
        or lookup.acquired
        or not satisfies_durability(
            lookup.achieved_durability, first.achieved_durability
        )
    ):
        raise AssertionError("state lookup did not return the prior replay receipt")
    record = store.get(state_ref=first.state_ref)
    if record.state_ref != first.state_ref or record.payload != b"state":
        raise AssertionError("state get did not return the exact accepted bytes")
    _expect(
        SETTPersistenceError,
        lambda: store.get(state_ref="sett.contract.state.unknown"),
        "missing state_ref did not raise SETTPersistenceError",
    )
    passive_lookup = store.lookup(write_id="sett.contract.state.1")
    passive_record = store.get(state_ref=first.state_ref)
    declaration = store.durability()
    if not isinstance(declaration, DurabilityDeclaration):
        raise AssertionError("state durability() returned the wrong type")
    if not isinstance(store.health(), PersistenceHealth):
        raise AssertionError("state health() returned the wrong type")
    if (
        store.lookup(write_id="sett.contract.state.1") != passive_lookup
        or store.get(state_ref=first.state_ref) != passive_record
    ):
        raise AssertionError("state durability or health mutated the store")
    for achieved in (
        first.achieved_durability,
        replay.achieved_durability,
        lookup.achieved_durability,
    ):
        if not satisfies_durability(declaration.maximum_durability, achieved):
            raise AssertionError("state receipt exceeds declared durability")
