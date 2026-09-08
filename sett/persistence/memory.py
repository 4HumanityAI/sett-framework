"""Honest in-memory reference implementations of persistence contracts."""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

from sett.exceptions import (
    SETTIdempotencyConflictError,
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistencePositionError,
    SETTValidationError,
)
from sett.persistence.contracts import (
    DurabilityDeclaration,
    DurabilityLevel,
    DurabilityReceipt,
    PersistenceHealth,
    PersistenceHealthStatus,
    StateRecord,
    StateRecordReceipt,
    StreamEntry,
    StreamPosition,
    StreamReceipt,
    validate_position_identity,
    validate_stream_position,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_write(write_id: str, payload: bytes) -> None:
    if not isinstance(write_id, str) or not write_id:
        raise SETTValidationError("write_id must be a non-empty string.")
    if not isinstance(payload, bytes):
        raise SETTValidationError("Persistence payload must be bytes.")


class InMemoryEvidenceStream:
    """Thread-safe reference stream that can only claim in-memory durability."""

    def __init__(self, *, backend_id: str, stream_id: str) -> None:
        """Create one empty global recorder stream."""
        self._head = StreamPosition(backend_id, stream_id, "0000000000000000")
        validate_stream_position(self._head)
        self._backend_id = backend_id
        self._stream_id = stream_id
        self._entries: list[StreamEntry] = []
        self._receipts: dict[str, StreamReceipt] = {}
        self._fingerprints: dict[str, tuple[int, bytes]] = {}
        self._position_indexes = {self._head.token: -1}
        self._lock = threading.RLock()

    def head(self) -> StreamPosition:
        """Return the current stable stream position."""
        with self._lock:
            return self._head

    def append(
        self,
        *,
        write_id: str,
        payload: bytes,
        expected_head: StreamPosition,
    ) -> StreamReceipt:
        """Append with idempotent replay and compare-and-append continuity."""
        _validate_write(write_id, payload)
        validate_position_identity(
            expected_head,
            backend_id=self._backend_id,
            stream_id=self._stream_id,
        )
        fingerprint = (len(payload), sha256(payload).digest())
        with self._lock:
            existing = self._receipts.get(write_id)
            if existing is not None:
                if self._fingerprints[write_id] != fingerprint:
                    raise SETTIdempotencyConflictError(
                        "write_id was already used for different evidence bytes.",
                        key=write_id,
                        expected_fingerprint=self._fingerprints[write_id][1].hex(),
                        actual_fingerprint=fingerprint[1].hex(),
                    )
                return replace(existing, acquired=False)
            if expected_head != self._head:
                raise SETTPersistenceContinuityError(
                    "Evidence append expected a stale stream head.",
                    reason_code="persistence.stream_continuity_conflict",
                    backend_id=self._backend_id,
                )
            position = StreamPosition(
                self._backend_id,
                self._stream_id,
                f"{len(self._entries) + 1:016d}",
            )
            accepted_at = _now()
            entry = StreamEntry(write_id=write_id, position=position, payload=payload)
            receipt = StreamReceipt(
                write_id=write_id,
                position=position,
                backend_id=self._backend_id,
                achieved_durability=DurabilityLevel.IN_MEMORY,
                accepted_at=accepted_at,
                acquired=True,
            )
            self._entries.append(entry)
            self._receipts[write_id] = receipt
            self._fingerprints[write_id] = fingerprint
            self._position_indexes[position.token] = len(self._entries) - 1
            self._head = position
            return receipt

    def read(self, *, after: StreamPosition | None = None) -> tuple[StreamEntry, ...]:
        """Return a contiguous immutable snapshot of the requested suffix."""
        with self._lock:
            if after is None:
                start = 0
            else:
                validate_position_identity(
                    after,
                    backend_id=self._backend_id,
                    stream_id=self._stream_id,
                )
                index = self._position_indexes.get(after.token)
                if index is None:
                    raise SETTPersistencePositionError(
                        "The stream position is not part of this lineage.",
                        reason_code="persistence.position_unknown",
                        backend_id=self._backend_id,
                    )
                start = index + 1
            return tuple(self._entries[start:])

    def flush(self, *, through: StreamPosition) -> DurabilityReceipt:
        """Confirm that this backend cannot raise data above IN_MEMORY."""
        validate_position_identity(
            through,
            backend_id=self._backend_id,
            stream_id=self._stream_id,
        )
        with self._lock:
            if through.token not in self._position_indexes:
                raise SETTPersistencePositionError(
                    "The stream position is not part of this lineage.",
                    reason_code="persistence.position_unknown",
                    backend_id=self._backend_id,
                )
        return DurabilityReceipt(
            backend_id=self._backend_id,
            through=through,
            achieved_durability=DurabilityLevel.IN_MEMORY,
            confirmed_at=_now(),
        )

    def durability(self) -> DurabilityDeclaration:
        """Declare the structural in-memory ceiling without writing."""
        return DurabilityDeclaration(
            maximum_durability=DurabilityLevel.IN_MEMORY,
            observed_at=_now(),
            reason_codes=("persistence.in_memory_only",),
        )

    def health(self) -> PersistenceHealth:
        """Return a passive local observation without mutating the stream."""
        return PersistenceHealth(
            status=PersistenceHealthStatus.HEALTHY,
            observed_at=_now(),
            reason_codes=("persistence.in_memory_only",),
        )


class InMemoryStateRecordStore:
    """Thread-safe state store that can only claim in-memory durability."""

    def __init__(self, *, backend_id: str) -> None:
        """Create an empty record and write-identity index."""
        if not isinstance(backend_id, str) or not backend_id:
            raise SETTValidationError("backend_id must be a non-empty string.")
        self._backend_id = backend_id
        self._records: dict[str, StateRecord] = {}
        self._receipts: dict[str, StateRecordReceipt] = {}
        self._fingerprints: dict[str, tuple[int, bytes]] = {}
        self._lock = threading.RLock()

    def put(self, *, write_id: str, payload: bytes) -> StateRecordReceipt:
        """Atomically accept exact bytes or replay an identical prior write."""
        _validate_write(write_id, payload)
        fingerprint = (len(payload), sha256(payload).digest())
        with self._lock:
            existing = self._receipts.get(write_id)
            if existing is not None:
                if self._fingerprints[write_id] != fingerprint:
                    raise SETTIdempotencyConflictError(
                        "write_id was already used for different state bytes.",
                        key=write_id,
                        expected_fingerprint=self._fingerprints[write_id][1].hex(),
                        actual_fingerprint=fingerprint[1].hex(),
                    )
                return replace(existing, acquired=False)
            state_ref = f"state-{len(self._records) + 1:016d}"
            accepted_at = _now()
            record = StateRecord(state_ref=state_ref, payload=payload)
            receipt = StateRecordReceipt(
                write_id=write_id,
                state_ref=state_ref,
                backend_id=self._backend_id,
                achieved_durability=DurabilityLevel.IN_MEMORY,
                accepted_at=accepted_at,
                acquired=True,
            )
            self._records[state_ref] = record
            self._receipts[write_id] = receipt
            self._fingerprints[write_id] = fingerprint
            return receipt

    def lookup(self, *, write_id: str) -> StateRecordReceipt | None:
        """Return an existing receipt without creating or changing state."""
        if not isinstance(write_id, str) or not write_id:
            raise SETTValidationError("write_id must be a non-empty string.")
        with self._lock:
            receipt = self._receipts.get(write_id)
            return None if receipt is None else replace(receipt, acquired=False)

    def get(self, *, state_ref: str) -> StateRecord:
        """Return exact stored bytes or report a missing opaque reference."""
        if not isinstance(state_ref, str) or not state_ref:
            raise SETTValidationError("state_ref must be a non-empty string.")
        with self._lock:
            try:
                return self._records[state_ref]
            except KeyError as error:
                raise SETTPersistenceError(
                    "State record was not found.",
                    reason_code="state.record_missing",
                    backend_id=self._backend_id,
                ) from error

    def durability(self) -> DurabilityDeclaration:
        """Declare the structural in-memory ceiling without writing."""
        return DurabilityDeclaration(
            maximum_durability=DurabilityLevel.IN_MEMORY,
            observed_at=_now(),
            reason_codes=("persistence.in_memory_only",),
        )

    def health(self) -> PersistenceHealth:
        """Return a passive local observation without mutating records."""
        return PersistenceHealth(
            status=PersistenceHealthStatus.HEALTHY,
            observed_at=_now(),
            reason_codes=("persistence.in_memory_only",),
        )
