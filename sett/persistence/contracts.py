"""Public, backend-neutral contracts for durable evidence and state."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from sett.exceptions import SETTConfigurationError, SETTValidationError
from sett.persistence.formats import StreamPosition, encode_stream_position


class DurabilityLevel(str, Enum):
    """The failure class a backend declares that accepted bytes survive."""

    IN_MEMORY = "in_memory"
    PROCESS_CRASH = "process_crash"
    POWER_LOSS = "power_loss"
    MEDIA_LOSS = "media_loss"
    NODE_LOSS = "node_loss"
    UNKNOWN = "unknown"


_DURABILITY_RANK = {
    DurabilityLevel.IN_MEMORY: 0,
    DurabilityLevel.PROCESS_CRASH: 1,
    DurabilityLevel.POWER_LOSS: 2,
    DurabilityLevel.MEDIA_LOSS: 3,
    DurabilityLevel.NODE_LOSS: 4,
}


def satisfies_durability(
    achieved: DurabilityLevel, required: DurabilityLevel
) -> bool:
    """Return whether one explicit declaration satisfies one requirement.

    ``UNKNOWN`` is deliberately outside the ordering. Missing evidence is
    not evidence of any durability level, and ``UNKNOWN`` is not a valid
    application requirement.
    """
    try:
        achieved_level = DurabilityLevel(achieved)
        required_level = DurabilityLevel(required)
    except (TypeError, ValueError):
        return False
    if (
        achieved_level is DurabilityLevel.UNKNOWN
        or required_level is DurabilityLevel.UNKNOWN
    ):
        return False
    return _DURABILITY_RANK[achieved_level] >= _DURABILITY_RANK[required_level]


class PersistenceHealthStatus(str, Enum):
    """The most recent passive observation of a persistence component."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DurabilityDeclaration:
    """The maximum durability a backend claims it can provide.

    This is not health, does not prove physical durability, and performs
    no write. It makes the backend's claim explicit and attributable.
    """

    maximum_durability: DurabilityLevel
    observed_at: datetime
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersistenceHealth:
    """A passive observation; never a durability assertion or write probe."""

    status: PersistenceHealthStatus
    observed_at: datetime
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DurabilityReceipt:
    """Durability declared through one accepted stream position."""

    backend_id: str
    through: StreamPosition
    achieved_durability: DurabilityLevel
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class StreamReceipt:
    """The attributable result of one evidence append or replay."""

    write_id: str
    position: StreamPosition
    backend_id: str
    achieved_durability: DurabilityLevel
    accepted_at: datetime
    acquired: bool


@dataclass(frozen=True, slots=True)
class StreamEntry:
    """One opaque payload read from a contiguous evidence stream."""

    write_id: str
    position: StreamPosition
    payload: bytes


@dataclass(frozen=True, slots=True)
class StateRecordReceipt:
    """The attributable result of one state write, lookup, or replay."""

    write_id: str
    state_ref: str
    backend_id: str
    achieved_durability: DurabilityLevel
    accepted_at: datetime
    acquired: bool


@dataclass(frozen=True, slots=True)
class StateRecord:
    """Opaque durable bytes addressed by a backend-owned reference."""

    state_ref: str
    payload: bytes


@runtime_checkable
class EvidenceStream(Protocol):
    """Append-only opaque stream with compare-and-append continuity."""

    def head(self) -> StreamPosition:
        """Return the current position, including the empty-stream head."""
        ...

    def append(
        self,
        *,
        write_id: str,
        payload: bytes,
        expected_head: StreamPosition,
    ) -> StreamReceipt:
        """Append exact bytes or replay an identical prior write."""
        ...

    def read(
        self, *, after: StreamPosition | None = None
    ) -> Iterable[StreamEntry]:
        """Read a contiguous suffix in authoritative backend order."""
        ...

    def flush(self, *, through: StreamPosition) -> DurabilityReceipt:
        """Report the level reached by every entry through a position."""
        ...

    def durability(self) -> DurabilityDeclaration:
        """Return a passive, attributable capability declaration."""
        ...

    def health(self) -> PersistenceHealth:
        """Return a passive observation without writing or probing billing."""
        ...


@runtime_checkable
class StateRecordStore(Protocol):
    """Random-access opaque records idempotent by caller-owned write id."""

    def put(self, *, write_id: str, payload: bytes) -> StateRecordReceipt:
        """Accept exact bytes or replay an identical prior write."""
        ...

    def lookup(self, *, write_id: str) -> StateRecordReceipt | None:
        """Find a prior receipt without creating or modifying a record."""
        ...

    def get(self, *, state_ref: str) -> StateRecord:
        """Return exact bytes for an opaque backend reference."""
        ...

    def durability(self) -> DurabilityDeclaration:
        """Return a passive, attributable capability declaration."""
        ...

    def health(self) -> PersistenceHealth:
        """Return a passive observation without writing or probing billing."""
        ...


@dataclass(frozen=True, slots=True)
class PersistenceBackend:
    """One namespace bound to independent evidence and state contracts."""

    namespace_id: str
    evidence: EvidenceStream
    state: StateRecordStore

    def __post_init__(self) -> None:
        if not isinstance(self.namespace_id, str) or not self.namespace_id:
            raise SETTValidationError("Persistence namespace_id cannot be empty.")
        if not isinstance(self.evidence, EvidenceStream):
            raise SETTValidationError("evidence must implement EvidenceStream.")
        if not isinstance(self.state, StateRecordStore):
            raise SETTValidationError("state must implement StateRecordStore.")


def validate_position_identity(
    position: StreamPosition,
    *,
    backend_id: str,
    stream_id: str,
) -> None:
    """Reject a position from another backend or stream before operating."""
    from sett.exceptions import SETTPersistencePositionError

    if not isinstance(position, StreamPosition):
        raise SETTPersistencePositionError(
            "A stream operation requires a StreamPosition.",
            reason_code="persistence.position_invalid",
            backend_id=backend_id,
        )
    if position.backend_id != backend_id:
        raise SETTPersistencePositionError(
            "The stream position belongs to another backend.",
            reason_code="persistence.position_backend_mismatch",
            backend_id=backend_id,
        )
    if position.stream_id != stream_id:
        raise SETTPersistencePositionError(
            "The stream position belongs to another stream.",
            reason_code="persistence.position_stream_mismatch",
            backend_id=backend_id,
        )


def validate_backend_declarations(
    backend: PersistenceBackend,
    *,
    snapshot_durability: DurabilityLevel,
    evidence_durability: DurabilityLevel,
) -> None:
    """Fail before execution when backend declarations cannot satisfy policy."""
    if snapshot_durability is DurabilityLevel.UNKNOWN:
        raise SETTConfigurationError(
            "snapshot_durability must be an explicit durability requirement."
        )
    if evidence_durability is DurabilityLevel.UNKNOWN:
        raise SETTConfigurationError(
            "evidence_durability must be an explicit durability requirement."
        )
    if not satisfies_durability(snapshot_durability, evidence_durability):
        raise SETTConfigurationError(
            "Snapshot durability must be at least as strong as evidence durability."
        )
    state_declaration = backend.state.durability()
    evidence_declaration = backend.evidence.durability()
    if not satisfies_durability(
        state_declaration.maximum_durability, snapshot_durability
    ):
        raise SETTConfigurationError(
            "The state backend cannot satisfy snapshot_durability."
        )
    if not satisfies_durability(
        evidence_declaration.maximum_durability, evidence_durability
    ):
        raise SETTConfigurationError(
            "The evidence backend cannot satisfy evidence_durability."
        )


def validate_stream_position(position: StreamPosition) -> None:
    """Validate the transport representation without exposing token order."""
    encode_stream_position(position)
