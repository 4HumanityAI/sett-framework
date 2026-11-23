"""Public review names stay in the policy package, not root or authorization."""

import inspect

import sett
import sett.authorization_ruler as authorization
import sett.policy_ruler as policy
from sett.policy_ruler import review as review_values
from sett.policy_ruler.contracts import ReviewVerifier

REVIEW_EXPORTS = (
    "DeadlineStatus",
    "DeadlineStatusFact",
    "NotificationDeliveredFact",
    "PolicyReviewObligation",
    "ReviewCompletedFact",
    "ReviewFact",
    "ReviewMode",
    "ReviewOutcome",
    "ReviewVerification",
    "ReviewVerificationRequest",
    "ReviewVerifier",
)
REVIEW_VALUE_EXPORTS = tuple(
    name for name in REVIEW_EXPORTS if name != "ReviewVerifier"
)


def test_review_names_are_exported_from_policy_only_and_keep_identity():
    for name in REVIEW_VALUE_EXPORTS:
        assert name in policy.__all__
        assert not hasattr(sett, name)
        assert not hasattr(authorization, name)
        assert getattr(policy, name) is getattr(review_values, name)
    assert "ReviewVerifier" in policy.__all__
    assert policy.ReviewVerifier is ReviewVerifier
    assert not hasattr(sett, "ReviewVerifier")
    assert not hasattr(authorization, "ReviewVerifier")


def test_review_verifier_protocol_has_the_accepted_positional_only_input():
    method = inspect.signature(ReviewVerifier.verify)
    assert tuple(method.parameters) == ("self", "request")
    assert method.parameters["request"].kind is inspect.Parameter.POSITIONAL_ONLY
    try:
        isinstance(object(), ReviewVerifier)
    except TypeError:
        pass
    else:
        raise AssertionError("ReviewVerifier must not promise runtime protocol checks.")
