"""Deterministic copy-on-write migration plans for durable stage state."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from sett.audit_ruler.integrity import (
    IntegrityPolicy,
    NoDigestPolicy,
    digest_from_trace_value,
    digest_to_trace_value,
)
from sett.core_ruler.execution_context import ExecutionContext
from sett.exceptions import (
    SETTConfigurationError,
    SETTPersistenceError,
    SETTPersistenceFormatError,
    SETTValidationError,
)
from sett.persistence.contracts import PersistenceBackend, satisfies_durability
from sett.persistence.formats import (
    STATE_ENVELOPE_VERSION,
    MigrationManifest,
    MigrationManifestEntry,
    PrivateMemoryMutationEnvelope,
    StateEnvelope,
    decode_private_memory_mutation,
    decode_state_envelope,
    encode_private_memory_mutation,
    encode_state_envelope,
    encode_stream_position,
    migration_manifest_digest,
)
from sett.persistence.identity import (
    MIGRATION_WRITE_ID_VERSION,
    derive_migration_write_id,
)
from sett.persistence.policy import PersistencePolicy
from sett.persistence.private_memory import (
    PrivateMemoryMutationDescriptor,
    _private_memory_mutation_heads,
    _PrivateMemoryMutationHead,
)
from sett.persistence.state import (
    IntegrityStatus,
    MaterializationStatus,
    StageStateDescriptor,
)

if TYPE_CHECKING:
    from sett.audit_ruler.trace import RecoveredStage, RecoveryReport

MigrationTransform = Callable[[bytes], bytes]
_ALLOWED_INTEGRITY_STATUSES = frozenset(
    {IntegrityStatus.VERIFIED, IntegrityStatus.EXPLICITLY_DISABLED}
)


class MigrationRemedyOwner(str, Enum):
    """The party or capability class that can address a blocked source."""

    NONE = "none"
    DEPLOYMENT_CAPABILITY = "deployment_capability"
    PLAN_CONFIGURATION = "plan_configuration"
    STORE_OR_CONFIGURATION = "store_or_configuration"
    DATA_OR_DECODER = "data_or_decoder"
    INVESTIGATION = "investigation"


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """One explicit, single-hop route within a state record store."""

    plan_id: str
    plan_version: int
    source_schema_id: str
    source_schema_version: int
    source_codec: str
    target_schema_id: str
    target_schema_version: int
    target_codec: str
    target_content_type: str
    transform: MigrationTransform
    accepted_integrity_statuses: frozenset[IntegrityStatus]
    target_envelope_version: int = STATE_ENVELOPE_VERSION
    write_id_version: int = MIGRATION_WRITE_ID_VERSION

    def __post_init__(self) -> None:
        for name in (
            "plan_id",
            "source_schema_id",
            "source_codec",
            "target_schema_id",
            "target_codec",
            "target_content_type",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise SETTValidationError(f"MigrationPlan.{name} cannot be empty.")
        for name in (
            "plan_version",
            "source_schema_version",
            "target_schema_version",
            "target_envelope_version",
            "write_id_version",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SETTValidationError(
                    f"MigrationPlan.{name} must be a positive integer."
                )
        if self.target_envelope_version != STATE_ENVELOPE_VERSION:
            raise SETTConfigurationError(
                "This SETT release can write only StateEnvelope version 1."
            )
        if self.write_id_version != MIGRATION_WRITE_ID_VERSION:
            raise SETTConfigurationError(
                "This SETT release supports only migration write_id version 1."
            )
        if not callable(self.transform):
            raise SETTValidationError("MigrationPlan.transform must be callable.")
        statuses = frozenset(self.accepted_integrity_statuses)
        if not statuses or not statuses.issubset(_ALLOWED_INTEGRITY_STATUSES):
            raise SETTConfigurationError(
                "accepted_integrity_statuses must explicitly select VERIFIED, "
                "EXPLICITLY_DISABLED, or both."
            )
        object.__setattr__(self, "accepted_integrity_statuses", statuses)


@dataclass(frozen=True, slots=True)
class MigrationAssessment:
    """Both source diagnostic axes and the resulting migration gate."""

    stage: RecoveredStage
    integrity_status: IntegrityStatus
    materialization_status: MaterializationStatus
    reason_code: str
    migratable: bool
    remedy_owner: MigrationRemedyOwner


@dataclass(frozen=True, slots=True)
class MigrationDryRun:
    """Immutable derived manifest plus every selected source assessment."""

    plan: MigrationPlan
    report: RecoveryReport
    manifest: MigrationManifest
    manifest_digest: str
    assessments: tuple[MigrationAssessment, ...]

    @property
    def ready(self) -> bool:
        """Whether every selected source passed both migration gates."""
        return all(item.migratable for item in self.assessments)

    @property
    def counts_by_integrity_status(self) -> dict[IntegrityStatus, int]:
        """Return detached counts for the integrity axis."""
        counts: dict[IntegrityStatus, int] = {}
        for item in self.assessments:
            counts[item.integrity_status] = counts.get(item.integrity_status, 0) + 1
        return counts

    @property
    def counts_by_materialization_status(
        self,
    ) -> dict[MaterializationStatus, int]:
        """Return detached counts for the materialization axis."""
        counts: dict[MaterializationStatus, int] = {}
        for item in self.assessments:
            counts[item.materialization_status] = (
                counts.get(item.materialization_status, 0) + 1
            )
        return counts

    @property
    def counts_by_remedy_owner(self) -> dict[MigrationRemedyOwner, int]:
        """Group dry-run findings by the owner of the next action."""
        counts: dict[MigrationRemedyOwner, int] = {}
        for item in self.assessments:
            counts[item.remedy_owner] = counts.get(item.remedy_owner, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class PrivateMemoryMigrationAssessment:
    """Both diagnostic axes for one evidence-confirmed memory mutation."""

    memory_id: str
    mutation_event_id: str
    authoritative_event_id: str
    descriptor: PrivateMemoryMutationDescriptor
    integrity_status: IntegrityStatus
    materialization_status: MaterializationStatus
    reason_code: str
    migratable: bool
    remedy_owner: MigrationRemedyOwner


@dataclass(frozen=True, slots=True)
class PrivateMemoryMigrationDryRun:
    """Derived manifest for one private-memory schema route."""

    plan: MigrationPlan
    report: RecoveryReport
    memory_id: str
    manifest: MigrationManifest
    manifest_digest: str
    assessments: tuple[PrivateMemoryMigrationAssessment, ...]

    @property
    def ready(self) -> bool:
        """Whether every selected mutation passed both migration gates."""
        return all(item.migratable for item in self.assessments)

    @property
    def counts_by_integrity_status(self) -> dict[IntegrityStatus, int]:
        """Return detached counts for the integrity axis."""
        counts: dict[IntegrityStatus, int] = {}
        for item in self.assessments:
            counts[item.integrity_status] = counts.get(item.integrity_status, 0) + 1
        return counts

    @property
    def counts_by_materialization_status(
        self,
    ) -> dict[MaterializationStatus, int]:
        """Return detached counts for the materialization axis."""
        counts: dict[MaterializationStatus, int] = {}
        for item in self.assessments:
            counts[item.materialization_status] = (
                counts.get(item.materialization_status, 0) + 1
            )
        return counts

    @property
    def counts_by_remedy_owner(self) -> dict[MigrationRemedyOwner, int]:
        """Group private-memory findings by the owner of the next action."""
        counts: dict[MigrationRemedyOwner, int] = {}
        for item in self.assessments:
            counts[item.remedy_owner] = counts.get(item.remedy_owner, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class MigratedStateRecord:
    """One confirmed copy-on-write destination."""

    source_state_ref: str
    destination_state_ref: str
    evidence_event_id: str
    state_digest: str | None


@dataclass(frozen=True, slots=True)
class MigrationRunResult:
    """A completed migration run whose terminal evidence is durable."""

    migration_run_id: str
    manifest_digest: str
    records: tuple[MigratedStateRecord, ...]
    terminal_event_id: str


@dataclass(frozen=True, slots=True)
class _StageHead:
    stage: RecoveredStage
    trace_id: str
    authoritative_sequence: int
    lineage_integrity_status: IntegrityStatus | None = None


def _stage_heads(report: RecoveryReport) -> tuple[_StageHead, ...]:
    events = {event.event_id: event for event in report.events}
    heads: list[_StageHead] = []
    for trace in report.traces:
        for stage in trace.stages:
            if stage.lineage_conflict:
                raise SETTPersistenceError(
                    "Migration evidence contains a branched lineage.",
                    reason_code="migration.lineage_conflict",
                    backend_id=report.backend_id,
                )
            authoritative_event_id = (
                stage.authoritative_event_id or stage.completion_event_id
            )
            event = events.get(authoritative_event_id)
            if event is None:
                raise SETTPersistenceError(
                    "Migration evidence references an unknown lineage head.",
                    reason_code="migration.lineage_invalid",
                    backend_id=report.backend_id,
                )
            lineage_integrity = (
                None
                if stage.lineage_integrity_status is None
                else IntegrityStatus(stage.lineage_integrity_status)
            )
            heads.append(
                _StageHead(
                    stage=stage,
                    trace_id=trace.trace_id,
                    authoritative_sequence=event.sequence,
                    lineage_integrity_status=lineage_integrity,
                )
            )
    return tuple(sorted(heads, key=lambda item: item.authoritative_sequence))


def _assess_source(
    descriptor: StageStateDescriptor,
    *,
    store,
    namespace_id: str,
    trace_id: str,
    policies: Mapping[str, IntegrityPolicy],
) -> tuple[
    IntegrityStatus,
    MaterializationStatus,
    str,
    StateEnvelope | None,
]:
    try:
        record = store.get(state_ref=descriptor.state_ref)
    except SETTPersistenceError as error:
        if error.reason_code == "state.record_missing":
            return (
                IntegrityStatus.NOT_EVALUATED,
                MaterializationStatus.RECORD_MISSING,
                error.reason_code,
                None,
            )
        raise
    if not descriptor.digest_policy_id:
        return (
            IntegrityStatus.POLICY_NOT_RECORDED,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_policy_not_recorded",
            None,
        )
    integrity_policy = policies.get(descriptor.digest_policy_id)
    if integrity_policy is None:
        return (
            IntegrityStatus.POLICY_UNAVAILABLE,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_policy_unavailable",
            None,
        )
    try:
        expected_digest = digest_from_trace_value(descriptor.state_digest)
    except SETTValidationError:
        return (
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_encoding_invalid",
            None,
        )
    if (
        integrity_policy.policy_id != descriptor.digest_policy_id
        or integrity_policy.algorithm != descriptor.digest_algorithm
        or not integrity_policy.verify(record.payload, expected_digest)
    ):
        return (
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_mismatch",
            None,
        )
    integrity = (
        IntegrityStatus.EXPLICITLY_DISABLED
        if isinstance(integrity_policy, NoDigestPolicy)
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
        return integrity, status, error.reason_code, None
    if envelope.namespace_id != namespace_id:
        return (
            integrity,
            MaterializationStatus.NAMESPACE_MISMATCH,
            "state.namespace_mismatch",
            None,
        )
    if (
        envelope.trace_id != trace_id
        or envelope.run_id != descriptor.run_id
        or envelope.stage_index != descriptor.stage_index
    ):
        return (
            integrity,
            MaterializationStatus.EXECUTION_IDENTITY_MISMATCH,
            "state.execution_identity_mismatch",
            None,
        )
    if (
        envelope.digest_algorithm != descriptor.digest_algorithm
        or envelope.digest_policy_id != descriptor.digest_policy_id
        or envelope.schema_id != descriptor.schema_id
        or envelope.schema_version != descriptor.schema_version
        or envelope.codec != descriptor.codec
        or envelope.content_type != descriptor.content_type
    ):
        return (
            integrity,
            MaterializationStatus.EVIDENCE_METADATA_MISMATCH,
            "state.evidence_metadata_mismatch",
            None,
        )
    return (
        integrity,
        MaterializationStatus.MATERIALIZED,
        "state.materialized",
        envelope,
    )


def _assess_private_memory_source(
    head: _PrivateMemoryMutationHead,
    *,
    store,
    namespace_id: str,
    policies: Mapping[str, IntegrityPolicy],
) -> tuple[
    IntegrityStatus,
    MaterializationStatus,
    str,
    PrivateMemoryMutationEnvelope | None,
]:
    descriptor = head.descriptor
    try:
        record = store.get(state_ref=descriptor.state_ref)
    except SETTPersistenceError as error:
        if error.reason_code == "state.record_missing":
            return (
                IntegrityStatus.NOT_EVALUATED,
                MaterializationStatus.RECORD_MISSING,
                error.reason_code,
                None,
            )
        raise
    integrity_policy = policies.get(descriptor.digest_policy_id)
    if integrity_policy is None:
        return (
            IntegrityStatus.POLICY_UNAVAILABLE,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_policy_unavailable",
            None,
        )
    try:
        expected_digest = digest_from_trace_value(descriptor.state_digest)
    except SETTValidationError:
        return (
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_encoding_invalid",
            None,
        )
    if (
        integrity_policy.policy_id != descriptor.digest_policy_id
        or integrity_policy.algorithm != descriptor.digest_algorithm
        or not integrity_policy.verify(record.payload, expected_digest)
    ):
        return (
            IntegrityStatus.MISMATCH,
            MaterializationStatus.NOT_EVALUATED,
            "state.digest_mismatch",
            None,
        )
    integrity = (
        IntegrityStatus.EXPLICITLY_DISABLED
        if isinstance(integrity_policy, NoDigestPolicy)
        else IntegrityStatus.VERIFIED
    )
    try:
        envelope = decode_private_memory_mutation(record.payload)
    except SETTPersistenceFormatError as error:
        status = (
            MaterializationStatus.ENVELOPE_UNSUPPORTED
            if error.reason_code == "state.envelope_unsupported"
            else MaterializationStatus.ENVELOPE_INVALID
        )
        return integrity, status, error.reason_code, None
    if envelope.namespace_id != namespace_id:
        return (
            integrity,
            MaterializationStatus.NAMESPACE_MISMATCH,
            "state.namespace_mismatch",
            None,
        )
    if (
        envelope.memory_id != descriptor.memory_id
        or envelope.trace_id != head.trace_id
        or envelope.run_id != head.run_id
        or envelope.mutation_event_id != descriptor.mutation_event_id
    ):
        return (
            integrity,
            MaterializationStatus.EXECUTION_IDENTITY_MISMATCH,
            "state.execution_identity_mismatch",
            None,
        )
    if (
        envelope.digest_algorithm != descriptor.digest_algorithm
        or envelope.digest_policy_id != descriptor.digest_policy_id
        or envelope.schema_id != descriptor.schema_id
        or envelope.schema_version != descriptor.schema_version
        or envelope.codec != descriptor.codec
        or envelope.content_type != descriptor.content_type
    ):
        return (
            integrity,
            MaterializationStatus.EVIDENCE_METADATA_MISMATCH,
            "state.evidence_metadata_mismatch",
            None,
        )
    return (
        integrity,
        MaterializationStatus.MATERIALIZED,
        "state.materialized",
        envelope,
    )


def _transformed_payload(plan: MigrationPlan, payload: bytes) -> bytes:
    try:
        transformed = plan.transform(payload)
    except Exception as error:
        raise SETTPersistenceError(
            "Migration transform failed.",
            reason_code="migration.transform_failed",
        ) from error
    if not isinstance(transformed, bytes):
        raise SETTPersistenceError(
            "Migration transform must return bytes.",
            reason_code="migration.transform_result_invalid",
        )
    return transformed


def _remedy_owner(
    *,
    integrity_status: IntegrityStatus,
    materialization_status: MaterializationStatus,
    reason_code: str,
    migratable: bool,
) -> MigrationRemedyOwner:
    if migratable:
        return MigrationRemedyOwner.NONE
    if reason_code == "migration.integrity_status_not_accepted":
        return MigrationRemedyOwner.PLAN_CONFIGURATION
    if integrity_status in {
        IntegrityStatus.MISMATCH,
        IntegrityStatus.POLICY_NOT_RECORDED,
    } or materialization_status in {
        MaterializationStatus.EXECUTION_IDENTITY_MISMATCH,
        MaterializationStatus.EVIDENCE_METADATA_MISMATCH,
    }:
        return MigrationRemedyOwner.INVESTIGATION
    if integrity_status is IntegrityStatus.POLICY_UNAVAILABLE or (
        materialization_status
        in {
            MaterializationStatus.ENVELOPE_UNSUPPORTED,
            MaterializationStatus.SCHEMA_UNSUPPORTED,
            MaterializationStatus.CODEC_UNSUPPORTED,
        }
    ):
        return MigrationRemedyOwner.DEPLOYMENT_CAPABILITY
    if materialization_status in {
        MaterializationStatus.RECORD_MISSING,
        MaterializationStatus.NAMESPACE_MISMATCH,
    }:
        return MigrationRemedyOwner.STORE_OR_CONFIGURATION
    if materialization_status in {
        MaterializationStatus.ENVELOPE_INVALID,
        MaterializationStatus.DECODE_FAILED,
    } or reason_code.startswith("migration.transform"):
        return MigrationRemedyOwner.DATA_OR_DECODER
    return MigrationRemedyOwner.INVESTIGATION


def dry_run_stage_migration(
    plan: MigrationPlan,
    report: RecoveryReport,
    *,
    backend: PersistenceBackend,
    policy: PersistencePolicy,
    historical_policies: Mapping[str, IntegrityPolicy],
) -> MigrationDryRun:
    """Classify the evidence-selected stage heads without writing anything."""
    from sett.audit_ruler.trace import RecoveryReport

    if not isinstance(plan, MigrationPlan):
        raise SETTValidationError("plan must be a MigrationPlan.")
    if not isinstance(report, RecoveryReport) or not report.chain_integrity:
        raise SETTConfigurationError(
            "A migration dry-run requires an intact RecoveryReport."
        )
    if report.namespace_id != backend.namespace_id:
        raise SETTConfigurationError(
            "Migration report and backend namespace disagree."
        )
    cutoff = report.last_durable_position
    if cutoff is None:
        raise SETTConfigurationError(
            "Migration report has no durable cutoff position."
        )
    durability = backend.evidence.flush(through=cutoff)
    if not satisfies_durability(
        durability.achieved_durability, policy.evidence_durability
    ):
        raise SETTPersistenceError(
            "Migration cutoff did not reach required evidence durability.",
            reason_code="migration.cutoff_durability_unsatisfied",
            backend_id=durability.backend_id,
        )
    policies = dict(historical_policies)
    policies.setdefault(policy.integrity_policy.policy_id, policy.integrity_policy)
    assessments: list[MigrationAssessment] = []
    entries: list[MigrationManifestEntry] = []
    for head in _stage_heads(report):
        stage = head.stage
        descriptor = stage.descriptor
        if descriptor is None or (
            descriptor.schema_id != plan.source_schema_id
            or descriptor.schema_version != plan.source_schema_version
            or descriptor.codec != plan.source_codec
        ):
            continue
        (
            integrity_status,
            materialization_status,
            materialization_reason,
            source_envelope,
        ) = _assess_source(
            descriptor,
            store=backend.state,
            namespace_id=backend.namespace_id,
            trace_id=head.trace_id,
            policies=policies,
        )
        if (
            head.lineage_integrity_status is not None
            and integrity_status in _ALLOWED_INTEGRITY_STATUSES
        ):
            integrity_status = head.lineage_integrity_status
        migratable = (
            integrity_status in plan.accepted_integrity_statuses
            and materialization_status is MaterializationStatus.MATERIALIZED
        )
        reason_code = materialization_reason
        if (
            integrity_status in _ALLOWED_INTEGRITY_STATUSES
            and integrity_status not in plan.accepted_integrity_statuses
        ):
            reason_code = "migration.integrity_status_not_accepted"
        if migratable:
            assert source_envelope is not None
            try:
                target_payload = _transformed_payload(
                    plan, source_envelope.payload_bytes
                )
                encode_state_envelope(
                    StateEnvelope(
                        digest_algorithm=policy.integrity_policy.algorithm,
                        digest_policy_id=policy.integrity_policy.policy_id,
                        namespace_id=source_envelope.namespace_id,
                        trace_id=source_envelope.trace_id,
                        run_id=source_envelope.run_id,
                        stage_index=source_envelope.stage_index,
                        schema_id=plan.target_schema_id,
                        schema_version=plan.target_schema_version,
                        codec=plan.target_codec,
                        content_type=plan.target_content_type,
                        payload_bytes=target_payload,
                    )
                )
            except SETTPersistenceError as error:
                migratable = False
                reason_code = error.reason_code
        assessment = MigrationAssessment(
            stage=stage,
            integrity_status=integrity_status,
            materialization_status=materialization_status,
            reason_code=reason_code,
            migratable=migratable,
            remedy_owner=_remedy_owner(
                integrity_status=integrity_status,
                materialization_status=materialization_status,
                reason_code=reason_code,
                migratable=migratable,
            ),
        )
        assessments.append(assessment)
        entries.append(
            MigrationManifestEntry(
                authoritative_sequence=head.authoritative_sequence,
                authoritative_event_id=(
                    stage.authoritative_event_id or stage.completion_event_id
                ),
                source_state_ref=descriptor.state_ref,
                expected_lineage_head_event_id=(
                    stage.authoritative_event_id or stage.completion_event_id
                ),
                state_digest=descriptor.state_digest,
                digest_algorithm=descriptor.digest_algorithm,
                digest_policy_id=descriptor.digest_policy_id,
                integrity_status=integrity_status.value,
                materialization_status=materialization_status.value,
                reason_codes=(reason_code,),
            )
        )
    manifest = MigrationManifest(
        namespace_id=backend.namespace_id,
        plan_id=plan.plan_id,
        plan_version=plan.plan_version,
        source_schema_id=plan.source_schema_id,
        source_schema_version=plan.source_schema_version,
        source_codec=plan.source_codec,
        target_schema_id=plan.target_schema_id,
        target_schema_version=plan.target_schema_version,
        target_codec=plan.target_codec,
        cutoff_position=cutoff,
        entries=tuple(entries),
    )
    return MigrationDryRun(
        plan=plan,
        report=report,
        manifest=manifest,
        manifest_digest=migration_manifest_digest(manifest).hex(),
        assessments=tuple(assessments),
    )


def dry_run_private_memory_migration(
    plan: MigrationPlan,
    report: RecoveryReport,
    *,
    memory_id: str,
    backend: PersistenceBackend,
    policy: PersistencePolicy,
    historical_policies: Mapping[str, IntegrityPolicy],
) -> PrivateMemoryMigrationDryRun:
    """Classify current durable mutations for one private memory."""
    from sett.audit_ruler.trace import RecoveryReport

    if not isinstance(plan, MigrationPlan):
        raise SETTValidationError("plan must be a MigrationPlan.")
    if not isinstance(memory_id, str) or not memory_id:
        raise SETTValidationError("memory_id cannot be empty.")
    if not isinstance(report, RecoveryReport) or not report.chain_integrity:
        raise SETTConfigurationError(
            "A migration dry-run requires an intact RecoveryReport."
        )
    if report.namespace_id != backend.namespace_id:
        raise SETTConfigurationError(
            "Migration report and backend namespace disagree."
        )
    cutoff = report.last_durable_position
    if cutoff is None:
        raise SETTConfigurationError(
            "Migration report has no durable cutoff position."
        )
    durability = backend.evidence.flush(through=cutoff)
    if not satisfies_durability(
        durability.achieved_durability, policy.evidence_durability
    ):
        raise SETTPersistenceError(
            "Migration cutoff did not reach required evidence durability.",
            reason_code="migration.cutoff_durability_unsatisfied",
            backend_id=durability.backend_id,
        )
    policies = dict(historical_policies)
    policies.setdefault(policy.integrity_policy.policy_id, policy.integrity_policy)
    assessments: list[PrivateMemoryMigrationAssessment] = []
    entries: list[MigrationManifestEntry] = []
    for head in sorted(
        _private_memory_mutation_heads(report, memory_id=memory_id),
        key=lambda item: item.authoritative_sequence,
    ):
        descriptor = head.descriptor
        if (
            descriptor.schema_id != plan.source_schema_id
            or descriptor.schema_version != plan.source_schema_version
            or descriptor.codec != plan.source_codec
        ):
            continue
        (
            integrity_status,
            materialization_status,
            materialization_reason,
            source_envelope,
        ) = _assess_private_memory_source(
            head,
            store=backend.state,
            namespace_id=backend.namespace_id,
            policies=policies,
        )
        if (
            head.lineage_integrity_status is not None
            and integrity_status in _ALLOWED_INTEGRITY_STATUSES
        ):
            integrity_status = head.lineage_integrity_status
        migratable = (
            integrity_status in plan.accepted_integrity_statuses
            and materialization_status is MaterializationStatus.MATERIALIZED
        )
        reason_code = materialization_reason
        if (
            integrity_status in _ALLOWED_INTEGRITY_STATUSES
            and integrity_status not in plan.accepted_integrity_statuses
        ):
            reason_code = "migration.integrity_status_not_accepted"
        if migratable:
            assert source_envelope is not None
            try:
                target_payload = (
                    None
                    if source_envelope.payload_bytes is None
                    else _transformed_payload(plan, source_envelope.payload_bytes)
                )
                encode_private_memory_mutation(
                    PrivateMemoryMutationEnvelope(
                        digest_algorithm=policy.integrity_policy.algorithm,
                        digest_policy_id=policy.integrity_policy.policy_id,
                        namespace_id=source_envelope.namespace_id,
                        memory_id=source_envelope.memory_id,
                        trace_id=source_envelope.trace_id,
                        run_id=source_envelope.run_id,
                        mutation_event_id=source_envelope.mutation_event_id,
                        operation=source_envelope.operation,
                        key=source_envelope.key,
                        schema_id=plan.target_schema_id,
                        schema_version=plan.target_schema_version,
                        codec=plan.target_codec,
                        content_type=plan.target_content_type,
                        payload_bytes=target_payload,
                    )
                )
            except SETTPersistenceError as error:
                migratable = False
                reason_code = error.reason_code
        assessments.append(
            PrivateMemoryMigrationAssessment(
                memory_id=memory_id,
                mutation_event_id=descriptor.mutation_event_id,
                authoritative_event_id=head.authoritative_event_id,
                descriptor=descriptor,
                integrity_status=integrity_status,
                materialization_status=materialization_status,
                reason_code=reason_code,
                migratable=migratable,
                remedy_owner=_remedy_owner(
                    integrity_status=integrity_status,
                    materialization_status=materialization_status,
                    reason_code=reason_code,
                    migratable=migratable,
                ),
            )
        )
        entries.append(
            MigrationManifestEntry(
                authoritative_sequence=head.authoritative_sequence,
                authoritative_event_id=head.authoritative_event_id,
                source_state_ref=descriptor.state_ref,
                expected_lineage_head_event_id=head.authoritative_event_id,
                state_digest=descriptor.state_digest,
                digest_algorithm=descriptor.digest_algorithm,
                digest_policy_id=descriptor.digest_policy_id,
                integrity_status=integrity_status.value,
                materialization_status=materialization_status.value,
                reason_codes=(reason_code,),
            )
        )
    manifest = MigrationManifest(
        namespace_id=backend.namespace_id,
        plan_id=plan.plan_id,
        plan_version=plan.plan_version,
        source_schema_id=plan.source_schema_id,
        source_schema_version=plan.source_schema_version,
        source_codec=plan.source_codec,
        target_schema_id=plan.target_schema_id,
        target_schema_version=plan.target_schema_version,
        target_codec=plan.target_codec,
        cutoff_position=cutoff,
        entries=tuple(entries),
    )
    return PrivateMemoryMigrationDryRun(
        plan=plan,
        report=report,
        memory_id=memory_id,
        manifest=manifest,
        manifest_digest=migration_manifest_digest(manifest).hex(),
        assessments=tuple(assessments),
    )


def _validate_current_prefix(
    dry_run: MigrationDryRun | PrivateMemoryMigrationDryRun,
    current: RecoveryReport,
) -> None:
    expected = dry_run.report.events
    actual_prefix = current.events[: len(expected)]
    if len(actual_prefix) != len(expected) or any(
        left.event_id != right.event_id or left.entry_hash != right.entry_hash
        for left, right in zip(expected, actual_prefix, strict=True)
    ):
        raise SETTPersistenceError(
            "Migration manifest prefix is no longer authoritative.",
            reason_code="migration.manifest_stale",
            backend_id=current.backend_id,
        )
    heads = {
        entry.expected_lineage_head_event_id for entry in dry_run.manifest.entries
    }
    for event in current.events[len(expected) :]:
        if (
            event.kind == "persistence.state_migrated"
            and event.attributes.get("source_authoritative_event_id") in heads
        ):
            raise SETTPersistenceError(
                "A selected migration lineage advanced after the dry-run.",
                reason_code="migration.manifest_stale",
                backend_id=current.backend_id,
            )


def execute_stage_migration(
    dry_run: MigrationDryRun,
    *,
    backend: PersistenceBackend,
    policy: PersistencePolicy,
    context: ExecutionContext,
) -> MigrationRunResult:
    """Execute one ready manifest as independent copy-on-write records."""
    from sett.audit_ruler.trace import TraceRecorder

    if not isinstance(dry_run, MigrationDryRun):
        raise SETTValidationError("dry_run must be a MigrationDryRun.")
    if not dry_run.ready:
        raise SETTConfigurationError(
            "Migration dry-run contains blocked sources and cannot execute."
        )
    if not isinstance(context, ExecutionContext):
        raise SETTValidationError("context must be an ExecutionContext.")
    current = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    _validate_current_prefix(dry_run, current)
    recorder = TraceRecorder.resume(
        current,
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    plan = dry_run.plan
    migration_run_id = str(uuid4())
    started = recorder.record(
        context,
        kind="persistence.migration_started",
        component_type="persistence",
        component_name=plan.plan_id,
        status="started",
        reason_codes=("migration.started",),
        attributes={
            "migration_run_id": migration_run_id,
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "source_schema_id": plan.source_schema_id,
            "source_schema_version": plan.source_schema_version,
            "source_codec": plan.source_codec,
            "target_schema_id": plan.target_schema_id,
            "target_schema_version": plan.target_schema_version,
            "target_codec": plan.target_codec,
            "target_content_type": plan.target_content_type,
            "cutoff_position": encode_stream_position(
                dry_run.manifest.cutoff_position
            ).hex(),
            "manifest_digest": dry_run.manifest_digest,
            "entry_count": len(dry_run.manifest.entries),
            "write_id_version": plan.write_id_version,
        },
    )
    migrated: list[MigratedStateRecord] = []
    failed_phase = "precondition"
    failed_source_state_ref: str | None = None
    destination_orphaned = False
    try:
        for entry, assessment in zip(
            dry_run.manifest.entries, dry_run.assessments, strict=True
        ):
            descriptor = assessment.stage.descriptor
            assert isinstance(descriptor, StageStateDescriptor)
            failed_source_state_ref = descriptor.state_ref
            failed_phase = "source_read"
            source = backend.state.get(state_ref=descriptor.state_ref)
            source_envelope = decode_state_envelope(source.payload)
            failed_phase = "transform"
            target_payload = _transformed_payload(
                plan, source_envelope.payload_bytes
            )
            target_envelope = StateEnvelope(
                digest_algorithm=policy.integrity_policy.algorithm,
                digest_policy_id=policy.integrity_policy.policy_id,
                namespace_id=source_envelope.namespace_id,
                trace_id=source_envelope.trace_id,
                run_id=source_envelope.run_id,
                stage_index=source_envelope.stage_index,
                schema_id=plan.target_schema_id,
                schema_version=plan.target_schema_version,
                codec=plan.target_codec,
                content_type=plan.target_content_type,
                payload_bytes=target_payload,
            )
            target_frame = encode_state_envelope(target_envelope)
            target_digest = policy.integrity_policy.digest(target_frame)
            write_id = derive_migration_write_id(
                namespace_id=backend.namespace_id,
                source_state_ref=descriptor.state_ref,
                target_envelope_version=plan.target_envelope_version,
                target_schema_id=plan.target_schema_id,
                target_schema_version=plan.target_schema_version,
                target_codec=plan.target_codec,
                target_content_type=plan.target_content_type,
                version=plan.write_id_version,
            )
            failed_phase = "destination_put"
            receipt = backend.state.put(write_id=write_id, payload=target_frame)
            destination_orphaned = True
            if not satisfies_durability(
                receipt.achieved_durability, policy.snapshot_durability
            ):
                raise SETTPersistenceError(
                    "Migration destination did not reach required durability.",
                    reason_code="migration.destination_durability_unsatisfied",
                    backend_id=receipt.backend_id,
                )
            failed_phase = "destination_evidence"
            event = recorder.record(
                context,
                kind="persistence.state_migrated",
                component_type="persistence",
                component_name=plan.plan_id,
                status="migrated",
                cause_id=started.event_id,
                reason_codes=("migration.state_migrated",),
                attributes={
                    "artifact_kind": "stage_output",
                    "migration_run_id": migration_run_id,
                    "source_authoritative_event_id": (
                        entry.authoritative_event_id
                    ),
                    "source_state_ref": descriptor.state_ref,
                    "source_state_digest": descriptor.state_digest,
                    "source_schema_id": descriptor.schema_id,
                    "source_schema_version": descriptor.schema_version,
                    "source_codec": descriptor.codec,
                    "source_integrity_status": entry.integrity_status,
                    "destination_state_ref": receipt.state_ref,
                    "destination_state_digest": digest_to_trace_value(
                        target_digest
                    ),
                    "destination_digest_algorithm": (
                        policy.integrity_policy.algorithm
                    ),
                    "destination_digest_policy_id": (
                        policy.integrity_policy.policy_id
                    ),
                    "destination_schema_id": plan.target_schema_id,
                    "destination_schema_version": plan.target_schema_version,
                    "destination_codec": plan.target_codec,
                    "destination_content_type": plan.target_content_type,
                    "state_backend_id": receipt.backend_id,
                    "snapshot_durability": receipt.achieved_durability.value,
                    "previous_migration_event_id": (
                        entry.authoritative_event_id
                        if any(
                            prior.event_id == entry.authoritative_event_id
                            and prior.kind == "persistence.state_migrated"
                            for prior in dry_run.report.events
                        )
                        else None
                    ),
                    "write_id_version": plan.write_id_version,
                },
            )
            destination_orphaned = False
            migrated.append(
                MigratedStateRecord(
                    source_state_ref=descriptor.state_ref,
                    destination_state_ref=receipt.state_ref,
                    evidence_event_id=event.event_id,
                    state_digest=digest_to_trace_value(target_digest),
                )
            )
    except Exception as error:
        try:
            recorder.record(
                context,
                kind="persistence.migration_failed",
                component_type="persistence",
                component_name=plan.plan_id,
                status="failed",
                cause_id=started.event_id,
                reason_codes=(
                    getattr(error, "reason_code", "migration.execution_failed"),
                ),
                attributes={
                    "migration_run_id": migration_run_id,
                    "completed_records": len(migrated),
                    "failed_phase": failed_phase,
                    "reason_code": getattr(
                        error, "reason_code", "migration.execution_failed"
                    ),
                    "source_state_ref": failed_source_state_ref,
                    "destination_orphaned": destination_orphaned,
                    "error_type": type(error).__name__,
                },
            )
        except SETTPersistenceError as audit_error:
            if not isinstance(error, SETTPersistenceError):
                raise audit_error from error
        raise
    terminal = recorder.record(
        context,
        kind="persistence.migration_completed",
        component_type="persistence",
        component_name=plan.plan_id,
        status="completed",
        cause_id=(migrated[-1].evidence_event_id if migrated else started.event_id),
        reason_codes=("migration.completed",),
        attributes={
            "migration_run_id": migration_run_id,
            "migrated_records": len(migrated),
            "manifest_digest": dry_run.manifest_digest,
        },
    )
    return MigrationRunResult(
        migration_run_id=migration_run_id,
        manifest_digest=dry_run.manifest_digest,
        records=tuple(migrated),
        terminal_event_id=terminal.event_id,
    )


def execute_private_memory_migration(
    dry_run: PrivateMemoryMigrationDryRun,
    *,
    backend: PersistenceBackend,
    policy: PersistencePolicy,
    context: ExecutionContext,
) -> MigrationRunResult:
    """Execute a ready private-memory manifest using copy-on-write records."""
    from sett.audit_ruler.trace import TraceRecorder

    if not isinstance(dry_run, PrivateMemoryMigrationDryRun):
        raise SETTValidationError(
            "dry_run must be a PrivateMemoryMigrationDryRun."
        )
    if not dry_run.ready:
        raise SETTConfigurationError(
            "Migration dry-run contains blocked sources and cannot execute."
        )
    if not isinstance(context, ExecutionContext):
        raise SETTValidationError("context must be an ExecutionContext.")
    current = TraceRecorder.restore(
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    _validate_current_prefix(dry_run, current)
    recorder = TraceRecorder.resume(
        current,
        evidence_stream=backend.evidence,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    plan = dry_run.plan
    migration_run_id = str(uuid4())
    started = recorder.record(
        context,
        kind="persistence.migration_started",
        component_type="persistence",
        component_name=plan.plan_id,
        status="started",
        reason_codes=("migration.started",),
        attributes={
            "artifact_kind": "private_memory",
            "memory_id": dry_run.memory_id,
            "migration_run_id": migration_run_id,
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "source_schema_id": plan.source_schema_id,
            "source_schema_version": plan.source_schema_version,
            "source_codec": plan.source_codec,
            "target_schema_id": plan.target_schema_id,
            "target_schema_version": plan.target_schema_version,
            "target_codec": plan.target_codec,
            "target_content_type": plan.target_content_type,
            "cutoff_position": encode_stream_position(
                dry_run.manifest.cutoff_position
            ).hex(),
            "manifest_digest": dry_run.manifest_digest,
            "entry_count": len(dry_run.manifest.entries),
            "write_id_version": plan.write_id_version,
        },
    )
    migrated: list[MigratedStateRecord] = []
    failed_phase = "precondition"
    failed_source_state_ref: str | None = None
    destination_orphaned = False
    try:
        for entry, assessment in zip(
            dry_run.manifest.entries, dry_run.assessments, strict=True
        ):
            descriptor = assessment.descriptor
            failed_source_state_ref = descriptor.state_ref
            failed_phase = "source_read"
            source = backend.state.get(state_ref=descriptor.state_ref)
            source_envelope = decode_private_memory_mutation(source.payload)
            failed_phase = "transform"
            target_payload = (
                None
                if source_envelope.payload_bytes is None
                else _transformed_payload(plan, source_envelope.payload_bytes)
            )
            target_envelope = PrivateMemoryMutationEnvelope(
                digest_algorithm=policy.integrity_policy.algorithm,
                digest_policy_id=policy.integrity_policy.policy_id,
                namespace_id=source_envelope.namespace_id,
                memory_id=source_envelope.memory_id,
                trace_id=source_envelope.trace_id,
                run_id=source_envelope.run_id,
                mutation_event_id=source_envelope.mutation_event_id,
                operation=source_envelope.operation,
                key=source_envelope.key,
                schema_id=plan.target_schema_id,
                schema_version=plan.target_schema_version,
                codec=plan.target_codec,
                content_type=plan.target_content_type,
                payload_bytes=target_payload,
            )
            target_frame = encode_private_memory_mutation(target_envelope)
            target_digest = policy.integrity_policy.digest(target_frame)
            write_id = derive_migration_write_id(
                namespace_id=backend.namespace_id,
                source_state_ref=descriptor.state_ref,
                target_envelope_version=plan.target_envelope_version,
                target_schema_id=plan.target_schema_id,
                target_schema_version=plan.target_schema_version,
                target_codec=plan.target_codec,
                target_content_type=plan.target_content_type,
                version=plan.write_id_version,
            )
            failed_phase = "destination_put"
            receipt = backend.state.put(write_id=write_id, payload=target_frame)
            destination_orphaned = True
            if not satisfies_durability(
                receipt.achieved_durability, policy.snapshot_durability
            ):
                raise SETTPersistenceError(
                    "Migration destination did not reach required durability.",
                    reason_code="migration.destination_durability_unsatisfied",
                    backend_id=receipt.backend_id,
                )
            failed_phase = "destination_evidence"
            event = recorder.record(
                context,
                kind="persistence.state_migrated",
                component_type="persistence",
                component_name=plan.plan_id,
                status="migrated",
                cause_id=started.event_id,
                reason_codes=("migration.state_migrated",),
                attributes={
                    "artifact_kind": "private_memory",
                    "memory_id": dry_run.memory_id,
                    "mutation_event_id": assessment.mutation_event_id,
                    "migration_run_id": migration_run_id,
                    "source_authoritative_event_id": (
                        entry.authoritative_event_id
                    ),
                    "source_state_ref": descriptor.state_ref,
                    "source_state_digest": descriptor.state_digest,
                    "source_schema_id": descriptor.schema_id,
                    "source_schema_version": descriptor.schema_version,
                    "source_codec": descriptor.codec,
                    "source_integrity_status": entry.integrity_status,
                    "destination_state_ref": receipt.state_ref,
                    "destination_state_digest": digest_to_trace_value(
                        target_digest
                    ),
                    "destination_digest_algorithm": (
                        policy.integrity_policy.algorithm
                    ),
                    "destination_digest_policy_id": (
                        policy.integrity_policy.policy_id
                    ),
                    "destination_schema_id": plan.target_schema_id,
                    "destination_schema_version": plan.target_schema_version,
                    "destination_codec": plan.target_codec,
                    "destination_content_type": plan.target_content_type,
                    "state_backend_id": receipt.backend_id,
                    "snapshot_durability": receipt.achieved_durability.value,
                    "previous_migration_event_id": (
                        entry.authoritative_event_id
                        if any(
                            prior.event_id == entry.authoritative_event_id
                            and prior.kind == "persistence.state_migrated"
                            for prior in dry_run.report.events
                        )
                        else None
                    ),
                    "write_id_version": plan.write_id_version,
                },
            )
            destination_orphaned = False
            migrated.append(
                MigratedStateRecord(
                    source_state_ref=descriptor.state_ref,
                    destination_state_ref=receipt.state_ref,
                    evidence_event_id=event.event_id,
                    state_digest=digest_to_trace_value(target_digest),
                )
            )
    except Exception as error:
        try:
            recorder.record(
                context,
                kind="persistence.migration_failed",
                component_type="persistence",
                component_name=plan.plan_id,
                status="failed",
                cause_id=started.event_id,
                reason_codes=(
                    getattr(error, "reason_code", "migration.execution_failed"),
                ),
                attributes={
                    "artifact_kind": "private_memory",
                    "memory_id": dry_run.memory_id,
                    "migration_run_id": migration_run_id,
                    "completed_records": len(migrated),
                    "failed_phase": failed_phase,
                    "reason_code": getattr(
                        error, "reason_code", "migration.execution_failed"
                    ),
                    "source_state_ref": failed_source_state_ref,
                    "destination_orphaned": destination_orphaned,
                    "error_type": type(error).__name__,
                },
            )
        except SETTPersistenceError as audit_error:
            if not isinstance(error, SETTPersistenceError):
                raise audit_error from error
        raise
    terminal = recorder.record(
        context,
        kind="persistence.migration_completed",
        component_type="persistence",
        component_name=plan.plan_id,
        status="completed",
        cause_id=(migrated[-1].evidence_event_id if migrated else started.event_id),
        reason_codes=("migration.completed",),
        attributes={
            "artifact_kind": "private_memory",
            "memory_id": dry_run.memory_id,
            "migration_run_id": migration_run_id,
            "migrated_records": len(migrated),
            "manifest_digest": dry_run.manifest_digest,
        },
    )
    return MigrationRunResult(
        migration_run_id=migration_run_id,
        manifest_digest=dry_run.manifest_digest,
        records=tuple(migrated),
        terminal_event_id=terminal.event_id,
    )
