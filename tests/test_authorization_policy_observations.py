"""Same-call diagnostics; synthetic unrelated slots prove no governed effect."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
import test_policy_condition_producer as conditions
import test_policy_handoff as foundation

from sett.authorization_ruler import AuthorizationRuler, AuthorizationVerdict
from sett.exceptions import SETTAuthorizationConfigurationError, SETTAuthorizationError
from sett.policy_ruler import ConditionVerificationState, PolicyDecision


def blocked(issuer, request=None):
    with pytest.raises(SETTAuthorizationError) as caught:
        issuer.authorize(foundation.request() if request is None else request)
    assert caught.value.verdict == "block"
    assert caught.value.receipt_id is None
    return caught.value


def fail_policy(request, verified_approvals):
    raise RuntimeError("test_fixture.raw_callback_secret")


def add_policy(harness, identifier, version="1", callback=None):
    bound = foundation.binding(identifier, version)
    policy = foundation.StaticPolicy(identifier)
    if callback is not None:
        policy.evaluate = callback
    harness.issuer.register_policy(foundation.EFFECT, policy, binding=bound)
    harness.resolver.required.append(bound)
    return policy


def second_receiver(harness):
    return AuthorizationRuler(
        issuer_id="test_fixture.second_receiver", receipt_ttl_seconds=30,
        fingerprint_policy=foundation.StrictJSONAuthorizationFingerprint(),
        receipt_store=foundation.CountingStore(), evidence_sink=harness.evidence,
        clock=harness.clock, policy_ruler=harness.owner,
    )


@pytest.mark.parametrize("error_class", [SETTAuthorizationError, SETTAuthorizationConfigurationError])
def test_old_error_construction_adds_only_unavailable_snapshot(error_class):
    error = error_class("test_fixture.message", reason_code="test_fixture.code",
                        verdict="block", receipt_id="test_fixture.receipt")
    assert type(error) is error_class
    assert error.args == ("test_fixture.message",)
    assert str(error) == "test_fixture.message"
    assert error.reason_code == "test_fixture.code"
    assert error.verdict == "block"
    assert error.receipt_id == "test_fixture.receipt"
    assert error.policy_observations is None


def test_completed_denial_remains_an_outcome_without_receipt():
    harness = conditions.Harness([])
    harness.policy._decision = PolicyDecision.deny("test_fixture.policy", conditions=(), review_obligations=())
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.decision.verdict is AuthorizationVerdict.DENY
    assert outcome.receipt is None
    assert harness.store.issued == harness.store.consumed == 0


@pytest.mark.parametrize("verdict", ["deny", "grant"])
def test_admitted_result_survives_real_required_condition_failure(verdict):
    harness = conditions.Harness()
    factory = getattr(PolicyDecision, verdict)
    harness.policy._decision = factory("test_fixture.policy", conditions=(conditions.condition(),), review_obligations=())
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.condition_unsatisfied"
    assert error.policy_observations == (("test_fixture.policy", "1", verdict),)
    assert harness.policy.calls == len(harness.verifier.calls) == 1
    assert harness.store.issued == harness.store.consumed == 0


def test_failed_later_callback_reports_only_admitted_prefix():
    harness = foundation.Harness(decision=PolicyDecision.deny("test_fixture.policy", conditions=(), review_obligations=()))
    calls = []

    def fails(request, verified_approvals):
        calls.append("failed")
        return fail_policy(request, verified_approvals)

    add_policy(harness, "test_fixture.second", "2", fails)
    last = add_policy(harness, "test_fixture.zz_last", "3")
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_failed"
    assert error.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert calls == ["failed"]
    assert harness.policy.calls == 1
    assert last.calls == 0
    assert harness.store.issued == harness.store.consumed == harness.effects == 0


@pytest.mark.parametrize("fault,code", [
    ("callback", "authorization.policy_failed"),
    ("type", "authorization.policy_result_invalid"),
    ("identity", "authorization.policy_identity_mismatch"),
    ("condition", "authorization.policy_result_invalid"),
])
def test_first_result_not_admitted_produces_covered_empty_snapshot(fault, code):
    harness = conditions.Harness([])
    if fault == "callback":
        harness.policy.evaluate = fail_policy
    elif fault == "type":
        harness.policy._decision = object()
    elif fault == "identity":
        harness.policy._decision = PolicyDecision.grant("test_fixture.other", conditions=(), review_obligations=())
    else:
        harness.policy._decision = PolicyDecision.grant(
            "test_fixture.policy", conditions=(conditions.condition(checkpoint_id="test_fixture.other"),),
            review_obligations=(),
        )
    error = blocked(harness.issuer)
    assert error.reason_code == code
    assert error.policy_observations == ()
    assert harness.verifier.calls == []
    assert harness.store.issued == harness.store.consumed == 0


@pytest.mark.parametrize("missing", ["consent", "review", "durability"])
def test_missing_producer_keeps_admitted_prefix_and_original_diagnostic(missing):
    harness = conditions.Harness(missing=(missing,))
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_evaluation_incomplete"
    assert error.policy_observations == (("test_fixture.policy", "1", "grant"),)
    assert harness.verifier.calls == []
    assert harness.store.issued == 0


@pytest.mark.parametrize("fail_report", [False, True])
def test_late_clock_failure_and_final_failure_write_keep_precedence(fail_report):
    # The configured clock changes after the real callback. No private guard
    # is bypassed: synthetic unrelated slots isolate the diagnostic route.
    harness = foundation.Harness()
    original = harness.policy.evaluate

    def evaluate(request, verified_approvals):
        result = original(request, verified_approvals)
        harness.clock.current = None
        return result

    harness.policy.evaluate = evaluate
    events = []
    original_record = harness.evidence.record

    def record(event):
        events.append(event.kind)
        if fail_report and event.kind == "authorization.failed":
            raise RuntimeError("test_fixture.sink_secret")
        original_record(event)

    harness.evidence.record = record
    error = blocked(harness.issuer)
    expected = "authorization.evidence_failed" if fail_report else "authorization.clock_invalid"
    assert error.reason_code == expected
    assert error.policy_observations == (("test_fixture.policy", "1", "grant"),)
    assert events.count("authorization.policy_checked") == 1
    assert events.count("authorization.failed") == 1
    assert harness.policy.calls == 1
    assert harness.store.issued == 0


@pytest.mark.parametrize("structured", [False, True])
def test_checked_write_failure_retains_row_and_overwrites_forged_attribute(structured):
    harness = foundation.Harness()
    supplied = SETTAuthorizationConfigurationError(
        "test_fixture.structured", reason_code="authorization.evidence_failed",
    )
    supplied.policy_observations = (("test_fixture.forged", "9", "grant"),)
    kinds = []
    original = harness.evidence.record

    def record(event):
        kinds.append(event.kind)
        if event.kind == "authorization.policy_checked":
            if structured:
                raise supplied
            raise RuntimeError("test_fixture.sink_secret")
        original(event)

    harness.evidence.record = record
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.evidence_failed"
    assert type(error) is SETTAuthorizationConfigurationError
    assert error.policy_observations == (("test_fixture.policy", "1", "grant"),)
    if structured:
        assert error is supplied
    assert kinds == ["authorization.requested", "authorization.policy_checked"]
    assert harness.store.issued == 0


def test_informative_observation_is_not_in_governing_snapshot():
    harness = conditions.Harness(missing=("review",))
    informational = foundation.StaticPolicy("test_fixture.informative")
    bound = foundation.binding("test_fixture.informative", "2")
    harness.issuer.register_policy(foundation.EFFECT, informational, binding=bound)
    original = harness.resolver.resolve

    def resolve(request):
        selection = original(request)
        row = foundation.PolicyApplicability(
            policy_id=bound.policy_id, policy_version=bound.policy_version,
            state=foundation.PolicyApplicabilityState.APPLIES,
            justification="Synthetic informative selection", fact_names=[], evidence_refs=[],
        )
        return replace(selection, informative=(bound,), applicability=selection.applicability + (row,))

    harness.resolver.resolve = resolve
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_evaluation_incomplete"
    assert error.policy_observations == (("test_fixture.policy", "1", "grant"),)
    assert informational.calls == 1
    # This explicit input belongs to a new synthetic inspection, not recovery
    # of approvals or diagnostics retained by the previous bound invocation.
    evaluation = harness.owner.evaluate(
        foundation.request(), verified_approvals=frozenset(), checkpoint_id=foundation.ENTRY,
    )
    assert len(evaluation.informative_decisions) == 1
    assert informational.calls == 2
    assert error.policy_observations == (("test_fixture.policy", "1", "grant"),)


def test_repeated_identifiers_and_modified_error_do_not_supply_authority():
    harness = conditions.Harness()
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED] * 3
    first = blocked(harness.issuer)
    retained = first.policy_observations
    first.policy_observations = (("test_fixture.forged", "1", "grant"),)
    harness.policy._decision = PolicyDecision.deny("test_fixture.policy", conditions=(conditions.condition(),), review_obligations=())
    second = blocked(harness.issuer)
    assert second is not first
    assert retained == (("test_fixture.policy", "1", "grant"),)
    assert second.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert all(type(value) is str for row in retained for value in row)
    with pytest.raises(TypeError):
        retained[0] = ("test_fixture.forged", "2", "grant")
    assert blocked(harness.issuer).reason_code == "authorization.condition_unsatisfied"
    assert harness.store.issued == harness.store.consumed == 0


def test_shared_owner_overlapping_receivers_keep_snapshots_separate():
    harness = foundation.Harness()
    other = second_receiver(harness)
    entered = Event()
    release = Event()

    def evaluate(request, verified_approvals):
        if request.payload_copy()["side"] == "first":
            entered.set()
            assert release.wait(10), "Explicit overlap schedule did not release"
            return PolicyDecision.deny("test_fixture.policy", conditions=(), review_obligations=())
        return PolicyDecision.grant("test_fixture.policy", conditions=(), review_obligations=())

    harness.policy.evaluate = evaluate
    add_policy(harness, "test_fixture.zz_last", callback=fail_policy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(blocked, harness.issuer, foundation.request(payload={"side": "first"}))
        try:
            assert entered.wait(10), "Explicit overlap schedule did not enter"
            second = blocked(other, foundation.request(payload={"side": "second"}))
        finally:
            release.set()
        first = pending.result(timeout=10)
    assert first.reason_code == second.reason_code == "authorization.policy_failed"
    assert first.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert second.policy_observations == (("test_fixture.policy", "1", "grant"),)
    assert harness.store.issued == 0


def test_callback_reentrancy_keeps_inner_and_outer_snapshots_separate():
    harness = foundation.Harness()
    other = second_receiver(harness)
    inner_errors = []

    def evaluate(request, verified_approvals):
        if request.payload_copy()["side"] == "outer":
            inner_errors.append(blocked(other, foundation.request(payload={"side": "inner"})))
            return PolicyDecision.deny("test_fixture.policy", conditions=(), review_obligations=())
        return PolicyDecision.grant("test_fixture.policy", conditions=(), review_obligations=())

    harness.policy.evaluate = evaluate
    add_policy(harness, "test_fixture.zz_last", callback=fail_policy)
    outer = blocked(harness.issuer, foundation.request(payload={"side": "outer"}))
    assert len(inner_errors) == 1
    assert inner_errors[0].policy_observations == (("test_fixture.policy", "1", "grant"),)
    assert outer.policy_observations == (("test_fixture.policy", "1", "deny"),)
    assert outer.reason_code == inner_errors[0].reason_code == "authorization.policy_failed"


def test_raw_callback_data_does_not_enter_snapshot_or_automatic_output():
    harness = foundation.Harness()
    add_policy(harness, "test_fixture.zz_last", callback=fail_policy)
    error = blocked(harness.issuer)
    assert error.policy_observations == (("test_fixture.policy", "1", "grant"),)
    assert "test_fixture.raw_callback_secret" not in repr(error.policy_observations)
    assert "test_fixture.policy" not in str(error)
    assert "test_fixture.policy" not in repr(error)
    assert all("policy_observations" not in event.attributes for event in harness.evidence.events())
    # This tests the new field and automatic rendering, not every legacy traceback.
    assert harness.store.issued == 0


@pytest.mark.parametrize("signal_class", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_termination_signal_propagates_as_same_object(signal_class):
    harness = foundation.Harness()
    signal = signal_class("test_fixture.stop")

    def evaluate(request, verified_approvals):
        raise signal

    harness.policy.evaluate = evaluate
    with pytest.raises(signal_class) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value is signal
    assert harness.store.issued == 0


def test_binding_versions_order_and_base_strings_are_retained():
    class Identifier(str):
        def __str__(self):
            return "test_fixture.not_registered"

    harness = foundation.Harness()
    identifier = Identifier("test_fixture.second")
    identifier.extra = foundation.request()
    add_policy(harness, identifier, Identifier("2"))
    add_policy(harness, "test_fixture.third", "3")
    add_policy(harness, "test_fixture.zz_last", "4", fail_policy)
    error = blocked(harness.issuer)
    assert error.policy_observations == (
        ("test_fixture.policy", "1", "grant"),
        ("test_fixture.second", "2", "grant"),
        ("test_fixture.third", "3", "grant"),
    )
    assert all(type(value) is str for row in error.policy_observations for value in row)


def test_failure_before_invocation_has_unavailable_snapshot():
    harness = foundation.Harness()

    def record(event):
        raise RuntimeError("test_fixture.early_sink")

    harness.evidence.record = record
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.evidence_failed"
    assert error.policy_observations is None
    assert harness.policy.calls == 0


def test_later_issue_failure_does_not_claim_initial_snapshot_coverage():
    harness = conditions.Harness()
    harness.verifier.states = [ConditionVerificationState.SATISFIED, ConditionVerificationState.UNSATISFIED]
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_evidence_changed"
    assert error.policy_observations is None
    assert harness.store.issued == 0


def test_admissible_long_prefix_has_no_new_truncation_or_callback_retries():
    # Twenty-four admitted rows are a concrete observation, not proof that
    # every policy-count/aggregate-byte maximum can be reached simultaneously.
    harness = foundation.Harness()
    policies = [harness.policy]
    expected = [("test_fixture.policy", "1", "grant")]
    for number in range(1, 24):
        identifier = f"test_fixture.q{number:02d}"
        version = str(number + 1)
        policies.append(add_policy(harness, identifier, version))
        expected.append((identifier, version, "grant"))
    add_policy(harness, "test_fixture.zz_last", callback=fail_policy)
    resolutions = []
    original = harness.resolver.resolve

    def resolve(request):
        resolutions.append(request.action_id)
        return original(request)

    harness.resolver.resolve = resolve
    error = blocked(harness.issuer)
    assert error.reason_code == "authorization.policy_failed"
    assert error.policy_observations == tuple(expected)
    assert [policy.calls for policy in policies] == [1] * 24
    assert resolutions == ["test_fixture.action"]
    assert harness.store.issued == 0
