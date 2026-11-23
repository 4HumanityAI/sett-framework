"""PolicyRuler(implementation_owned_consent=...): an explicit consent declaration.

The declaration fills the consent slot with a record that says consent is
owned by the implementation and that SETT verified none. Review and
durability still have no real producer; the authorization cases below use
synthetic review and durability states, as the other handoff tests do, and
prove no real integration and no governed effect.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest
import test_policy_handoff as foundation

from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    StrictJSONAuthorizationFingerprint,
)
from sett.exceptions import SETTAuthorizationError, SETTPolicyConfigurationError
from sett.policy_ruler import PolicyDecision, PolicyRuler, _handoff

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "templates"))

from consent_model import (
    ConsentRecord,
    ConsentRegistry,
    ConsentVerifier,
    consent_condition,
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
    """A declared consent slot, synthetic review and durability, no effect."""

    def __init__(self, *, declared=True, decision=None, condition_verifiers=None):
        self.clock = foundation.FixedClock()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.owner = ruler(self.clock, self.evidence, implementation_owned_consent=declared,
                           condition_verifiers=condition_verifiers)
        for name in ("review", "durability"):
            self.owner._register_state_producer(foundation.SyntheticStateProducer(name))
        if condition_verifiers is None:
            self.owner._register_state_producer(foundation.SyntheticStateProducer("conditions"))
        self.store = foundation.CountingStore()
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        self.policy = foundation.StaticPolicy(decision=decision)
        self.issuer.register_policy(foundation.EFFECT, self.policy, binding=foundation.binding())


# ── configuration ────────────────────────────────────────────────────────


def test_default_installs_no_consent_producer():
    owner = ruler()
    assert owner.implementation_owned_consent is False
    assert "consent" not in owner._state_producers


def test_declaration_installs_the_internal_consent_producer():
    owner = ruler(implementation_owned_consent=True)
    assert owner.implementation_owned_consent is True
    assert type(owner._state_producers["consent"]) is _handoff.ImplementationOwnedConsentProducer


@pytest.mark.parametrize("value", [1, 0, "true", None, 1.0])
def test_only_an_exact_bool_is_accepted(value):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ruler(implementation_owned_consent=value)
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


def test_a_second_consent_producer_is_rejected():
    owner = ruler(implementation_owned_consent=True)
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        owner._register_state_producer(foundation.SyntheticStateProducer("consent"))
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


def test_the_declaration_cannot_be_changed_after_construction():
    owner = ruler(implementation_owned_consent=True)
    with pytest.raises(AttributeError):
        owner.implementation_owned_consent = False


# ── the recorded state ───────────────────────────────────────────────────


def test_the_record_states_the_declaration_and_nothing_observed():
    producer = _handoff.ImplementationOwnedConsentProducer()
    context = _handoff.StateContext(foundation.request(), foundation.ENTRY, frozenset(), 0)
    slot = producer.produce(context)
    assert _handoff.validated_slot(slot, context)
    assert slot.slot == "consent"
    assert slot.producer_id == "sett.policy_ruler.consent_owned_by_implementation"
    assert slot.action_id == "test_fixture.action" and slot.checkpoint_id == foundation.ENTRY
    assert slot.required_keys == slot.observed_keys == slot.evidence_refs == ()
    assert slot.valid_until is None
    assert slot.value_bytes == canonical_json_bytes(
        {"consent": "owned_by_implementation"}, field="test fixture"
    )
    assert producer.confirm(slot) is True


def test_confirmation_rejects_a_record_that_is_not_its_own():
    producer = _handoff.ImplementationOwnedConsentProducer()
    context = _handoff.StateContext(foundation.request(), foundation.ENTRY, frozenset(), 0)
    slot = producer.produce(context)
    synthetic = foundation.SyntheticStateProducer("consent").produce(context)
    assert producer.confirm(synthetic) is False
    assert producer.confirm(object()) is False
    assert producer.confirm(replace(slot, value_bytes=b"{}")) is False


# ── inside authorization ─────────────────────────────────────────────────


def test_without_the_declaration_a_governed_attempt_still_stops():
    harness = Harness(declared=False)
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"
    assert harness.store.issued == 0


def test_with_the_declaration_a_governed_attempt_reaches_a_receipt():
    harness = Harness()
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.decision.verdict is AuthorizationVerdict.GRANT
    assert outcome.receipt is not None
    harness.issuer.verify_and_consume(outcome.receipt, foundation.request())
    assert harness.store.consumed == 1


def test_the_declaration_does_not_lift_a_denial():
    harness = Harness(decision=PolicyDecision.deny(
        "test_fixture.policy", reason_codes=("test_fixture.denied",), conditions=(), review_obligations=(),
    ))
    outcome = harness.issuer.authorize(foundation.request())
    assert outcome.decision.verdict is AuthorizationVerdict.DENY
    assert outcome.receipt is None


def test_the_declaration_does_not_replace_a_missing_review_or_durability_producer():
    owner = ruler(implementation_owned_consent=True)
    owner._register_state_producer(foundation.SyntheticStateProducer("conditions"))
    evidence = owner._evidence_sink
    issuer = AuthorizationRuler(
        issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=foundation.CountingStore(),
        evidence_sink=evidence, clock=owner._clock, policy_ruler=owner,
    )
    issuer.register_policy(foundation.EFFECT, foundation.StaticPolicy(), binding=foundation.binding())
    with pytest.raises(SETTAuthorizationError) as caught:
        issuer.authorize(foundation.request())
    assert caught.value.reason_code == "authorization.policy_evaluation_incomplete"


# ── with the consent model template ──────────────────────────────────────


class ConsentPolicy:
    policy_id = "test_fixture.policy"

    def evaluate(self, request, verified_approvals):
        return PolicyDecision.grant(
            self.policy_id,
            conditions=(consent_condition(foundation.binding(), request, checkpoint_id=foundation.ENTRY),),
            review_obligations=(),
        )


def consent_request():
    def field(value):
        return FieldValue(value, source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE)

    return AuthorizationRequest(
        action_id="test_fixture.action", trace_id="test_fixture.trace", action_type=foundation.EFFECT,
        payload={"value": 1}, principal=field("test_fixture.subject"), purpose=field("test_fixture.purpose"),
        scope=[field("test_fixture.read")], execution_mode=ExecutionMode.PLAIN,
    )


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "missing"])
def test_the_consent_model_works_as_a_condition_beside_the_declaration(recorded):
    clock = foundation.FixedClock()
    evidence = foundation.InMemoryAuthorizationEvidence()
    registry = ConsentRegistry()
    if recorded:
        registry.record(ConsentRecord(
            record_id="test_fixture.record", subject="test_fixture.subject", purpose="test_fixture.purpose",
            scopes=frozenset({"test_fixture.read"}),
        ))
    owner = ruler(clock, evidence, implementation_owned_consent=True,
                  condition_verifiers=[ConsentVerifier(registry, clock)])
    for name in ("review", "durability"):
        owner._register_state_producer(foundation.SyntheticStateProducer(name))
    issuer = AuthorizationRuler(
        issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=foundation.CountingStore(),
        evidence_sink=evidence, clock=clock, policy_ruler=owner,
    )
    issuer.register_policy(foundation.EFFECT, ConsentPolicy(), binding=foundation.binding())
    if recorded:
        assert issuer.authorize(consent_request()).receipt is not None
    else:
        with pytest.raises(SETTAuthorizationError) as caught:
            issuer.authorize(consent_request())
        assert caught.value.reason_code == "authorization.condition_evidence_not_present"
