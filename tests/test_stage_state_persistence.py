"""Durable pipeline output ordering, integrity, and materialization."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sett.audit_ruler.integrity import (
    NoDigestPolicy,
    Sha256IntegrityPolicy,
    digest_from_trace_value,
    digest_to_trace_value,
)
from sett.exceptions import SETTConfigurationError, SETTValidationError
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryStateRecordStore,
    IntegrityStatus,
    MaterializationStatus,
    PersistencePolicy,
    materialize_stage_output,
    persist_stage_output,
)


def _policy(integrity_policy=None) -> PersistencePolicy:
    return PersistencePolicy(
        snapshot_durability=DurabilityLevel.IN_MEMORY,
        evidence_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=integrity_policy or Sha256IntegrityPolicy(),
        audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )


def test_persistence_policy_has_no_implicit_unknown_or_audit_gap_choice() -> None:
    with pytest.raises(SETTConfigurationError, match="UNKNOWN"):
        PersistencePolicy(
            snapshot_durability=DurabilityLevel.UNKNOWN,
            evidence_durability=DurabilityLevel.IN_MEMORY,
            integrity_policy=Sha256IntegrityPolicy(),
            audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
        )
    with pytest.raises(SETTConfigurationError, match="at least"):
        PersistencePolicy(
            snapshot_durability=DurabilityLevel.IN_MEMORY,
            evidence_durability=DurabilityLevel.POWER_LOSS,
            integrity_policy=Sha256IntegrityPolicy(),
            audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
        )


def test_digest_trace_representation_is_lowercase_hex_or_explicit_none() -> None:
    digest = bytes.fromhex("00aaff")
    assert digest_to_trace_value(digest) == "00aaff"
    assert digest_from_trace_value("00aaff") == digest
    assert digest_to_trace_value(None) is None
    assert digest_from_trace_value(None) is None
    for invalid in ("", "ABC0", "abc", "0x00"):
        with pytest.raises(SETTValidationError):
            digest_from_trace_value(invalid)


def test_stage_output_round_trip_is_idempotent_and_bound_to_execution() -> None:
    store = InMemoryStateRecordStore(backend_id="backend.demo")
    policy = _policy()
    arguments = {
        "store": store,
        "namespace_id": "demo.namespace",
        "trace_id": "trace-1",
        "run_id": "run-1",
        "stage_index": 0,
        "integrity_policy": policy.integrity_policy,
        "required_durability": policy.snapshot_durability,
    }
    first = persist_stage_output({"answer": 3}, **arguments)
    replay = persist_stage_output({"answer": 3}, **arguments)

    assert replay.state_ref == first.state_ref
    assert replay.state_digest == first.state_digest
    result = materialize_stage_output(
        first,
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert result.integrity_status is IntegrityStatus.VERIFIED
    assert result.materialization_status is MaterializationStatus.MATERIALIZED
    assert result.value == {"answer": 3}

    wrong_run = replace(first, run_id="run-2")
    mismatch = materialize_stage_output(
        wrong_run,
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert mismatch.integrity_status is IntegrityStatus.VERIFIED
    assert (
        mismatch.materialization_status
        is MaterializationStatus.EXECUTION_IDENTITY_MISMATCH
    )


def test_no_digest_is_declared_and_not_confused_with_missing_policy() -> None:
    store = InMemoryStateRecordStore(backend_id="backend.demo")
    policy = _policy(NoDigestPolicy())
    descriptor = persist_stage_output(
        {"answer": 3},
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        run_id="run-1",
        stage_index=0,
        integrity_policy=policy.integrity_policy,
        required_durability=policy.snapshot_durability,
    )
    assert descriptor.state_digest is None
    assert descriptor.digest_policy_id == "sett.no-digest.v1"

    materialized = materialize_stage_output(
        descriptor,
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        policies={policy.integrity_policy.policy_id: policy.integrity_policy},
    )
    assert materialized.integrity_status is IntegrityStatus.EXPLICITLY_DISABLED
    assert materialized.materialization_status is MaterializationStatus.MATERIALIZED

    unavailable = materialize_stage_output(
        descriptor,
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        policies={},
    )
    assert unavailable.integrity_status is IntegrityStatus.POLICY_UNAVAILABLE
    assert unavailable.materialization_status is MaterializationStatus.NOT_EVALUATED


def test_evidence_metadata_mismatch_does_not_reinterpret_authentic_bytes() -> None:
    store = InMemoryStateRecordStore(backend_id="backend.demo")
    integrity = Sha256IntegrityPolicy()
    descriptor = persist_stage_output(
        {"answer": 3},
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        run_id="run-1",
        stage_index=0,
        integrity_policy=integrity,
        required_durability=DurabilityLevel.IN_MEMORY,
    )
    contradicted = replace(descriptor, codec="sett.other-json.v1")
    result = materialize_stage_output(
        contradicted,
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        policies={integrity.policy_id: integrity},
    )
    assert result.integrity_status is IntegrityStatus.VERIFIED
    assert (
        result.materialization_status
        is MaterializationStatus.EVIDENCE_METADATA_MISMATCH
    )


def test_digest_mismatch_stops_before_envelope_parsing() -> None:
    store = InMemoryStateRecordStore(backend_id="backend.demo")
    integrity = Sha256IntegrityPolicy()
    descriptor = persist_stage_output(
        {"answer": 3},
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        run_id="run-1",
        stage_index=0,
        integrity_policy=integrity,
        required_durability=DurabilityLevel.IN_MEMORY,
    )
    result = materialize_stage_output(
        replace(descriptor, state_digest="00" * 32),
        store=store,
        namespace_id="demo.namespace",
        trace_id="trace-1",
        policies={integrity.policy_id: integrity},
    )
    assert result.integrity_status is IntegrityStatus.MISMATCH
    assert result.materialization_status is MaterializationStatus.NOT_EVALUATED
    assert result.value is None
