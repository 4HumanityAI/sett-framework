"""Explicit application policy for durable SETT state and evidence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sett.audit_ruler.integrity import IntegrityPolicy
from sett.exceptions import SETTConfigurationError
from sett.persistence.contracts import DurabilityLevel, satisfies_durability


class AuditGapPolicy(str, Enum):
    """How a caller treats a known outcome whose audit closure has a gap."""

    RETURN_KNOWN_OUTCOME = "return_known_outcome"
    ESCALATE_KNOWN_OUTCOME = "escalate_known_outcome"


@dataclass(frozen=True, slots=True)
class PersistencePolicy:
    """Application-owned durability, integrity, and audit-gap requirements.

    Every field is required. SETT has no implicit durability, integrity, or
    compliance preference that could be mistaken for an application choice.
    """

    snapshot_durability: DurabilityLevel
    evidence_durability: DurabilityLevel
    integrity_policy: IntegrityPolicy
    audit_gap_policy: AuditGapPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_durability, DurabilityLevel):
            raise SETTConfigurationError(
                "snapshot_durability must be a DurabilityLevel member."
            )
        if not isinstance(self.evidence_durability, DurabilityLevel):
            raise SETTConfigurationError(
                "evidence_durability must be a DurabilityLevel member."
            )
        if (
            self.snapshot_durability is DurabilityLevel.UNKNOWN
            or self.evidence_durability is DurabilityLevel.UNKNOWN
        ):
            raise SETTConfigurationError(
                "Persistence durability requirements cannot be UNKNOWN."
            )
        if not satisfies_durability(
            self.snapshot_durability, self.evidence_durability
        ):
            raise SETTConfigurationError(
                "Snapshot durability must be at least evidence durability."
            )
        if not isinstance(self.integrity_policy, IntegrityPolicy):
            raise SETTConfigurationError(
                "integrity_policy must implement IntegrityPolicy."
            )
        if not isinstance(self.audit_gap_policy, AuditGapPolicy):
            raise SETTConfigurationError(
                "audit_gap_policy must be an AuditGapPolicy member."
            )
