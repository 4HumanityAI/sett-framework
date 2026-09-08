"""Tamper-evident audit helpers used by SETT's public audit surfaces."""

from sett.audit_ruler.chain import append_chained_entry, verify_chain
from sett.audit_ruler.closure import (
    AuditClosure,
    AuditClosureStatus,
    AuditGapToken,
)
from sett.audit_ruler.integrity import (
    IntegrityPolicy,
    NoDigestPolicy,
    Sha256IntegrityPolicy,
    digest_from_trace_value,
    digest_to_trace_value,
)
from sett.audit_ruler.recovery import (
    BoundaryDispositionReceipt,
    record_boundary_disposition,
)
from sett.audit_ruler.trace import (
    BoundaryClaimedOutcome,
    BoundaryDisposition,
    BoundaryDispositionBasis,
    BoundaryDispositionStatus,
    RecoveredRun,
    RecoveredStage,
    RecoveredTrace,
    RecoveryReport,
    UnresolvedBoundary,
)

__all__ = [
    "AuditClosure",
    "AuditClosureStatus",
    "AuditGapToken",
    "BoundaryClaimedOutcome",
    "BoundaryDisposition",
    "BoundaryDispositionBasis",
    "BoundaryDispositionReceipt",
    "BoundaryDispositionStatus",
    "IntegrityPolicy",
    "NoDigestPolicy",
    "RecoveredRun",
    "RecoveredStage",
    "RecoveredTrace",
    "RecoveryReport",
    "Sha256IntegrityPolicy",
    "UnresolvedBoundary",
    "append_chained_entry",
    "digest_from_trace_value",
    "digest_to_trace_value",
    "record_boundary_disposition",
    "verify_chain",
]
