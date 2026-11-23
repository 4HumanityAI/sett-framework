"""The review state producer: restrictions declared by earlier decisions.

Consent is declared by the implementation, durability comes from a real
governance journal, and the review verifier is a test double standing for
the application's own. No governed effect is performed.
"""

import pytest
import test_policy_handoff as foundation

from sett._governance.validation import canonical_json_bytes
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    StrictJSONAuthorizationFingerprint,
)
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    SETTPolicyConfigurationError,
)
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream
from sett.policy_ruler import (
    DeadlineStatus,
    DeadlineStatusFact,
    GovernanceJournal,
    GovernanceRecord,
    NotificationDeliveredFact,
    PolicyDecision,
    PolicyReviewObligation,
    PolicyRuler,
    ReviewCompletedFact,
    ReviewMode,
    ReviewOutcome,
    ReviewVerification,
)
from sett.policy_ruler._review_state import ReviewStateProducer
from sett.policy_ruler.decision_record import DECISION_SCHEMA_ID, read_decision_records

LATER = "test_fixture.later"
OTHER = "test_fixture.other"


def journal(stream=None):
    stream = stream or InMemoryEvidenceStream(backend_id="backend.reviews", stream_id="reviews")
    return GovernanceJournal(
        namespace_id="namespace.test", journal_id="journal.reviews", stream=stream,
        origin=stream.head(), required_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(), historical_integrity_policies=(),
        max_frame_bytes=65536, max_records=64, max_total_bytes=4_194_304,
    )


def request(action_type=foundation.EFFECT, action_id="test_fixture.action"):
    return AuthorizationRequest(
        action_id=action_id, trace_id="test_fixture.trace", action_type=action_type,
        payload={"value": 1}, execution_mode=ExecutionMode.PLAIN,
        principal=FieldValue("test_fixture.subject", source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE),
    )


def obligation(mode=ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS, *, verifier="test_fixture.reviewer",
               future=(LATER,), identifier="test_fixture.restriction"):
    return PolicyReviewObligation(
        obligation_id=identifier, binding=foundation.binding(), mode=mode,
        verifier_id=verifier, verifier_version="1", action_id="test_fixture.action",
        action_type=foundation.EFFECT,
        future_action_types=list(future) if mode is ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS else [],
    )


def declaring(*obligations):
    return PolicyDecision.grant("test_fixture.policy", conditions=(), review_obligations=obligations)


class Reviewer:
    """The application's review verifier, as a test double."""

    verifier_id = "test_fixture.reviewer"
    verifier_version = "1"

    def __init__(self):
        self.outcome = None
        self.result = None
        self.error = None
        self.calls = []

    def verify(self, review_request, /):
        self.calls.append(review_request)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result(review_request)
        facts = () if self.outcome is None else (
            ReviewCompletedFact(evidence_ref="test_fixture.review_evidence", outcome=self.outcome),
        )
        return ReviewVerification(
            obligation_id=review_request.obligation.obligation_id, binding=review_request.obligation.binding,
            action_id=review_request.action_id, action_type=review_request.action_type, facts=facts,
        )


class Harness:
    """Declared consent, a real journal, review verifiers and synthetic conditions."""

    def __init__(self, *, declared=None, later=None, governance=None):
        self.clock = foundation.FixedClock()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.journal = governance or journal()
        self.reviewer = Reviewer()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=60, max_live_evaluations=16, clock=self.clock,
            evidence_sink=self.evidence, selection_resolver=foundation.Resolver(),
            implementation_owned_consent=True, governance_journal=self.journal,
            review_verifiers=[self.reviewer],
        )
        self.owner._register_state_producer(foundation.SyntheticStateProducer("conditions"))
        self.store = foundation.CountingStore()
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        for action_type, decision in ((foundation.EFFECT, declared), (LATER, later), (OTHER, None)):
            self.issuer.register_policy(
                action_type, foundation.StaticPolicy(decision=decision), binding=foundation.binding(),
            )

    def blocked(self, req):
        with pytest.raises(SETTAuthorizationError) as caught:
            self.issuer.authorize(req)
        return caught.value


# ── configuration ────────────────────────────────────────────────────────


def ruler(**options):
    return PolicyRuler(
        ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
        evaluation_ttl_seconds=60, max_live_evaluations=16, clock=foundation.FixedClock(),
        evidence_sink=foundation.InMemoryAuthorizationEvidence(), selection_resolver=foundation.Resolver(),
        **options,
    )


def test_review_verifiers_install_the_review_producer():
    owner = ruler(governance_journal=journal(), review_verifiers=[Reviewer()])
    assert type(owner._state_producers["review"]) is ReviewStateProducer


def test_review_verifiers_require_a_governance_journal():
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ruler(review_verifiers=[Reviewer()])
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


@pytest.mark.parametrize("verifiers", [[object()], [Reviewer(), Reviewer()], "test_fixture.reviewer"])
def test_invalid_review_verifiers_are_rejected(verifiers):
    with pytest.raises(SETTPolicyConfigurationError):
        ruler(governance_journal=journal(), review_verifiers=verifiers)


# ── what blocks and what does not ────────────────────────────────────────


def test_without_earlier_restrictions_an_action_proceeds():
    harness = Harness()
    assert harness.issuer.authorize(request(LATER)).receipt is not None
    assert harness.reviewer.calls == []


def test_the_full_configuration_issues_a_receipt_without_synthetic_state():
    governance = journal()
    clock = foundation.FixedClock()
    evidence = foundation.InMemoryAuthorizationEvidence()
    owner = PolicyRuler(
        ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
        evaluation_ttl_seconds=60, max_live_evaluations=16, clock=clock,
        evidence_sink=evidence, selection_resolver=foundation.Resolver(),
        condition_verifiers=[], implementation_owned_consent=True,
        governance_journal=governance, review_verifiers=[Reviewer()],
    )
    issuer = AuthorizationRuler(
        issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=foundation.CountingStore(),
        evidence_sink=evidence, clock=clock, policy_ruler=owner,
    )
    issuer.register_policy(
        foundation.EFFECT, foundation.StaticPolicy(decision=declaring()), binding=foundation.binding(),
    )
    assert issuer.authorize(request()).receipt is not None
    (record,) = read_decision_records(governance).decisions
    assert record.decision["verdict"] == "grant"
    assert record.review_obligations == ()


@pytest.mark.parametrize("mode", list(ReviewMode))
def test_an_action_is_not_blocked_by_its_own_obligations(mode):
    harness = Harness(declared=declaring(obligation(mode)))
    assert harness.issuer.authorize(request()).receipt is not None
    assert harness.reviewer.calls == []


def test_a_restriction_blocks_a_later_action_of_a_declared_type():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    error = harness.blocked(request(LATER, "test_fixture.later_action"))
    assert error.reason_code == "authorization.review_restriction_active"
    assert type(error) is SETTAuthorizationError
    (call,) = harness.reviewer.calls
    assert call.obligation.obligation_id == "test_fixture.restriction"
    assert call.action_type == LATER
    assert call.evidence_ref.startswith("decision.")
    assert harness.store.issued == 1


def test_a_favorable_completed_review_lifts_the_restriction():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    harness.reviewer.outcome = ReviewOutcome.FAVORABLE
    later = request(LATER, "test_fixture.later_action")
    outcome = harness.issuer.authorize(later)
    assert outcome.receipt is not None
    harness.issuer.verify_and_consume(outcome.receipt, later)
    assert harness.store.consumed == 1
    records = read_decision_records(harness.journal).decisions
    assert records[0].review_obligations[0]["obligation_id"] == "test_fixture.restriction"
    assert records[1].review == ({"operation_id": records[0].operation_id,
                                  "obligation_id": "test_fixture.restriction",
                                  "outcome": "favorable", "evidence_ref": "test_fixture.review_evidence"},)


def test_an_unfavorable_review_keeps_the_restriction():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    harness.reviewer.outcome = ReviewOutcome.UNFAVORABLE
    assert harness.blocked(request(LATER, "test_fixture.later_action")).reason_code == (
        "authorization.review_restriction_active"
    )


def test_notification_and_deadline_facts_do_not_lift_the_restriction():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    harness.reviewer.result = lambda review_request: ReviewVerification(
        obligation_id=review_request.obligation.obligation_id, binding=review_request.obligation.binding,
        action_id=review_request.action_id, action_type=review_request.action_type,
        facts=(NotificationDeliveredFact(evidence_ref="test_fixture.notice"),
               DeadlineStatusFact(evidence_ref="test_fixture.deadline", status=DeadlineStatus.NOT_OVERDUE)),
    )
    assert harness.blocked(request(LATER, "test_fixture.later_action")).reason_code == (
        "authorization.review_restriction_active"
    )


def test_unrelated_action_types_are_not_restricted():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    assert harness.issuer.authorize(request(OTHER, "test_fixture.other_action")).receipt is not None
    assert harness.reviewer.calls == []


def test_a_lifted_restriction_does_not_lift_an_independent_denial():
    harness = Harness(declared=declaring(obligation()), later=PolicyDecision.deny(
        "test_fixture.policy", reason_codes=("test_fixture.denied",), conditions=(), review_obligations=(),
    ))
    harness.issuer.authorize(request())
    harness.reviewer.outcome = ReviewOutcome.FAVORABLE
    outcome = harness.issuer.authorize(request(LATER, "test_fixture.later_action"))
    assert outcome.decision.verdict is AuthorizationVerdict.DENY
    assert outcome.receipt is None


def test_a_restriction_recorded_after_issue_blocks_the_receipt_use():
    harness = Harness(declared=declaring(obligation()))
    later = request(LATER, "test_fixture.later_action")
    outcome = harness.issuer.authorize(later)
    harness.issuer.authorize(request())
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.verify_and_consume(outcome.receipt, later)
    assert caught.value.reason_code == "authorization.review_restriction_active"
    assert harness.store.consumed == 0


# ── failures ─────────────────────────────────────────────────────────────


def test_a_restriction_without_its_verifier_blocks_as_configuration():
    harness = Harness(declared=declaring(obligation(verifier="test_fixture.absent")))
    harness.issuer.authorize(request())
    error = harness.blocked(request(LATER, "test_fixture.later_action"))
    assert error.reason_code == "authorization.review_verifier_missing"
    assert type(error) is SETTAuthorizationConfigurationError


def test_a_failing_verifier_blocks():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    harness.reviewer.error = RuntimeError("test_fixture.reviewer_secret")
    error = harness.blocked(request(LATER, "test_fixture.later_action"))
    assert error.reason_code == "authorization.review_verifier_failed"
    assert "test_fixture.reviewer_secret" not in str(error)


def test_a_result_for_another_obligation_is_invalid():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    harness.reviewer.result = lambda review_request: ReviewVerification(
        obligation_id="test_fixture.other_obligation", binding=review_request.obligation.binding,
        action_id=review_request.action_id, action_type=review_request.action_type,
        facts=(ReviewCompletedFact(evidence_ref="test_fixture.review_evidence", outcome=ReviewOutcome.FAVORABLE),),
    )
    assert harness.blocked(request(LATER, "test_fixture.later_action")).reason_code == (
        "authorization.review_result_invalid"
    )


class FailingStream(InMemoryEvidenceStream):
    def read(self, *, after=None):
        raise RuntimeError("test_fixture.storage_down")


def test_an_unreadable_journal_blocks_before_the_record_is_written():
    harness = Harness(governance=journal(FailingStream(backend_id="backend.reviews", stream_id="reviews")))
    assert harness.blocked(request(LATER)).reason_code == "authorization.review_state_unavailable"
    assert harness.store.issued == 0


# ── decision record version 2 ────────────────────────────────────────────


def test_version_one_records_stay_readable():
    governance = journal()
    payload = canonical_json_bytes({
        "authorization_attempt_id": None, "action_id": "test_fixture.action", "action_type": foundation.EFFECT,
        "trace_id": "test_fixture.trace", "checkpoint_id": foundation.ENTRY,
        "selection": {"selection_id": "test_fixture.selection", "selection_revision": "1"},
        "policies": [], "decision": {"verdict": "grant"}, "conditions": [], "approvals": [],
        "consent": None, "request_fingerprint": None, "fingerprint_policy_id": None,
        "recorded_at": "2026-08-30T12:00:00.000000Z",
    }, field="test fixture")
    head = governance.read().observed_head
    record = GovernanceRecord(operation_id="decision.test_fixture_v1", schema_id=DECISION_SCHEMA_ID,
                              schema_version=1, payload=payload)
    governance.append(governance.prepare(record, expected_head=head))
    (decoded,) = read_decision_records(governance).decisions
    assert decoded.review_obligations == () and decoded.review == ()
