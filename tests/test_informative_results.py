"""Informative results of an attempt, kept in its decision record.

Informative policies never decide anything. Their results, or SETT's
diagnostic when they have none, are written with the attempt's decision
record and shown by its explanation. The four real state producers run with
a real governance journal.
"""

from datetime import datetime, timezone

import pytest
import test_policy_handoff as foundation
from test_policy_condition_producer import Verifier
from test_policy_origins import bound, origin
from test_review_restrictions import Reviewer, journal

from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRuler,
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicySelection,
    StrictJSONAuthorizationFingerprint,
)
from sett.policy_ruler import GovernanceRecord, PolicyDecision, PolicyRuler
from sett.policy_ruler.decision_record import DECISION_SCHEMA_ID, read_decision_records
from sett.policy_ruler.explanation import PublicValues, explain

EFFECT = foundation.EFFECT
APPLIES = PolicyApplicabilityState.APPLIES
REQUIRED = foundation.binding()


class Declaring:
    """Declares one required policy and the given informative entries."""

    resolver_id = "test_fixture.resolver"

    def __init__(self, informative):
        self.informative = informative

    def resolve(self, request):
        entries = [(REQUIRED, APPLIES), *self.informative]
        return PolicySelection(
            selection_id="test_fixture.selection", selection_revision="1", contract_version="2.0",
            authority_ref="test_fixture.authority", effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id, action_type=request.action_type,
            required=[REQUIRED], informative=[binding for binding, _state in self.informative],
            applicability=[
                PolicyApplicability(policy_id=binding.policy_id, policy_version=binding.policy_version,
                                    state=state, justification="Synthetic test declaration",
                                    fact_names=[], evidence_refs=[])
                for binding, state in entries
            ],
            facts={},
        )


class Failing:
    def __init__(self, policy_id):
        self.policy_id = policy_id

    def evaluate(self, request, verified_approvals):
        raise RuntimeError("test_fixture.informative_failure")


def harness(informative, policies):
    """A full configuration whose selection declares the given informative entries."""
    clock = foundation.FixedClock()
    evidence = foundation.InMemoryAuthorizationEvidence()
    governance = journal()
    owner = PolicyRuler(
        ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
        evaluation_ttl_seconds=60, max_live_evaluations=16, clock=clock, evidence_sink=evidence,
        selection_resolver=Declaring(informative), condition_verifiers=[Verifier(clock)],
        implementation_owned_consent=True, governance_journal=governance, review_verifiers=[Reviewer()],
    )
    issuer = AuthorizationRuler(
        issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=foundation.CountingStore(),
        evidence_sink=evidence, clock=clock, policy_ruler=owner,
    )
    issuer.register_policy(EFFECT, foundation.StaticPolicy(), binding=REQUIRED)
    for binding, policy in policies:
        issuer.register_policy(EFFECT, policy, binding=binding)
    outcome = issuer.authorize(foundation.request())
    (record,) = read_decision_records(governance).decisions
    return outcome, record


ADVISORY = bound("test_fixture.advisory", origin("test_fixture.guidance", "test_fixture.organization"))


def advisory_denial():
    return foundation.StaticPolicy("test_fixture.advisory", PolicyDecision.deny(
        "test_fixture.advisory", reason_codes=("test_fixture.advisory_reason",), conditions=(),
        review_obligations=(),
    ))


def test_an_informative_denial_beside_a_required_grant_leaves_the_grant():
    outcome, record = harness([(ADVISORY, APPLIES)], [(ADVISORY, advisory_denial())])
    assert outcome.receipt is not None and record.decision["verdict"] == "grant"
    assert record.informative == ({
        "policy_id": "test_fixture.advisory", "policy_version": "1", "verdict": "deny",
        "reason_codes": ["test_fixture.advisory_reason"], "diagnostics": [],
    },)


def test_informative_entries_without_a_result_keep_their_diagnostics():
    undetermined = bound("test_fixture.undetermined", origin("test_fixture.one", "test_fixture.layer"))
    missing = bound("test_fixture.missing", origin("test_fixture.two", "test_fixture.layer"))
    failing = bound("test_fixture.failing", origin("test_fixture.three", "test_fixture.layer"))
    outcome, record = harness(
        [(undetermined, PolicyApplicabilityState.UNDETERMINED), (missing, APPLIES), (failing, APPLIES)],
        [(undetermined, foundation.StaticPolicy("test_fixture.undetermined")),
         (failing, Failing("test_fixture.failing"))],
    )
    assert outcome.receipt is not None
    # Rows follow the selection, which keeps its bindings sorted by identity.
    assert [(row["policy_id"], row["verdict"], row["diagnostics"]) for row in record.informative] == [
        ("test_fixture.failing", None, ["authorization.policy_failed"]),
        ("test_fixture.missing", None, ["authorization.policy_missing"]),
        ("test_fixture.undetermined", None, ["authorization.policy_applicability_undetermined"]),
    ]
    assert "test_fixture.informative_failure" not in str(record.informative)


def test_an_informative_entry_that_does_not_apply_leaves_no_row():
    _outcome, record = harness([(ADVISORY, PolicyApplicabilityState.DOES_NOT_APPLY)], [])
    assert record.informative == ()


def test_the_explanation_shows_informative_rows_by_audience():
    _outcome, record = harness([(ADVISORY, APPLIES)], [(ADVISORY, advisory_denial())])
    explanation = explain(record)
    assert explanation.operational_view()["informative"] == [{
        "policy_id": "test_fixture.advisory", "policy_version": "1", "verdict": "deny",
        "reason_codes": ["test_fixture.advisory_reason"], "diagnostics": [],
    }]
    assert explanation.operational_view()["verdict"] == "grant"
    public = explanation.public_view()
    assert public["informative"] == [] and "test_fixture.advisory" not in str(public)
    shown = explanation.public_view(PublicValues(policy_ids={"test_fixture.advisory"}))
    assert shown["informative"] == [
        {"policy_id": "test_fixture.advisory", "verdict": "deny", "reason_codes": [], "diagnostics": []},
    ]
    approved = explanation.public_view(PublicValues(
        policy_ids={"test_fixture.advisory"}, reason_codes={"test_fixture.advisory_reason"},
    ))
    assert approved["informative"][0]["reason_codes"] == ["test_fixture.advisory_reason"]


@pytest.mark.parametrize("version", [2, 3])
def test_earlier_records_report_informative_results_as_not_recorded(version):
    governance = journal()
    payload = {
        "authorization_attempt_id": None, "action_id": "test_fixture.action", "action_type": EFFECT,
        "trace_id": "test_fixture.trace", "checkpoint_id": foundation.ENTRY,
        "selection": {"selection_id": "test_fixture.selection", "selection_revision": "1"},
        "policies": [], "decision": {"verdict": "grant"}, "conditions": [], "approvals": [], "consent": None,
        "request_fingerprint": None, "fingerprint_policy_id": None,
        "recorded_at": "2026-08-30T12:00:00.000000Z", "review_obligations": [], "review": [],
    }
    record = GovernanceRecord(
        operation_id=f"decision.test_fixture_v{version}", schema_id=DECISION_SCHEMA_ID,
        schema_version=version, payload=canonical_json_bytes(payload, field="test fixture"),
    )
    governance.append(governance.prepare(record, expected_head=governance.read().observed_head))
    (decoded,) = read_decision_records(governance).decisions
    assert decoded.informative is None
    assert explain(decoded).operational_view()["informative"] is None
    assert explain(decoded).public_view()["informative"] is None
