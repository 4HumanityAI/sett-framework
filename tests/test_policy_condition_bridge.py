"""Independent inventory checks; synthetic other slots never prove integration."""

import json
from dataclasses import replace

import pytest
import test_policy_handoff as foundation
from test_policy_condition_producer import Harness, Verifier, condition

from sett._governance.validation import canonical_json_bytes
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    SETTPolicyConfigurationError,
)
from sett.policy_ruler import (
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyDecision,
    _handoff,
)
from sett.policy_ruler._condition_state import _admit_condition_verifiers


@pytest.mark.parametrize("missing", ["consent", "review", "durability"])
def test_presence_precheck_stops_state_calls_after_policy_evaluation(missing):
    harness = Harness(missing=(missing,))
    calls = []
    for producer in harness.producers.values():
        original = producer.produce
        def counted(context, *, original=original):
            calls.append(context)
            return original(context)
        producer.produce = counted
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert harness.policy.calls == 1
    assert calls == []
    assert harness.verifier.calls == []
    assert harness.store.issued == 0


def test_none_producer_is_not_explicit_empty():
    harness = Harness([], configured=False)
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert harness.verifier.calls == []


def test_complete_explicit_empty_inventory_can_produce_empty_slot():
    harness = Harness([])
    _invocation, _evaluation, record = harness.evaluate()
    slot = record.state["conditions"]
    assert slot.required_keys == slot.observed_keys == ()
    assert json.loads(slot.value_bytes) == {"inventory": [], "observations": {}}
    assert harness.verifier.calls == []


def test_incomplete_coverage_cannot_make_empty_success():
    """Private fault injection isolates coverage from earlier engine rejection."""
    harness = Harness([])
    _invocation, _evaluation, record = harness.evaluate()
    context = harness.owner._condition_context(record, frozenset(), 0, "initial")
    row = json.loads(context.condition_coverage[0])
    row["status"] = "unavailable"
    context = replace(context, condition_proof=_handoff.ConditionProof(), retained_condition_slot=None,
                      condition_coverage=(canonical_json_bytes(row, field="test fixture"),))
    result = harness.owner._condition_producer.produce(context)
    assert type(result) is _handoff.ConditionRejection
    assert result.code == "policy.evaluation_incomplete"


@pytest.mark.parametrize("fault", ["missing_key", "substituted_inventory", "missing_inventory"])
def test_ruler_independent_comparison_rejects_self_consistent_wrong_slot(fault):
    harness = Harness()
    producer = harness.owner._condition_producer
    original = producer.produce
    def changed(context):
        slot = original(context)
        if fault == "missing_key":
            return replace(slot, required_keys=(), observed_keys=())
        body = json.loads(slot.value_bytes)
        if fault == "substituted_inventory":
            body["inventory"][0]["condition"]["condition_id"] = "test_fixture.other"
        else:
            body.pop("inventory")
        return replace(slot, value_bytes=canonical_json_bytes(body, field="test fixture"))
    producer.produce = changed
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert harness.store.issued == 0


@pytest.mark.parametrize("change", [
    {"action_id": "test_fixture.other"},
    {"action_type": "test_fixture.other"},
    {"checkpoint_id": "test_fixture.later"},
    {"binding": foundation.binding(version="2"), "origins": foundation.binding(version="2").origins},
])
def test_required_declaration_is_rejected_instead_of_filtered(change):
    harness = Harness([condition(**change)])
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_result_invalid"
    assert harness.verifier.calls == []
    assert harness.store.issued == 0


def test_optional_condition_produces_no_observation():
    harness = Harness([condition(required=False, verifier_id="test_fixture.missing")])
    _invocation, _evaluation, record = harness.evaluate()
    assert record.state["conditions"].observed_keys == ()
    assert harness.verifier.calls == []
    assert json.loads(record.condition_informational)[0]["condition"]["required"] is False


def install_informative(harness, declaration):
    bound = declaration.binding
    resolver = harness.resolver
    original = resolver.resolve
    def resolve(request):
        selected = original(request)
        return replace(selected, informative=[bound], applicability=[*selected.applicability,
            PolicyApplicability(policy_id=bound.policy_id, policy_version=bound.policy_version,
                state=PolicyApplicabilityState.APPLIES, justification="test_fixture.declared",
                fact_names=[], evidence_refs=[])])
    resolver.resolve = resolve
    policy = foundation.StaticPolicy(bound.policy_id, PolicyDecision.grant(bound.policy_id, conditions=[declaration], review_obligations=()))
    harness.owner.register_policy(foundation.EFFECT, policy, binding=bound)
    return policy


def test_informative_required_flag_cannot_gain_authority():
    harness = Harness([])
    bound = foundation.binding("test_fixture.informative")
    install_informative(harness, condition(bound=bound, verifier_id="test_fixture.missing"))
    _invocation, _evaluation, record = harness.evaluate()
    assert record.state["conditions"].required_keys == ()
    assert harness.verifier.calls == []


def test_inspection_pairs_are_from_new_evaluation_not_bound_recovery():
    harness = Harness([])
    bound = foundation.binding("test_fixture.informative")
    policy = install_informative(harness, condition(bound=bound, checkpoint_id="test_fixture.later"))
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.receipt is not None
    assert not hasattr(outcome, "informative_diagnostics")
    before = policy.calls
    # This fresh synthetic inspection explicitly declares an empty approval input.
    # It does not recover, derive, or assert the approvals or diagnostics
    # of the preceding bound invocation.
    inspected = harness.owner.evaluate(
        foundation.request(), verified_approvals=frozenset(), checkpoint_id=foundation.ENTRY,
    )
    assert policy.calls == before + 1
    assert (bound.policy_id, "authorization.policy_result_invalid") in inspected.informative_diagnostics


def test_equal_condition_ids_keep_separate_origins_and_calls():
    harness = Harness()
    bound = foundation.binding("test_fixture.second_policy")
    harness.resolver.required.append(bound)
    second = condition(bound=bound)
    harness.owner.register_policy(foundation.EFFECT,
        foundation.StaticPolicy(bound.policy_id, PolicyDecision.grant(bound.policy_id, conditions=[second], review_obligations=())), binding=bound)
    _invocation, _evaluation, record = harness.evaluate()
    assert record.state["conditions"].required_keys == ("condition.000001", "condition.000002")
    assert len(harness.verifier.calls) == 2
    assert len({item.binding.policy_id for _request, item in harness.verifier.calls}) == 2


def test_governing_aggregate_bound_precedes_producer_calls():
    harness = Harness([])
    for index in range(4):
        bound = foundation.binding(f"test_fixture.large.{index}")
        harness.resolver.required.append(bound)
        declarations = [condition(f"test_fixture.condition.{j:03}", bound=bound) for j in range(30)]
        harness.owner.register_policy(foundation.EFFECT,
            foundation.StaticPolicy(bound.policy_id, PolicyDecision.grant(bound.policy_id, conditions=declarations, review_obligations=())), binding=bound)
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_result_invalid"
    assert harness.verifier.calls == []
    assert harness.store.issued == 0


@pytest.mark.parametrize("suffix,configuration", [
    ("condition_verifier_missing", True), ("condition_verifier_failed", True),
    ("condition_result_invalid", True), ("condition_unsatisfied", False),
    ("condition_evidence_not_present", False), ("condition_evidence_invalid", False),
    ("condition_evidence_unavailable", False), ("condition_inventory_invalid", True),
])
def test_every_explicit_translation_and_class(suffix, configuration):
    harness = Harness()
    def fail():
        raise _handoff.policy_error("policy." + suffix)
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer._translate(fail)
    expected = "policy_result_invalid" if suffix == "condition_inventory_invalid" else suffix
    assert caught.value.reason_code == "authorization." + expected
    assert isinstance(caught.value, SETTAuthorizationConfigurationError) is configuration


@pytest.mark.parametrize("values", ["test_fixture.not_sequence", {}, [object()], [None] * 257])
def test_constructor_admission_rejects_invalid_sequences(values):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        _admit_condition_verifiers(values)
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_constructor_registry_is_frozen_and_duplicate_pairs_reject():
    verifier = Verifier(foundation.FixedClock())
    values = [verifier]
    registry = _admit_condition_verifiers(values)
    values.clear()
    assert registry[(verifier.verifier_id, verifier.verifier_version)] is verifier
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        _admit_condition_verifiers([verifier, verifier])
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


@pytest.mark.parametrize("fault", ["extra", "duplicate"])
def test_evidence_key_admission_layers(fault):
    harness = Harness()
    _invocation, _evaluation, record = harness.evaluate()
    context = harness.owner._condition_context(record, frozenset(), 0, "initial")
    slot = record.state["conditions"]
    keys = (*slot.required_keys, "condition.999999" if fault == "extra" else slot.required_keys[0])
    wrong = replace(slot, required_keys=keys, observed_keys=keys)
    assert _handoff.validated_slot(wrong, context) is (fault == "extra")
    assert not _handoff.condition_slot_matches(wrong, context)
    assert _handoff.validated_slot(slot, context)
    assert _handoff.condition_slot_matches(slot, context)


def test_evidence_independent_boundary_rejects_self_consistent_omission():
    """Private boundary case: a later proof check cannot mask removed condition keys."""
    harness = Harness()
    _invocation, _evaluation, record = harness.evaluate()
    context = harness.owner._condition_context(record, frozenset(), 0, "initial")
    slot = record.state["conditions"]
    wrong = replace(slot, required_keys=(), observed_keys=())
    assert _handoff.validated_slot(wrong, context)
    assert not _handoff.condition_slot_matches(wrong, context)
    assert _handoff.condition_slot_matches(slot, context)


def test_evidence_synthetic_empty_keys_cannot_fill_real_inventory():
    harness = Harness()
    _invocation, _evaluation, record = harness.evaluate()
    context = harness.owner._condition_context(record, frozenset(), 0, "initial")
    fixture = foundation.SyntheticStateProducer("conditions")
    synthetic = fixture.produce(context)
    assert synthetic.required_keys == synthetic.observed_keys == ()
    assert not _handoff.condition_slot_matches(synthetic, context)
    empty = Harness([])
    _invocation, _evaluation, empty_record = empty.evaluate()
    empty_context = empty.owner._condition_context(empty_record, frozenset(), 0, "initial")
    assert _handoff.condition_slot_matches(fixture.produce(empty_context), empty_context)


@pytest.mark.parametrize("change", [
    {"action_id": "test_fixture.other"},
    {"action_type": "test_fixture.other"},
    {"checkpoint_id": "test_fixture.later"},
    {"binding": foundation.binding("test_fixture.informative", version="2"),
     "origins": foundation.binding("test_fixture.informative", version="2").origins},
])
def test_evidence_informative_mismatch_has_only_diagnostic(change):
    harness = Harness([])
    bound = foundation.binding("test_fixture.informative")
    policy = install_informative(harness, condition(bound=bound))
    policy._decision = PolicyDecision.grant(bound.policy_id, conditions=[replace(condition(bound=bound), **change)], review_obligations=())
    result = harness.owner.evaluate(foundation.request(), verified_approvals=frozenset(), checkpoint_id=foundation.ENTRY)
    assert (bound.policy_id, "authorization.policy_result_invalid") in result.informative_diagnostics
    assert result.informative_decisions == ()
    assert harness.issuer.authorize(foundation.request()).receipt is not None
    assert harness.verifier.calls == []


def test_evidence_collision_order_is_complete_and_deterministic():
    harness = Harness([])
    expected = []
    for suffix in ["z", "a"]:
        bound = foundation.binding("test_fixture.policy." + suffix)
        harness.resolver.required.append(bound)
        declared = condition("test_fixture.same", bound=bound)
        harness.owner.register_policy(foundation.EFFECT,
            foundation.StaticPolicy(bound.policy_id, PolicyDecision.grant(bound.policy_id, conditions=[declared], review_obligations=())), binding=bound)
        expected.append((bound.policy_id, bound.policy_version, declared.condition_id))
    _invocation, _evaluation, record = harness.evaluate()
    assert [(item.binding.policy_id, item.binding.policy_version, item.condition_id)
            for _request, item in harness.verifier.calls] == sorted(expected)
    assert [key for key, _condition, _coverage in record.condition_entries] == ["condition.000001", "condition.000002"]


def test_evidence_informational_overflow_does_not_consume_governing_budget():
    harness = Harness()
    informative = []
    applicability = []
    base_resolve = harness.resolver.resolve
    for index in range(4):
        bound = foundation.binding(f"test_fixture.info.{index}")
        informative.append(bound)
        applicability.append(PolicyApplicability(
            policy_id=bound.policy_id, policy_version=bound.policy_version,
            state=PolicyApplicabilityState.APPLIES,
            justification="test_fixture.declared", fact_names=[], evidence_refs=[],
        ))
        declarations = [condition(f"test_fixture.condition.{item:03}", bound=bound) for item in range(30)]
        policy = foundation.StaticPolicy(
            bound.policy_id, PolicyDecision.grant(bound.policy_id, conditions=declarations, review_obligations=()),
        )
        harness.owner.register_policy(foundation.EFFECT, policy, binding=bound)

    def resolve(request):
        selected = base_resolve(request)
        return replace(selected, informative=informative,
                       applicability=[*selected.applicability, *applicability])

    harness.resolver.resolve = resolve
    invocation, evaluation, record = harness.evaluate()
    assert record.condition_informational is None
    assert len(record.state["conditions"].required_keys) == 1
    assert len(harness.verifier.calls) == 1
    assert set(evaluation.informative_diagnostics) == {
        (f"test_fixture.info.{index}", "policy.condition_inventory_invalid") for index in range(4)
    }
    assert harness.issuer._issue(invocation, evaluation, invocation.request, frozenset(), evaluation.decision) is not None


@pytest.mark.parametrize("size", [65536, 65537])
def test_evidence_slot_size_exact_boundary(size):
    """Generic slot size boundary, isolated from JSON/inventory admission."""
    harness = Harness([])
    _invocation, _evaluation, record = harness.evaluate()
    context = harness.owner._condition_context(record, frozenset(), 0, "initial")
    slot = replace(record.state["conditions"], value_bytes=b"x" * size)
    assert _handoff.validated_slot(slot, context) is (size == 65536)


@pytest.mark.parametrize("count", [256, 257])
def test_evidence_reference_union_has_its_own_bound(count):
    from sett.policy_ruler import ConditionVerification, ConditionVerificationState
    harness = Harness([condition("test_fixture.a"), condition("test_fixture.b")])
    def verify(request, *, condition):
        harness.verifier.calls.append((request, condition))
        indices = range(128) if condition.condition_id.endswith(".a") else range(128, count)
        return ConditionVerification(condition=condition, state=ConditionVerificationState.SATISFIED,
            checked_at=harness.clock.now(), valid_until=None,
            evidence_refs=[f"test_fixture.ref.{index:03}" for index in indices], reason_codes=[])
    harness.verifier.verify = verify
    if count == 256:
        _invocation, _evaluation, record = harness.evaluate()
        assert len(record.state["conditions"].evidence_refs) == 256
    else:
        with pytest.raises(SETTAuthorizationError) as caught:
            harness.issuer.authorize(foundation.request())
        assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
        assert harness.store.issued == 0
    assert len(harness.verifier.calls) == 2


def test_evidence_valid_inventory_can_overflow_complete_real_slot():
    from sett._governance.validation import MAX_IDENTIFIER_BYTES, MAX_JSON_BYTES
    from sett.exceptions import SETTValidationError
    from sett.policy_ruler import ConditionVerification, ConditionVerificationState

    harness = Harness([condition("test_fixture.a"), condition("test_fixture.b")])
    references = [f"test_fixture.{index:03}." + "x" * 111 for index in range(256)]
    admitted = []
    contexts = []
    producer_results = []
    produce = harness.owner._condition_producer.produce

    def observed_produce(context):
        contexts.append(context)
        result = produce(context)
        producer_results.append(result)
        return result

    def verify(request, *, condition):
        harness.verifier.calls.append((request, condition))
        result = ConditionVerification(condition=condition, state=ConditionVerificationState.SATISFIED,
            checked_at=harness.clock.now(), valid_until=None, evidence_refs=references, reason_codes=[])
        admitted.append(result)
        return result

    harness.owner._condition_producer.produce = observed_produce
    harness.verifier.verify = verify
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert len(harness.verifier.calls) == 2
    assert harness.store.issued == 0

    # Successful public value construction and independent canonical admission
    # distinguish per-result validity from the complete-slot bound below.
    assert len(admitted) == 2
    assert all(type(item) is ConditionVerification for item in admitted)
    assert len(set(references)) == 256
    assert all(len(ref.encode("utf-8")) <= MAX_IDENTIFIER_BYTES for ref in references)
    for item in admitted:
        encoded = canonical_json_bytes(item.canonical_view(), field="test fixture result")
        assert len(encoded) <= MAX_JSON_BYTES
    assert len(contexts) == 1
    context = contexts[0]
    assert context.stage == "initial"
    assert len(context.condition_entries) == 2
    assert harness.owner._condition_producer._entries(context) == context.condition_entries
    inventory = json.loads(context.condition_inventory)
    assert canonical_json_bytes(inventory, field="test fixture inventory") == context.condition_inventory
    assert len(context.condition_inventory) <= MAX_JSON_BYTES
    assert len(producer_results) == 1
    assert type(producer_results[0]) is _handoff.ConditionRejection
    assert producer_results[0].code == "policy.evaluation_incomplete"

    # Reconstruct the actual observations and retained input, not guessed sizes.
    # This diagnostic encoding does not substitute for the producer's encoder.
    body = {"inventory": inventory, "observations": {
        key: item.canonical_view()
        for (key, _declaration, _coverage), item in zip(context.condition_entries, admitted, strict=True)
    }}
    unbounded_bytes = json.dumps(body, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert len(unbounded_bytes) > MAX_JSON_BYTES
    with pytest.raises(SETTValidationError):
        canonical_json_bytes(body, field="test fixture complete slot")


def test_evidence_unrecognized_private_rejection_does_not_transport_code():
    harness = Harness()
    harness.owner._condition_producer.produce = lambda context: _handoff.ConditionRejection("test_fixture.unrecognized")
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert harness.store.issued == 0
