"""Real condition-producer cases with explicitly synthetic other state slots."""

from dataclasses import replace
from datetime import timedelta

import pytest
import test_policy_handoff as foundation

from sett.authorization_ruler import (
    AuthorizationRuler,
    StrictJSONAuthorizationFingerprint,
)
from sett.exceptions import SETTAuthorizationConfigurationError, SETTAuthorizationError
from sett.policy_ruler import (
    ConditionValidityMode,
    ConditionVerification,
    ConditionVerificationState,
    PolicyCondition,
    PolicyDecision,
    PolicyRuler,
    _handoff,
)
from sett.policy_ruler._condition_state import (
    _admit_condition_verifiers,
    _call_confirm,
    _call_verify,
)


def condition(identifier="test_fixture.condition", *, bound=None, **changes):
    bound = bound or foundation.binding()
    data = dict(condition_id=identifier, binding=bound, origins=bound.origins,
                verifier_id="test_fixture.verifier", verifier_version="1", required=True,
                action_id="test_fixture.action", action_type=foundation.EFFECT,
                checkpoint_id=foundation.ENTRY, validity_mode=ConditionValidityMode.RECHECK,
                attestation_not_after=None)
    data.update(changes)
    return PolicyCondition(**data)


class Verifier:
    verifier_id = "test_fixture.verifier"
    verifier_version = "1"

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.confirmations = []
        self.states = []
        self.result = None
        self.error = None
        self.current = True
        self.on_verify = None

    def verify(self, request, *, condition):
        self.calls.append((request, condition))
        if self.on_verify is not None:
            self.on_verify()
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        state = self.states.pop(0) if self.states else ConditionVerificationState.SATISFIED
        positive = state is ConditionVerificationState.SATISFIED
        deadline = condition.attestation_not_after if positive else None
        return ConditionVerification(
            condition=condition, state=state, checked_at=self.clock.now(),
            valid_until=deadline,
            evidence_refs=() if state is ConditionVerificationState.EVIDENCE_NOT_PRESENT else ("test_fixture.evidence",),
            reason_codes=() if positive else ("test_fixture.negative",),
        )

    def confirm(self, verification, *, request):
        self.confirmations.append((verification, request))
        if self.error is not None:
            raise self.error
        return self.current


class Harness:
    """Real condition production, synthetic consent/review/durability, no effect."""

    def __init__(self, declarations=None, *, missing=(), configured=True):
        self.clock = foundation.FixedClock()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.resolver = foundation.Resolver()
        self.verifier = Verifier(self.clock)
        self.store = foundation.CountingStore()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=60, max_live_evaluations=16,
            clock=self.clock, evidence_sink=self.evidence, selection_resolver=self.resolver,
            condition_verifiers=[self.verifier] if configured else None,
        )
        self.producers = {}
        for name in ("consent", "review", "durability"):
            if name not in missing:
                producer = foundation.SyntheticStateProducer(name)
                self.owner._register_state_producer(producer)
                self.producers[name] = producer
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        self.policy = foundation.StaticPolicy(decision=PolicyDecision.grant(
            "test_fixture.policy", conditions=(condition(),) if declarations is None else declarations,
            review_obligations=(),
        ))
        self.issuer.register_policy(foundation.EFFECT, self.policy, binding=foundation.binding())

    def evaluate(self):
        invocation = self.owner._open_invocation(
            self.issuer._link, request=foundation.request(), checkpoint_id=foundation.ENTRY,
        )
        evaluation = self.owner._evaluate_bound(invocation, verified_approvals=frozenset(), receiver_generation=0)
        return invocation, evaluation, self.owner._live[invocation]


@pytest.mark.parametrize("state,suffix,configuration", [
    (ConditionVerificationState.UNSATISFIED, "condition_unsatisfied", False),
    (ConditionVerificationState.EVIDENCE_NOT_PRESENT, "condition_evidence_not_present", False),
    (ConditionVerificationState.EVIDENCE_INVALID, "condition_evidence_invalid", False),
    (ConditionVerificationState.EVIDENCE_UNAVAILABLE, "condition_evidence_unavailable", False),
])
def test_initial_negative_states_have_distinct_codes(state, suffix, configuration):
    harness = Harness()
    harness.verifier.states = [state]
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization." + suffix
    assert isinstance(caught.value, SETTAuthorizationConfigurationError) is configuration
    assert len(harness.verifier.calls) == 1
    assert harness.store.issued == 0


@pytest.mark.parametrize("site", ["pre_issue", "pre_consumption"])
def test_fresh_recheck_failure_at_each_later_site(site):
    harness = Harness()
    if site == "pre_issue":
        harness.verifier.states = [ConditionVerificationState.SATISFIED, ConditionVerificationState.UNSATISFIED]
        with pytest.raises(SETTAuthorizationError) as caught:
            harness.issuer.authorize(foundation.request())
        assert harness.store.issued == 0
    else:
        outcome = harness.issuer.authorize(foundation.request())
        harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
        with pytest.raises(SETTAuthorizationError) as caught:
            harness.issuer.verify_and_consume(outcome.receipt, request=foundation.request())
        assert harness.store.issued == 1
        assert harness.store.consumed == 0
    assert caught.value.reason_code == "authorization.policy_evidence_changed"


def test_fresh_history_preserves_original_slot_and_deadline():
    harness = Harness()
    invocation, _evaluation, record = harness.evaluate()
    retained = record.state["conditions"]
    signature = record.state_signature
    deadline = record.valid_until
    original = record.condition_proof.observations
    harness.clock.current += timedelta(seconds=1)
    assert harness.owner._confirm_invocation_state(invocation)
    assert record.state["conditions"] is retained
    assert record.state_signature == signature
    assert record.valid_until == deadline
    assert record.condition_proof.observations is original
    assert len(record.condition_proof.history) == 1
    assert record.condition_proof.history[0][2].checked_at > original[0][1].checked_at


@pytest.mark.parametrize("answer", [False, None, 1, "test_fixture.true"])
def test_attestation_confirmation_requires_exact_true(answer):
    clock = foundation.FixedClock()
    declaration = condition(validity_mode=ConditionValidityMode.ATTESTATION,
                            attestation_not_after=clock.now() + timedelta(seconds=10))
    harness = Harness([declaration])
    harness.verifier.current = answer
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evidence_changed"
    assert harness.store.issued == 0
    assert len(harness.verifier.confirmations) == 1


def test_earliest_attestation_limit_is_exclusive():
    clock = foundation.FixedClock()
    first = condition(attestation_not_after=clock.now() + timedelta(seconds=4),
                      validity_mode=ConditionValidityMode.ATTESTATION)
    second = replace(first, condition_id="test_fixture.second", attestation_not_after=clock.now() + timedelta(seconds=8))
    harness = Harness([second, first])
    outcome = harness.issuer.authorize(foundation.request())
    record = next(record for record in harness.owner._live.values() if record is not None)
    assert record.valid_until == first.attestation_not_after
    harness.clock.current = first.attestation_not_after
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.verify_and_consume(outcome.receipt, request=foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_expired"
    assert harness.store.consumed == 0


@pytest.mark.parametrize("offset", [-1, 1])
def test_recheck_rejects_observation_outside_call_window(offset):
    harness = Harness()
    harness.verifier.result = ConditionVerification(
        condition=condition(), state=ConditionVerificationState.SATISFIED,
        checked_at=harness.clock.now() + timedelta(seconds=offset), valid_until=None,
        evidence_refs=["test_fixture.evidence"], reason_codes=[],
    )
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_result_invalid"
    assert harness.store.issued == 0


def test_full_expected_condition_is_independent_of_returned_value():
    harness = Harness()
    harness.verifier.result = ConditionVerification(
        condition=condition("test_fixture.other"), state=ConditionVerificationState.SATISFIED,
        checked_at=harness.clock.now(), valid_until=None,
        evidence_refs=["test_fixture.evidence"], reason_codes=[],
    )
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_result_invalid"
    assert harness.verifier.calls[0][1].condition_id == "test_fixture.condition"


@pytest.mark.parametrize("boundary", ["verify", "confirm"])
@pytest.mark.parametrize("signal_class", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_callback_termination_signals_preserve_identity(boundary, signal_class):
    verifier = Verifier(foundation.FixedClock())
    signal = signal_class("test_fixture.stop")
    verifier.error = signal
    with pytest.raises(signal_class) as caught:
        if boundary == "verify":
            _call_verify(verifier, foundation.request(), condition())
        else:
            _call_confirm(verifier, None, foundation.request())
    assert caught.value is signal


@pytest.mark.parametrize("active_caller", [False, True])
def test_callback_exception_privacy_and_distinct_caller_context(active_caller):
    harness = Harness()
    secret = RuntimeError("test_fixture.private_callback")
    caller = ValueError("test_fixture.caller")
    harness.verifier.error = secret
    if active_caller:
        try:
            raise caller
        except ValueError:
            with pytest.raises(SETTAuthorizationError) as caught:
                harness.issuer.authorize(foundation.request())
    else:
        with pytest.raises(SETTAuthorizationError) as caught:
            harness.issuer.authorize(foundation.request())
    error = caught.value
    assert error.reason_code == "authorization.condition_verifier_failed"
    assert error.__cause__ is None
    assert error.__context__ is (caller if active_caller else None)
    assert "private_callback" not in str(error)
    assert harness.store.issued == 0


def test_second_missing_verifier_prevents_all_calls():
    harness = Harness([condition(), condition("test_fixture.second", verifier_version="2")])
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_verifier_missing"
    assert harness.verifier.calls == []


def test_first_failure_stops_later_verification():
    harness = Harness([condition("test_fixture.z"), condition("test_fixture.a")])
    harness.verifier.states = [ConditionVerificationState.UNSATISFIED]
    with pytest.raises(SETTAuthorizationError):
        harness.issuer.authorize(foundation.request())
    assert [item.condition_id for _request, item in harness.verifier.calls] == ["test_fixture.a"]


def test_shared_mutation_during_confirmation_is_stale_not_poisoned():
    harness = Harness()
    def mutate_on_second_call():
        if len(harness.verifier.calls) == 2:
            bound = foundation.binding("test_fixture.added")
            harness.owner.register_policy(foundation.EFFECT, foundation.StaticPolicy(bound.policy_id), binding=bound)
    harness.verifier.on_verify = mutate_on_second_call
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_generation_changed"
    assert harness.store.issued == 0


@pytest.mark.parametrize("signal_class", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_constructor_identity_signal_is_not_caught(signal_class):
    signal = signal_class("test_fixture.stop")
    class Identity:
        verifier_version = "1"
        @property
        def verifier_id(self):
            raise signal
        def verify(self, request, *, condition):
            return None
        def confirm(self, verification, *, request):
            return True
    with pytest.raises(signal_class) as caught:
        _admit_condition_verifiers([Identity()])
    assert caught.value is signal


def test_direct_producer_missing_inventory_does_not_become_empty():
    harness = Harness([])
    context = _handoff.StateContext(foundation.request(), foundation.ENTRY, frozenset(), 0)
    result = harness.owner._condition_producer.produce(context)
    assert type(result) is _handoff.ConditionRejection
    assert result.code == "policy.evaluation_incomplete"
    assert harness.verifier.calls == []


def test_attestation_equal_observation_does_not_reuse_an_invocation_proof():
    clock = foundation.FixedClock()
    declaration = condition(validity_mode=ConditionValidityMode.ATTESTATION,
                            attestation_not_after=clock.now() + timedelta(seconds=10))
    harness = Harness([declaration])
    shared = ConditionVerification(condition=declaration, state=ConditionVerificationState.SATISFIED,
        checked_at=clock.now(), valid_until=declaration.attestation_not_after,
        evidence_refs=["test_fixture.evidence"], reason_codes=[])
    harness.verifier.result = shared
    first, evaluation_one, record_one = harness.evaluate()
    second, evaluation_two, record_two = harness.evaluate()
    assert first is not second
    assert evaluation_one is not evaluation_two
    assert len(harness.verifier.calls) == 2
    assert record_one.condition_proof is not record_two.condition_proof
    assert record_one.state["conditions"] is not record_two.state["conditions"]
    assert record_one.state["conditions"].value_bytes == record_two.state["conditions"].value_bytes


@pytest.mark.parametrize("result", [object(), False, "test_fixture.not_result"])
def test_result_type_admission_precedes_property_access(result):
    harness = Harness()
    harness.verifier.result = result
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_result_invalid"
    assert harness.store.issued == 0


def test_wrong_result_properties_are_not_read():
    class Unexpected:
        @property
        def condition(self):
            raise AssertionError("test_fixture.property_read")
    harness = Harness()
    harness.verifier.result = Unexpected()
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_result_invalid"


def test_confirm_boundary_sanitizes_an_ordinary_callback_exception():
    verifier = Verifier(foundation.FixedClock())
    verifier.error = RuntimeError("test_fixture.private")
    assert _call_confirm(verifier, None, foundation.request()) is False


def test_constructor_identity_exception_is_sanitized():
    class Identity:
        verifier_version = "1"
        @property
        def verifier_id(self):
            raise RuntimeError("test_fixture.private")
        def verify(self, request, *, condition):
            return None
        def confirm(self, verification, *, request):
            return True
    from sett.exceptions import SETTPolicyConfigurationError
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        _admit_condition_verifiers([Identity()])
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "test_fixture.private" not in str(caught.value)


@pytest.mark.parametrize("field", ["required", "verifier_version"])
def test_evidence_full_condition_mismatch_with_same_identifier(field):
    harness = Harness()
    different = replace(condition(), **{field: False if field == "required" else "2"})
    harness.verifier.result = ConditionVerification(
        condition=different, state=ConditionVerificationState.SATISFIED,
        checked_at=harness.clock.now(), valid_until=None,
        evidence_refs=["test_fixture.evidence"], reason_codes=[],
    )
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_result_invalid"
    assert harness.verifier.calls[0][1].condition_id == different.condition_id
    assert harness.store.issued == 0


@pytest.mark.parametrize("edge", ["start", "end"])
def test_evidence_recheck_distinct_window_boundaries(edge):
    harness = Harness()
    original = harness.verifier.verify
    def verify(request, *, condition):
        start = harness.clock.now()
        harness.clock.current += timedelta(seconds=1)
        result = original(request, condition=condition)
        return replace(result, checked_at=start if edge == "start" else harness.clock.now())
    harness.verifier.verify = verify
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.receipt is not None
    harness.issuer.verify_and_consume(outcome.receipt, request=foundation.request())
    assert harness.store.consumed == 1
    assert len(harness.verifier.calls) == 3


def test_evidence_attestation_future_checked_at_rejects():
    clock = foundation.FixedClock()
    declared = condition(validity_mode=ConditionValidityMode.ATTESTATION,
                         attestation_not_after=clock.now() + timedelta(seconds=10))
    harness = Harness([declared])
    harness.verifier.result = ConditionVerification(
        condition=declared, state=ConditionVerificationState.SATISFIED,
        checked_at=clock.now() + timedelta(seconds=1), valid_until=declared.attestation_not_after,
        evidence_refs=["test_fixture.evidence"], reason_codes=[],
    )
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.condition_result_invalid"
    assert harness.store.issued == 0


@pytest.mark.parametrize("answer", [False, None, 1, "test_fixture.true", True])
def test_evidence_attestation_confirmation_before_consumption(answer):
    clock = foundation.FixedClock()
    declared = condition(validity_mode=ConditionValidityMode.ATTESTATION,
                         attestation_not_after=clock.now() + timedelta(seconds=10))
    harness = Harness([declared])
    outcome = harness.issuer.authorize(foundation.request())
    observed = harness.verifier.confirmations[0][0]
    harness.verifier.current = answer
    if answer is True:
        harness.issuer.verify_and_consume(outcome.receipt, request=foundation.request())
        assert harness.store.consumed == 1
    else:
        with pytest.raises(SETTAuthorizationError) as caught:
            harness.issuer.verify_and_consume(outcome.receipt, request=foundation.request())
        assert caught.value.reason_code == "authorization.policy_evidence_changed"
        assert harness.store.consumed == 0
    assert len(harness.verifier.confirmations) == 2
    assert all(item[0] is observed for item in harness.verifier.confirmations)
    assert len(harness.verifier.calls) == 1


def test_evidence_fresh_history_at_both_later_sites_keeps_retained_proof():
    harness = Harness()
    invocation, evaluation, record = harness.evaluate()
    retained = record.state["conditions"]
    signature, deadline = record.state_signature, record.valid_until
    original = record.condition_proof.observations
    harness.clock.current += timedelta(seconds=1)
    receipt = harness.issuer._issue(invocation, evaluation, invocation.request, frozenset(), evaluation.decision)
    assert receipt is not None
    harness.clock.current += timedelta(seconds=1)
    harness.issuer.verify_and_consume(receipt, request=foundation.request())
    assert record.condition_proof.slot is retained
    assert record.condition_proof.observations is original
    assert record.state_signature == signature
    assert record.valid_until == deadline
    assert [item[0] for item in record.condition_proof.history] == ["pre_issue", "pre_consumption"]
    assert all(item[2].checked_at > original[0][1].checked_at for item in record.condition_proof.history)


@pytest.mark.parametrize("active_caller", [False, True])
@pytest.mark.parametrize("site", ["pre_issue", "pre_consumption"])
def test_evidence_confirm_exception_origin_in_bound_path(active_caller, site):
    harness = Harness([condition(validity_mode=ConditionValidityMode.ATTESTATION,
        attestation_not_after=foundation.FixedClock().now() + timedelta(seconds=10))])
    receipt = harness.issuer.authorize(foundation.request()).receipt if site == "pre_consumption" else None
    secret = RuntimeError("test_fixture.private_confirm")
    caller = ValueError("test_fixture.caller")
    def fail(verification, *, request):
        raise secret
    harness.verifier.confirm = fail
    def invoke():
        if site == "pre_issue":
            return harness.issuer.authorize(foundation.request())
        return harness.issuer.verify_and_consume(receipt, request=foundation.request())
    if active_caller:
        try:
            raise caller
        except ValueError:
            with pytest.raises(SETTAuthorizationError) as caught:
                invoke()
    else:
        with pytest.raises(SETTAuthorizationError) as caught:
            invoke()
    error = caught.value
    assert error.reason_code == "authorization.policy_evidence_changed"
    assert error.__cause__ is None
    assert error.__context__ is (caller if active_caller else None)
    assert "private_confirm" not in str(error)
    assert harness.store.consumed == 0


@pytest.mark.parametrize("count", [256, 257])
def test_evidence_constructor_sequence_capacity_and_reads(count):
    from collections.abc import Sequence

    from sett.exceptions import SETTPolicyConfigurationError
    values = []
    for index in range(count):
        verifier = Verifier(foundation.FixedClock())
        verifier.verifier_id = f"test_fixture.verifier.{index:03}"
        values.append(verifier)
    class BoundedSequence(Sequence):
        def __init__(self):
            self.reads = []
        def __len__(self):
            return count
        def __getitem__(self, index):
            self.reads.append(index)
            return values[index]
    source = BoundedSequence()
    if count == 256:
        registry = _admit_condition_verifiers(source)
        assert len(registry) == 256
        assert max(source.reads) <= 256
    else:
        with pytest.raises(SETTPolicyConfigurationError) as caught:
            _admit_condition_verifiers(source)
        assert caught.value.reason_code == "policy.ruler_configuration_invalid"
        assert source.reads == []


def test_evidence_constructor_dishonest_sequence_is_bounded():
    from collections.abc import Sequence

    from sett.exceptions import SETTPolicyConfigurationError
    class EndlessSequence(Sequence):
        def __init__(self):
            self.reads = []
        def __len__(self):
            return 256
        def __getitem__(self, index):
            self.reads.append(index)
            if index > 256:
                raise AssertionError("test_fixture.read_beyond_bound")
            return Verifier(foundation.FixedClock())
    source = EndlessSequence()
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        _admit_condition_verifiers(source)
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"
    assert source.reads == list(range(257))


def test_evidence_same_verifier_id_different_versions_are_distinct():
    first, second = Verifier(foundation.FixedClock()), Verifier(foundation.FixedClock())
    second.verifier_version = "2"
    registry = _admit_condition_verifiers([first, second])
    assert registry[(first.verifier_id, "1")] is first
    assert registry[(second.verifier_id, "2")] is second


@pytest.mark.parametrize("stage", ["claim", "consume"])
@pytest.mark.parametrize("change", ["local", "shared"])
def test_evidence_configuration_isolation_with_real_condition_production(stage, change):
    first, independent = Harness(), Harness()
    other_store = foundation.CountingStore()
    second = AuthorizationRuler(
        issuer_id="test_fixture.second_issuer", receipt_ttl_seconds=30,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=other_store,
        evidence_sink=first.evidence, clock=first.clock, policy_ruler=first.owner,
    )
    receivers = [first.issuer, second, independent.issuer]
    stores = [first.store, other_store, independent.store]
    requests = [foundation.request() for _ in receivers]
    pending = [foundation.residual_pending(receiver, request, stage)
               for receiver, request in zip(receivers, requests, strict=True)]
    shared_generation, independent_generation = first.owner._generation, independent.owner._generation
    receiver_generations = [receiver._generation() for receiver in receivers]
    if change == "local":
        first.issuer.register_approval_verifier(foundation.ResidualVerifier())
        assert first.owner._generation == shared_generation
        assert [receiver._generation() for receiver in receivers] == [receiver_generations[0] + 1, *receiver_generations[1:]]
    else:
        first.issuer.register_policy("test_fixture.other_effect", foundation.StaticPolicy("test_fixture.other_policy"),
                                    binding=foundation.binding("test_fixture.other_policy"))
        assert first.owner._generation == shared_generation + 1
        assert [receiver._generation() for receiver in receivers] == receiver_generations
    assert independent.owner._generation == independent_generation
    for index, (receiver, item, request, store) in enumerate(zip(receivers, pending, requests, stores, strict=True)):
        affected = index == 0 or (change == "shared" and index == 1)
        if affected:
            with pytest.raises(SETTAuthorizationError) as caught:
                foundation.residual_continue(receiver, item, request, stage)
            assert caught.value.reason_code == "authorization.policy_generation_changed"
            assert store.consumed == 0
            assert store.issued == (1 if stage == "consume" else 0)
        else:
            foundation.residual_continue(receiver, item, request, stage)
            assert store.issued == store.consumed == 1


@pytest.mark.parametrize("change", ["local", "shared"])
@pytest.mark.parametrize("site", ["pre_issue", "pre_consumption"])
def test_evidence_mutation_inside_condition_callback_is_stale(site, change):
    harness = Harness()
    receipt = harness.issuer.authorize(foundation.request()).receipt if site == "pre_consumption" else None
    trigger = 3 if site == "pre_consumption" else 2
    def mutate():
        if len(harness.verifier.calls) == trigger:
            if change == "local":
                harness.issuer.register_approval_verifier(foundation.ResidualVerifier())
            else:
                harness.issuer.register_policy("test_fixture.other_effect", foundation.StaticPolicy("test_fixture.other_policy"),
                                              binding=foundation.binding("test_fixture.other_policy"))
    harness.verifier.on_verify = mutate
    with pytest.raises(SETTAuthorizationError) as caught:
        if site == "pre_issue":
            harness.issuer.authorize(foundation.request())
        else:
            harness.issuer.verify_and_consume(receipt, request=foundation.request())
    assert caught.value.reason_code == "authorization.policy_generation_changed"
    assert harness.store.consumed == 0


@pytest.mark.parametrize("cleanup", ["none", "expired", "terminal"])
def test_evidence_public_issue_recognition_precedes_state_after_purge(cleanup):
    """Private-boundary purge injection through public authorize; no external effect.

    The continuation counter follows public receipt consumption only. It is
    synthetic observation, not evidence of final delivery or of an executor.
    Other required state slots are the harness's explicit synthetic producers.
    """
    harness = Harness()
    item = foundation.request()
    original_state = harness.owner._current_state
    captured = {}
    effects = []

    def collect_at_boundary(invocation, *, verified_approvals, receiver_generation):
        if invocation.request is item and not captured:
            record = harness.owner._live[invocation]
            captured.update(invocation=invocation, record=record, evaluation=record.evaluation)
            assert record.invocation is invocation
            assert record.link.receiver is harness.issuer
            assert record.evaluation is harness.owner._live[invocation].evaluation
            assert set(record.state) == set(_handoff.SLOT_NAMES)
            assert record.condition_proof.slot is record.state["conditions"]
            assert record.state["conditions"].required_keys == ("condition.000001",)

            # The sibling is acquired independently one second later. Its
            # different trace does not change the declaration's action scope.
            harness.clock.current += timedelta(seconds=1)
            sibling_request = foundation.request(trace_id="test_fixture.sibling")
            sibling = harness.owner._open_invocation(
                harness.issuer._link, request=sibling_request, checkpoint_id=foundation.ENTRY,
            )
            sibling_evaluation = harness.owner._evaluate_bound(
                sibling, verified_approvals=verified_approvals,
                receiver_generation=receiver_generation,
            )
            sibling_record = harness.owner._live[sibling]
            captured.update(sibling=sibling, sibling_request=sibling_request,
                            sibling_evaluation=sibling_evaluation, sibling_record=sibling_record)
            assert sibling is not invocation
            assert sibling_record.evaluation is sibling_evaluation
            assert sibling_record.valid_until > record.valid_until

            # Use existing lifecycle and purge behavior, never a direct ledger
            # removal or an empty-state substitute. The positive row also
            # crosses the same boundary, with no purge or termination injected.
            if cleanup == "expired":
                harness.clock.current = record.valid_until
            elif cleanup == "terminal":
                harness.owner._terminate(record)
            if cleanup != "none":
                harness.owner._guard.acquire()
                try:
                    harness.owner._purge(harness.clock.now())
                finally:
                    harness.owner._guard.release()
                assert invocation not in harness.owner._live
            else:
                assert harness.owner._live[invocation] is record
            assert harness.owner._live[sibling] is sibling_record
            assert sibling_record.is_live(harness.clock.now())
        return original_state(
            invocation, verified_approvals=verified_approvals,
            receiver_generation=receiver_generation,
        )

    failure = None
    outcome = None
    harness.owner._current_state = collect_at_boundary
    try:
        try:
            outcome = harness.issuer.authorize(item)
            harness.issuer.verify_and_consume(outcome.receipt, request=item)
            effects.append("test_fixture.target")
        except SETTAuthorizationError as error:
            failure = error.reason_code
    finally:
        harness.owner._current_state = original_state

    assert captured["invocation"].request is item
    assert captured["record"].evaluation is captured["evaluation"]
    if cleanup == "none":
        assert outcome is not None and outcome.receipt is not None
        assert harness.store.issued == harness.store.consumed == 1
        assert effects == ["test_fixture.target"]
    else:
        assert outcome is None
        assert harness.store.issued == harness.store.consumed == 0
        assert effects == []

    # Prove sibling usability before the final diagnostic assertion, so a red
    # precedence row does not silently leave this control unobserved.
    sibling = captured["sibling"]
    assert harness.owner._live[sibling] is captured["sibling_record"]
    issued_before, consumed_before = harness.store.issued, harness.store.consumed
    receipt = harness.issuer._issue(
        sibling, captured["sibling_evaluation"], captured["sibling_request"],
        frozenset(), captured["sibling_evaluation"].decision,
    )
    harness.issuer.verify_and_consume(receipt, request=captured["sibling_request"])
    assert harness.store.issued == issued_before + 1
    assert harness.store.consumed == consumed_before + 1
    assert effects == (["test_fixture.target"] if cleanup == "none" else [])
    assert failure == (None if cleanup == "none" else "authorization.policy_evaluation_unrecognized")


def test_ordering_absent_observation_with_live_record_cannot_claim():
    """Inject an absent current observation at the private ordered claim boundary."""
    harness = Harness()
    invocation, evaluation, record = harness.evaluate()
    assert harness.owner._live[invocation] is record
    assert record.evaluation is evaluation
    assert record.is_live(harness.clock.now())
    assert record.lifecycle == _handoff.PENDING
    phase = harness.owner._begin_critical()
    try:
        claimed, failure = harness.owner._validate_and_claim(
            invocation, evaluation, receiver=harness.issuer,
            request_bytes=record.request_bytes,
            current_selection_bytes=record.selection_bytes,
            current_state=None, state_confirmed=True,
            receiver_generation=record.receiver_generation, now=harness.clock.now(),
        )
    finally:
        harness.owner._end_critical(phase)
    assert claimed is None
    assert failure == "authorization.policy_evaluation_incomplete"
    assert harness.owner._live[invocation] is record
    assert record.lifecycle == _handoff.PENDING
    assert record.receipt is None
    assert harness.store.issued == harness.store.consumed == 0


def test_ordering_invocation_mismatch_precedes_absent_observation():
    """A recognized result from another invocation fails before absent state."""
    harness = Harness()
    invocation, evaluation, record = harness.evaluate()
    other, other_evaluation, other_record = harness.evaluate()
    assert other is not invocation
    assert other_evaluation is not evaluation
    assert harness.owner._live[invocation] is record
    assert harness.owner._live[other] is other_record
    assert record.is_live(harness.clock.now())
    assert other_record.is_live(harness.clock.now())
    phase = harness.owner._begin_critical()
    try:
        claimed, failure = harness.owner._validate_and_claim(
            other, evaluation, receiver=harness.issuer,
            request_bytes=record.request_bytes,
            current_selection_bytes=record.selection_bytes,
            current_state=None, state_confirmed=True,
            receiver_generation=record.receiver_generation, now=harness.clock.now(),
        )
    finally:
        harness.owner._end_critical(phase)
    assert claimed is None
    assert failure == "authorization.policy_invocation_mismatch"
    assert record.lifecycle == other_record.lifecycle == _handoff.PENDING
    assert record.receipt is other_record.receipt is None
    assert harness.store.issued == harness.store.consumed == 0


def test_ordering_live_record_missing_required_producer_is_incomplete():
    """Remove one synthetic required producer after evaluation, not its record.

    This private-boundary fault checks actual producer absence. It supplies no
    empty slot and makes no claim about a real review implementation or controlled checkpoints.
    """
    harness = Harness()
    invocation, evaluation, record = harness.evaluate()
    assert record.evaluation is evaluation
    assert record.is_live(harness.clock.now())
    assert set(record.state) == set(_handoff.SLOT_NAMES)
    original = harness.owner._state_producers
    assert original["review"] is harness.producers["review"]
    calls_before = len(harness.verifier.calls)
    harness.owner._guard.acquire()
    try:
        harness.owner._state_producers = {
            name: producer for name, producer in original.items() if name != "review"
        }
    finally:
        harness.owner._guard.release()
    try:
        assert "review" not in harness.owner._state_producers
        with pytest.raises(SETTAuthorizationError) as caught:
            harness.issuer._translate(
                harness.owner._current_state, invocation,
                verified_approvals=frozenset(),
                receiver_generation=record.receiver_generation,
            )
        assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    finally:
        harness.owner._guard.acquire()
        try:
            harness.owner._state_producers = original
        finally:
            harness.owner._guard.release()
    assert harness.owner._live[invocation] is record
    assert record.lifecycle == _handoff.PENDING
    assert record.receipt is None
    assert len(harness.verifier.calls) == calls_before
    assert harness.store.issued == harness.store.consumed == 0
