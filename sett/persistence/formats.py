"""Versioned byte formats for durable SETT evidence and state."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from hashlib import sha256
from typing import Any

from sett.exceptions import SETTPersistenceFormatError
from sett.persistence._binary import (
    BinaryReader,
    encode_blob,
    encode_optional,
    encode_text,
    encode_uint,
)

TRACE_FRAME_MAGIC = b"SETTEVT\0"
STATE_ENVELOPE_MAGIC = b"SETTENV\0"
PRIVATE_MEMORY_MAGIC = b"SETTPMM\0"
STREAM_POSITION_MAGIC = b"SETTPOS\0"
MIGRATION_MANIFEST_MAGIC = b"SETTMNF\0"

TRACE_FRAME_VERSION = 1
STATE_ENVELOPE_VERSION = 1
PRIVATE_MEMORY_VERSION = 1
STREAM_POSITION_VERSION = 1
MIGRATION_MANIFEST_VERSION = 1

TRACE_CANONICALIZATION_ID = "sett.trace-json.v1"
TRACE_HASH_ALGORITHM_ID = "sha256"
STATE_ENVELOPE_FORMAT_ID = "sett.state-envelope.v1"
PRIVATE_MEMORY_FORMAT_ID = "sett.private-memory-mutation.v1"
STREAM_POSITION_FORMAT_ID = "sett.stream-position.v1"
MIGRATION_MANIFEST_FORMAT_ID = "sett.migration-manifest.v1"

MAX_TRACE_FRAME_BYTES = 32 * 1024
MAX_IDENTIFIER_BYTES = 256
MAX_CONTEXT_ID_BYTES = 1024
MAX_POSITION_FIELD_BYTES = 256
MAX_STATE_PAYLOAD_BYTES = (1 << 32) - 1


def _error(message: str, reason_code: str) -> SETTPersistenceFormatError:
    return SETTPersistenceFormatError(message, reason_code=reason_code)


def _strict_json(preimage: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite number {value!r} is not valid JSON.")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key!r}.")
            result[key] = value
        return result

    try:
        value = json.loads(
            preimage.decode("utf-8", "strict"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, ValueError, TypeError) as error:
        raise _error(
            "Trace evidence contains an invalid JSON preimage.",
            "evidence.payload_invalid",
        ) from error
    if not isinstance(value, dict):
        raise _error(
            "Trace evidence preimage must be a JSON object.",
            "evidence.payload_invalid",
        )
    return value


@dataclass(frozen=True, slots=True)
class TraceEvidenceFrame:
    """Exact trace preimage and the digest that authenticates it."""

    preimage: bytes
    entry_hash: bytes
    payload: dict[str, Any]
    canonicalization_id: str = TRACE_CANONICALIZATION_ID
    hash_algorithm_id: str = TRACE_HASH_ALGORITHM_ID


def encode_trace_evidence_frame(
    preimage: bytes,
    *,
    entry_hash: bytes | None = None,
    canonicalization_id: str = TRACE_CANONICALIZATION_ID,
    hash_algorithm_id: str = TRACE_HASH_ALGORITHM_ID,
) -> bytes:
    """Frame exact trace bytes without reconstructing their JSON."""
    reason = "evidence.frame_invalid"
    if not isinstance(preimage, bytes):
        raise _error("Trace preimage must be bytes.", reason)
    digest = sha256(preimage).digest() if entry_hash is None else entry_hash
    if not isinstance(digest, bytes) or len(digest) != 32:
        raise _error("SHA-256 entry hash must contain 32 bytes.", reason)
    frame = b"".join(
        (
            TRACE_FRAME_MAGIC,
            encode_uint(TRACE_FRAME_VERSION, 2, field="version", reason_code=reason),
            encode_text(
                canonicalization_id,
                field="canonicalization id",
                reason_code=reason,
                identifier=True,
            ),
            encode_text(
                hash_algorithm_id,
                field="hash algorithm id",
                reason_code=reason,
                identifier=True,
            ),
            encode_uint(len(digest), 2, field="entry hash length", reason_code=reason),
            digest,
            encode_blob(
                preimage,
                field="preimage",
                maximum=MAX_TRACE_FRAME_BYTES,
                reason_code=reason,
            ),
        )
    )
    if len(frame) > MAX_TRACE_FRAME_BYTES:
        raise _error(
            f"Trace evidence frame exceeds {MAX_TRACE_FRAME_BYTES} bytes.",
            reason,
        )
    return frame


def decode_trace_evidence_frame(frame: bytes) -> TraceEvidenceFrame:
    """Validate one complete trace frame and return its exact preimage."""
    if not isinstance(frame, bytes):
        raise _error("Trace evidence frame must be bytes.", "evidence.frame_invalid")
    if len(frame) > MAX_TRACE_FRAME_BYTES:
        raise _error(
            f"Trace evidence frame exceeds {MAX_TRACE_FRAME_BYTES} bytes.",
            "evidence.frame_invalid",
        )
    reader = BinaryReader(
        frame,
        reason_code="evidence.frame_invalid",
        truncated_reason_code="evidence.frame_truncated",
    )
    if reader.take(8, field="magic") != TRACE_FRAME_MAGIC:
        raise _error("Unknown trace evidence magic.", "evidence.format_unsupported")
    version = reader.uint(2, field="version")
    canonicalization_id = reader.text(field="canonicalization id", identifier=True)
    hash_algorithm_id = reader.text(field="hash algorithm id", identifier=True)
    if (
        version != TRACE_FRAME_VERSION
        or canonicalization_id != TRACE_CANONICALIZATION_ID
        or hash_algorithm_id != TRACE_HASH_ALGORITHM_ID
    ):
        raise _error(
            "Trace evidence uses an unsupported version or identifier.",
            "evidence.format_unsupported",
        )
    digest_length = reader.uint(2, field="entry hash length")
    if digest_length != 32:
        raise _error("SHA-256 entry hash must contain 32 bytes.", "evidence.frame_invalid")
    entry_hash = reader.take(digest_length, field="entry hash")
    preimage = reader.blob(field="preimage", maximum=MAX_TRACE_FRAME_BYTES)
    reader.finish()
    if sha256(preimage).digest() != entry_hash:
        raise _error(
            "Trace evidence hash does not match its preimage.",
            "evidence.hash_mismatch",
        )
    payload = _strict_json(preimage)
    if (
        payload.get("canonicalization_id") != canonicalization_id
        or payload.get("hash_algorithm_id") != hash_algorithm_id
    ):
        raise _error(
            "Trace evidence header contradicts its authenticated payload.",
            "evidence.metadata_mismatch",
        )
    return TraceEvidenceFrame(
        preimage=preimage,
        entry_hash=entry_hash,
        payload=payload,
        canonicalization_id=canonicalization_id,
        hash_algorithm_id=hash_algorithm_id,
    )


@dataclass(frozen=True, slots=True)
class StateEnvelope:
    """A stage output bound to its execution identity and interpretation."""

    digest_algorithm: str
    digest_policy_id: str
    namespace_id: str
    trace_id: str
    run_id: str
    stage_index: int
    schema_id: str
    schema_version: int
    codec: str
    content_type: str
    payload_bytes: bytes
    format_id: str = STATE_ENVELOPE_FORMAT_ID


def encode_state_envelope(envelope: StateEnvelope) -> bytes:
    """Encode one execution-bound stage output as a complete v1 frame."""
    reason = "state.envelope_invalid"
    body = b"".join(
        (
            encode_text(envelope.format_id, field="format id", reason_code=reason, identifier=True),
            encode_text(envelope.digest_algorithm, field="digest algorithm", reason_code=reason, identifier=True),
            encode_text(envelope.digest_policy_id, field="digest policy id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.namespace_id, field="namespace id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.trace_id, field="trace id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
            encode_text(envelope.run_id, field="run id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
            encode_uint(envelope.stage_index, 4, field="stage index", reason_code=reason),
            encode_text(envelope.schema_id, field="schema id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_uint(envelope.schema_version, 4, field="schema version", reason_code=reason),
            encode_text(envelope.codec, field="codec", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.content_type, field="content type", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_blob(envelope.payload_bytes, field="payload", maximum=MAX_STATE_PAYLOAD_BYTES, reason_code=reason),
        )
    )
    return b"".join(
        (
            STATE_ENVELOPE_MAGIC,
            encode_uint(STATE_ENVELOPE_VERSION, 2, field="version", reason_code=reason),
            encode_text(envelope.format_id, field="format id", reason_code=reason, identifier=True),
            encode_text(envelope.digest_algorithm, field="digest algorithm", reason_code=reason, identifier=True),
            encode_blob(body, field="body", maximum=MAX_STATE_PAYLOAD_BYTES, reason_code=reason),
        )
    )


def decode_state_envelope(frame: bytes) -> StateEnvelope:
    """Decode and structurally validate a complete state envelope."""
    reason = "state.envelope_invalid"
    reader = BinaryReader(frame, reason_code=reason)
    if reader.take(8, field="magic") != STATE_ENVELOPE_MAGIC:
        raise _error("Unknown state envelope magic.", "state.envelope_unsupported")
    version = reader.uint(2, field="version")
    format_id = reader.text(field="format id", identifier=True)
    digest_algorithm = reader.text(field="digest algorithm", identifier=True)
    if version != STATE_ENVELOPE_VERSION or format_id != STATE_ENVELOPE_FORMAT_ID:
        raise _error("Unsupported state envelope format.", "state.envelope_unsupported")
    body = reader.blob(field="body", maximum=MAX_STATE_PAYLOAD_BYTES)
    reader.finish()
    body_reader = BinaryReader(body, reason_code=reason)
    body_format = body_reader.text(field="body format id", identifier=True)
    body_algorithm = body_reader.text(field="body digest algorithm", identifier=True)
    if body_format != format_id or body_algorithm != digest_algorithm:
        raise _error(
            "State envelope header contradicts its body.",
            "state.evidence_metadata_mismatch",
        )
    result = StateEnvelope(
        format_id=body_format,
        digest_algorithm=body_algorithm,
        digest_policy_id=body_reader.text(field="digest policy id", maximum=MAX_IDENTIFIER_BYTES),
        namespace_id=body_reader.text(field="namespace id", maximum=MAX_IDENTIFIER_BYTES),
        trace_id=body_reader.text(field="trace id", maximum=MAX_CONTEXT_ID_BYTES),
        run_id=body_reader.text(field="run id", maximum=MAX_CONTEXT_ID_BYTES),
        stage_index=body_reader.uint(4, field="stage index"),
        schema_id=body_reader.text(field="schema id", maximum=MAX_IDENTIFIER_BYTES),
        schema_version=body_reader.uint(4, field="schema version"),
        codec=body_reader.text(field="codec", maximum=MAX_IDENTIFIER_BYTES),
        content_type=body_reader.text(field="content type", maximum=MAX_IDENTIFIER_BYTES),
        payload_bytes=body_reader.blob(field="payload", maximum=MAX_STATE_PAYLOAD_BYTES),
    )
    body_reader.finish(field="state envelope body")
    return result


class PrivateMemoryOperation(IntEnum):
    """Operation encoded in a private-memory mutation envelope."""

    WRITE = 1
    CLEAR = 2


@dataclass(frozen=True, slots=True)
class PrivateMemoryMutationEnvelope:
    """One durable private-memory mutation, including its causal identity."""

    digest_algorithm: str
    digest_policy_id: str
    namespace_id: str
    memory_id: str
    trace_id: str
    run_id: str
    mutation_event_id: str
    operation: PrivateMemoryOperation
    key: str | None
    schema_id: str
    schema_version: int
    codec: str
    content_type: str
    payload_bytes: bytes | None
    format_id: str = PRIVATE_MEMORY_FORMAT_ID


def _validate_private_memory_presence(envelope: PrivateMemoryMutationEnvelope) -> None:
    valid_write = (
        envelope.operation is PrivateMemoryOperation.WRITE
        and envelope.key is not None
        and envelope.payload_bytes is not None
    )
    valid_clear = (
        envelope.operation is PrivateMemoryOperation.CLEAR
        and envelope.key is None
        and envelope.payload_bytes is None
    )
    if not (valid_write or valid_clear):
        raise _error(
            "Private-memory operation and presence markers disagree.",
            "state.envelope_invalid",
        )


def encode_private_memory_mutation(envelope: PrivateMemoryMutationEnvelope) -> bytes:
    """Encode one causally identified private-memory mutation."""
    reason = "state.envelope_invalid"
    _validate_private_memory_presence(envelope)
    key_encoder = lambda value: encode_text(  # noqa: E731
        value, field="key", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason
    )
    payload_encoder = lambda value: encode_blob(  # noqa: E731
        value, field="payload", maximum=MAX_STATE_PAYLOAD_BYTES, reason_code=reason
    )
    body = b"".join(
        (
            encode_text(envelope.format_id, field="format id", reason_code=reason, identifier=True),
            encode_text(envelope.digest_algorithm, field="digest algorithm", reason_code=reason, identifier=True),
            encode_text(envelope.digest_policy_id, field="digest policy id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.namespace_id, field="namespace id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.memory_id, field="memory id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.trace_id, field="trace id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
            encode_text(envelope.run_id, field="run id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
            encode_text(envelope.mutation_event_id, field="mutation event id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
            encode_uint(int(envelope.operation), 1, field="operation", reason_code=reason),
            encode_optional(envelope.key, encoder=key_encoder),
            encode_text(envelope.schema_id, field="schema id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_uint(envelope.schema_version, 4, field="schema version", reason_code=reason),
            encode_text(envelope.codec, field="codec", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(envelope.content_type, field="content type", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_optional(envelope.payload_bytes, encoder=payload_encoder),
        )
    )
    return b"".join(
        (
            PRIVATE_MEMORY_MAGIC,
            encode_uint(PRIVATE_MEMORY_VERSION, 2, field="version", reason_code=reason),
            encode_text(envelope.format_id, field="format id", reason_code=reason, identifier=True),
            encode_text(envelope.digest_algorithm, field="digest algorithm", reason_code=reason, identifier=True),
            encode_blob(body, field="body", maximum=MAX_STATE_PAYLOAD_BYTES, reason_code=reason),
        )
    )


def decode_private_memory_mutation(frame: bytes) -> PrivateMemoryMutationEnvelope:
    """Decode a private-memory mutation and enforce presence semantics."""
    reason = "state.envelope_invalid"
    reader = BinaryReader(frame, reason_code=reason)
    if reader.take(8, field="magic") != PRIVATE_MEMORY_MAGIC:
        raise _error("Unknown private-memory envelope magic.", "state.envelope_unsupported")
    version = reader.uint(2, field="version")
    format_id = reader.text(field="format id", identifier=True)
    digest_algorithm = reader.text(field="digest algorithm", identifier=True)
    if version != PRIVATE_MEMORY_VERSION or format_id != PRIVATE_MEMORY_FORMAT_ID:
        raise _error("Unsupported private-memory envelope.", "state.envelope_unsupported")
    body = reader.blob(field="body", maximum=MAX_STATE_PAYLOAD_BYTES)
    reader.finish()
    body_reader = BinaryReader(body, reason_code=reason)
    body_format = body_reader.text(field="body format id", identifier=True)
    body_algorithm = body_reader.text(field="body digest algorithm", identifier=True)
    if body_format != format_id or body_algorithm != digest_algorithm:
        raise _error(
            "Private-memory envelope header contradicts its body.",
            "state.evidence_metadata_mismatch",
        )
    digest_policy_id = body_reader.text(
        field="digest policy id", maximum=MAX_IDENTIFIER_BYTES
    )
    namespace_id = body_reader.text(
        field="namespace id", maximum=MAX_IDENTIFIER_BYTES
    )
    memory_id = body_reader.text(field="memory id", maximum=MAX_IDENTIFIER_BYTES)
    trace_id = body_reader.text(field="trace id", maximum=MAX_CONTEXT_ID_BYTES)
    run_id = body_reader.text(field="run id", maximum=MAX_CONTEXT_ID_BYTES)
    mutation_event_id = body_reader.text(
        field="mutation event id", maximum=MAX_CONTEXT_ID_BYTES
    )
    operation_raw = body_reader.uint(1, field="operation")
    try:
        operation = PrivateMemoryOperation(operation_raw)
    except ValueError as error:
        raise _error("Unknown private-memory operation.", reason) from error
    result = PrivateMemoryMutationEnvelope(
        format_id=body_format,
        digest_algorithm=body_algorithm,
        digest_policy_id=digest_policy_id,
        namespace_id=namespace_id,
        memory_id=memory_id,
        trace_id=trace_id,
        run_id=run_id,
        mutation_event_id=mutation_event_id,
        operation=operation,
        key=body_reader.optional_text(field="key", maximum=MAX_IDENTIFIER_BYTES),
        schema_id=body_reader.text(field="schema id", maximum=MAX_IDENTIFIER_BYTES),
        schema_version=body_reader.uint(4, field="schema version"),
        codec=body_reader.text(field="codec", maximum=MAX_IDENTIFIER_BYTES),
        content_type=body_reader.text(field="content type", maximum=MAX_IDENTIFIER_BYTES),
        payload_bytes=body_reader.optional_blob(field="payload", maximum=MAX_STATE_PAYLOAD_BYTES),
    )
    body_reader.finish(field="private-memory envelope body")
    _validate_private_memory_presence(result)
    return result


@dataclass(frozen=True, slots=True)
class StreamPosition:
    """Opaque stable position in one backend stream; equality only."""

    backend_id: str
    stream_id: str
    token: str


def encode_stream_position(position: StreamPosition) -> bytes:
    """Encode an opaque position without assigning it any public order."""
    reason = "persistence.position_invalid"
    return b"".join(
        (
            STREAM_POSITION_MAGIC,
            encode_uint(STREAM_POSITION_VERSION, 2, field="version", reason_code=reason),
            encode_text(STREAM_POSITION_FORMAT_ID, field="format id", reason_code=reason, identifier=True),
            encode_text(position.backend_id, field="backend id", maximum=MAX_POSITION_FIELD_BYTES, reason_code=reason, reject_controls=True),
            encode_text(position.stream_id, field="stream id", maximum=MAX_POSITION_FIELD_BYTES, reason_code=reason, reject_controls=True),
            encode_text(position.token, field="token", maximum=MAX_POSITION_FIELD_BYTES, reason_code=reason, reject_controls=True),
        )
    )


def decode_stream_position(encoded: bytes) -> StreamPosition:
    """Decode one transportable position with strict identity fields."""
    reader = BinaryReader(encoded, reason_code="persistence.position_invalid")
    if reader.take(8, field="magic") != STREAM_POSITION_MAGIC:
        raise _error("Unknown stream position magic.", "persistence.position_invalid")
    version = reader.uint(2, field="version")
    format_id = reader.text(field="format id", identifier=True)
    if version != STREAM_POSITION_VERSION or format_id != STREAM_POSITION_FORMAT_ID:
        raise _error("Unsupported stream position format.", "persistence.position_invalid")
    result = StreamPosition(
        backend_id=reader.text(field="backend id", maximum=MAX_POSITION_FIELD_BYTES, reject_controls=True),
        stream_id=reader.text(field="stream id", maximum=MAX_POSITION_FIELD_BYTES, reject_controls=True),
        token=reader.text(field="token", maximum=MAX_POSITION_FIELD_BYTES, reject_controls=True),
    )
    reader.finish(field="stream position")
    return result


@dataclass(frozen=True, slots=True)
class MigrationManifestEntry:
    """One evidence-selected source record in authoritative stream order."""

    authoritative_sequence: int
    authoritative_event_id: str
    source_state_ref: str
    expected_lineage_head_event_id: str
    state_digest: str | None
    digest_algorithm: str
    digest_policy_id: str
    integrity_status: str
    materialization_status: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MigrationManifest:
    """Deterministic identity of one bounded copy-on-write migration run."""

    namespace_id: str
    plan_id: str
    plan_version: int
    source_schema_id: str
    source_schema_version: int
    source_codec: str
    target_schema_id: str
    target_schema_version: int
    target_codec: str
    cutoff_position: StreamPosition
    entries: tuple[MigrationManifestEntry, ...]


def encode_migration_manifest(manifest: MigrationManifest) -> bytes:
    """Encode the deterministic, non-stored manifest preimage."""
    reason = "migration.manifest_invalid"
    encoded_entries: list[bytes] = []
    for entry in manifest.entries:
        digest_encoder = lambda value: encode_text(  # noqa: E731
            value, field="state digest", reason_code=reason
        )
        encoded_entries.append(
            b"".join(
                (
                    encode_uint(entry.authoritative_sequence, 8, field="authoritative sequence", reason_code=reason),
                    encode_text(entry.authoritative_event_id, field="authoritative event id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
                    encode_text(entry.source_state_ref, field="source state ref", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
                    encode_text(entry.expected_lineage_head_event_id, field="expected lineage head event id", maximum=MAX_CONTEXT_ID_BYTES, reason_code=reason),
                    encode_optional(entry.state_digest, encoder=digest_encoder),
                    encode_text(entry.digest_algorithm, field="digest algorithm", reason_code=reason, identifier=True),
                    encode_text(entry.digest_policy_id, field="digest policy id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
                    encode_text(entry.integrity_status, field="integrity status", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
                    encode_text(entry.materialization_status, field="materialization status", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
                    encode_uint(len(entry.reason_codes), 2, field="reason code count", reason_code=reason),
                    *(encode_text(code, field="reason code", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason) for code in entry.reason_codes),
                )
            )
        )
    return b"".join(
        (
            MIGRATION_MANIFEST_MAGIC,
            encode_uint(MIGRATION_MANIFEST_VERSION, 2, field="version", reason_code=reason),
            encode_text(MIGRATION_MANIFEST_FORMAT_ID, field="format id", reason_code=reason, identifier=True),
            encode_text("sha256", field="hash algorithm", reason_code=reason, identifier=True),
            encode_text(manifest.namespace_id, field="namespace id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(manifest.plan_id, field="plan id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_uint(manifest.plan_version, 2, field="plan version", reason_code=reason),
            encode_text(manifest.source_schema_id, field="source schema id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_uint(manifest.source_schema_version, 4, field="source schema version", reason_code=reason),
            encode_text(manifest.source_codec, field="source codec", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_text(manifest.target_schema_id, field="target schema id", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_uint(manifest.target_schema_version, 4, field="target schema version", reason_code=reason),
            encode_text(manifest.target_codec, field="target codec", maximum=MAX_IDENTIFIER_BYTES, reason_code=reason),
            encode_blob(encode_stream_position(manifest.cutoff_position), field="cutoff position", maximum=4096, reason_code=reason),
            encode_uint(len(manifest.entries), 4, field="entry count", reason_code=reason),
            *encoded_entries,
        )
    )


def migration_manifest_digest(manifest: MigrationManifest) -> bytes:
    """Return SHA-256 over the complete deterministic manifest preimage."""
    return sha256(encode_migration_manifest(manifest)).digest()
