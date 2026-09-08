"""Public contracts and deterministic mechanism for SETT authorization."""

from sett.authorization_ruler.contracts import (
    ApprovalVerifier,
    AuthorizationClock,
    AuthorizationFingerprintPolicy,
    AuthorizationPolicy,
    AuthorizationRequestResolver,
)
from sett.authorization_ruler.errors import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
)
from sett.authorization_ruler.evidence import (
    AuthorizationEvent,
    AuthorizationEvidenceSink,
    InMemoryAuthorizationEvidence,
)
from sett.authorization_ruler.fingerprint import StrictJSONAuthorizationFingerprint
from sett.authorization_ruler.models import (
    ApprovalInput,
    ApprovalRequirement,
    AuthorizationConstraint,
    AuthorizationDecision,
    AuthorizationOutcome,
    AuthorizationReceipt,
    AuthorizationRequest,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    PolicyDecision,
    ReceiptUse,
    VerifiedApproval,
)
from sett.authorization_ruler.ruler import AuthorizationRuler, SystemAuthorizationClock
from sett.authorization_ruler.store import (
    AuthorizationReceiptStore,
    InMemoryAuthorizationReceiptStore,
)
from sett.authorization_ruler.verdict import (
    AuthorizationVerdict,
    additional_authority_may_be_evaluated,
    permits_effect,
    validate_verdict_semantics,
)

__all__ = [
    "ApprovalInput",
    "ApprovalRequirement",
    "ApprovalVerifier",
    "AuthorizationClock",
    "AuthorizationConstraint",
    "AuthorizationDecision",
    "AuthorizationEvent",
    "AuthorizationEvidenceSink",
    "AuthorizationFingerprintPolicy",
    "AuthorizationOutcome",
    "AuthorizationPolicy",
    "AuthorizationReceipt",
    "AuthorizationReceiptStore",
    "AuthorizationRequest",
    "AuthorizationRequestResolver",
    "AuthorizationRuler",
    "AuthorizationVerdict",
    "ExecutionMode",
    "FieldTrust",
    "FieldValue",
    "InMemoryAuthorizationEvidence",
    "InMemoryAuthorizationReceiptStore",
    "PolicyDecision",
    "ReceiptUse",
    "SETTAuthorizationConfigurationError",
    "SETTAuthorizationError",
    "StrictJSONAuthorizationFingerprint",
    "SystemAuthorizationClock",
    "VerifiedApproval",
    "additional_authority_may_be_evaluated",
    "permits_effect",
    "validate_verdict_semantics",
]
