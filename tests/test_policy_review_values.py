"""Review declarations and reported facts; no producer or lifecycle runs."""

from dataclasses import FrozenInstanceError

import pytest

from sett.authorization_ruler import ApprovalRequirement, AuthorizationVerdict
from sett.exceptions import SETTPolicyConfigurationError
from sett.policy_ruler import (
    DeadlineStatus,
    DeadlineStatusFact,
    NotificationDeliveredFact,
    PolicyBinding,
    PolicyDecision,
    PolicyOrigin,
    PolicyReviewObligation,
    ReviewCompletedFact,
    ReviewMode,
    ReviewOutcome,
    ReviewVerification,
    ReviewVerificationRequest,
)

POLICY = "test_fixture.policy"
OTHER_POLICY = "test_fixture.other_policy"
VERSION = "test_fixture.version"
OMITTED = object()


def build_decision(name, values=OMITTED):
    review_kwargs = {} if values is OMITTED else {"review_obligations": values}
    if name == "constructor":
        return PolicyDecision(
            POLICY,
            AuthorizationVerdict.GRANT,
            conditions=(),
            **review_kwargs,
        )
    if name == "grant":
        return PolicyDecision.grant(POLICY, conditions=(), **review_kwargs)
    if name == "deny":
        return PolicyDecision.deny(POLICY, conditions=(), **review_kwargs)
    return PolicyDecision.require_approval(
        POLICY,
        requirements=(
            ApprovalRequirement(
                "test_fixture.requirement", "test_fixture.verifier"
            ),
        ),
        conditions=(),
        **review_kwargs,
    )


def binding(policy_id=POLICY):
    origin = PolicyOrigin(
        requirement_id="test_fixture.requirement",
        requirement_revision="test_fixture.revision",
        layer_id="test_fixture.layer",
        source=None,
        source_absence_reason="test_fixture.synthetic",
    )
    return PolicyBinding(
        policy_id=policy_id,
        policy_version=VERSION,
        contract_version="2.0",
        origins=[origin],
        profile_id=None,
        profile_revision=None,
    )


def obligation(*, policy_id=POLICY, mode=ReviewMode.NOTIFY_AFTER_ACTION):
    future_types = (
        ["test_fixture.future_action"]
        if mode is ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS
        else []
    )
    return PolicyReviewObligation(
        obligation_id="test_fixture.obligation",
        binding=binding(policy_id),
        mode=mode,
        verifier_id="test_fixture.verifier",
        verifier_version=VERSION,
        action_id="test_fixture.action",
        action_type="test_fixture.action_type",
        future_action_types=future_types,
    )


@pytest.mark.parametrize(
    "name", ("constructor", "grant", "deny", "require_approval")
)
def test_review_obligations_are_required_and_explicitly_empty_when_absent(name):
    decision = build_decision(name, ())
    assert decision.review_obligations == ()
    assert type(decision.review_obligations) is tuple
    with pytest.raises(TypeError):
        build_decision(name)


@pytest.mark.parametrize(
    "name", ("constructor", "grant", "deny", "require_approval")
)
def test_all_decision_builders_reject_an_absent_inventory(name):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        build_decision(name, None)
    assert caught.value.reason_code == "policy.review_obligation_invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "values",
    (
        "test_fixture.text",
        b"test_fixture.bytes",
        {"test_fixture.key": obligation()},
        (item for item in (obligation(),)),
    ),
    ids=("text", "bytes", "mapping", "generator"),
)
def test_grant_rejects_non_sequence_review_inventory(values):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        build_decision("grant", values)
    assert caught.value.reason_code == "policy.review_obligation_invalid"
    assert caught.value.args == (
        "The policy review obligation declaration is invalid.",
    )


def test_decision_detaches_valid_review_obligation_sequences():
    item = obligation()
    source = [item]
    decision = PolicyDecision.grant(
        POLICY, conditions=(), review_obligations=source
    )
    source.clear()
    assert decision.review_obligations == (item,)
    assert type(decision.review_obligations) is tuple


def test_decision_rejects_duplicate_obligation_ids_with_fixed_code():
    item = obligation()
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyDecision.grant(
            POLICY, conditions=(), review_obligations=(item, item)
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"
    assert caught.value.args == (
        "The policy review obligation declaration is invalid.",
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_decision_rejects_foreign_policy_obligation_with_fixed_code():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyDecision.grant(
            POLICY,
            conditions=(),
            review_obligations=(obligation(policy_id=OTHER_POLICY),),
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"


@pytest.mark.parametrize(
    ("mode", "expected_future_types"),
    (
        (ReviewMode.NOTIFY_AFTER_ACTION, ()),
        (ReviewMode.REQUIRE_HUMAN_REVIEW_WITH_DEADLINE, ()),
        (
            ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS,
            ("test_fixture.future_action",),
        ),
    ),
)
def test_obligation_modes_retain_only_their_declared_future_action_types(
    mode, expected_future_types
):
    assert obligation(mode=mode).future_action_types == expected_future_types


def test_non_restriction_mode_rejects_future_action_types():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyReviewObligation(
            obligation_id="test_fixture.obligation",
            binding=binding(),
            mode=ReviewMode.NOTIFY_AFTER_ACTION,
            verifier_id="test_fixture.verifier",
            verifier_version=VERSION,
            action_id="test_fixture.action",
            action_type="test_fixture.action_type",
            future_action_types=["test_fixture.future_action"],
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"


def test_obligation_requires_an_exact_binding_value():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyReviewObligation(
            obligation_id="test_fixture.obligation",
            binding=object(),
            mode=ReviewMode.NOTIFY_AFTER_ACTION,
            verifier_id="test_fixture.verifier",
            verifier_version=VERSION,
            action_id="test_fixture.action",
            action_type="test_fixture.action_type",
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"


def test_review_fact_variants_are_typed_and_values_are_immutable():
    completed = ReviewCompletedFact(
        evidence_ref="test_fixture.evidence",
        outcome=ReviewOutcome.FAVORABLE,
    )
    notified = NotificationDeliveredFact(evidence_ref="test_fixture.notice")
    deadline = DeadlineStatusFact(
        evidence_ref="test_fixture.deadline",
        status=DeadlineStatus.NOT_OVERDUE,
    )
    facts = [completed, notified, deadline]
    result = ReviewVerification(
        obligation_id="test_fixture.obligation",
        binding=binding(),
        action_id="test_fixture.action",
        action_type="test_fixture.action_type",
        facts=facts,
    )
    facts.clear()
    assert result.facts == (completed, notified, deadline)
    assert type(result.facts) is tuple
    with pytest.raises(FrozenInstanceError):
        completed.outcome = ReviewOutcome.UNFAVORABLE


def test_duplicate_fact_kinds_are_rejected_with_fixed_code():
    fact = NotificationDeliveredFact(evidence_ref="test_fixture.notice")
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ReviewVerification(
            obligation_id="test_fixture.obligation",
            binding=binding(),
            action_id="test_fixture.action",
            action_type="test_fixture.action_type",
            facts=(fact, fact),
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_verification_rejects_an_unrecognized_fact_value():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ReviewVerification(
            obligation_id="test_fixture.obligation",
            binding=binding(),
            action_id="test_fixture.action",
            action_type="test_fixture.action_type",
            facts=(object(),),
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"


def test_verification_empty_fact_tuple_is_only_an_empty_report_value():
    result = ReviewVerification(
        obligation_id="test_fixture.obligation",
        binding=binding(),
        action_id="test_fixture.action",
        action_type="test_fixture.action_type",
        facts=(),
    )
    assert result.facts == ()


def test_verification_request_names_the_obligation_and_opaque_reference():
    item = obligation()
    request = ReviewVerificationRequest(
        obligation=item,
        action_id=item.action_id,
        action_type=item.action_type,
        evidence_ref="test_fixture.evidence",
    )
    assert request.obligation is item
    assert request.evidence_ref == "test_fixture.evidence"


def test_verification_request_rejects_an_unrecognized_obligation_value():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ReviewVerificationRequest(
            obligation=object(),
            action_id="test_fixture.action",
            action_type="test_fixture.action_type",
            evidence_ref="test_fixture.evidence",
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"


def test_invalid_identifier_uses_the_fixed_review_diagnostic():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        NotificationDeliveredFact(evidence_ref=" ")
    assert caught.value.reason_code == "policy.review_obligation_invalid"
    assert caught.value.args == (
        "The policy review obligation declaration is invalid.",
    )


def test_review_obligation_inventory_exceeding_shared_bound_is_rejected():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyDecision.grant(
            POLICY,
            conditions=(),
            review_obligations=tuple(obligation() for _ in range(257)),
        )
    assert caught.value.reason_code == "policy.review_obligation_invalid"
