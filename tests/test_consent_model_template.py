"""Keep templates/consent_model.py working against the condition contract.

The template is a conceptual model meant to be copied and adapted. These
tests check that it still does what its docstring says: a current record is
satisfied, a missing one is not present, a revoked or expired one is not
satisfied, and the result passes SETT's own condition checks. The
authorization cases use synthetic review and durability states, as the
other condition tests do; they prove no real integration and no governed
effect.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import test_policy_handoff as foundation

from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    StrictJSONAuthorizationFingerprint,
)
from sett.exceptions import SETTAuthorizationError
from sett.policy_ruler import (
    ConditionValidityMode,
    ConditionVerificationState,
    PolicyDecision,
    PolicyRuler,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "templates"))

from consent_model import (
    ConsentRecord,
    ConsentRegistry,
    ConsentVerifier,
    consent_condition,
    consent_key,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def field(value):
    return FieldValue(value, source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE)


def consent_request(*, subject="test_fixture.subject", purpose="test_fixture.purpose", scopes=("test_fixture.read",)):
    return AuthorizationRequest(
        action_id="test_fixture.action", trace_id="test_fixture.trace", action_type=foundation.EFFECT,
        payload={"value": 1}, principal=field(subject),
        purpose=None if purpose is None else field(purpose),
        scope=[field(scope) for scope in scopes], execution_mode=ExecutionMode.PLAIN,
    )


def record(record_id="test_fixture.record", **changes):
    data = dict(record_id=record_id, subject="test_fixture.subject", purpose="test_fixture.purpose",
                scopes=frozenset({"test_fixture.read", "test_fixture.write"}),
                granted_at=NOW - timedelta(days=1))
    data.update(changes)
    return ConsentRecord(**data)


class ConsentPolicy:
    """A policy that grants and declares the template's consent check."""

    policy_id = "test_fixture.policy"

    def evaluate(self, request, verified_approvals):
        return PolicyDecision.grant(
            self.policy_id,
            conditions=(consent_condition(foundation.binding(), request, checkpoint_id=foundation.ENTRY),),
            review_obligations=(),
        )


class Harness:
    """The template's verifier, synthetic review and durability states, no effect."""

    def __init__(self):
        self.clock = foundation.FixedClock()
        self.clock.current = NOW
        self.registry = ConsentRegistry()
        evidence = foundation.InMemoryAuthorizationEvidence()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=60, max_live_evaluations=16, clock=self.clock,
            evidence_sink=evidence, selection_resolver=foundation.Resolver(),
            condition_verifiers=[ConsentVerifier(self.registry, self.clock)],
        )
        for name in ("consent", "review", "durability"):
            self.owner._register_state_producer(foundation.SyntheticStateProducer(name))
        self.store = foundation.CountingStore()
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=evidence, clock=self.clock, policy_ruler=self.owner,
        )
        self.issuer.register_policy(foundation.EFFECT, ConsentPolicy(), binding=foundation.binding())


def observe(registry, request=None, *, mode=ConditionValidityMode.RECHECK):
    clock = foundation.FixedClock()
    clock.current = NOW
    request = request or consent_request()
    condition = consent_condition(foundation.binding(), request, checkpoint_id=foundation.ENTRY)
    if mode is ConditionValidityMode.ATTESTATION:
        condition = replace(condition, validity_mode=mode, attestation_not_after=NOW + timedelta(days=30))
    return ConsentVerifier(registry, clock).verify(request, condition=condition)


# ── the model itself ─────────────────────────────────────────────────────


def test_record_currency_boundaries():
    assert record().is_current(NOW)
    assert not record(granted_at=NOW + timedelta(seconds=1)).is_current(NOW)
    assert not record(expires_at=NOW).is_current(NOW)
    assert record(expires_at=NOW + timedelta(seconds=1)).is_current(NOW)
    assert not record(revoked_at=NOW).is_current(NOW)


def test_registry_rejects_a_reused_identifier_and_revokes_in_place():
    registry = ConsentRegistry()
    registry.record(record())
    with pytest.raises(ValueError):
        registry.record(record())
    registry.revoke("test_fixture.record", NOW)
    assert registry.get("test_fixture.record").revoked_at == NOW


def test_find_requires_subject_purpose_and_every_scope():
    registry = ConsentRegistry()
    registry.record(record())
    assert registry.find("test_fixture.subject", "test_fixture.purpose", frozenset({"test_fixture.read"}))
    assert not registry.find("test_fixture.other", "test_fixture.purpose", frozenset())
    assert not registry.find("test_fixture.subject", "test_fixture.other", frozenset())
    assert not registry.find("test_fixture.subject", "test_fixture.purpose", frozenset({"test_fixture.delete"}))


def test_consent_key_reads_principal_purpose_and_scope():
    assert consent_key(consent_request()) == (
        "test_fixture.subject", "test_fixture.purpose", frozenset({"test_fixture.read"}),
    )
    assert consent_key(consent_request(purpose=None)) is None


# ── the verifier's observations ──────────────────────────────────────────


def test_current_record_is_satisfied_with_its_identifier_as_evidence():
    registry = ConsentRegistry()
    registry.record(record())
    observed = observe(registry)
    assert observed.state is ConditionVerificationState.SATISFIED
    assert observed.evidence_refs == ("test_fixture.record",)
    assert observed.valid_until is None


@pytest.mark.parametrize("change,state,reason", [
    ({"revoked_at": NOW - timedelta(hours=1)}, ConditionVerificationState.UNSATISFIED, "consent.not_current"),
    ({"expires_at": NOW}, ConditionVerificationState.UNSATISFIED, "consent.not_current"),
    ({"scopes": frozenset({"test_fixture.write"})}, ConditionVerificationState.EVIDENCE_NOT_PRESENT,
     "consent.not_found"),
    ({"purpose": "test_fixture.other"}, ConditionVerificationState.EVIDENCE_NOT_PRESENT, "consent.not_found"),
])
def test_records_that_do_not_cover_the_request(change, state, reason):
    registry = ConsentRegistry()
    registry.record(record(**change))
    observed = observe(registry)
    assert observed.state is state
    assert observed.reason_codes == (reason,)


def test_request_without_purpose_is_invalid_evidence():
    observed = observe(ConsentRegistry(), consent_request(purpose=None))
    assert observed.state is ConditionVerificationState.EVIDENCE_INVALID
    assert observed.reason_codes == ("consent.request_incomplete",)


def test_attestation_is_bounded_by_the_record_and_confirmed_while_current():
    registry = ConsentRegistry()
    registry.record(record(expires_at=NOW + timedelta(days=2)))
    observed = observe(registry, mode=ConditionValidityMode.ATTESTATION)
    assert observed.valid_until == NOW + timedelta(days=2)
    clock = foundation.FixedClock()
    clock.current = NOW
    verifier = ConsentVerifier(registry, clock)
    assert verifier.confirm(observed, request=consent_request()) is True
    registry.revoke("test_fixture.record", NOW)
    assert verifier.confirm(observed, request=consent_request()) is False


# ── inside authorization, with synthetic review and durability ──────────


def test_a_current_consent_lets_the_attempt_reach_a_receipt():
    harness = Harness()
    harness.registry.record(record())
    outcome = harness.issuer.authorize(consent_request())
    assert outcome.decision.verdict is AuthorizationVerdict.GRANT
    assert outcome.receipt is not None
    harness.issuer.verify_and_consume(outcome.receipt, consent_request())
    assert harness.store.consumed == 1


@pytest.mark.parametrize("registered,code", [
    (None, "authorization.condition_evidence_not_present"),
    ({"revoked_at": NOW - timedelta(hours=1)}, "authorization.condition_unsatisfied"),
])
def test_a_missing_or_revoked_consent_blocks_the_attempt(registered, code):
    harness = Harness()
    if registered is not None:
        harness.registry.record(record(**registered))
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(consent_request())
    assert caught.value.reason_code == code
    assert caught.value.failed_condition == ("test_fixture.policy", "1", "consent.required")
    assert harness.store.issued == 0
