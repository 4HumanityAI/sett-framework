"""The policy domain: the single owner of policy governance in SETT.

PolicyRuler owns registration, declared selection, evaluation and
composition. AuthorizationRuler keeps final authority and receipts, and
execution keeps the effect boundary.

Metadata and selection values govern no decision by themselves.
Constructing or reexporting them selects no executable policy, determines
no legal applicability, verifies no evidence, issues no receipt and
authorizes no effect. A governed attempt is held before an outcome unless
every verified-state slot has a producer:
consent has a producer only when the implementation declares that it owns
consent, durability only when a governance journal is configured, and
review only when review verifiers are configured with that journal; a
bound evaluation that cannot obtain a complete verified-state record is
held and blocks.

The standalone governance journal preserves opaque bytes without interpreting
domain payloads and confers no effect authority. Configured on PolicyRuler,
it receives the decision records of the durability producer; no
cross-store transaction exists.
"""

from sett.exceptions import SETTPolicyConfigurationError
from sett.policy_ruler.condition_results import (
    ConditionVerification,
    ConditionVerificationState,
)
from sett.policy_ruler.conditions import ConditionValidityMode, PolicyCondition
from sett.policy_ruler.contracts import (
    AuthorizationPolicy,
    ConditionVerifier,
    PolicySelectionResolver,
    ReviewVerifier,
)
from sett.policy_ruler.evaluation import PolicyEvaluation
from sett.policy_ruler.evaluator import PolicyRuler
from sett.policy_ruler.models import (
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyDecision,
    PolicyOrigin,
    PolicySelection,
    PolicySourceReference,
)
from sett.policy_ruler.persistence import (
    GovernanceEntry,
    GovernanceJournal,
    GovernanceReadResult,
    GovernanceReconciliation,
    GovernanceRecord,
    GovernanceSubmission,
    GovernanceWriteResult,
)
from sett.policy_ruler.review import (
    DeadlineStatus,
    DeadlineStatusFact,
    NotificationDeliveredFact,
    PolicyReviewObligation,
    ReviewCompletedFact,
    ReviewFact,
    ReviewMode,
    ReviewOutcome,
    ReviewVerification,
    ReviewVerificationRequest,
)

__all__ = [
    "AuthorizationPolicy",
    "ConditionValidityMode",
    "ConditionVerification",
    "ConditionVerificationState",
    "ConditionVerifier",
    "DeadlineStatus",
    "DeadlineStatusFact",
    "GovernanceEntry",
    "GovernanceJournal",
    "GovernanceReadResult",
    "GovernanceReconciliation",
    "GovernanceRecord",
    "GovernanceSubmission",
    "GovernanceWriteResult",
    "NotificationDeliveredFact",
    "PolicyApplicability",
    "PolicyApplicabilityState",
    "PolicyBinding",
    "PolicyCondition",
    "PolicyDecision",
    "PolicyEvaluation",
    "PolicyOrigin",
    "PolicyReviewObligation",
    "PolicyRuler",
    "PolicySelection",
    "PolicySelectionResolver",
    "PolicySourceReference",
    "ReviewCompletedFact",
    "ReviewFact",
    "ReviewMode",
    "ReviewOutcome",
    "ReviewVerification",
    "ReviewVerificationRequest",
    "ReviewVerifier",
    "SETTPolicyConfigurationError",
]
