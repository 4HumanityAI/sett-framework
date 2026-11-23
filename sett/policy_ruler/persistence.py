"""Opaque governance journal over the persistence contracts.

Payloads remain opaque and not interpreted. Empty bytes represent no decision,
review or absence of obligations. Integrity verifies only the configured policy's
check of bytes, not writer authenticity. No transaction joins storage, journal,
trace, authority consumption and effect. There is no durability producer
here.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Literal

from sett.audit_ruler.integrity import IntegrityPolicy
from sett.exceptions import (
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistenceFormatError,
    SETTPersistencePositionError,
)
from sett.persistence._binary import (
    BinaryReader,
    encode_blob,
    encode_text,
    encode_uint,
)
from sett.persistence.contracts import (
    DurabilityLevel,
    DurabilityReceipt,
    EvidenceStream,
    StreamEntry,
    StreamReceipt,
    satisfies_durability,
)
from sett.persistence.formats import (
    StreamPosition,
    decode_stream_position,
    encode_stream_position,
)

__all__ = [
    "GovernanceEntry",
    "GovernanceJournal",
    "GovernanceReadResult",
    "GovernanceReconciliation",
    "GovernanceRecord",
    "GovernanceSubmission",
    "GovernanceWriteResult",
]

_PREFIX = "policy.journal_"
_MAGIC = b"SETTGOV\0"
_WRITE_DOMAIN = b"SETT-GOVERNANCE-WRITE\x00v1\x00"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]*\Z", re.ASCII)
_END = object()
_REASONS = frozenset(
    _PREFIX + suffix
    for suffix in (
        "invalid", "format_invalid", "version_unsupported", "identity_mismatch",
        "continuity_invalid", "limit_exceeded", "integrity_unavailable",
        "integrity_failed", "head_changed", "stale_head", "identity_conflict",
        "storage_failed", "receipt_invalid", "durability_unconfirmed",
    )
)


def _require(condition: bool, reason: str = "invalid") -> None:
    if not condition:
        raise SETTPersistenceFormatError(
            "Governance journal validation failed.", reason_code=_PREFIX + reason
        )


def _raise_journal_error() -> None:
    error = SETTPersistenceFormatError(
        "Governance journal input is invalid.", reason_code=_PREFIX + "invalid"
    )
    try:
        raise error
    finally:
        error.__cause__ = None
        error.__context__ = None


def _admit_journal_value(
    operation: Callable, *, reason: str = "invalid", preserve: bool = False,
) -> tuple[object, str | None]:
    value = None
    failure = None
    try:
        value = operation()
    except (SETTPersistenceFormatError, UnicodeError, ValueError, TypeError, OverflowError) as error:
        failure = _PREFIX + reason
        if (
            preserve
            and type(error) is SETTPersistenceFormatError
            and type(error.reason_code) is str
            and error.reason_code in _REASONS
        ):
            failure = error.reason_code
    return value, failure


def _identifier(value: str, reason: str = "invalid") -> str:
    _require(
        type(value) is str
        and 0 < len(value) <= 256
        and _IDENTIFIER.fullmatch(value) is not None,
        reason,
    )
    return value


def _position(
    value: StreamPosition, origin: StreamPosition | None = None,
    reason: str = "invalid",
) -> StreamPosition:
    _require(type(value) is StreamPosition, reason)
    _require(
        all(type(item) is str for item in (value.backend_id, value.stream_id, value.token)),
        reason,
    )
    encode_stream_position(value)
    if origin is not None:
        _require(
            (value.backend_id, value.stream_id) == (origin.backend_id, origin.stream_id),
            reason,
        )
    return value


def _record_fields(
    operation_id: str, schema_id: str, schema_version: int, payload: bytes,
    reason: str = "invalid",
) -> None:
    _identifier(operation_id, reason)
    _identifier(schema_id, reason)
    _require(type(schema_version) is int and 1 <= schema_version < 2**32, reason)
    _require(type(payload) is bytes and len(payload) < 2**32, reason)


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceRecord:
    """Opaque versioned bytes; an empty payload is no governance decision."""

    operation_id: str
    schema_id: str
    schema_version: int
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _, failure = _admit_journal_value(
            lambda: _record_fields(
                self.operation_id, self.schema_id, self.schema_version, self.payload
            )
        )
        if failure:
            _raise_journal_error()


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceSubmission:
    """Exact prepared bytes, not a credential or permission to execute."""

    record: GovernanceRecord = field(repr=False)
    expected_head: StreamPosition
    write_id: str
    frame: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceEntry:
    """Admitted bytes with separate integrity and non-interpretation labels."""

    position: StreamPosition
    write_id: str
    record: GovernanceRecord = field(repr=False)
    frame: bytes = field(repr=False)
    integrity_status: Literal["verified", "disabled"]
    schema_status: Literal["not_interpreted"]


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceReadResult:
    """A bounded observed prefix; complete never implies domain validity."""

    entries: tuple[GovernanceEntry, ...] = field(repr=False)
    observed_head: StreamPosition | None
    complete: bool
    reason_code: str | None
    failure_index: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceWriteResult:
    """An observed write outcome, never a claim of rollback or effect authority."""

    submission: GovernanceSubmission = field(repr=False)
    state: Literal[
        "not_submitted", "append_uncertain", "appended_unconfirmed", "confirmed"
    ]
    stage: Literal["admission", "scan", "append", "confirmation", "complete"]
    entry: GovernanceEntry | None = field(repr=False)
    append_receipt: StreamReceipt | None
    durability_receipt: DurabilityReceipt | None
    reason_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceReconciliation:
    """Explicit reconciliation can flush storage, but never append or execute."""

    submission: GovernanceSubmission = field(repr=False)
    state: Literal[
        "found_confirmed", "found_unconfirmed", "conflicting",
        "not_found_in_prefix", "inconclusive",
    ]
    read_result: GovernanceReadResult = field(repr=False)
    entry: GovernanceEntry | None = field(repr=False)
    durability_receipt: DurabilityReceipt | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class _Envelope:
    namespace_id: str
    journal_id: str
    record: GovernanceRecord = field(repr=False)
    origin: StreamPosition
    predecessor: StreamPosition
    policy_id: str
    algorithm: str
    preimage: bytes = field(repr=False)
    digest: bytes | None = field(repr=False)


def _text(value: str) -> bytes:
    return encode_text(value, field="journal identity", maximum=256, reason_code=_PREFIX + "invalid")


def _blob(value: bytes, maximum: int) -> bytes:
    return encode_blob(value, field="journal bytes", maximum=maximum, reason_code=_PREFIX + "format_invalid")


def _write_id(namespace_id: str, journal_id: str, operation_id: str) -> str:
    return "governance.v1." + sha256(
        _WRITE_DOMAIN + _text(namespace_id) + _text(journal_id) + _text(operation_id)
    ).hexdigest()


def _decode(frame: bytes, maximum: int) -> _Envelope:
    _require(type(frame) is bytes, "format_invalid")
    _require(len(frame) <= maximum, "limit_exceeded")
    reader = BinaryReader(frame, reason_code=_PREFIX + "format_invalid")
    _require(reader.take(8, field="magic") == _MAGIC, "format_invalid")
    _require(reader.uint(2, field="version") == 1, "version_unsupported")
    body = reader.blob(field="body", maximum=maximum)
    preimage = frame[:reader.offset]
    digest = reader.optional_blob(field="digest", maximum=65535)
    _require(digest is None or len(digest) > 0, "integrity_failed")
    reader.finish()
    values = BinaryReader(body, reason_code=_PREFIX + "format_invalid")
    namespace = values.text(field="namespace", maximum=256)
    journal = values.text(field="journal", maximum=256)
    operation = values.text(field="operation", maximum=256)
    schema = values.text(field="schema", maximum=256)
    version = values.uint(4, field="schema version")
    origin = decode_stream_position(values.blob(field="origin", maximum=maximum))
    predecessor = decode_stream_position(values.blob(field="predecessor", maximum=maximum))
    policy_id = values.text(field="integrity policy", maximum=256)
    algorithm = values.text(field="algorithm", maximum=256)
    payload = values.blob(field="payload", maximum=maximum)
    values.finish()
    for item in (namespace, journal, policy_id, algorithm):
        _identifier(item, "format_invalid")
    _record_fields(operation, schema, version, payload, "format_invalid")
    return _Envelope(
        namespace, journal,
        GovernanceRecord(operation_id=operation, schema_id=schema, schema_version=version, payload=payload),
        origin, predecessor, policy_id, algorithm, preimage, digest,
    )


def _call_integrity(
    policy: IntegrityPolicy, operation: str, *, expected: tuple[str, str] | None = None,
    payload: bytes = b"", digest: bytes | None = None,
) -> tuple[object, str | None]:
    result = None
    failure = None
    try:
        identity = (_identifier(policy.policy_id), _identifier(policy.algorithm))
        if expected is not None and identity != expected:
            failure = _PREFIX + "integrity_unavailable"
        elif operation == "identity":
            result = identity
        elif operation == "digest":
            result = policy.digest(payload)
            if result is not None and not (type(result) is bytes and 1 <= len(result) <= 65535):
                result = None
                failure = _PREFIX + "integrity_failed"
        else:
            result = policy.verify(payload, digest)
            if result is not True:
                result = None
                failure = _PREFIX + "integrity_failed"
    except Exception:  # noqa: BLE001 - translate failures only at the private journal integrity boundary.
        failure = _PREFIX + "integrity_failed"
    return result, failure


def _call_storage(
    target: object, operation: str, *, validator: Callable,
    invalid_reason: str, **arguments: object,
) -> tuple[object, str | None]:
    value = None
    failure = None
    validating = False
    try:
        if operation == "next":
            value = next(target, _END)
        elif operation == "read":
            value = iter(target.read(**arguments))
        else:
            value = getattr(target, operation)(**arguments)
        validating = True
        if value is not _END:
            value = validator(value)
    except Exception as error:  # noqa: BLE001 - translate failures only at the private journal storage boundary.
        if validating:
            failure = _PREFIX + invalid_reason
            if (
                type(error) is SETTPersistenceFormatError
                and type(error.reason_code) is str
                and error.reason_code in _REASONS
            ):
                failure = error.reason_code
        elif isinstance(error, SETTPersistenceFormatError):
            failure = _PREFIX + "format_invalid"
        elif isinstance(error, SETTPersistencePositionError):
            failure = _PREFIX + "identity_mismatch"
        elif isinstance(error, SETTPersistenceContinuityError):
            failure = _PREFIX + "continuity_invalid"
        elif isinstance(error, SETTPersistenceError):
            failure = _PREFIX + "storage_failed"
        else:
            failure = _PREFIX + "storage_failed"
        value = None
    return value, failure


def _unchanged(value: object) -> object:
    return value


def _entry_shape(entry: StreamEntry, origin: StreamPosition) -> StreamEntry:
    _require(type(entry) is StreamEntry, "format_invalid")
    _require(type(entry.write_id) is str and type(entry.payload) is bytes, "format_invalid")
    _position(entry.position, origin, "identity_mismatch")
    return entry


def _receipt(
    value: object, origin: StreamPosition, *, write_id: str | None = None,
    through: StreamPosition | None = None,
) -> object:
    expected_type = StreamReceipt if write_id is not None else DurabilityReceipt
    _require(type(value) is expected_type, "receipt_invalid")
    _require(type(value.backend_id) is str and value.backend_id == origin.backend_id, "receipt_invalid")
    _require(type(value.achieved_durability) is DurabilityLevel, "receipt_invalid")
    if write_id is not None:
        _require(type(value.write_id) is str and value.write_id == write_id, "receipt_invalid")
        _require(type(value.acquired) is bool, "receipt_invalid")
        _position(value.position, origin, "receipt_invalid")
        _require(value.position != origin, "receipt_invalid")
        timestamp = value.accepted_at
    else:
        _position(value.through, origin, "receipt_invalid")
        _require(value.through == through, "receipt_invalid")
        timestamp = value.confirmed_at
    _require(type(timestamp) is datetime and timestamp.utcoffset() is not None, "receipt_invalid")
    return value


class GovernanceJournal:
    """Bounded governance-envelope storage using injected persistence.

    The stream must be dedicated to this journal. Payload schemas remain opaque;
    no real durability producer, governed recovery or cross-store transaction is
    installed. Confirmations report the backend's operation-specific declaration,
    not independent physical-durability tests.
    """

    def __init__(
        self, *, namespace_id: str, journal_id: str, stream: EvidenceStream,
        origin: StreamPosition, required_durability: DurabilityLevel,
        integrity_policy: IntegrityPolicy,
        historical_integrity_policies: tuple[IntegrityPolicy, ...],
        max_frame_bytes: int, max_records: int, max_total_bytes: int,
    ) -> None:
        """Configure the journal with explicit storage, integrity and bounds."""
        _, failure = _admit_journal_value(
            lambda: self._validate_configuration(
                namespace_id, journal_id, origin, required_durability,
                historical_integrity_policies, max_frame_bytes, max_records, max_total_bytes,
            )
        )
        if failure:
            _raise_journal_error()
        policies = {}
        active_identity = None
        for policy in (integrity_policy, *historical_integrity_policies):
            identity, failure = _call_integrity(policy, "identity")
            if failure or identity[0] in policies:
                _raise_journal_error()
            policies[identity[0]] = (policy, identity)
            if active_identity is None:
                active_identity = identity
        self._namespace_id = namespace_id
        self._journal_id = journal_id
        self._stream = stream
        self._origin = origin
        self._required_durability = required_durability
        self._policies = policies
        self._active_identity = active_identity
        self._max_frame_bytes = max_frame_bytes
        self._max_records = max_records
        self._max_total_bytes = max_total_bytes

    @staticmethod
    def _validate_configuration(
        namespace: str, journal: str, origin: StreamPosition, required: DurabilityLevel,
        historical: tuple, frame_limit: int, count_limit: int, total_limit: int,
    ) -> None:
        _identifier(namespace)
        _identifier(journal)
        _position(origin)
        _require(type(required) is DurabilityLevel and required is not DurabilityLevel.UNKNOWN)
        _require(type(historical) is tuple)
        _require(type(frame_limit) is int and 1 <= frame_limit < 2**32)
        _require(type(count_limit) is int and 1 <= count_limit < 2**32)
        _require(type(total_limit) is int and frame_limit <= total_limit < 2**64)

    def _preimage(self, record: GovernanceRecord, head: StreamPosition) -> bytes:
        _require(type(record) is GovernanceRecord)
        _record_fields(record.operation_id, record.schema_id, record.schema_version, record.payload)
        _position(head, self._origin)
        body = b"".join((
            _text(self._namespace_id), _text(self._journal_id),
            _text(record.operation_id), _text(record.schema_id),
            encode_uint(record.schema_version, 4, field="schema version", reason_code=_PREFIX + "invalid"),
            _blob(encode_stream_position(self._origin), self._max_frame_bytes),
            _blob(encode_stream_position(head), self._max_frame_bytes),
            _text(self._active_identity[0]), _text(self._active_identity[1]),
            _blob(record.payload, self._max_frame_bytes),
        ))
        preimage = _MAGIC + b"\x00\x01" + _blob(body, self._max_frame_bytes)
        _require(len(preimage) + 1 <= self._max_frame_bytes)
        return preimage

    def prepare(
        self, record: GovernanceRecord, *, expected_head: StreamPosition,
    ) -> GovernanceSubmission:
        """Prepare exact immutable bytes without any storage call."""
        preimage, failure = _admit_journal_value(lambda: self._preimage(record, expected_head))
        if failure:
            _raise_journal_error()
        policy, identity = self._policies[self._active_identity[0]]
        digest, failure = _call_integrity(policy, "digest", expected=identity, payload=preimage)
        if failure:
            _raise_journal_error()
        frame = preimage + (b"\x00" if digest is None else b"\x01" + _blob(digest, 65535))
        if len(frame) > self._max_frame_bytes:
            _raise_journal_error()
        return GovernanceSubmission(
            record=record, expected_head=expected_head,
            write_id=_write_id(self._namespace_id, self._journal_id, record.operation_id),
            frame=frame,
        )

    def _associations(self, envelope: _Envelope, write_id: str) -> None:
        _require(
            (envelope.namespace_id, envelope.journal_id, envelope.origin)
            == (self._namespace_id, self._journal_id, self._origin),
            "identity_mismatch",
        )
        _position(envelope.predecessor, self._origin, "identity_mismatch")
        _require(
            write_id == _write_id(self._namespace_id, self._journal_id, envelope.record.operation_id),
            "identity_mismatch",
        )

    def _integrity(self, envelope: _Envelope) -> str | None:
        configured = self._policies.get(envelope.policy_id)
        if configured is None or configured[1] != (envelope.policy_id, envelope.algorithm):
            return _PREFIX + "integrity_unavailable"
        _, failure = _call_integrity(
            configured[0], "verify", expected=configured[1],
            payload=envelope.preimage, digest=envelope.digest,
        )
        return failure

    def _submission_fields(self, submission: GovernanceSubmission, active: bool) -> _Envelope:
        _require(type(submission) is GovernanceSubmission)
        _require(type(submission.record) is GovernanceRecord)
        _record_fields(
            submission.record.operation_id, submission.record.schema_id,
            submission.record.schema_version, submission.record.payload,
        )
        _position(submission.expected_head, self._origin)
        _require(type(submission.write_id) is str)
        envelope = _decode(submission.frame, self._max_frame_bytes)
        self._associations(envelope, submission.write_id)
        _require(envelope.record == submission.record and envelope.predecessor == submission.expected_head)
        if active:
            _require((envelope.policy_id, envelope.algorithm) == self._active_identity)
        return envelope

    def _admit_submission(self, submission: GovernanceSubmission, *, active: bool) -> None:
        envelope, failure = _admit_journal_value(lambda: self._submission_fields(submission, active))
        if failure or self._integrity(envelope):
            _raise_journal_error()

    def read(self) -> GovernanceReadResult:
        """Read a bounded stable prefix without appending or flushing."""
        entries = []
        head, failure = _call_storage(
            self._stream, "head",
            validator=lambda value: _position(value, self._origin, "identity_mismatch"),
            invalid_reason="identity_mismatch",
        )
        if failure:
            return self._read_result(entries, None, failure)
        iterator, failure = _call_storage(
            self._stream, "read", validator=_unchanged,
            invalid_reason="format_invalid", after=None,
        )
        if failure:
            return self._read_result(entries, head, failure)
        previous = self._origin
        positions = set()
        identities = set()
        total = 0
        for index in range(self._max_records + 1):
            raw, failure = _call_storage(
                iterator, "next",
                validator=lambda value: _entry_shape(value, self._origin),
                invalid_reason="format_invalid",
            )
            if failure:
                return self._read_result(entries, head, failure, index)
            if raw is _END:
                break
            if (
                index == self._max_records
                or len(raw.payload) > self._max_frame_bytes
                or total + len(raw.payload) > self._max_total_bytes
            ):
                return self._read_result(entries, head, _PREFIX + "limit_exceeded", index)
            envelope, failure = _admit_journal_value(
                lambda: _decode(raw.payload, self._max_frame_bytes),
                reason="format_invalid", preserve=True,
            )
            if failure:
                return self._read_result(entries, head, failure, index)
            _, failure = _admit_journal_value(
                lambda: self._associations(envelope, raw.write_id),
                reason="identity_mismatch", preserve=True,
            )
            if failure:
                return self._read_result(entries, head, failure, index)
            if (
                envelope.predecessor != previous or raw.position == self._origin
                or raw.position in positions or raw.write_id in identities
            ):
                return self._read_result(entries, head, _PREFIX + "continuity_invalid", index)
            failure = self._integrity(envelope)
            if failure:
                return self._read_result(entries, head, failure, index)
            entries.append(GovernanceEntry(
                position=raw.position, write_id=raw.write_id, record=envelope.record,
                frame=raw.payload, integrity_status="disabled" if envelope.digest is None else "verified",
                schema_status="not_interpreted",
            ))
            total += len(raw.payload)
            positions.add(raw.position)
            identities.add(raw.write_id)
            previous = raw.position
        final_head, failure = _call_storage(
            self._stream, "head",
            validator=lambda value: _position(value, self._origin, "identity_mismatch"),
            invalid_reason="identity_mismatch",
        )
        if failure:
            return self._read_result(entries, head, failure)
        if head != final_head:
            return self._read_result(entries, final_head, _PREFIX + "head_changed")
        if previous != final_head:
            return self._read_result(entries, final_head, _PREFIX + "continuity_invalid")
        return self._read_result(entries, final_head, None)

    @staticmethod
    def _read_result(
        entries: list, head: StreamPosition | None,
        reason: str | None, index: int | None = None,
    ) -> GovernanceReadResult:
        return GovernanceReadResult(
            entries=tuple(entries), observed_head=head, complete=reason is None,
            reason_code=reason, failure_index=index,
        )

    def _confirm(self, entry: GovernanceEntry) -> tuple[DurabilityReceipt | None, str | None]:
        receipt, failure = _call_storage(
            self._stream, "flush",
            validator=lambda value: _receipt(value, self._origin, through=entry.position),
            invalid_reason="receipt_invalid", through=entry.position,
        )
        if failure:
            return None, failure
        if not satisfies_durability(receipt.achieved_durability, self._required_durability):
            return receipt, _PREFIX + "durability_unconfirmed"
        return receipt, None

    @staticmethod
    def _write_result(
        submission: GovernanceSubmission, state: str, stage: str, reason: str | None,
        entry: GovernanceEntry | None = None, append_receipt: StreamReceipt | None = None,
        durability_receipt: DurabilityReceipt | None = None,
    ) -> GovernanceWriteResult:
        return GovernanceWriteResult(
            submission=submission, state=state, stage=stage, reason_code=reason,
            entry=entry, append_receipt=append_receipt, durability_receipt=durability_receipt,
        )

    def append(self, submission: GovernanceSubmission) -> GovernanceWriteResult:
        """Attempt one append at most; preserve uncertainty without retry."""
        self._admit_submission(submission, active=True)
        scan = self.read()
        if not scan.complete:
            return self._write_result(submission, "not_submitted", "scan", scan.reason_code)
        existing = next((item for item in scan.entries if item.write_id == submission.write_id), None)
        if existing is not None:
            if existing.frame != submission.frame:
                return self._write_result(submission, "not_submitted", "scan", _PREFIX + "identity_conflict")
            durability, failure = self._confirm(existing)
            return self._write_result(
                submission, "appended_unconfirmed" if failure else "confirmed",
                "confirmation" if failure else "complete", failure,
                entry=existing, durability_receipt=durability,
            )
        if scan.observed_head != submission.expected_head:
            return self._write_result(submission, "not_submitted", "scan", _PREFIX + "stale_head")
        if (
            len(scan.entries) >= self._max_records
            or sum(len(item.frame) for item in scan.entries) + len(submission.frame) > self._max_total_bytes
        ):
            return self._write_result(submission, "not_submitted", "scan", _PREFIX + "limit_exceeded")
        receipt, failure = _call_storage(
            self._stream, "append",
            validator=lambda value: _receipt(value, self._origin, write_id=submission.write_id),
            invalid_reason="receipt_invalid", write_id=submission.write_id,
            payload=submission.frame, expected_head=submission.expected_head,
        )
        if failure:
            return self._write_result(submission, "append_uncertain", "append", failure)
        after = self.read()
        entry = next((
            item for item in after.entries
            if item.write_id == submission.write_id and item.position == receipt.position
            and item.frame == submission.frame
        ), None)
        if not after.complete or entry is None:
            return self._write_result(
                submission, "append_uncertain", "scan",
                after.reason_code or _PREFIX + "identity_mismatch", append_receipt=receipt,
            )
        durability, failure = self._confirm(entry)
        return self._write_result(
            submission, "appended_unconfirmed" if failure else "confirmed",
            "confirmation" if failure else "complete", failure,
            entry=entry, append_receipt=receipt, durability_receipt=durability,
        )

    def confirm(self, position: StreamPosition) -> str | None:
        """Flush through one of this journal's entries; ``None`` when durable as required.

        Otherwise it returns the reason code: the read's own reason, a
        position the journal does not hold (``policy.journal_identity_mismatch``),
        a storage failure, or durability below the journal's requirement. It
        never appends. A flush through a position also covers every earlier
        entry.
        """
        if type(position) is not StreamPosition:
            _raise_journal_error()
        scan = self.read()
        if not scan.complete:
            return scan.reason_code
        entry = next((item for item in scan.entries if item.position == position), None)
        if entry is None:
            return _PREFIX + "identity_mismatch"
        _receipt, reason = self._confirm(entry)
        return reason

    def reconcile(self, submission: GovernanceSubmission) -> GovernanceReconciliation:
        """Locate original bytes and explicitly flush; never append or execute."""
        self._admit_submission(submission, active=False)
        scan = self.read()
        entry = next((item for item in scan.entries if item.write_id == submission.write_id), None)
        receipt = None
        reason = scan.reason_code
        if not scan.complete:
            state = "inconclusive"
            entry = None
        elif entry is None:
            state = "not_found_in_prefix"
        elif entry.frame != submission.frame:
            state = "conflicting"
            reason = _PREFIX + "identity_conflict"
        else:
            receipt, reason = self._confirm(entry)
            state = "found_unconfirmed" if reason else "found_confirmed"
        return GovernanceReconciliation(
            submission=submission, state=state, read_result=scan, entry=entry,
            durability_receipt=receipt, reason_code=reason,
        )
