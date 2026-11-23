"""Clock, guard, capacity and backend faults around the private handoff.

Nothing here claims network atomicity, forced interruption of Python or a
rollback of an uncertain backend write. Each case records what the backend
may already have done and checks that no usable authority survives.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyDecision,
    PolicyOrigin,
    PolicySelection,
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    StrictJSONAuthorizationFingerprint,
)
from sett.exceptions import SETTPolicyConfigurationError
from sett.policy_ruler import PolicyRuler
from sett.policy_ruler._handoff import PRODUCED_SLOT_NAMES, VerifiedStateSlot

ENTRY = "test_fixture.entry"
EFFECT = "test_fixture.effect"
SECRET = "test_fixture.secret-value"


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        return self.current


class NaiveClock:
    def now(self) -> datetime:
        # A deliberately naive reading: the boundary must reject it.
        return datetime(2026, 8, 30, 12, 0)  # noqa: DTZ001


class ExplodingClock:
    def now(self) -> datetime:
        raise RuntimeError(SECRET)


class RegressingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        if self.calls > 3:
            return self.current - timedelta(hours=1)
        return self.current


def binding(policy_id: str = "test_fixture.policy", version: str = "1") -> PolicyBinding:
    return PolicyBinding(
        policy_id=policy_id,
        policy_version=version,
        contract_version="2.0",
        origins=[
            PolicyOrigin(
                requirement_id="test_fixture.requirement",
                requirement_revision="1",
                layer_id="test_fixture.layer",
                source=None,
                source_absence_reason="Synthetic test fixture",
            )
        ],
        profile_id=None,
        profile_revision=None,
    )


class Resolver:
    resolver_id = "test_fixture.resolver"

    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        item = binding()
        return PolicySelection(
            selection_id="test_fixture.selection",
            selection_revision="1",
            contract_version="2.0",
            authority_ref="test_fixture.authority",
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id,
            action_type=request.action_type,
            required=[item],
            informative=[],
            applicability=[
                PolicyApplicability(
                    policy_id=item.policy_id,
                    policy_version=item.policy_version,
                    state=PolicyApplicabilityState.APPLIES,
                    justification="Synthetic test declaration",
                    fact_names=[],
                    evidence_refs=[],
                )
            ],
            facts={},
        )


class SyntheticStateProducer:
    """A test-only producer for a slot whose real adapter is not built."""

    schema_revision = "1"

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self.producer_id = "test_fixture.producer." + slot

    def produce(self, context: Any) -> VerifiedStateSlot:
        value_bytes = b"{}"
        if self.slot == "conditions":
            value_bytes = canonical_json_bytes({
                "inventory": json.loads(context.condition_inventory),
                "observations": {},
                "test_fixture_value_hex": value_bytes.hex(),
            }, field="synthetic condition state")
        return VerifiedStateSlot(
            slot=self.slot,
            producer_id=self.producer_id,
            schema_revision=self.schema_revision,
            epoch=0,
            action_id=context.request.action_id,
            checkpoint_id=context.checkpoint_id,
            required_keys=(),
            observed_keys=(),
            value_bytes=value_bytes,
            evidence_refs=(),
            valid_until=None,
        )

    def confirm(self, slot: VerifiedStateSlot) -> bool:
        return True


class ExplodingProducer(SyntheticStateProducer):
    def produce(self, context: Any) -> VerifiedStateSlot:
        raise RuntimeError(SECRET)


class StaticPolicy:
    policy_id = "test_fixture.policy"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, request: Any, verified_approvals: Any) -> Any:
        self.calls += 1
        return PolicyDecision.grant(self.policy_id, conditions=(), review_obligations=())


class ExplodingPolicy:
    policy_id = "test_fixture.policy"

    def evaluate(self, request: Any, verified_approvals: Any) -> Any:
        raise RuntimeError(SECRET)


class WrongResultPolicy:
    policy_id = "test_fixture.policy"

    def evaluate(self, request: Any, verified_approvals: Any) -> Any:
        return object()


class OtherIdentityPolicy:
    policy_id = "test_fixture.policy"

    def evaluate(self, request: Any, verified_approvals: Any) -> Any:
        return PolicyDecision.grant("test_fixture.other", conditions=(), review_obligations=())


class NeutralVerifier:
    verifier_id = "test_fixture.verifier"

    def verify(self, approval_input: Any, request: Any) -> Any:
        return None


class HookedFingerprint:
    """A fingerprint callback that can run one action once, as callbacks may."""

    policy_id = "test_fixture.fingerprint"

    def __init__(self) -> None:
        self.action: Any = None

    def fingerprint(self, request: Any, verified_approvals: Any) -> str:
        action, self.action = self.action, None
        if action is not None:
            action()
        return "test_fixture.constant"


class FaultyStore(InMemoryAuthorizationReceiptStore):
    """A backend that can fail, swallow a rejection or move the clock."""

    def __init__(self) -> None:
        super().__init__()
        self.on_issue: Any = None
        self.on_consume: Any = None
        self.issued = 0
        self.consumed = 0
        self.wrote = False

    def issue(self, receipt: Any) -> None:
        self.issued += 1
        if self.on_issue is not None:
            self.on_issue(receipt, self)
            return
        self.wrote = True
        super().issue(receipt)

    def verify_and_consume(self, receipt: Any, **kwargs: Any) -> None:
        super().verify_and_consume(receipt, **kwargs)
        self.consumed += 1
        if self.on_consume is not None:
            self.on_consume(receipt, self)


class Harness:
    def __init__(
        self,
        *,
        clock: Any = None,
        capacity: int = 16,
        evaluation_ttl: float = 3_600,
        producers: tuple[str, ...] = PRODUCED_SLOT_NAMES,
        producer_factory: Any = SyntheticStateProducer,
        policy: Any = None,
        fingerprint: Any = None,
    ) -> None:
        self.clock = clock or FixedClock()
        self.evidence = InMemoryAuthorizationEvidence()
        self.store = FaultyStore()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.policy_ruler",
            entry_checkpoint_id=ENTRY,
            evaluation_ttl_seconds=evaluation_ttl,
            max_live_evaluations=capacity,
            clock=self.clock,
            evidence_sink=self.evidence,
            selection_resolver=Resolver(),
        )
        for name in producers:
            self.owner._register_state_producer(producer_factory(name))
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer",
            receipt_ttl_seconds=3_600,
            fingerprint_policy=fingerprint or StrictJSONAuthorizationFingerprint(),
            receipt_store=self.store,
            evidence_sink=self.evidence,
            clock=self.clock,
            policy_ruler=self.owner,
        )
        self.policy = policy or StaticPolicy()
        self.issuer.register_policy(EFFECT, self.policy, binding=binding())


def request(action_id: str = "test_fixture.action") -> AuthorizationRequest:
    return AuthorizationRequest(
        action_id=action_id,
        trace_id="test_fixture.trace",
        action_type=EFFECT,
        payload={"value": SECRET},
        principal=FieldValue(
            "test_fixture.subject",
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
    )


def reason(raised: Any) -> str:
    return raised.value.reason_code


@pytest.mark.parametrize("mode", ["single", "controlled"])
@pytest.mark.parametrize("phase", ["before", "after", "uncertain"])
def test_consumption_backend_failures_are_terminal_without_retry(
    mode: str, phase: str,
) -> None:
    from sett.authorization_ruler import ExecutionMode, ReceiptUse

    class TestFixtureStore(InMemoryAuthorizationReceiptStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.observed_consumptions = 0
            self.reported_completion = "not_started"

        def verify_and_consume(self, receipt: Any, **kwargs: Any) -> None:
            self.calls += 1
            if phase == "before":
                raise RuntimeError(SECRET)
            super().verify_and_consume(receipt, **kwargs)
            self.observed_consumptions += 1
            self.reported_completion = "observed" if phase == "after" else "unknown_to_caller"
            raise RuntimeError(SECRET)

    class TestFixturePolicy:
        policy_id = "test_fixture.policy"

        def evaluate(self, item: Any, verified: Any) -> PolicyDecision:
            return PolicyDecision.grant(
                self.policy_id,
                receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY if mode == "controlled" else ReceiptUse.SINGLE_USE,
                conditions=(),
                review_obligations=(),
            )

    harness = Harness(policy=TestFixturePolicy())
    store = TestFixtureStore()
    harness.issuer._receipt_store = store
    item = request()
    if mode == "controlled":
        item = type(item)(
            action_id=item.action_id, action_type=item.action_type, trace_id=item.trace_id,
            payload=item.payload_copy(), principal=item.principal,
            execution_mode=ExecutionMode.CONTROLLED, idempotency_key="test_fixture.operation",
        )
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)
    assert reason(raised) == "authorization.receipt_store_failed"
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert SECRET not in str(raised.value)
    assert store.calls == 1
    assert store.observed_consumptions == (0 if phase == "before" else 1)
    assert store.reported_completion == {
        "before": "not_started", "after": "observed", "uncertain": "unknown_to_caller",
    }[phase]
    assert all(record.lifecycle == "terminal" for record in harness.owner._live.values())
    with pytest.raises(SETTAuthorizationError) as repeated:
        harness.issuer.verify_and_consume(receipt, item)
    assert reason(repeated) == "authorization.policy_binding_unavailable"
    assert store.calls == 1


@pytest.mark.parametrize("mode", ["single", "controlled"])
def test_structured_consumption_error_keeps_its_diagnostic(mode: str) -> None:
    from sett.authorization_ruler import ExecutionMode, ReceiptUse

    harness = Harness()
    if mode == "controlled":
        harness.policy.evaluate = lambda item, approvals: PolicyDecision.grant(
            "test_fixture.policy", receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
            conditions=(),
            review_obligations=(),
        )
    item = request()
    if mode == "controlled":
        item = type(item)(action_id=item.action_id, action_type=item.action_type,
                          trace_id=item.trace_id, payload=item.payload_copy(), principal=item.principal,
                          execution_mode=ExecutionMode.CONTROLLED, idempotency_key="test_fixture.operation")
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    failure = SETTAuthorizationError("Synthetic rejection.", reason_code="authorization.receipt_revoked")

    def reject(consumed: Any, store: Any) -> None:
        raise failure

    harness.store.on_consume = reject
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)
    assert raised.value is failure
    assert harness.store.consumed == 1
    with pytest.raises(SETTAuthorizationError) as repeated:
        harness.issuer.verify_and_consume(receipt, item)
    assert reason(repeated) == "authorization.policy_binding_unavailable"
    assert harness.store.consumed == 1


# ── the clock is never a source of authority ─────────────────────────────


@pytest.mark.parametrize("clock", [NaiveClock(), ExplodingClock()])
def test_an_unusable_clock_blocks_the_policy_boundary(clock: Any) -> None:
    owner = PolicyRuler(
        ruler_id="test_fixture.policy_ruler",
        entry_checkpoint_id=ENTRY,
        evaluation_ttl_seconds=60,
        max_live_evaluations=4,
        clock=clock,
        evidence_sink=InMemoryAuthorizationEvidence(),
        selection_resolver=Resolver(),
    )
    owner.register_policy(EFFECT, StaticPolicy(), binding=binding())

    with pytest.raises(SETTPolicyConfigurationError) as raised:
        owner.evaluate(
            request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
        )

    assert reason(raised) == "policy.clock_invalid"
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_an_unusable_clock_is_translated_at_authorization() -> None:
    harness = Harness(clock=NaiveClock())

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.clock_invalid"


def test_a_clock_regression_blocks_without_extending_validity() -> None:
    harness = Harness(clock=RegressingClock(), evaluation_ttl=60)

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) in {
        "authorization.policy_evaluation_expired",
        "authorization.clock_invalid",
    }
    assert harness.store.wrote is False


def test_reading_the_clock_schedules_no_work() -> None:
    harness = Harness()
    reads = harness.clock.reads
    harness.clock.current += timedelta(hours=5)

    assert harness.policy.calls == 0
    assert harness.store.issued == 0
    assert harness.clock.reads == reads


# ── the guard and poisoned reentry ───────────────────────────────────────


def test_a_reentrant_registration_is_rejected_and_poisons_the_transition() -> None:
    harness = Harness()

    def reenter(receipt: Any, store: FaultyStore) -> None:
        harness.owner.register_policy(
            "test_fixture.other",
            StaticPolicy(),
            binding=binding("test_fixture.policy"),
        )

    harness.store.on_issue = reenter

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_handoff_reentrant"
    assert harness.store.wrote is False
    assert harness.owner._live == {}


def test_a_swallowed_reentry_error_still_poisons_the_outer_transition() -> None:
    harness = Harness()

    def swallow(receipt: Any, store: FaultyStore) -> None:
        try:
            harness.owner.register_policy(
                "test_fixture.other",
                StaticPolicy(),
                binding=binding("test_fixture.policy"),
            )
        except SETTPolicyConfigurationError:
            pass
        store.wrote = True
        InMemoryAuthorizationReceiptStore.issue(store, receipt)

    harness.store.on_issue = swallow

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_handoff_reentrant"
    # The backend orphan exists and must not become usable authority.
    assert harness.store.wrote is True
    assert harness.owner._live == {}


def test_a_reentrant_verifier_registration_is_also_rejected() -> None:
    harness = Harness()

    def reenter(receipt: Any, store: FaultyStore) -> None:
        harness.issuer.register_approval_verifier(NeutralVerifier())

    harness.store.on_issue = reenter

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_handoff_reentrant"


# ── expiry observed after the backend call ───────────────────────────────


def test_expiry_during_the_backend_call_yields_no_usable_authority() -> None:
    harness = Harness(evaluation_ttl=60)

    def delay(receipt: Any, store: FaultyStore) -> None:
        store.wrote = True
        InMemoryAuthorizationReceiptStore.issue(store, receipt)
        harness.clock.current += timedelta(seconds=120)

    harness.store.on_issue = delay

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_evaluation_expired"
    assert harness.store.wrote is True
    assert harness.owner._live == {}


# ── backend failure phases ───────────────────────────────────────────────


def test_a_backend_failure_before_the_write_is_terminal() -> None:
    harness = Harness()

    def fail(receipt: Any, store: FaultyStore) -> None:
        raise RuntimeError(SECRET)

    harness.store.on_issue = fail

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.receipt_store_failed"
    assert SECRET not in str(raised.value)
    assert harness.store.wrote is False
    assert harness.owner._live == {}


def test_a_backend_failure_after_an_uncertain_write_is_not_retried() -> None:
    harness = Harness()

    def uncertain(receipt: Any, store: FaultyStore) -> None:
        store.wrote = True
        InMemoryAuthorizationReceiptStore.issue(store, receipt)
        raise RuntimeError(SECRET)

    harness.store.on_issue = uncertain

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.receipt_store_failed"
    assert harness.store.issued == 1
    assert harness.store.wrote is True
    assert harness.owner._live == {}


def test_a_structured_backend_rejection_keeps_its_own_diagnostic() -> None:
    harness = Harness()

    def duplicate(receipt: Any, store: FaultyStore) -> None:
        raise SETTAuthorizationError(
            "Receipt identity is already in use.",
            reason_code="authorization.receipt_duplicate",
            receipt_id=receipt.receipt_id,
        )

    harness.store.on_issue = duplicate

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.receipt_duplicate"
    assert harness.owner._live == {}


# ── capacity blocks, never evicts ────────────────────────────────────────


def test_a_full_store_blocks_without_evicting_valid_authority() -> None:
    harness = Harness(capacity=1)
    first = harness.issuer.authorize(request()).receipt
    assert first is not None

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request("test_fixture.second"))

    assert reason(raised) == "authorization.policy_evaluation_capacity_exceeded"
    assert harness.store.issued == 1

    harness.issuer.verify_and_consume(first, request())
    assert harness.store.consumed == 1


def test_expired_records_are_purged_before_new_capacity_is_refused() -> None:
    harness = Harness(capacity=1, evaluation_ttl=60)
    first = harness.issuer.authorize(request()).receipt
    assert first is not None
    harness.clock.current += timedelta(seconds=120)

    second = harness.issuer.authorize(request("test_fixture.second")).receipt

    assert second is not None
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(first, request())
    assert reason(raised) == "authorization.policy_binding_unavailable"


def test_the_policy_capacity_failure_is_visible_at_its_own_boundary() -> None:
    harness = Harness(capacity=1)
    harness.issuer.authorize(request())

    with pytest.raises(SETTPolicyConfigurationError) as raised:
        harness.owner._open_invocation(
            harness.issuer._link, request=request(), checkpoint_id=ENTRY
        )

    assert reason(raised) == "policy.evaluation_capacity_exceeded"


# ── preserved codes, no leaked text ──────────────────────────────────────


@pytest.mark.parametrize(
    ("policy", "code"),
    [
        (ExplodingPolicy(), "authorization.policy_failed"),
        (WrongResultPolicy(), "authorization.policy_result_invalid"),
        (OtherIdentityPolicy(), "authorization.policy_identity_mismatch"),
    ],
)
def test_callback_failures_keep_their_codes_without_leaking(
    policy: Any, code: str
) -> None:
    harness = Harness(policy=policy)

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == code
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.store.issued == 0


def test_a_failing_producer_never_exposes_its_exception() -> None:
    harness = Harness(producer_factory=ExplodingProducer)

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_evaluation_incomplete"
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_new_diagnostics_carry_no_binding_content_in_evidence() -> None:
    harness = Harness(producers=())

    with pytest.raises(SETTAuthorizationError):
        harness.issuer.authorize(request())

    exported = repr(
        [
            (event.kind, event.reason_codes, event.attributes)
            for event in harness.evidence.events()
        ]
    )
    assert SECRET not in exported
    assert "test_fixture.authority" not in exported


# ── the receiver generation must be current at the final check ───────────


class NeutralVerifierFactory:
    """Build distinct neutral verifiers for the regression cases."""

    def __init__(self) -> None:
        self.count = 0

    def next(self) -> Any:
        self.count += 1
        return type(
            "GeneratedVerifier",
            (),
            {
                "verifier_id": f"test_fixture.generated-{self.count}",
                "verify": lambda self, approval_input, request: None,
            },
        )()


def test_a_verifier_registered_during_the_fingerprint_callback_blocks_issue() -> None:
    """A callback may change the receiver after the generation was read.

    Found by an independent review: issue captured the generation before the
    application callbacks ran, so a verifier registered from inside the
    fingerprint callback advanced the real generation while the stale value
    still passed the final check.
    """
    fingerprint = HookedFingerprint()
    harness = Harness(fingerprint=fingerprint)
    factory = NeutralVerifierFactory()
    fingerprint.action = lambda: harness.issuer.register_approval_verifier(
        factory.next()
    )

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_generation_changed"
    assert harness.issuer._generation() == 1
    assert harness.store.issued == 0
    assert harness.store.wrote is False


def test_a_verifier_registered_during_the_fingerprint_callback_blocks_consume() -> None:
    """The same stale generation reached the consumption check."""
    fingerprint = HookedFingerprint()
    harness = Harness(fingerprint=fingerprint)
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    factory = NeutralVerifierFactory()
    fingerprint.action = lambda: harness.issuer.register_approval_verifier(
        factory.next()
    )

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_generation_changed"
    assert harness.issuer._generation() == 1
    assert harness.store.consumed == 0


def test_a_registered_verifier_increments_the_generation_under_the_guard() -> None:
    """The receiver mutation participates in the policy ruler's own guard.

    Same independent review, static concern: acquiring and releasing the
    guard before mutating the receiver under a different lock leaves a
    window in which the increment is not serialized with the final
    transition. A writer must not be able to commit while the guard is
    held.
    """
    harness = Harness()
    factory = NeutralVerifierFactory()
    attempted = threading.Event()
    guard = harness.owner._guard
    original_acquire = guard.acquire

    def signalling_acquire() -> None:
        attempted.set()
        original_acquire()

    phase = harness.owner._begin_critical()
    committed: list[int] = []

    def writer() -> None:
        guard.acquire = signalling_acquire  # type: ignore[method-assign]
        harness.issuer.register_approval_verifier(factory.next())
        committed.append(harness.issuer._generation())

    thread = threading.Thread(target=writer)
    thread.start()
    assert attempted.wait(timeout=5)
    generation_inside_the_guard = harness.issuer._generation()
    harness.owner._end_critical(phase)
    thread.join(timeout=5)
    guard.acquire = original_acquire  # type: ignore[method-assign]

    assert generation_inside_the_guard == 0
    assert committed == [1]


# ── a backward observation after the backend must be rejected ────────────


def test_a_clock_regression_inside_the_backend_blocks_issue() -> None:
    """Found by an independent review: only expiry was checked after the backend."""
    harness = Harness()

    def regress(receipt: Any, store: FaultyStore) -> None:
        store.wrote = True
        InMemoryAuthorizationReceiptStore.issue(store, receipt)
        harness.clock.current -= timedelta(seconds=1)

    harness.store.on_issue = regress

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.clock_invalid"
    # The backend write already happened. It stays an observed fact.
    assert harness.store.issued == 1
    assert harness.store.wrote is True
    assert harness.owner._live == {}


def test_a_clock_regression_inside_the_backend_blocks_consume() -> None:
    """The consumption path had the same gap."""
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None

    def regress(consumed: Any, store: FaultyStore) -> None:
        harness.clock.current -= timedelta(seconds=1)

    harness.store.on_consume = regress

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.clock_invalid"
    # The store already consumed. It is not undone and not retried.
    assert harness.store.consumed == 1


def test_a_regression_below_the_previous_observation_blocks_even_when_later_than_evaluation() -> None:
    """The reference is the observation taken immediately before the backend.

    The clock here moves back to an instant that is still later than the
    retained evaluation time, so only a comparison against the pre-backend
    reading can catch it.
    """
    harness = Harness(evaluation_ttl=3_600)
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    evaluated_at = harness.clock.current
    harness.clock.current += timedelta(seconds=10)

    def regress(consumed: Any, store: FaultyStore) -> None:
        harness.clock.current -= timedelta(seconds=5)
        assert harness.clock.current > evaluated_at

    harness.store.on_consume = regress

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.clock_invalid"
    assert harness.store.consumed == 1


# ── the full request is part of the pre-issue order ──────────────────────


def other_payload_request(action_id: str = "test_fixture.action") -> Any:
    """Same action and type as `request()`, with different payload bytes.

    The resolver keys its selection on the action identity, so this differs
    from `request()` only in the canonical request view. It isolates the
    request comparison from the selection one.
    """
    return AuthorizationRequest(
        action_id=action_id,
        trace_id="test_fixture.trace",
        action_type=EFFECT,
        payload={"value": "test_fixture.other-value"},
        principal=FieldValue(
            "test_fixture.subject",
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
    )


def test_a_request_the_invocation_never_opened_cannot_reach_the_store() -> None:
    """The receiver order before issue names the full request before the
    selection. Driving the issue stage with another request must be
    rejected, whatever the fingerprint callback returns for it.
    """
    harness = Harness()
    invocation = harness.owner._open_invocation(
        harness.issuer._link, request=request(), checkpoint_id=ENTRY
    )
    evaluation = harness.owner._evaluate_bound(
        invocation,
        verified_approvals=frozenset(),
        receiver_generation=harness.issuer._generation(),
    )

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer._issue(
            invocation,
            evaluation,
            other_payload_request(),
            frozenset(),
            evaluation.decision,
        )

    assert reason(raised) == "authorization.request_mismatch"
    assert harness.store.issued == 0


# ── callbacks, other threads and the clock ───────────────────────────────


class _LateVerifier:
    verifier_id = "test_fixture.late_verifier"

    def verify(self, approval_input: Any, item: Any) -> Any:
        return None


def test_another_thread_registering_during_a_callback_invalidates_it() -> None:
    """The fingerprint callback runs outside the guard. A second thread that
    registers a verifier while it is running advances the receiver
    generation, and the guarded check reads the live value, not the one
    captured before the callback.

    The threads rendezvous on events: a pause would not establish an order.
    """
    harness = Harness(fingerprint=HookedFingerprint())
    inside = threading.Event()
    registered = threading.Event()

    def writer() -> None:
        assert inside.wait(timeout=5)
        harness.issuer.register_approval_verifier(_LateVerifier())
        registered.set()

    def hook() -> None:
        inside.set()
        assert registered.wait(timeout=5)

    harness.issuer._fingerprint_policy.action = hook
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        with pytest.raises(SETTAuthorizationError) as raised:
            harness.issuer.authorize(request())
    finally:
        thread.join(timeout=5)

    assert reason(raised) == "authorization.policy_generation_changed"
    assert harness.store.issued == 0


class _ReenteringClock:
    """A clock that breaks its own integration constraint exactly once.

    The contract requires a read-only, nonblocking observation with no SETT
    reentry. This one attempts a configuration change from inside the
    guarded section and swallows the rejection, which is the case the
    poisoned-transition rule exists for.
    """

    def __init__(self, harness: Any) -> None:
        self.harness = harness
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        self.attempts = 0

    def now(self) -> datetime:
        if self.attempts == 0 and self.harness.owner._guard._phase is not None:
            self.attempts += 1
            try:
                self.harness.issuer.register_approval_verifier(_LateVerifier())
            except SETTPolicyConfigurationError:
                pass
        return self.current


def test_a_clock_that_reenters_the_boundary_returns_no_usable_authority() -> None:
    """The reentry is rejected before it mutates anything, and the outer
    transition stays poisoned even though the clock caught the rejection.
    Whatever the backend already wrote is recorded, not denied.
    """
    harness = Harness()
    clock = _ReenteringClock(harness)
    harness.issuer._clock = clock

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_handoff_reentrant"
    assert clock.attempts == 1
    assert harness.store.issued == 1


def test_a_producer_that_changes_configuration_before_consumption() -> None:
    """Verified-state producers run outside the guard at consumption. One
    that registers a verifier advances the receiver generation, which the
    guarded check must observe before the store consumes anything.
    """
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    producer = harness.owner._state_producers["conditions"]
    original = producer.produce

    def mutate(context: Any) -> Any:
        producer.produce = original
        harness.issuer.register_approval_verifier(_LateVerifier())
        return original(context)

    producer.produce = mutate

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_generation_changed"
    assert harness.store.consumed == 0


# ── reservations, receivers and single use ───────────────────────────────


class _ExplodingFingerprint:
    policy_id = "test_fixture.fingerprint"

    def fingerprint(self, item: Any, verified_approvals: Any) -> str:
        raise RuntimeError(SECRET)


def test_a_stage_that_fails_releases_the_capacity_it_reserved() -> None:
    """A failed stage releases its reservation without granting. Otherwise
    one failure would consume a live slot for the rest of the process.
    """
    harness = Harness(capacity=1)
    harness.issuer._fingerprint_policy = _ExplodingFingerprint()

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())
    assert reason(raised) == "authorization.fingerprint_failed"

    harness.issuer._fingerprint_policy = StrictJSONAuthorizationFingerprint()
    assert harness.issuer.authorize(request()).receipt is not None


def test_a_sibling_receiver_cannot_consume_through_the_public_path() -> None:
    """The binding names the exact receiving ruler. A sibling sharing the
    same PolicyRuler and the same store finds no binding of its own.
    """
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    sibling = AuthorizationRuler(
        issuer_id="test_fixture.other_issuer",
        receipt_ttl_seconds=3_600,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=harness.store,
        evidence_sink=harness.evidence,
        clock=harness.clock,
        policy_ruler=harness.owner,
    )

    with pytest.raises(SETTAuthorizationError) as raised:
        sibling.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert harness.store.consumed == 0


def test_a_single_use_binding_is_terminal_after_one_consumption() -> None:
    """A successful single-use consumption terminates the binding before an
    effect can run, so a second attempt finds nothing to validate.
    """
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    harness.issuer.verify_and_consume(receipt, item)

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert harness.store.consumed == 1


# Residual tests preserve the retained-evaluation reference; no global history.
@pytest.mark.parametrize("stage", ["claim", "consume"])
@pytest.mark.parametrize("regress", [False, True], ids=["stable", "regressed"])
def test_residual_regression_before_retained_evaluation_blocks(monkeypatch: Any, stage: str, regress: bool) -> None:
    """Regress only after a real evaluation was retained."""
    harness = Harness()
    item = request()
    retained = []
    evaluate = harness.owner._evaluate_bound

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = evaluate(*args, **kwargs)
        record = harness.owner._live[args[0]]
        retained.append((record, result.evaluated_at, result.valid_until))
        if stage == "claim" and regress:
            harness.clock.current = result.evaluated_at - timedelta(microseconds=1)
        return result

    monkeypatch.setattr(harness.owner, "_evaluate_bound", observe)
    receipt = harness.issuer.authorize(item).receipt if stage == "consume" else None
    if stage == "consume" and regress:
        harness.clock.current = retained[0][1] - timedelta(microseconds=1)
    if regress:
        with pytest.raises(SETTAuthorizationError) as raised:
            if stage == "claim":
                harness.issuer.authorize(item)
            else:
                harness.issuer.verify_and_consume(receipt, item)
        assert reason(raised) == "authorization.clock_invalid"
        assert harness.store.issued == (1 if stage == "consume" else 0)
        assert harness.store.consumed == 0
    else:
        if stage == "claim":
            receipt = harness.issuer.authorize(item).receipt
        assert receipt is not None
        harness.issuer.verify_and_consume(receipt, item)
        assert harness.store.issued == harness.store.consumed == 1
    assert len(retained) == 1
    record, evaluated_at, valid_until = retained[0]
    assert record.evaluated_at == evaluated_at
    assert record.valid_until == valid_until


class ResidualUnrenderableValue:
    def __init__(self) -> None:
        self.repr_calls = 0
        self.str_calls = 0

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise RuntimeError("test_fixture.render_failure")

    def __str__(self) -> str:
        self.str_calls += 1
        raise RuntimeError("test_fixture.render_failure")


class ResidualUnrenderableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("test_fixture.private_marker")
        self.repr_calls = 0
        self.str_calls = 0

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise RuntimeError("test_fixture.render_failure")

    def __str__(self) -> str:
        self.str_calls += 1
        raise RuntimeError("test_fixture.render_failure")


def residual_assert_private_diagnostic(error: Any, code: str, message: str, evidence: Any) -> None:
    assert error.reason_code == code
    assert str(error) == message
    assert error.__cause__ is None and error.__context__ is None
    public = repr([(event.kind, event.reason_codes, event.attributes) for event in evidence.events()])
    for marker in ("test_fixture.private_marker", "test_fixture.render_failure", SECRET, "test_fixture.authority"):
        assert marker not in str(error) and marker not in public


@pytest.mark.parametrize("unrenderable", [False, True], ids=["printable", "failing-repr"])
def test_residual_rejected_input_does_not_render_failing_repr(unrenderable: bool) -> None:
    """Malformed new policy input rejected before rendering."""
    harness = Harness()
    value = ResidualUnrenderableValue() if unrenderable else "test_fixture.private_marker"
    with pytest.raises(SETTPolicyConfigurationError) as raised:
        harness.owner.evaluate(value, verified_approvals=frozenset(), checkpoint_id=ENTRY)
    residual_assert_private_diagnostic(raised.value, "policy.evaluation_input_invalid",
        "Policy evaluation could not establish a valid binding.", harness.evidence)
    assert harness.store.issued == harness.policy.calls == 0 and not harness.owner._live
    if unrenderable:
        assert value.repr_calls == value.str_calls == 0
    observation = harness.owner.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert observation.verification_ref is None and not harness.owner._live


@pytest.mark.parametrize("unrenderable", [False, True], ids=["printable", "failing-repr"])
def test_residual_rejected_state_does_not_render_failing_repr(unrenderable: bool) -> None:
    """Invalid output traverses real synthetic producer collection."""
    value = ResidualUnrenderableValue() if unrenderable else "test_fixture.private_marker"

    class ResidualInvalidProducer(SyntheticStateProducer):
        def produce(self, context: Any) -> Any:
            return value

    harness = Harness(producer_factory=ResidualInvalidProducer)
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())
    residual_assert_private_diagnostic(raised.value, "authorization.policy_evaluation_incomplete",
        "Authorization could not validate the policy binding.", harness.evidence)
    assert harness.store.issued == 0 and not harness.owner._live
    if unrenderable:
        assert value.repr_calls == value.str_calls == 0
    control = Harness()
    assert control.issuer.authorize(request()).receipt is not None


@pytest.mark.parametrize("stage", ["private", "backend-issue", "backend-consume"])
@pytest.mark.parametrize("unrenderable", [False, True], ids=["printable", "failing-repr-str"])
def test_residual_exception_diagnostic_does_not_render_failing_repr(monkeypatch: Any, stage: str, unrenderable: bool) -> None:
    """Existing private translator and ordinary backend boundaries."""
    harness = Harness()
    error = ResidualUnrenderableError() if unrenderable else RuntimeError("test_fixture.private_marker")
    item = request()
    receipt = harness.issuer.authorize(item).receipt if stage == "backend-consume" else None

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    if stage == "private":
        monkeypatch.setattr(harness.owner, "_evaluate_bound", fail)
    elif stage == "backend-issue":
        harness.store.on_issue = fail
    else:
        harness.store.on_consume = fail
    with pytest.raises(SETTAuthorizationError) as raised:
        if stage == "backend-consume":
            harness.issuer.verify_and_consume(receipt, item)
        else:
            harness.issuer.authorize(item)
    code = "authorization.policy_evaluation_failed" if stage == "private" else "authorization.receipt_store_failed"
    message = ("Authorization could not validate the policy binding." if stage == "private"
               else "Authorization could not produce a trustworthy result.")
    residual_assert_private_diagnostic(raised.value, code, message, harness.evidence)
    assert not harness.owner._live
    assert harness.store.issued == (0 if stage == "private" else 1)
    assert harness.store.consumed == (1 if stage == "backend-consume" else 0)
    if unrenderable:
        assert error.repr_calls == error.str_calls == 0
    control = Harness()
    good = control.issuer.authorize(item).receipt
    assert good is not None
    control.issuer.verify_and_consume(good, item)
    assert control.store.consumed == 1
