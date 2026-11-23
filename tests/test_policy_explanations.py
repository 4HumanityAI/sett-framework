"""Explanations of one decision: an operational view and a public view.

Each test runs the four real state producers with a real governance journal,
then explains the decision record, or the error of a blocked attempt, and
checks what each view shows. Planted values stand for secrets and personal
data an application or its policies could put in any field they fill.
"""

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
import test_policy_handoff as foundation
from test_governance_recovery import Durable, ProcessStopped, only
from test_policy_condition_producer import Verifier, condition
from test_review_restrictions import LATER, Reviewer, journal, obligation

from sett.authorization_ruler import (
    ApprovalRequirement,
    AuthorizationRequest,
    AuthorizationRuler,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    StrictJSONAuthorizationFingerprint,
)
from sett.ethics_ruler.ethic_kernel.filter import FilterVerdict
from sett.exceptions import SETTAuthorizationError, SETTValidationError
from sett.policy_ruler import (
    ConditionVerification,
    ConditionVerificationState,
    PolicyBinding,
    PolicyDecision,
    PolicyOrigin,
    PolicyRuler,
    ReviewCompletedFact,
    ReviewOutcome,
    ReviewVerification,
)
from sett.policy_ruler.decision_record import read_decision_records
from sett.policy_ruler.explanation import _SETT_CODES, PublicValues, explain

EFFECT = foundation.EFFECT
ROOT = Path(__file__).resolve().parents[1]

PLANTED_PAYLOAD = "planted-payload-4c1d"
PLANTED_PERSON = "planted.person@example.invalid"
PLANTED_REASON = "planted.reason.secret-9e2a"
PLANTED_POLICY = "planted.policy.secret-71b0"
PLANTED_VERSION = "planted-version-secret-5f3c"
PLANTED_CONDITION = "planted.condition.secret-0d8e"
PLANTED_OBLIGATION = "planted.obligation.secret-a6f4"
PLANTED_EVIDENCE = "planted.evidence.secret-3b97"
PLANTED_MESSAGE = "planted message secret-e05c"

OPERATIONAL_FIELDS = {
    "view", "source", "reference", "action_type", "verdict", "reason_code", "original_reason_code",
    "policies", "policy_rows_may_be_incomplete", "conditions", "failed_required_condition",
    "attempted_conditions", "pending_approvals", "review_obligations", "restrictions_cleared",
    "informative", "recovery_outcome", "effect_end", "ethics_verdict", "next_steps",
}
PUBLIC_FIELDS = OPERATIONAL_FIELDS - {"attempted_conditions"} | {"withheld"}


def layered(policy_id, layer, version="1"):
    return PolicyBinding(
        policy_id=policy_id, policy_version=version, contract_version="2.0",
        origins=[PolicyOrigin(
            requirement_id="test_fixture.requirement", requirement_revision="1", layer_id=layer,
            source=None, source_absence_reason="Synthetic test fixture",
        )],
        profile_id=None, profile_revision=None,
    )


class Selecting(foundation.Resolver):
    """Selects, for each action type, the policies registered for it."""

    def __init__(self, by_type):
        super().__init__()
        self.by_type = by_type

    def resolve(self, request):
        self.required = self.by_type[request.action_type]
        return super().resolve(request)


class Harness:
    """All four real state producers, a real journal and the given policies."""

    def __init__(self, policies):
        self.clock = foundation.FixedClock()
        self.verifier = Verifier(self.clock)
        self.reviewer = Reviewer()
        self.journal = journal()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=60, max_live_evaluations=16, clock=self.clock,
            evidence_sink=self.evidence, selection_resolver=Selecting({
                action_type: [binding for binding, _decision in rows]
                for action_type, rows in policies.items()
            }),
            condition_verifiers=[self.verifier], implementation_owned_consent=True,
            governance_journal=self.journal, review_verifiers=[self.reviewer],
        )
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=foundation.CountingStore(),
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        for action_type, rows in policies.items():
            for binding, decision in rows:
                self.issuer.register_policy(
                    action_type, foundation.StaticPolicy(binding.policy_id, decision), binding=binding,
                )

    def authorize(self, action_type=EFFECT, *, action_id="test_fixture.action", payload=None,
                  principal="test_fixture.subject"):
        return self.issuer.authorize(AuthorizationRequest(
            action_id=action_id, trace_id="test_fixture.trace", action_type=action_type,
            payload=payload if payload is not None else {"value": 1}, execution_mode=ExecutionMode.PLAIN,
            principal=FieldValue(principal, source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE),
        ))

    def blocked(self, **request):
        with pytest.raises(SETTAuthorizationError) as caught:
            self.authorize(**request)
        return caught.value

    def records(self):
        return read_decision_records(self.journal).decisions


def grant(policy_id, *, reasons=(), conditions=(), obligations=()):
    return PolicyDecision.grant(
        policy_id, reason_codes=reasons, conditions=conditions, review_obligations=obligations,
    )


def checked(binding, identifier="test_fixture.condition"):
    """A grant whose policy declares one required condition."""
    return grant(binding.policy_id, conditions=(
        condition(identifier, bound=binding, verifier_id="test_fixture.verifier"),
    ))


def text(view):
    return json.dumps(view, sort_keys=True)


# ── one record, both views ───────────────────────────────────────────────


def test_a_grant_explains_each_policy_with_its_own_verdict():
    first = layered("test_fixture.first", "test_fixture.organization")
    second = layered("test_fixture.second", "test_fixture.regulation")
    harness = Harness({EFFECT: [
        (first, grant(first.policy_id, reasons=("test_fixture.first_reason",))),
        (second, grant(second.policy_id, reasons=("test_fixture.second_reason",))),
    ]})
    outcome = harness.authorize()
    (record,) = harness.records()
    explanation = explain(record)
    operational = explanation.operational_view()
    assert explanation.source == operational["source"] == "decision_record"
    assert operational["verdict"] == "grant"
    assert operational["reference"] == outcome.authorization_attempt_id
    assert operational["policies"] == [
        {"policy_id": "test_fixture.first", "policy_version": "1", "verdict": "grant",
         "reason_codes": ["test_fixture.first_reason"],
         "origins": [{"layer_id": "test_fixture.organization", "requirement_id": "test_fixture.requirement",
                      "requirement_revision": "1", "source": None,
                      "source_absence_reason": "Synthetic test fixture"}],
         "profile": None},
        {"policy_id": "test_fixture.second", "policy_version": "1", "verdict": "grant",
         "reason_codes": ["test_fixture.second_reason"],
         "origins": [{"layer_id": "test_fixture.regulation", "requirement_id": "test_fixture.requirement",
                      "requirement_revision": "1", "source": None,
                      "source_absence_reason": "Synthetic test fixture"}],
         "profile": None},
    ]
    assert operational["policy_rows_may_be_incomplete"] is False

    public = explanation.public_view()
    assert (public["verdict"], public["policies"], public["action_type"]) == ("grant", [], None)
    assert public["reference"] == outcome.authorization_attempt_id
    assert public["withheld"] == 3  # two policies and the action type

    approved = explanation.public_view(PublicValues(
        policy_ids={"test_fixture.second"}, action_types={EFFECT},
        layer_ids={"test_fixture.regulation"}, requirement_ids={"test_fixture.requirement"},
    ))
    assert approved["policies"] == [{
        "policy_id": "test_fixture.second", "verdict": "grant", "reason_codes": [],
        "origins": [{"layer_id": "test_fixture.regulation", "requirement_id": "test_fixture.requirement",
                     "source_id": None, "source_declared": False}],
    }]
    assert approved["action_type"] == EFFECT
    assert approved["withheld"] == 2  # the first policy and the second one's reason code


def test_a_denial_keeps_the_granting_policy_s_own_verdict():
    first = layered("test_fixture.first", "test_fixture.organization")
    second = layered("test_fixture.second", "test_fixture.regulation")
    harness = Harness({EFFECT: [
        (first, None),
        (second, PolicyDecision.deny(second.policy_id, reason_codes=("test_fixture.denied",),
                                     conditions=(), review_obligations=())),
    ]})
    harness.authorize()
    (record,) = harness.records()
    view = explain(record).operational_view()
    assert view["verdict"] == "deny"
    assert [(row["policy_id"], row["verdict"]) for row in view["policies"]] == [
        ("test_fixture.first", "grant"), ("test_fixture.second", "deny"),
    ]


def test_each_view_has_a_closed_set_of_fields():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, checked(binding))]})
    harness.authorize()
    (record,) = harness.records()
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    error = harness.blocked()
    for explanation in (explain(record), explain(error)):
        assert set(explanation.operational_view()) == OPERATIONAL_FIELDS
        assert set(explanation.public_view()) == PUBLIC_FIELDS
    assert explain(record).operational_view()["conditions"] == [{
        "policy_id": "test_fixture.policy", "policy_version": "1",
        "condition_id": "test_fixture.condition", "state": "satisfied",
        "requirements": [{"layer_id": "test_fixture.layer", "requirement_id": "test_fixture.requirement"}],
    }]


# ── blocked attempts ─────────────────────────────────────────────────────


def test_a_failed_required_condition_stays_visible_in_both_views():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, checked(binding))]})
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    error = harness.blocked()
    # A blocked attempt writes no decision record; its error is the source.
    assert harness.records() == ()
    explanation = explain(error)
    assert explanation.source == "authorization_error"
    operational = explanation.operational_view()
    identity = {"policy_id": "test_fixture.policy", "policy_version": "1",
                "condition_id": "test_fixture.condition"}
    assert operational["reason_code"] == "authorization.condition_unsatisfied"
    assert operational["failed_required_condition"] == identity
    assert operational["attempted_conditions"] == [identity]
    assert operational["verdict"] is None
    assert operational["policy_rows_may_be_incomplete"] is True
    assert operational["reference"] == error.authorization_attempt_id

    public = explanation.public_view()
    assert public["reason_code"] == "authorization.condition_unsatisfied"
    assert public["failed_required_condition"] == {"condition_id": None}
    assert public["reference"] == error.authorization_attempt_id
    approved = explanation.public_view(PublicValues(condition_ids={"test_fixture.condition"}))
    assert approved["failed_required_condition"] == {"condition_id": "test_fixture.condition"}


@pytest.mark.parametrize(("state", "code"), [
    (ConditionVerificationState.EVIDENCE_NOT_PRESENT, "authorization.condition_evidence_not_present"),
    (ConditionVerificationState.EVIDENCE_INVALID, "authorization.condition_evidence_invalid"),
    (ConditionVerificationState.EVIDENCE_UNAVAILABLE, "authorization.condition_evidence_unavailable"),
    (ConditionVerificationState.UNSATISFIED, "authorization.condition_unsatisfied"),
    (None, "authorization.condition_verifier_failed"),
])
def test_evidence_and_verifier_failures_keep_their_own_codes(state, code):
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, checked(binding))]})
    if state is None:
        harness.verifier.error = RuntimeError(PLANTED_MESSAGE)
    else:
        harness.verifier.states = [state]
    explanation = explain(harness.blocked())
    assert explanation.operational_view()["reason_code"] == code
    assert explanation.public_view()["reason_code"] == code


def test_exception_messages_reach_neither_view():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, checked(binding))]})
    harness.verifier.error = RuntimeError(PLANTED_MESSAGE)
    raised = harness.blocked()
    foreign = SETTAuthorizationError(PLANTED_MESSAGE, reason_code="test_fixture.store_code")
    for error in (raised, foreign):
        explanation = explain(error)
        assert PLANTED_MESSAGE not in text(explanation.operational_view())
        assert PLANTED_MESSAGE not in text(explanation.public_view())
    # A code SETT does not raise is the application's: withheld unless approved.
    assert explain(foreign).public_view()["reason_code"] is None
    assert explain(foreign).operational_view()["reason_code"] == "test_fixture.store_code"
    approved = PublicValues(reason_codes={"test_fixture.store_code"})
    assert explain(foreign).public_view(approved)["reason_code"] == "test_fixture.store_code"


# ── application values ───────────────────────────────────────────────────


def test_a_planted_reason_code_is_shown_only_to_operators_unless_approved():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, grant(binding.policy_id, reasons=(PLANTED_REASON,)))]})
    harness.authorize()
    (record,) = harness.records()
    explanation = explain(record)
    assert explanation.operational_view()["policies"][0]["reason_codes"] == [PLANTED_REASON]
    assert PLANTED_REASON not in text(explanation.public_view())
    approved = PublicValues(policy_ids={binding.policy_id}, reason_codes={PLANTED_REASON})
    assert explanation.public_view(approved)["policies"][0]["reason_codes"] == [PLANTED_REASON]


def test_planted_values_never_reach_the_public_view():
    planted = layered(PLANTED_POLICY, "test_fixture.layer", version=PLANTED_VERSION)
    declared = condition(PLANTED_CONDITION, bound=planted)
    harness = Harness({
        EFFECT: [(planted, grant(
            PLANTED_POLICY, reasons=(PLANTED_REASON,), conditions=(declared,),
            obligations=(replace(obligation(identifier=PLANTED_OBLIGATION), binding=planted),),
        ))],
        LATER: [(foundation.binding(), None)],
    })
    harness.verifier.result = ConditionVerification(
        condition=declared, state=ConditionVerificationState.SATISFIED,
        checked_at=harness.clock.now(), valid_until=None,
        evidence_refs=(PLANTED_EVIDENCE,), reason_codes=(),
    )
    harness.reviewer.result = lambda review: ReviewVerification(
        obligation_id=review.obligation.obligation_id, binding=review.obligation.binding,
        action_id=review.action_id, action_type=review.action_type,
        facts=(ReviewCompletedFact(evidence_ref=PLANTED_EVIDENCE, outcome=ReviewOutcome.FAVORABLE),),
    )
    harness.authorize(payload={"note": PLANTED_PAYLOAD}, principal=PLANTED_PERSON)
    harness.authorize(LATER, action_id="test_fixture.later_action",
                      payload={"note": PLANTED_PAYLOAD}, principal=PLANTED_PERSON)
    declaring, clearing = harness.records()
    assert clearing.review  # the second action cleared the first one's restriction
    secrets = (PLANTED_PAYLOAD, PLANTED_PERSON, PLANTED_REASON, PLANTED_POLICY, PLANTED_VERSION,
               PLANTED_CONDITION, PLANTED_OBLIGATION, PLANTED_EVIDENCE)
    for record in (declaring, clearing):
        explanation = explain(record)
        public = text(explanation.public_view())
        assert not [value for value in secrets if value in public]
        operational = text(explanation.operational_view())
        for value in (PLANTED_PAYLOAD, PLANTED_PERSON, PLANTED_EVIDENCE, record.request_fingerprint):
            assert value not in operational
    assert explain(clearing).operational_view()["restrictions_cleared"] == [{
        "obligation_id": PLANTED_OBLIGATION, "declared_by": declaring.operation_id, "outcome": "favorable",
    }]
    assert explain(clearing).public_view()["restrictions_cleared"] == 1
    assert explain(declaring).public_view()["review_obligations"] == []


def test_next_steps_appear_only_as_declared():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, None)]})
    harness.authorize()
    (record,) = harness.records()
    explanation = explain(record)
    assert explanation.operational_view()["next_steps"] == []
    assert explanation.public_view()["next_steps"] == []
    steps = ["test_fixture.contact_operator", "test_fixture.retry_later"]
    assert explanation.operational_view(next_steps=steps)["next_steps"] == steps
    assert explanation.public_view(next_steps=steps)["next_steps"] == steps
    for invalid in ("test_fixture.single_string", [""], [7]):
        with pytest.raises(SETTValidationError):
            explanation.public_view(next_steps=invalid)


def test_an_approval_requirement_shows_pending_approvals_and_nothing_about_who():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, PolicyDecision.require_approval(
        binding.policy_id,
        requirements=(ApprovalRequirement("test_fixture.approval", "test_fixture.approver"),),
        conditions=(), review_obligations=(),
    ))]})
    harness.authorize()
    (record,) = harness.records()
    explanation = explain(record)
    operational = explanation.operational_view()
    public = explanation.public_view()
    assert operational["verdict"] == public["verdict"] == "require_approval"
    assert operational["pending_approvals"] == ["test_fixture.approval"]
    assert public["pending_approvals"] == 1
    for view in (operational, public):
        assert "test_fixture.approver" not in text(view)


# ── the effect and the ethics verdict stay separate ──────────────────────


def test_an_uncertain_effect_is_never_reported_as_done():
    durable = Durable()

    def stop():
        raise ProcessStopped

    durable.handler(stop)
    with pytest.raises(ProcessStopped):
        durable.submit()
    item = only(durable.recovery())
    (record,) = read_decision_records(durable.journal).decisions
    explanation = explain(record, recovered=item)
    for view in (explanation.operational_view(), explanation.public_view()):
        assert view["verdict"] == "grant"
        assert (view["recovery_outcome"], view["effect_end"]) == ("effect_uncertain", None)
        assert "completed" not in text(view)

    finished = Durable()
    finished.handler()
    finished.submit()
    (done,) = read_decision_records(finished.journal).decisions
    view = explain(done, recovered=only(finished.recovery())).public_view()
    assert (view["recovery_outcome"], view["effect_end"]) == ("effect_known", "completed")
    with pytest.raises(ValueError):
        explain(done, recovered=item)


def test_the_ethics_verdict_is_kept_apart_from_authorization():
    binding = foundation.binding()
    harness = Harness({EFFECT: [(binding, None)]})
    harness.authorize()
    (record,) = harness.records()
    view = explain(record, ethics_verdict=FilterVerdict.REJECT).public_view()
    assert (view["verdict"], view["ethics_verdict"]) == ("grant", "reject")
    with pytest.raises(TypeError):
        explain(record, ethics_verdict="reject")
    with pytest.raises(TypeError):
        explain(object())
    with pytest.raises(TypeError):
        explain(record).public_view(approved={"policy_ids": set()})


# ── the public vocabulary ────────────────────────────────────────────────


def test_the_public_vocabulary_matches_the_codes_sett_raises():
    # Evidence event kinds share the prefix but are not reason codes.
    kinds = {"authorization." + kind for kind in (
        "requested", "granted", "failed", "policy_checked",
        "receipt_issued", "receipt_rejected", "receipt_verified",
    )}
    found = {
        match.group(1)
        for path in sorted((ROOT / "sett").rglob("*.py"))
        if path.name != "explanation.py"
        for match in re.finditer(r"\"(authorization\.[a-z_]+)\"", path.read_text(encoding="utf-8"))
    }
    assert found == _SETT_CODES | kinds
