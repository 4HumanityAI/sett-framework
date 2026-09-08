"""Normative byte vectors for the first SETT durable formats."""
from __future__ import annotations

import hashlib
import hmac

import pytest

from sett.exceptions import SETTPersistenceFormatError
from sett.persistence import (
    MigrationManifest,
    MigrationManifestEntry,
    PrivateMemoryMutationEnvelope,
    PrivateMemoryOperation,
    StateEnvelope,
    StreamPosition,
    decode_private_memory_mutation,
    decode_state_envelope,
    decode_stream_position,
    decode_trace_evidence_frame,
    encode_migration_manifest,
    encode_private_memory_mutation,
    encode_state_envelope,
    encode_stream_position,
    encode_trace_evidence_frame,
    migration_manifest_digest,
)

TRACE_PREIMAGE = (
    b'{"attributes":{},"canonicalization_id":"sett.trace-json.v1",'
    b'"cause_id":null,"component_name":"SETTOrchestrator",'
    b'"component_type":"orchestrator",'
    b'"event_id":"00000000-0000-0000-0000-000000000001",'
    b'"hash_algorithm_id":"sha256","kind":"trace.started",'
    b'"parent_id":null,"previous_hash":"00000000000000000000000000000000'
    b'00000000000000000000000000000000","reason_codes":[],"run_id":'
    b'"00000000-0000-0000-0000-000000000003","sequence":1,'
    b'"status":"started","timestamp":"2026-01-01T00:00:00+00:00",'
    b'"trace_id":"00000000-0000-0000-0000-000000000002"}'
)


def _assert_reason(error: pytest.ExceptionInfo[SETTPersistenceFormatError], code: str) -> None:
    assert error.value.reason_code == code


def test_trace_frame_matches_normative_vector_and_round_trips_exact_bytes() -> None:
    expected_hash = "33dc36fd5b028627c068de4fa649178c83fb8db7250928ac50a38f1323bf92c6"
    frame = encode_trace_evidence_frame(TRACE_PREIMAGE)

    assert hashlib.sha256(TRACE_PREIMAGE).hexdigest() == expected_hash
    assert len(frame) == 610
    decoded = decode_trace_evidence_frame(frame)
    assert decoded.preimage is TRACE_PREIMAGE or decoded.preimage == TRACE_PREIMAGE
    assert decoded.entry_hash.hex() == expected_hash
    assert decoded.payload["sequence"] == 1


def test_trace_frame_classifies_truncation_hash_and_trailing_data() -> None:
    frame = encode_trace_evidence_frame(TRACE_PREIMAGE)
    with pytest.raises(SETTPersistenceFormatError) as truncated:
        decode_trace_evidence_frame(frame[:-1])
    _assert_reason(truncated, "evidence.frame_truncated")

    altered = bytearray(frame)
    altered[-2] ^= 1
    with pytest.raises(SETTPersistenceFormatError) as mismatch:
        decode_trace_evidence_frame(bytes(altered))
    _assert_reason(mismatch, "evidence.hash_mismatch")

    with pytest.raises(SETTPersistenceFormatError) as trailing:
        decode_trace_evidence_frame(frame + b"\x00")
    _assert_reason(trailing, "evidence.frame_invalid")


def test_trace_frame_rejects_duplicate_keys_and_non_finite_numbers() -> None:
    duplicate = TRACE_PREIMAGE[:-1] + b',"trace_id":"duplicate"}'
    duplicate_frame = encode_trace_evidence_frame(duplicate)
    with pytest.raises(SETTPersistenceFormatError) as duplicate_error:
        decode_trace_evidence_frame(duplicate_frame)
    _assert_reason(duplicate_error, "evidence.payload_invalid")

    non_finite = TRACE_PREIMAGE.replace(b'"attributes":{}', b'"attributes":{"x":NaN}')
    non_finite_frame = encode_trace_evidence_frame(non_finite)
    with pytest.raises(SETTPersistenceFormatError) as number_error:
        decode_trace_evidence_frame(non_finite_frame)
    _assert_reason(number_error, "evidence.payload_invalid")


def test_state_envelope_matches_normative_vector() -> None:
    envelope = StateEnvelope(
        digest_algorithm="sha256",
        digest_policy_id="reference-sha256-v1",
        namespace_id="demo.namespace",
        trace_id="00000000-0000-0000-0000-000000000001",
        run_id="00000000-0000-0000-0000-000000000002",
        stage_index=0,
        schema_id="demo.stage-output",
        schema_version=1,
        codec="json",
        content_type="application/json",
        payload_bytes=b'{"ok":true}',
    )
    frame = encode_state_envelope(envelope)

    assert len(frame) == 257
    assert hashlib.sha256(frame).hexdigest() == (
        "d198041618c1066008534492a4e2eb3edff0c30dbc8e34b4a0c7e4943a62ce45"
    )
    assert decode_state_envelope(frame) == envelope


@pytest.mark.parametrize(
    ("operation", "key", "payload", "length", "expected_digest"),
    [
        (
            PrivateMemoryOperation.WRITE,
            "heart_rate_bpm",
            b"137",
            358,
            "01eaabfbd8863afbfa3a7b57fcc61f2d9ef88c07f1685df27e74603f5e29faf3",
        ),
        (
            PrivateMemoryOperation.CLEAR,
            None,
            None,
            335,
            "cb3a02b886259288e4f5c7f9d64bb5f7048ff8ca89d816a2b9a9d2eac8d56f20",
        ),
    ],
)
def test_private_memory_vectors(
    operation: PrivateMemoryOperation,
    key: str | None,
    payload: bytes | None,
    length: int,
    expected_digest: str,
) -> None:
    envelope = PrivateMemoryMutationEnvelope(
        digest_algorithm="hmac-sha256",
        digest_policy_id="demo.private-memory.hmac.v1",
        namespace_id="demo.namespace",
        memory_id="memory-001",
        trace_id="00000000-0000-0000-0000-000000000001",
        run_id="00000000-0000-0000-0000-000000000002",
        mutation_event_id="00000000-0000-0000-0000-000000000003",
        operation=operation,
        key=key,
        schema_id="demo.private-memory-value",
        schema_version=1,
        codec="json",
        content_type="application/json",
        payload_bytes=payload,
    )
    frame = encode_private_memory_mutation(envelope)
    test_key = bytes(range(32))

    assert len(frame) == length
    assert hmac.new(test_key, frame, hashlib.sha256).hexdigest() == expected_digest
    assert decode_private_memory_mutation(frame) == envelope


def test_private_memory_presence_is_structural() -> None:
    invalid = PrivateMemoryMutationEnvelope(
        digest_algorithm="none",
        digest_policy_id="no-digest-v1",
        namespace_id="demo.namespace",
        memory_id="memory-001",
        trace_id="trace",
        run_id="run",
        mutation_event_id="event",
        operation=PrivateMemoryOperation.CLEAR,
        key="unexpected",
        schema_id="demo.private-memory-value",
        schema_version=1,
        codec="json",
        content_type="application/json",
        payload_bytes=None,
    )
    with pytest.raises(SETTPersistenceFormatError) as error:
        encode_private_memory_mutation(invalid)
    _assert_reason(error, "state.envelope_invalid")


def test_stream_position_matches_normative_vector() -> None:
    position = StreamPosition("backend.demo", "evidence.main", "0000000000000001")
    encoded = encode_stream_position(position)
    assert len(encoded) == 82
    assert encoded.hex() == (
        "53455454504f530000010017736574742e73747265616d2d706f736974696f6e2e"
        "7631000c6261636b656e642e64656d6f000d65766964656e63652e6d61696e0010"
        "30303030303030303030303030303031"
    )
    assert decode_stream_position(encoded) == position


def test_migration_manifest_matches_normative_digest() -> None:
    manifest = MigrationManifest(
        namespace_id="demo.namespace",
        plan_id="demo.plan",
        plan_version=1,
        source_schema_id="demo.stage-output",
        source_schema_version=1,
        source_codec="json",
        target_schema_id="demo.stage-output",
        target_schema_version=2,
        target_codec="json",
        cutoff_position=StreamPosition(
            "backend.demo", "evidence.main", "0000000000000001"
        ),
        entries=(
            MigrationManifestEntry(
                authoritative_sequence=2,
                authoritative_event_id="00000000-0000-0000-0000-000000000004",
                source_state_ref="state-001",
                expected_lineage_head_event_id=(
                    "00000000-0000-0000-0000-000000000004"
                ),
                state_digest=(
                    "d198041618c1066008534492a4e2eb3edff0c30dbc8e34b4a0c7e4943a62ce45"
                ),
                digest_algorithm="sha256",
                digest_policy_id="reference-sha256-v1",
                integrity_status="verified",
                materialization_status="materialized",
            ),
        ),
    )
    assert len(encode_migration_manifest(manifest)) == 440
    assert migration_manifest_digest(manifest).hex() == (
        "451879e45c53e7711ad980dab6a10a467ba71fda4b5350b4c68cf2dd9d38feee"
    )
