"""Public contracts and deterministic mechanism for SETT authorization."""

from sett.authorization_ruler.authorizer import (
    AuthorizationRuler,
    SystemAuthorizationClock,
)
from sett.authorization_ruler.contracts import (
    ApprovalVerifier,
    AuthorizationClock,
    AuthorizationFingerprintPolicy,
    AuthorizationPolicy,
    AuthorizationRequestResolver,
    PolicySelectionResolver,
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
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyDecision,
    PolicyOrigin,
    PolicySelection,
    PolicySourceReference,
    ReceiptUse,
    VerifiedApproval,
)
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
    "PolicyApplicability",
    "PolicyApplicabilityState",
    "PolicyBinding",
    "PolicyDecision",
    "PolicyOrigin",
    "PolicySelection",
    "PolicySelectionResolver",
    "PolicySourceReference",
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
