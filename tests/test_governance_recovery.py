"""Recovery across the governance journal and the restored trace.

Each test runs a governed executor with the four real state producers, a
real governance journal and a durable trace, stops it at one point, and
reads what the evidence can vouch for. Nothing is resumed: continuing is a
new submission.
"""

import pytest
import test_policy_handoff as foundation
from test_authorization_execution_integration import AuthoritativeResolver
from test_controlled_checkpoints import durable_orchestrator
from test_review_follow_up import SwitchingStream
from test_review_restrictions import (
    LATER,
    Reviewer,
    declaring,
    journal,
    obligation,
    request,
)

from sett import (
    Action,
    AuthorizationRuler,
    PolicyDecision,
    SETTAuthorizationError,
    SETTExecutor,
    StrictJSONAuthorizationFingerprint,
)
from sett.audit_ruler.recovery import record_boundary_disposition
from sett.audit_ruler.trace import BoundaryDispositionBasis, TraceRecorder
from sett.authorization_ruler import ApprovalRequirement
from sett.exceptions import SETTPersistenceError, SETTPolicyConfigurationError
from sett.persistence import (
    DurabilityLevel,
    InMemoryEvidenceStream,
    decode_trace_evidence_frame,
)
from sett.policy_ruler import PolicyRuler, ReviewMode, ReviewOutcome
from sett.policy_ruler.governance_recovery import (
    RecoveredOutcome,
    read_governance_recovery,
)

EFFECT = foundation.EFFECT


class ProcessStopped(BaseException):
    """Stands for the process stopping while the handler runs."""


class CrashingStream(InMemoryEvidenceStream):
    """Lose one trace kind, as a process that stops before writing it would."""

    def __init__(self, lost):
        super().__init__(backend_id="backend.local", stream_id="evidence.local")
        self.lost = lost

    def append(self, *, write_id, payload, expected_head):
        if decode_trace_evidence_frame(payload).payload["kind"] == self.lost:
            raise SETTPersistenceError(
                "The process stopped before this record.",
                reason_code="audit.evidence_write_failed", backend_id="backend.local",
            )
        return super().append(write_id=write_id, payload=payload, expected_head=expected_head)


class FailingIssueStore(foundation.CountingStore):
    def issue(self, receipt):
        raise RuntimeError("test_fixture.store_down")


class Durable:
    """A governed executor over a durable trace and a governance journal."""

    def __init__(self, *, decision=None, lost="test_fixture.never", governance=None, store=None):
        self.stream = CrashingStream(lost=lost)
        self.journal = governance or journal()
        self.clock = foundation.FixedClock()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.reviewer = Reviewer()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=60, max_live_evaluations=16, clock=self.clock,
            evidence_sink=self.evidence, selection_resolver=foundation.Resolver(),
            condition_verifiers=[], implementation_owned_consent=True,
            governance_journal=self.journal, review_verifiers=[self.reviewer],
        )
        self.store = store or foundation.CountingStore()
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        for action_type, chosen in ((EFFECT, decision), (LATER, None)):
            self.issuer.register_policy(
                action_type, foundation.StaticPolicy(decision=chosen), binding=foundation.binding(),
            )
        self.executor = SETTExecutor(
            authorization_ruler=self.issuer, authorization_request_resolver=AuthoritativeResolver(),
        )
        self.orchestrator = durable_orchestrator(self.stream)
        self.orchestrator.register_executor(self.executor)
        self.effects = []

    def handler(self, behaviour=None):
        def handle(payload):
            self.effects.append(payload)
            if behaviour is not None:
                behaviour()
            return "done"

        self.executor.register_handler(EFFECT, handle)

    def submit(self, action_type=EFFECT):
        return self.executor.submit(Action(action_type=action_type, payload={"value": 1}, action_id="test_fixture.action"))

    def restored(self):
        return TraceRecorder.restore(
            evidence_stream=self.stream, namespace_id="namespace.local",
            evidence_durability=DurabilityLevel.IN_MEMORY,
        )

    def recovery(self, *, trace=True):
        return read_governance_recovery(self.journal, trace=self.restored() if trace else None)


def only(recovery):
    (decision,) = recovery.decisions
    return decision


# ── what the evidence can vouch for ──────────────────────────────────────


def test_a_completed_effect_is_known():
    durable = Durable()
    durable.handler()
    durable.submit()
    recovery = durable.recovery()
    decision = only(recovery)
    assert (decision.outcome, decision.effect) == (RecoveredOutcome.EFFECT_KNOWN, "completed")
    assert decision.verdict == "grant" and decision.boundary is None
    assert recovery.trace_given and recovery.complete and not recovery.requires_reconciliation


def test_an_effect_blocked_at_the_final_gate_is_known_as_blocked():
    durable = Durable()
    durable.handler()

    def register_other(view):
        if view["kind"] == "handler.authorized":
            durable.issuer.register_policy(
                "test_fixture.other", foundation.StaticPolicy("test_fixture.other"),
                binding=foundation.binding("test_fixture.other"),
            )

    durable.orchestrator.register_trace_exporter(register_other)
    with pytest.raises(SETTAuthorizationError):
        durable.submit()
    decision = only(durable.recovery())
    assert (decision.outcome, decision.effect) == (RecoveredOutcome.EFFECT_KNOWN, "blocked")
    assert durable.effects == []


@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        (PolicyDecision.deny("test_fixture.policy", reason_codes=("test_fixture.denied",),
                             conditions=(), review_obligations=()), RecoveredOutcome.DENIED),
        (PolicyDecision.require_approval(
            "test_fixture.policy",
            requirements=(ApprovalRequirement("test_fixture.approval", "test_fixture.approver"),),
            conditions=(), review_obligations=(),
        ), RecoveredOutcome.APPROVAL_REQUIRED),
    ],
)
def test_denials_and_approval_requirements_hold_nothing(decision, outcome):
    durable = Durable(decision=decision)
    durable.handler()
    with pytest.raises(SETTAuthorizationError):
        durable.submit()
    assert only(durable.recovery()).outcome is outcome
    assert durable.effects == []


def test_without_a_trace_a_grant_is_reported_as_unknown():
    durable = Durable()
    durable.handler()
    durable.submit()
    recovery = durable.recovery(trace=False)
    assert only(recovery).outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN
    assert not recovery.trace_given


def test_obligations_carry_their_review_records_and_the_actions_that_cleared_them():
    durable = Durable(decision=declaring(obligation()))
    durable.issuer.authorize(request())
    durable.reviewer.outcome = ReviewOutcome.FAVORABLE
    durable.owner.follow_up_reviews()
    later = durable.issuer.authorize(request(LATER, "test_fixture.later_action"))
    assert later.receipt is not None
    recovery = read_governance_recovery(durable.journal)
    (item,) = recovery.obligations
    assert item.mode is ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS
    assert item.future_action_types == (LATER,)
    (record,) = item.review_records
    assert (record.fact, record.outcome) == ("review_completed", "favorable")
    first, second = recovery.decisions
    assert item.decision_operation_id == first.operation_id
    assert item.cleared_by == (second.operation_id,)


def test_the_reader_accepts_only_a_journal_and_a_restored_trace():
    with pytest.raises(TypeError):
        read_governance_recovery(object())
    with pytest.raises(TypeError):
        read_governance_recovery(journal(), trace=object())


# ── crash points between the journal and the trace ───────────────────────


def test_stopped_after_the_decision_record_before_the_receipt():
    durable = Durable(store=FailingIssueStore())
    durable.handler()
    with pytest.raises(SETTAuthorizationError):
        durable.submit()
    assert only(durable.recovery()).outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN
    assert durable.effects == []


def test_stopped_after_the_receipt_before_its_use():
    first = Durable()
    outcome = first.issuer.authorize(request())
    restarted = Durable(governance=first.journal)
    with pytest.raises(SETTAuthorizationError) as caught:
        restarted.issuer.verify_and_consume(outcome.receipt, request())
    assert caught.value.reason_code == "authorization.policy_binding_unavailable"
    assert only(first.recovery()).outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN


def test_stopped_before_the_effect_barrier_is_confirmed():
    durable = Durable(lost="handler.authorized")
    durable.handler()
    with pytest.raises(SETTPersistenceError):
        durable.submit()
    assert durable.effects == []
    # This fixture stopped before the barrier. The reader cannot prove
    # that an arbitrary supplied prefix includes every subsequent event.
    assert only(durable.recovery()).outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN


def test_stopped_after_the_barrier_before_the_handler_started():
    durable = Durable(lost="handler.started")
    durable.handler()
    with pytest.raises(SETTPersistenceError):
        durable.submit()
    assert durable.effects == []
    recovery = durable.recovery()
    decision = only(recovery)
    assert decision.outcome is RecoveredOutcome.EFFECT_UNCERTAIN
    assert decision.boundary.start_kind == "handler.authorized"
    assert recovery.requires_reconciliation


def test_stopped_while_the_handler_runs():
    durable = Durable()

    def stop():
        raise ProcessStopped

    durable.handler(stop)
    with pytest.raises(ProcessStopped):
        durable.submit()
    recovery = durable.recovery()
    decision = only(recovery)
    assert decision.outcome is RecoveredOutcome.EFFECT_UNCERTAIN
    assert decision.boundary.start_kind == "handler.started"
    assert recovery.requires_reconciliation


def test_stopped_after_the_handler_before_its_completion_is_confirmed():
    durable = Durable(lost="handler.completed")
    durable.handler()
    with pytest.raises(SETTPersistenceError):
        durable.submit()
    assert durable.effects == [{"value": 1}]
    assert only(durable.recovery()).outcome is RecoveredOutcome.EFFECT_UNCERTAIN


def test_stopped_before_a_review_record_is_confirmed():
    stream = SwitchingStream(backend_id="backend.reviews", stream_id="reviews")
    durable = Durable(decision=declaring(obligation()), governance=journal(stream))
    durable.issuer.authorize(request())
    durable.reviewer.outcome = ReviewOutcome.FAVORABLE
    stream.refuse = True
    with pytest.raises(SETTPolicyConfigurationError):
        durable.owner.follow_up_reviews()
    stream.refuse = False
    restarted = Durable(decision=declaring(obligation()), governance=durable.journal)
    with pytest.raises(SETTAuthorizationError) as caught:
        restarted.issuer.authorize(request(LATER, "test_fixture.later_action"))
    assert caught.value.reason_code == "authorization.review_restriction_active"
    # The unconfirmed record may still be read back, as a decision record
    # written before a durability failure can be; it lifts nothing either way.
    (item,) = read_governance_recovery(durable.journal).obligations
    assert len(item.review_records) == 1


# ── explicit continuation ────────────────────────────────────────────────


def test_documenting_an_uncertain_effect_keeps_it_uncertain_and_authorizes_nothing():
    durable = Durable()

    def stop():
        raise ProcessStopped

    durable.handler(stop)
    with pytest.raises(ProcessStopped):
        durable.submit()
    report = durable.restored()
    boundary = only(read_governance_recovery(durable.journal, trace=report)).boundary
    record_boundary_disposition(
        report, boundary, evidence_stream=durable.stream, namespace_id="namespace.local",
        evidence_durability=DurabilityLevel.IN_MEMORY, basis=BoundaryDispositionBasis.ABANDONED,
    )
    recovery = durable.recovery()
    decision = only(recovery)
    assert decision.outcome is RecoveredOutcome.EFFECT_UNCERTAIN
    assert len(decision.boundary.dispositions) == 1
    assert not recovery.requires_reconciliation


def test_continuing_is_a_new_submission_with_a_new_decision():
    first = Durable()

    def stop():
        raise ProcessStopped

    first.handler(stop)
    with pytest.raises(ProcessStopped):
        first.submit()
    restarted = Durable(governance=first.journal)
    restarted.handler()
    assert restarted.submit() == "done"
    stopped, continued = read_governance_recovery(first.journal).decisions
    assert stopped.operation_id != continued.operation_id
    assert restarted.recovery().decisions[-1].outcome is RecoveredOutcome.EFFECT_KNOWN
