"""Versioned derivation of caller-owned persistence write identities."""
from __future__ import annotations

from hashlib import sha256

from sett.exceptions import SETTValidationError

EVIDENCE_WRITE_ID_VERSION = 1
STAGE_OUTPUT_WRITE_ID_VERSION = 1
PRIVATE_MEMORY_WRITE_ID_VERSION = 1
MIGRATION_WRITE_ID_VERSION = 1


def _identity_part(value: str, *, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SETTValidationError(f"{field} must be a non-empty string.")
    encoded = value.encode("utf-8", "strict")
    if len(encoded) > 65535:
        raise SETTValidationError(f"{field} exceeds 65535 UTF-8 bytes.")
    return len(encoded).to_bytes(2, "big") + encoded


def derive_evidence_write_id(
    *,
    namespace_id: str,
    event_id: str,
    version: int = EVIDENCE_WRITE_ID_VERSION,
) -> str:
    """Derive a restart-stable identity for one immutable trace event."""
    if version != EVIDENCE_WRITE_ID_VERSION:
        raise SETTValidationError("Unsupported evidence write_id version.")
    preimage = b"".join(
        (
            b"SETT-WRITE-ID\0evidence\0",
            version.to_bytes(2, "big"),
            _identity_part(namespace_id, field="namespace_id"),
            _identity_part(event_id, field="event_id"),
        )
    )
    return f"evidence.v{version}.{sha256(preimage).hexdigest()}"


def derive_stage_output_write_id(
    *,
    namespace_id: str,
    trace_id: str,
    run_id: str,
    stage_index: int,
    version: int = STAGE_OUTPUT_WRITE_ID_VERSION,
) -> str:
    """Derive the restart-stable identity of one pipeline stage output."""
    if version != STAGE_OUTPUT_WRITE_ID_VERSION:
        raise SETTValidationError("Unsupported stage-output write_id version.")
    if (
        isinstance(stage_index, bool)
        or not isinstance(stage_index, int)
        or not 0 <= stage_index <= 0xFFFFFFFF
    ):
        raise SETTValidationError(
            "stage_index must be an integer between 0 and 4294967295."
        )
    preimage = b"".join(
        (
            b"SETT-WRITE-ID\0stage-output\0",
            version.to_bytes(2, "big"),
            _identity_part(namespace_id, field="namespace_id"),
            _identity_part(trace_id, field="trace_id"),
            _identity_part(run_id, field="run_id"),
            stage_index.to_bytes(4, "big"),
        )
    )
    return f"stage-output.v{version}.{sha256(preimage).hexdigest()}"


def derive_private_memory_write_id(
    *,
    namespace_id: str,
    mutation_event_id: str,
    version: int = PRIVATE_MEMORY_WRITE_ID_VERSION,
) -> str:
    """Derive the restart-stable identity of one private-memory mutation."""
    if version != PRIVATE_MEMORY_WRITE_ID_VERSION:
        raise SETTValidationError("Unsupported private-memory write_id version.")
    preimage = b"".join(
        (
            b"SETT-WRITE-ID\0private-memory\0",
            version.to_bytes(2, "big"),
            _identity_part(namespace_id, field="namespace_id"),
            _identity_part(mutation_event_id, field="mutation_event_id"),
        )
    )
    return f"private-memory.v{version}.{sha256(preimage).hexdigest()}"


def derive_migration_write_id(
    *,
    namespace_id: str,
    source_state_ref: str,
    target_envelope_version: int,
    target_schema_id: str,
    target_schema_version: int,
    target_codec: str,
    target_content_type: str,
    version: int = MIGRATION_WRITE_ID_VERSION,
) -> str:
    """Derive one copy-on-write destination identity from its exact route."""
    if version != MIGRATION_WRITE_ID_VERSION:
        raise SETTValidationError("Unsupported migration write_id version.")
    for field, value in (
        ("target_envelope_version", target_envelope_version),
        ("target_schema_version", target_schema_version),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 0xFFFFFFFF
        ):
            raise SETTValidationError(
                f"{field} must be an integer between 1 and 4294967295."
            )
    preimage = b"".join(
        (
            b"SETT-WRITE-ID\0migration\0",
            version.to_bytes(2, "big"),
            _identity_part(namespace_id, field="namespace_id"),
            _identity_part(source_state_ref, field="source_state_ref"),
            target_envelope_version.to_bytes(4, "big"),
            _identity_part(target_schema_id, field="target_schema_id"),
            target_schema_version.to_bytes(4, "big"),
            _identity_part(target_codec, field="target_codec"),
            _identity_part(target_content_type, field="target_content_type"),
        )
    )
    return f"migration.v{version}.{sha256(preimage).hexdigest()}"
