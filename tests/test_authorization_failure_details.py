"""Authorization failure details: reasons, failed condition, attempt identifier, original code.

Synthetic consent, review and durability slots stand in for the state
verifiers that do not exist yet, as in the other condition tests. They let a
governed attempt reach condition verification; they prove no real
integration and no governed effect.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
import test_policy_condition_producer as conditions
import test_policy_handoff as foundation

from sett.authorization_ruler import (
    ApprovalRequirement,
    AuthorizationOutcome,
    AuthorizationRequest,
    AuthorizationVerdict,
    ExecutionMode,
    FieldTrust,
    FieldValue,
)
from sett.exceptions import SETTAuthorizationConfigurationError, SETTAuthorizationError
from sett.policy_ruler import ConditionVerificationState, PolicyDecision

UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
NEW_FIELDS = (
    "policy_observation_reason_codes",
    "failed_condition",
    "attempted_conditions",
    "authorization_attempt_id",
    "original_reason_code",
)


def blocked(issuer, request=None):
    with pytest.raises(SETTAuthorizationError) as caught:
        issuer.authorize(foundation.request() if request is None else request)
    return caught.value


def proposed_request():
    """A request whose principal is only proposed, not authoritative."""
    return AuthorizationRequest(
        action_id="test_fixture.action", trace_id="test_fixture.trace", action_type=foundation.EFFECT,
        payload={"value": 1}, execution_mode=ExecutionMode.PLAIN,
        principal=FieldValue("test_fixture.subject", source_id="test_fixture.source", trust=FieldTrust.PROPOSED),
    )


def identity(condition_id):
    return ("test_fixture.policy", "1", condition_id)


def attempt_ids(evidence):
    return [event.attributes.get("authorization_attempt_id") for event in evidence.events()]


def fail_on(harness, kind):
    original = harness.evidence.record

    def record(event):
        if event.kind == kind:
            raise RuntimeError("test_fixture.sink_secret")
        original(event)

    harness.evidence.record = record


def deny(*declared, reason_codes=("test_fixture.denied",)):
    return PolicyDecision.deny(
        "test_fixture.policy", reason_codes=reason_codes, conditions=declared, review_obligations=(),
    )


def add_policy(harness, identifier, decision):
    bound = foundation.binding(identifier, "2")
    harness.issuer.register_policy(
        foundation.EFFECT, foundation.StaticPolicy(identifier, decision), binding=bound,
    )
    harness.resolver.required.append(bound)


def granted(harness):
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.decision.verdict is AuthorizationVerdict.GRANT
    assert outcome.receipt is not None
    return outcome


# ── construction and text output ─────────────────────────────────────────


@pytest.mark.parametrize("error_class", [SETTAuthorizationError, SETTAuthorizationConfigurationError])
def test_new_attributes_default_to_none_and_stay_out_of_text(error_class):
    error = error_class("test_fixture.message", reason_code="test_fixture.code")
    assert all(getattr(error, name) is None for name in NEW_FIELDS)
    assert error.policy_observations is None
    assert error.args == ("test_fixture.message",)
    assert str(error) == "test_fixture.message"


def test_attempt_fields_are_not_added_to_str_or_repr():
    harness = conditions.Harness()
    harness.policy._decision = deny(conditions.condition())
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    error = blocked(harness.issuer)
    rendered = str(error) + repr(error)
    assert error.authorization_attempt_id not in rendered
    assert "test_fixture.condition" not in rendered
    assert "test_fixture.denied" not in rendered


# ── 1. a completed denial with all required state verified ──────────────


def test_completed_denial_is_an_outcome_with_its_attempt_id():
    harness = conditions.Harness()
    harness.policy._decision = deny(conditions.condition())
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.decision.verdict is AuthorizationVerdict.DENY
    assert outcome.receipt is None
    assert len(harness.verifier.calls) == 1
    assert UUID4.match(outcome.authorization_attempt_id)
    assert set(attempt_ids(harness.evidence)) == {outcome.authorization_attempt_id}


# ── 2. a denial together with a failed required condition ───────────────


def test_denial_with_failed_condition_reports_reasons_and_condition():
    harness = conditions.Harness()
    harness.policy._decision = deny(conditions.condition())
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.condition_unsatisfied"
    assert error.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert error.policy_observation_reason_codes == (("test_fixture.denied",),)
    assert error.failed_condition == identity("test_fixture.condition")
    assert error.attempted_conditions == (identity("test_fixture.condition"),)
    assert error.original_reason_code is None
    assert UUID4.match(error.authorization_attempt_id)


# ── 3. a failure before any policy returned a result ────────────────────


def test_failure_before_policy_evaluation_carries_only_the_attempt_id():
    harness = conditions.Harness()
    proposed = proposed_request()
    error = blocked(harness.issuer, proposed)
    assert error.reason_code == "authorization.provenance_not_authoritative"
    assert error.policy_observations is None
    assert error.policy_observation_reason_codes is None
    assert error.failed_condition is None and error.attempted_conditions is None
    assert error.original_reason_code is None
    assert UUID4.match(error.authorization_attempt_id)
    assert [event.kind for event in harness.evidence.events()] == [
        "authorization.requested", "authorization.failed",
    ]
    assert set(attempt_ids(harness.evidence)) == {error.authorization_attempt_id}
    assert harness.policy.calls == 0


# ── 4. a denial followed by a missing verifier, a clock or a malformed result ─


def test_denial_then_missing_state_verifier_has_no_failed_condition():
    harness = conditions.Harness(missing=("review",))
    harness.policy._decision = deny(conditions.condition())
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_evaluation_incomplete"
    assert error.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert error.policy_observation_reason_codes == (("test_fixture.denied",),)
    assert error.failed_condition is None and error.attempted_conditions is None
    assert harness.verifier.calls == []


def test_denial_then_clock_failure_before_the_verifier_call():
    harness = conditions.Harness()
    original = harness.policy.evaluate

    def evaluate(request, verified_approvals):
        result = original(request, verified_approvals)
        harness.clock.current = None
        return result

    harness.policy._decision = deny(conditions.condition())
    harness.policy.evaluate = evaluate
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.clock_invalid"
    assert error.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert error.failed_condition == identity("test_fixture.condition")
    assert error.attempted_conditions == ()
    assert harness.verifier.calls == []


def test_denial_then_clock_failure_after_the_verifier_call():
    harness = conditions.Harness()
    harness.policy._decision = deny(conditions.condition())

    def break_clock():
        harness.clock.current = None

    harness.verifier.on_verify = break_clock
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.clock_invalid"
    assert error.failed_condition == identity("test_fixture.condition")
    assert error.attempted_conditions == (identity("test_fixture.condition"),)
    assert len(harness.verifier.calls) == 1


def test_denial_then_malformed_condition_result():
    harness = conditions.Harness()
    harness.policy._decision = deny(conditions.condition())
    harness.verifier.result = object()
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.condition_result_invalid"
    assert error.policy_observation_reason_codes == (("test_fixture.denied",),)
    assert error.failed_condition == identity("test_fixture.condition")
    assert error.attempted_conditions == (identity("test_fixture.condition"),)


# ── conditions: missing verifiers, order and verifier failures ──────────


@pytest.mark.parametrize("absent", ["test_fixture.a_condition", "test_fixture.b_condition"])
def test_missing_verifier_reports_first_missing_condition_and_none_attempted(absent):
    harness = conditions.Harness()
    declared = tuple(
        conditions.condition(
            name, verifier_id="test_fixture.absent" if name >= absent else "test_fixture.verifier",
        )
        for name in ("test_fixture.a_condition", "test_fixture.b_condition")
    )
    harness.policy._decision = PolicyDecision.grant(
        "test_fixture.policy", conditions=declared, review_obligations=(),
    )
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.condition_verifier_missing"
    assert error.failed_condition == identity(absent)
    assert error.attempted_conditions == ()
    assert harness.verifier.calls == []


def test_conditions_before_the_failing_one_were_satisfied():
    harness = conditions.Harness()
    names = ("test_fixture.a_condition", "test_fixture.b_condition", "test_fixture.c_condition")
    harness.policy._decision = PolicyDecision.grant(
        "test_fixture.policy", conditions=tuple(conditions.condition(name) for name in names),
        review_obligations=(),
    )
    harness.verifier.states = [ConditionVerificationState.SATISFIED, ConditionVerificationState.EVIDENCE_INVALID]
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.condition_evidence_invalid"
    assert error.failed_condition == identity("test_fixture.b_condition")
    assert error.attempted_conditions == (identity("test_fixture.a_condition"), identity("test_fixture.b_condition"))
    assert [called.condition_id for _request, called in harness.verifier.calls] == list(names[:2])


def test_verifier_failure_reports_its_condition():
    harness = conditions.Harness()
    harness.verifier.error = RuntimeError("test_fixture.verifier_secret")
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.condition_verifier_failed"
    assert error.failed_condition == identity("test_fixture.condition")
    assert error.attempted_conditions == (identity("test_fixture.condition"),)
    assert "test_fixture.verifier_secret" not in repr(error.attempted_conditions)


def test_failure_without_a_failing_condition_leaves_condition_fields_none():
    harness = conditions.Harness([])
    harness.policy.evaluate = lambda request, verified_approvals: object()
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_result_invalid"
    assert error.policy_observations == ()
    assert error.policy_observation_reason_codes == ()
    assert error.failed_condition is None and error.attempted_conditions is None


# ── reasons for observed policy decisions ────────────────────────────────


def test_reason_codes_align_with_observation_rows():
    harness = conditions.Harness(missing=("review",))
    harness.policy._decision = deny(reason_codes=("test_fixture.first", "test_fixture.second"))
    add_policy(harness, "test_fixture.zz_other", PolicyDecision.grant(
        "test_fixture.zz_other", conditions=(), review_obligations=(),
    ))
    error = blocked(harness.issuer)
    assert error.policy_observations == (
        ("test_fixture.policy", "1", "deny"), ("test_fixture.zz_other", "2", "grant"),
    )
    assert error.policy_observation_reason_codes == (("test_fixture.first", "test_fixture.second"), ())
    assert all(type(code) is str for codes in error.policy_observation_reason_codes for code in codes)


def test_reason_codes_match_the_policy_checked_evidence():
    harness = conditions.Harness(missing=("durability",))
    harness.policy._decision = deny(reason_codes=("test_fixture.first",))
    error = blocked(harness.issuer)
    checked = [event for event in harness.evidence.events() if event.kind == "authorization.policy_checked"]
    assert [event.reason_codes for event in checked] == list(error.policy_observation_reason_codes)


# ── 5. a failure of the failure report itself ───────────────────────────


def test_failed_report_keeps_the_original_code():
    harness = conditions.Harness()
    proposed = proposed_request()
    fail_on(harness, "authorization.failed")
    error = blocked(harness.issuer, proposed)
    assert type(error) is SETTAuthorizationConfigurationError
    assert error.reason_code == "authorization.evidence_failed"
    assert error.original_reason_code == "authorization.provenance_not_authoritative"
    assert UUID4.match(error.authorization_attempt_id)


def test_failed_report_in_the_policy_layer_keeps_the_original_code():
    harness = conditions.Harness([])

    def fails(request, verified_approvals):
        raise RuntimeError("test_fixture.callback_secret")

    harness.policy.evaluate = fails
    fail_on(harness, "authorization.failed")
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.evidence_failed"
    assert error.original_reason_code == "authorization.policy_failed"
    assert error.policy_observations == ()


def test_failed_condition_report_keeps_the_original_code_and_condition():
    harness = conditions.Harness()
    harness.policy._decision = deny(conditions.condition())
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    fail_on(harness, "authorization.failed")
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.evidence_failed"
    assert error.original_reason_code == "authorization.condition_unsatisfied"
    assert error.failed_condition == identity("test_fixture.condition")
    assert error.policy_observation_reason_codes == (("test_fixture.denied",),)


@pytest.mark.parametrize("kind", ["authorization.requested", "authorization.policy_checked"])
def test_other_evidence_failures_have_no_original_code(kind):
    harness = conditions.Harness(missing=("review",))
    fail_on(harness, kind)
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.evidence_failed"
    assert error.original_reason_code is None
    assert UUID4.match(error.authorization_attempt_id)


def test_failed_receipt_rejection_report_keeps_the_original_code():
    reference = foundation.Harness()
    receipt = granted(reference).receipt
    reference.issuer.verify_and_consume(receipt, foundation.request())
    with pytest.raises(SETTAuthorizationError) as expected:
        reference.issuer.verify_and_consume(receipt, foundation.request())

    harness = foundation.Harness()
    receipt = granted(harness).receipt
    harness.issuer.verify_and_consume(receipt, foundation.request())
    fail_on(harness, "authorization.receipt_rejected")
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.verify_and_consume(receipt, foundation.request())
    assert caught.value.reason_code == "authorization.evidence_failed"
    assert caught.value.original_reason_code == expected.value.reason_code
    assert expected.value.original_reason_code is None


# ── the attempt identifier ───────────────────────────────────────────────


def test_attempt_id_is_on_the_error_and_on_every_event_of_the_attempt():
    harness = conditions.Harness(missing=("review",))
    error = blocked(harness.issuer)
    kinds = [event.kind for event in harness.evidence.events()]
    assert kinds == ["authorization.requested", "authorization.policy_checked", "authorization.failed"]
    assert attempt_ids(harness.evidence) == [error.authorization_attempt_id] * 3


def test_attempt_ids_differ_between_attempts_and_are_not_derived_from_the_request():
    harness = foundation.Harness(producers=(), fingerprint=foundation.ConstantFingerprint())
    first = blocked(harness.issuer)
    second = blocked(harness.issuer)
    assert first.authorization_attempt_id != second.authorization_attempt_id
    for error in (first, second):
        assert UUID(error.authorization_attempt_id).version == 4
        assert "test_fixture" not in error.authorization_attempt_id
    assert set(attempt_ids(harness.evidence)) == {first.authorization_attempt_id, second.authorization_attempt_id}


@pytest.mark.parametrize("verdict", ["deny", "require_approval", "grant"])
def test_every_outcome_returned_by_authorize_carries_its_attempt_id(verdict):
    decision = {
        "deny": PolicyDecision.deny("test_fixture.policy", conditions=(), review_obligations=()),
        "require_approval": PolicyDecision.require_approval(
            "test_fixture.policy",
            requirements=(ApprovalRequirement("test_fixture.approval", "test_fixture.approver"),),
            conditions=(), review_obligations=(),
        ),
        "grant": None,
    }[verdict]
    harness = foundation.Harness(decision=decision)
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.decision.verdict.value == verdict
    assert UUID4.match(outcome.authorization_attempt_id)
    assert set(attempt_ids(harness.evidence)) == {outcome.authorization_attempt_id}


def test_each_verify_and_consume_call_is_an_attempt_of_its_own():
    harness = foundation.Harness()
    outcome = granted(harness)
    issued = len(harness.evidence.events())
    harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
    consumed = attempt_ids(harness.evidence)[issued:]
    assert consumed and len(set(consumed)) == 1
    assert UUID4.match(consumed[0])
    assert consumed[0] != outcome.authorization_attempt_id


def test_a_rejected_receipt_use_carries_its_own_attempt_id():
    harness = foundation.Harness()
    outcome = granted(harness)
    harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
    before = len(harness.evidence.events())
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
    rejected = attempt_ids(harness.evidence)[before:]
    assert rejected == [caught.value.authorization_attempt_id] * len(rejected)
    assert caught.value.authorization_attempt_id != outcome.authorization_attempt_id


def test_an_outcome_built_by_application_code_has_no_attempt_id():
    harness = foundation.Harness(decision=PolicyDecision.deny(
        "test_fixture.policy", conditions=(), review_obligations=(),
    ))
    returned = harness.issuer.authorize(foundation.request())
    built = AuthorizationOutcome(returned.decision)
    assert built.authorization_attempt_id is None
    assert built == returned and hash(built) == hash(returned)
    assert returned.authorization_attempt_id not in repr(returned)
    with pytest.raises(TypeError):
        AuthorizationOutcome(returned.decision, authorization_attempt_id="test_fixture.forged")


def test_events_outside_an_attempt_carry_no_attempt_id():
    harness = conditions.Harness(missing=("review",))
    blocked(harness.issuer)
    before = len(harness.evidence.events())
    harness.owner.evaluate(foundation.request(), verified_approvals=frozenset(), checkpoint_id=foundation.ENTRY)
    inspection = harness.evidence.events()[before:]
    assert inspection
    assert all("authorization_attempt_id" not in event.attributes for event in inspection)


def test_concurrent_attempts_keep_their_own_identifiers():
    harness = conditions.Harness(missing=("review",))
    barrier = Barrier(4)
    original = harness.policy.evaluate

    def evaluate(request, verified_approvals):
        barrier.wait(timeout=10)
        return original(request, verified_approvals)

    harness.policy.evaluate = evaluate
    with ThreadPoolExecutor(max_workers=4) as pool:
        errors = list(pool.map(lambda _index: blocked(harness.issuer), range(4)))
    identifiers = [error.authorization_attempt_id for error in errors]
    assert len(set(identifiers)) == 4
    recorded = attempt_ids(harness.evidence)
    assert sorted(recorded) == sorted(identifiers * 3)
