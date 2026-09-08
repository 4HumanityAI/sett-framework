"""Durable pipeline stage outputs and explicit materialization."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sett.audit_ruler.integrity import (
    IntegrityPolicy,
    NoDigestPolicy,
    digest_from_trace_value,
    digest_to_trace_value,
)
from sett.exceptions import (
    SETTPersistenceError,
    SETTPersistenceFormatError,
    SETTValidationError,
)
from sett.persistence.contracts import (
    DurabilityLevel,
    StateRecordStore,
    satisfies_durability,
)
from sett.persistence.formats import (
    StateEnvelope,
    decode_state_envelope,
    encode_state_envelope,
)
from sett.persistence.identity import (
    STAGE_OUTPUT_WRITE_ID_VERSION,
    derive_stage_output_write_id,
)

PIPELINE_OUTPUT_SCHEMA_ID = "sett.pipeline-stage-output"
PIPELINE_OUTPUT_SCHEMA_VERSION = 1
PIPELINE_OUTPUT_CODEC = "sett.json.v1"
PIPELINE_OUTPUT_CONTENT_TYPE = "application/json"


class IntegrityStatus(str, Enum):
    """Whether the historical state-integrity assertion could be evaluated."""

    NOT_EVALUATED = "not_evaluated"
    VERIFIED = "verified"
    EXPLICITLY_DISABLED = "explicitly_disabled"
    POLICY_UNAVAILABLE = "policy_unavailable"
    POLICY_NOT_RECORDED = "policy_not_recorded"
    MISMATCH = "mismatch"


class MaterializationStatus(str, Enum):
    """Result of locating, validating, and decoding one durable output."""

    NOT_EVALUATED = "not_evaluated"
    RECORD_MISSING = "record_missing"
    NAMESPACE_MISMATCH = "namespace_mismatch"
    EXECUTION_IDENTITY_MISMATCH = "execution_identity_mismatch"
    EVIDENCE_METADATA_MISMATCH = "evidence_metadata_mismatch"
    ENVELOPE_UNSUPPORTED = "envelope_unsupported"
    ENVELOPE_INVALID = "envelope_invalid"
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    CODEC_UNSUPPORTED = "codec_unsupported"
    DECODE_FAILED = "decode_failed"
    MATERIALIZED = "materialized"


@dataclass(frozen=True, slots=True)
class StageStateDescriptor:
    """JSON-safe evidence that identifies one durable stage output."""

    stage_index: int
    run_id: str
    state_ref: str
    state_digest: str | None
    digest_algorithm: str
    digest_policy_id: str
    schema_id: str
    schema_version: int
    codec: str
    content_type: str
    backend_id: str
    achieved_durability: DurabilityLevel
    write_id_version: int = STAGE_OUTPUT_WRITE_ID_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.stage_index, bool)
            or not isinstance(self.stage_index, int)
            or self.stage_index < 0
        ):
            raise SETTValidationError(
                "StageStateDescriptor.stage_index must be a non-negative integer."
            )
        for name in (
            "run_id",
            "state_ref",
            "digest_algorithm",
            "digest_policy_id",
            "schema_id",
            "codec",
            "content_type",
            "backend_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise SETTValidationError(
                    f"StageStateDescriptor.{name} must be a non-empty string."
                )
        digest_from_trace_value(self.state_digest)
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise SETTValidationError(
                "StageStateDescriptor.schema_version must be a positive integer."
            )
        if not isinstance(self.achieved_durability, DurabilityLevel):
            raise SETTValidationError(
                "StageStateDescriptor.achieved_durability must be a "
                "DurabilityLevel."
            )
        if (
            isinstance(self.write_id_version, bool)
            or not isinstance(self.write_id_version, int)
            or self.write_id_version < 1
        ):
            raise SETTValidationError(
                "StageStateDescriptor.write_id_version must be a positive integer."
            )

    def trace_attributes(self) -> dict[str, Any]:
        """Return the complete public evidence view for stage completion."""
        return {
            "index": self.stage_index,
            "state_ref": self.state_ref,
            "state_digest": self.state_digest,
            "digest_algorithm": self.digest_algorithm,
            "digest_policy_id": self.digest_policy_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "codec": self.codec,
            "content_type": self.content_type,
            "state_backend_id": self.backend_id,
            "snapshot_durability": self.achieved_durability.value,
            "write_id_version": self.write_id_version,
        }


@dataclass(frozen=True, slots=True)
class StageMaterializationResult:
    """Both diagnostic axes and the decoded value, when materialized."""

    descriptor: StageStateDescriptor
    integrity_status: IntegrityStatus
    materialization_status: MaterializationStatus
    reason_code: str
    value: dict[str, Any] | None = None


def _encode_pipeline_output(output: dict[str, Any]) -> bytes:
    if not isinstance(output, dict):
        raise SETTValidationError("Pipeline stage output must be a dict.")
    try:
        return json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise SETTPersistenceError(
            "Pipeline stage output is not representable as strict JSON.",
            reason_code="state.output_not_serializable",
        ) from error


def persist_stage_output(
    output: dict[str, Any],
    *,
    store: StateRecordStore,
    namespace_id: str,
    trace_id: str,
    run_id: str,
    stage_index: int,
    integrity_policy: IntegrityPolicy,
    required_durability: DurabilityLevel,
    write_id_version: int = STAGE_OUTPUT_WRITE_ID_VERSION,
) -> StageStateDescriptor:
    """Persist exact output bytes before producing completion evidence."""
    payload = _encode_pipeline_output(output)
    envelope = StateEnvelope(
        digest_algorithm=integrity_policy.algorithm,
        digest_policy_id=integrity_policy.policy_id,
        namespace_id=namespace_id,
        trace_id=trace_id,
        run_id=run_id,
        stage_index=stage_index,
        schema_id=PIPELINE_OUTPUT_SCHEMA_ID,
        schema_version=PIPELINE_OUTPUT_SCHEMA_VERSION,
        codec=PIPELINE_OUTPUT_CODEC,
        content_type=PIPELINE_OUTPUT_CONTENT_TYPE,
        payload_bytes=payload,
    )
    frame = encode_state_envelope(envelope)
    digest = integrity_policy.digest(frame)
    write_id = derive_stage_output_write_id(
        namespace_id=namespace_id,
        trace_id=trace_id,
        run_id=run_id,
        stage_index=stage_index,
        version=write_id_version,
    )
    receipt = store.put(write_id=write_id, payload=frame)
    if not satisfies_durability(
        receipt.achieved_durability, required_durability
    ):
        raise SETTPersistenceError(
            "Stage output did not reach the required durability.",
            reason_code="state.snapshot_durability_unsatisfied",
            backend_id=receipt.backend_id,
        )
    return StageStateDescriptor(
        stage_index=stage_index,
        run_id=run_id,
        state_ref=receipt.state_ref,
        state_digest=digest_to_trace_value(digest),
        digest_algorithm=integrity_policy.algorithm,
        digest_policy_id=integrity_policy.policy_id,
        schema_id=PIPELINE_OUTPUT_SCHEMA_ID,
        schema_version=PIPELINE_OUTPUT_SCHEMA_VERSION,
        codec=PIPELINE_OUTPUT_CODEC,
        content_type=PIPELINE_OUTPUT_CONTENT_TYPE,
        backend_id=receipt.backend_id,
        achieved_durability=receipt.achieved_durability,
        write_id_version=write_id_version,
    )


def _result(
    descriptor: StageStateDescriptor,
    integrity: IntegrityStatus,
    materialization: MaterializationStatus,
    reason_code: str,
    value: dict[str, Any] | None = None,
) -> StageMaterializationResult:
    return StageMaterializationResult(
        descriptor=descriptor,
        integrity_status=integrity,
        materialization_status=materialization,
        reason_code=reason_code,
        value=value,
    )


def materialize_stage_output(
    descriptor: StageStateDescriptor,
    *,
    store: StateRecordStore,
    namespace_id: str,
    trace_id: str,
    policies: Mapping[str, IntegrityPolicy],
) -> StageMaterializationResult:
    """Verify exact bytes before parsing, interpreting, or decoding output."""
    try:
        record = store.get(state_ref=descriptor.state_ref)
    except SETTPersistenceError as error:
        if error.reason_code == "state.record_missing":
            return _result(
                descriptor,
                IntegrityStatus.NOT_EVALUATED,
                MaterializationStatus.RECORD_MISSING,
                "state.record_missing",
            )
        raise

    if not descriptor.digest_policy_id:
        return _result(
            descriptor,
            IntegrityStatus.POLICY_NOT_RECORDED,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_policy_not_recorded",
        )
    policy = policies.get(descriptor.digest_policy_id)
    if policy is None:
        return _result(
            descriptor,
            IntegrityStatus.POLICY_UNAVAILABLE,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_policy_unavailable",
        )
    if (
        policy.policy_id != descriptor.digest_policy_id
        or policy.algorithm != descriptor.digest_algorithm
    ):
        return _result(
            descriptor,
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_policy_mismatch",
        )
    try:
        expected_digest = digest_from_trace_value(descriptor.state_digest)
    except SETTValidationError:
        return _result(
            descriptor,
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_encoding_invalid",
        )
    if not policy.verify(record.payload, expected_digest):
        return _result(
            descriptor,
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_mismatch",
        )
    integrity_status = (
        IntegrityStatus.EXPLICITLY_DISABLED
        if isinstance(policy, NoDigestPolicy)
        else IntegrityStatus.VERIFIED
    )

    try:
        envelope = decode_state_envelope(record.payload)
    except SETTPersistenceFormatError as error:
        status = (
            MaterializationStatus.ENVELOPE_UNSUPPORTED
            if error.reason_code == "state.envelope_unsupported"
            else MaterializationStatus.ENVELOPE_INVALID
        )
        return _result(descriptor, integrity_status, status, error.reason_code)
    if envelope.namespace_id != namespace_id:
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.NAMESPACE_MISMATCH,
            "state.namespace_mismatch",
        )
    if (
        envelope.trace_id != trace_id
        or envelope.run_id != descriptor.run_id
        or envelope.stage_index != descriptor.stage_index
    ):
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.EXECUTION_IDENTITY_MISMATCH,
            "state.execution_identity_mismatch",
        )
    if (
        envelope.digest_algorithm != descriptor.digest_algorithm
        or envelope.digest_policy_id != descriptor.digest_policy_id
        or envelope.schema_id != descriptor.schema_id
        or envelope.schema_version != descriptor.schema_version
        or envelope.codec != descriptor.codec
        or envelope.content_type != descriptor.content_type
    ):
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.EVIDENCE_METADATA_MISMATCH,
            "state.evidence_metadata_mismatch",
        )
    if (
        envelope.schema_id != PIPELINE_OUTPUT_SCHEMA_ID
        or envelope.schema_version != PIPELINE_OUTPUT_SCHEMA_VERSION
    ):
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.SCHEMA_UNSUPPORTED,
            "state.schema_unsupported",
        )
    if (
        envelope.codec != PIPELINE_OUTPUT_CODEC
        or envelope.content_type != PIPELINE_OUTPUT_CONTENT_TYPE
    ):
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.CODEC_UNSUPPORTED,
            "state.codec_unsupported",
        )
    try:
        value = json.loads(envelope.payload_bytes.decode("utf-8", "strict"))
    except (UnicodeError, ValueError, TypeError):
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.DECODE_FAILED,
            "state.decode_failed",
        )
    if not isinstance(value, dict):
        return _result(
            descriptor,
            integrity_status,
            MaterializationStatus.DECODE_FAILED,
            "state.decoded_value_invalid",
        )
    return _result(
        descriptor,
        integrity_status,
        MaterializationStatus.MATERIALIZED,
        "state.materialized",
        value,
    )
