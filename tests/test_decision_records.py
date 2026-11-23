"""Decision records: the durability producer over a governance journal, and their recovery.

Review still has no real producer; the authorization cases install a
synthetic review state, as the other handoff tests do. They prove no real
integration and no governed effect.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import test_policy_condition_producer as conditions
import test_policy_handoff as foundation

from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.authorization_ruler import (
    ApprovalRequirement,
    AuthorizationRuler,
    StrictJSONAuthorizationFingerprint,
)
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    SETTPolicyConfigurationError,
)
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream
from sett.policy_ruler import (
    GovernanceJournal,
    GovernanceRecord,
    PolicyDecision,
    PolicyRuler,
)
from sett.policy_ruler.decision_record import (
    DECISION_SCHEMA_ID,
    DECISION_SCHEMA_VERSION,
    DecisionRecordProducer,
    read_decision_records,
)


def journal(stream=None, *, required=DurabilityLevel.IN_MEMORY):
    stream = stream or InMemoryEvidenceStream(backend_id="backend.decisions", stream_id="decisions")
    return GovernanceJournal(
        namespace_id="namespace.test", journal_id="journal.decisions", stream=stream,
        origin=stream.head(), required_durability=required,
        integrity_policy=Sha256IntegrityPolicy(), historical_integrity_policies=(),
        max_frame_bytes=65536, max_records=64, max_total_bytes=4_194_304,
    )


def ruler(clock=None, evidence=None, **options):
    return PolicyRuler(
        ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
        evaluation_ttl_seconds=60, max_live_evaluations=16,
        clock=clock or foundation.FixedClock(),
        evidence_sink=evidence or foundation.InMemoryAuthorizationEvidence(),
        selection_resolver=foundation.Resolver(), **options,
    )


class Harness:
    """A governance journal, declared consent and synthetic review; no effect."""

    def __init__(self, *, decision=None, governance=None, review=True, verifier=False):
        self.clock = foundation.FixedClock()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.journal = governance or journal()
        self.verifier = conditions.Verifier(self.clock) if verifier else None
        self.owner = ruler(
            self.clock, self.evidence, implementation_owned_consent=True,
            governance_journal=self.journal,
            condition_verifiers=[self.verifier] if verifier else None,
        )
        if not verifier:
            self.owner._register_state_producer(foundation.SyntheticStateProducer("conditions"))
        if review:
            self.owner._register_state_producer(foundation.SyntheticStateProducer("review"))
        self.store = foundation.CountingStore()
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        self.issuer.register_policy(
            foundation.EFFECT, foundation.StaticPolicy(decision=decision), binding=foundation.binding(),
        )


# ── configuration ────────────────────────────────────────────────────────


def test_default_installs_no_durability_producer():
    owner = ruler()
    assert "durability" not in owner._state_producers


def test_a_governance_journal_installs_the_decision_record_producer():
    owner = ruler(governance_journal=journal())
    assert type(owner._state_producers["durability"]) is DecisionRecordProducer


@pytest.mark.parametrize("value", [object(), "journal.decisions", 1])
def test_only_a_governance_journal_is_accepted(value):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ruler(governance_journal=value)
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


def test_a_second_durability_producer_is_rejected():
    owner = ruler(governance_journal=journal())
    with pytest.raises(SETTPolicyConfigurationError):
        owner._register_state_producer(foundation.SyntheticStateProducer("durability"))


# ── recording ────────────────────────────────────────────────────────────


def test_a_grant_writes_one_confirmed_decision_record_before_the_receipt():
    harness = Harness()
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.receipt is not None
    recovered = read_decision_records(harness.journal)
    assert recovered.complete and not recovered.requires_reconciliation
    assert len(recovered.decisions) == 1
    record = recovered.decisions[0]
    assert record.operation_id == "decision." + outcome.authorization_attempt_id
    assert record.authorization_attempt_id == outcome.authorization_attempt_id
    assert record.action_id == "test_fixture.action" and record.action_type == foundation.EFFECT
    assert record.decision["verdict"] == "grant"
    assert record.policies == ({"policy_id": "test_fixture.policy", "policy_version": "1",
                                "verdict": "grant", "reason_codes": [],
                                "origins": [{"requirement_id": "test_fixture.requirement", "requirement_revision": "1",
                                            "layer_id": "test_fixture.layer", "source": None,
                                            "source_absence_reason": "Synthetic test fixture"}],
                                "profile": None},)
    assert record.consent == "owned_by_implementation"
    assert record.request_fingerprint == outcome.receipt.request_fingerprint
    assert record.fingerprint_policy_id == StrictJSONAuthorizationFingerprint.policy_id


def test_using_the_receipt_writes_no_further_record():
    harness = Harness()
    outcome = harness.issuer.authorize(foundation.request())
    harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
    assert harness.store.consumed == 1
    assert len(read_decision_records(harness.journal).decisions) == 1


@pytest.mark.parametrize("verdict", ["deny", "require_approval"])
def test_denials_and_approval_requirements_are_recorded(verdict):
    decision = {
        "deny": PolicyDecision.deny("test_fixture.policy", reason_codes=("test_fixture.denied",),
                                    conditions=(), review_obligations=()),
        "require_approval": PolicyDecision.require_approval(
            "test_fixture.policy",
            requirements=(ApprovalRequirement("test_fixture.approval", "test_fixture.approver"),),
            conditions=(), review_obligations=(),
        ),
    }[verdict]
    harness = Harness(decision=decision)
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.receipt is None
    (record,) = read_decision_records(harness.journal).decisions
    assert record.decision["verdict"] == verdict
    if verdict == "require_approval":
        assert record.decision["requirement_ids"] == ["test_fixture.approval"]


def test_the_record_carries_no_request_payload():
    harness = Harness()
    harness.issuer.authorize(foundation.request(payload={"value": "test_fixture.payload_secret"}))
    for entry in harness.journal.read().entries:
        assert b"test_fixture.payload_secret" not in entry.record.payload
        assert b"test_fixture.subject" not in entry.record.payload


def test_verified_conditions_are_recorded_by_identity_and_state():
    harness = Harness(verifier=True, decision=PolicyDecision.grant(
        "test_fixture.policy", conditions=(conditions.condition(),), review_obligations=(),
    ))
    harness.issuer.authorize(foundation.request())
    (record,) = read_decision_records(harness.journal).decisions
    assert record.conditions == ({"policy_id": "test_fixture.policy", "policy_version": "1",
                                  "condition_id": "test_fixture.condition", "state": "satisfied",
                                  "requirements": [{"layer_id": "test_fixture.layer",
                                                    "requirement_id": "test_fixture.requirement"}]},)


def test_each_attempt_writes_its_own_record():
    harness = Harness()
    first = harness.issuer.authorize(foundation.request(action_id="test_fixture.action.one"))
    second = harness.issuer.authorize(foundation.request(action_id="test_fixture.action.two"))
    records = read_decision_records(harness.journal).decisions
    assert [r.authorization_attempt_id for r in records] == [
        first.authorization_attempt_id, second.authorization_attempt_id,
    ]


def test_concurrent_attempts_each_get_a_confirmed_record():
    harness = Harness()
    barrier = Barrier(4)
    policy = harness.owner._registry[foundation.EFFECT]["test_fixture.policy"].policy
    original = policy.evaluate

    def evaluate(request, verified_approvals):
        barrier.wait(timeout=10)
        return original(request, verified_approvals)

    policy.evaluate = evaluate
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(
            lambda index: harness.issuer.authorize(foundation.request(action_id=f"test_fixture.action.{index}")),
            range(4),
        ))
    assert all(outcome.receipt is not None for outcome in outcomes)
    assert len(read_decision_records(harness.journal).decisions) == 4


# ── when the record is not confirmed ─────────────────────────────────────


def test_insufficient_durability_blocks_with_its_own_code():
    harness = Harness(governance=journal(required=DurabilityLevel.POWER_LOSS))
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert type(caught.value) is SETTAuthorizationConfigurationError
    assert caught.value.reason_code == "authorization.decision_record_unconfirmed"
    assert harness.store.issued == 0


class FailingStream(InMemoryEvidenceStream):
    def read(self, *, after=None):
        raise RuntimeError("test_fixture.storage_down")


def test_a_journal_that_cannot_be_read_blocks_the_attempt():
    stream = FailingStream(backend_id="backend.decisions", stream_id="decisions")
    harness = Harness(governance=journal(stream))
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.decision_record_unconfirmed"
    assert harness.store.issued == 0


def test_without_a_review_producer_nothing_is_written_and_the_attempt_stops():
    harness = Harness(review=False)
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert read_decision_records(harness.journal).decisions == ()


# ── recovery ─────────────────────────────────────────────────────────────


def test_recovery_counts_records_of_other_schemas_without_reading_them():
    harness = Harness()
    harness.issuer.authorize(foundation.request())
    head = harness.journal.read().observed_head
    foreign = GovernanceRecord(operation_id="test_fixture.other", schema_id="test_fixture.schema",
                               schema_version=1, payload=b"opaque")
    assert harness.journal.append(harness.journal.prepare(foreign, expected_head=head)).state == "confirmed"
    recovered = read_decision_records(harness.journal)
    assert len(recovered.decisions) == 1 and recovered.other_records == 1
    assert recovered.last_position == harness.journal.read().observed_head


def test_an_unreadable_decision_record_requires_reconciliation():
    governance = journal()
    head = governance.read().observed_head
    broken = GovernanceRecord(operation_id="decision.test_fixture", schema_id=DECISION_SCHEMA_ID,
                              schema_version=DECISION_SCHEMA_VERSION, payload=b"{}")
    governance.append(governance.prepare(broken, expected_head=head))
    recovered = read_decision_records(governance)
    assert recovered.decisions == ()
    assert recovered.requires_reconciliation
    assert recovered.diagnostics == ("decision_record_unreadable:0",)


def test_recovery_only_reads_a_governance_journal():
    with pytest.raises(TypeError):
        read_decision_records(object())


def test_recovered_decisions_grant_nothing():
    harness = Harness()
    outcome = harness.issuer.authorize(foundation.request())
    harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
    (record,) = read_decision_records(harness.journal).decisions
    assert record.decision["verdict"] == "grant"
    assert not hasattr(record, "receipt")
    with pytest.raises(SETTAuthorizationError):
        harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
