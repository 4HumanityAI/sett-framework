"""The private evaluation binding between PolicyRuler and AuthorizationRuler.

These cases exercise the real receiver validation. Their state producers
are synthetic and test-only: they prove that the handoff checks work, not
that a real condition, consent, review or durability verifier exists. The
governed integration stays held until those producers are accepted with
their own contracts, and no case here reaches a real SETTExecutor effect.
"""

from __future__ import annotations

import copy
import json
import pickle
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from queue import Queue
from typing import Any

import pytest

from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
    ExecutionMode,
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
    ReceiptUse,
    SETTAuthorizationError,
    StrictJSONAuthorizationFingerprint,
)
from sett.policy_ruler import PolicyRuler
from sett.policy_ruler._handoff import (
    PRODUCED_SLOT_NAMES,
    VerifiedStateSlot,
    canonical_request_bytes,
)
from sett.policy_ruler.evaluation import PolicyEvaluation

ENTRY = "test_fixture.entry"
EFFECT = "test_fixture.effect"


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current


class ConstantFingerprint:
    """A deliberately permissive application fingerprint.

    It normalizes every request to one value, so the store alone could not
    tell two requests apart. The private canonical bytes must.
    """

    policy_id = "test_fixture.fingerprint"

    def fingerprint(self, request: Any, verified_approvals: Any) -> str:
        return "test_fixture.constant"


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

    def __init__(self) -> None:
        self.revision = "1"
        self.authority_ref = "test_fixture.authority"
        self.required = [binding()]

    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        return PolicySelection(
            selection_id="test_fixture.selection",
            selection_revision=self.revision,
            contract_version="2.0",
            authority_ref=self.authority_ref,
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id,
            action_type=request.action_type,
            required=list(self.required),
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
                for item in self.required
            ],
            facts={},
        )


class SyntheticStateProducer:
    """A test-only producer for a slot whose real adapter is not built."""

    schema_revision = "1"

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self.producer_id = "test_fixture.producer." + slot
        self.epoch = 0
        self.value = b"{}"
        self.valid_until: datetime | None = None
        self.current = True

    def produce(self, context: Any) -> VerifiedStateSlot:
        value_bytes = self.value
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
            epoch=self.epoch,
            action_id=context.request.action_id,
            checkpoint_id=context.checkpoint_id,
            required_keys=(),
            observed_keys=(),
            value_bytes=value_bytes,
            evidence_refs=("test_fixture.evidence",),
            valid_until=self.valid_until,
        )

    def confirm(self, slot: VerifiedStateSlot) -> bool:
        return self.current


class CountingStore(InMemoryAuthorizationReceiptStore):
    """The reference store with issue and consume counters."""

    def __init__(self) -> None:
        super().__init__()
        self.issued = 0
        self.consumed = 0

    def issue(self, receipt: Any) -> None:
        self.issued += 1
        super().issue(receipt)

    def verify_and_consume(self, receipt: Any, **kwargs: Any) -> None:
        super().verify_and_consume(receipt, **kwargs)
        self.consumed += 1


class StaticPolicy:
    def __init__(
        self,
        policy_id: str = "test_fixture.policy",
        decision: PolicyDecision | None = None,
    ) -> None:
        self.policy_id = policy_id
        self._decision = decision
        self.calls = 0

    def evaluate(self, request: Any, verified_approvals: Any) -> Any:
        self.calls += 1
        if self._decision is None:
            return PolicyDecision.grant(self.policy_id, conditions=(), review_obligations=())
        return self._decision


class Harness:
    """One configured policy owner, issuer, store and producer set."""

    def __init__(
        self,
        *,
        producers: tuple[str, ...] = PRODUCED_SLOT_NAMES,
        fingerprint: Any = None,
        capacity: int = 16,
        evaluation_ttl: float = 3_600,
        receipt_ttl: float = 3_600,
        decision: PolicyDecision | None = None,
    ) -> None:
        self.clock = FixedClock()
        self.evidence = InMemoryAuthorizationEvidence()
        self.resolver = Resolver()
        self.store = CountingStore()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.policy_ruler",
            entry_checkpoint_id=ENTRY,
            evaluation_ttl_seconds=evaluation_ttl,
            max_live_evaluations=capacity,
            clock=self.clock,
            evidence_sink=self.evidence,
            selection_resolver=self.resolver,
        )
        self.producers = {name: SyntheticStateProducer(name) for name in producers}
        for producer in self.producers.values():
            self.owner._register_state_producer(producer)
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer",
            receipt_ttl_seconds=receipt_ttl,
            fingerprint_policy=fingerprint or StrictJSONAuthorizationFingerprint(),
            receipt_store=self.store,
            evidence_sink=self.evidence,
            clock=self.clock,
            policy_ruler=self.owner,
        )
        self.policy = StaticPolicy(decision=decision)
        self.issuer.register_policy(EFFECT, self.policy, binding=binding())
        self.effects = 0

    def effect(self) -> None:
        self.effects += 1


def request(
    *,
    action_id: str = "test_fixture.action",
    trace_id: str = "test_fixture.trace",
    payload: dict[str, Any] | None = None,
    principal: Any = "test_fixture.subject",
    mode: ExecutionMode = ExecutionMode.PLAIN,
    idempotency_key: str | None = None,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        action_id=action_id,
        trace_id=trace_id,
        action_type=EFFECT,
        payload=payload if payload is not None else {"value": 1},
        principal=FieldValue(
            principal,
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
        execution_mode=mode,
        idempotency_key=idempotency_key,
    )


def reason(raised: Any) -> str:
    return raised.value.reason_code


# Complete request distinctions at the actual pre-issue boundary.
@pytest.mark.parametrize(
    "dimension,before,after",
    [
        ("payload", {"value": True}, {"value": 1}),
        ("payload", {"value": 1}, {"value": 1.0}),
        ("payload", {"value": 0.0}, {"value": -0.0}),
        ("payload", {"value": "\u00e9"}, {"value": "e\u0301"}),
        ("trace_id", "test_fixture.trace.one", "test_fixture.trace.two"),
        ("principal", "test_fixture.subject.one", "test_fixture.subject.two"),
        ("resource", "test_fixture.resource.one", "test_fixture.resource.two"),
        ("purpose", "test_fixture.purpose.one", "test_fixture.purpose.two"),
        ("scope", "test_fixture.scope.one", "test_fixture.scope.two"),
        ("context_facts", "test_fixture.fact.one", "test_fixture.fact.two"),
        ("idempotency_key", "test_fixture.operation.one", "test_fixture.operation.two"),
        ("payload", {"value": 1}, {"value": 2}),
    ],
    ids=["bool-int", "int-float", "signed-zero", "unicode", "trace", "principal",
         "resource", "purpose", "scope", "facts", "idempotency", "payload"],
)
def test_complete_request_distinctions_before_issue(
    dimension: str, before: Any, after: Any,
) -> None:
    def item(value: Any) -> AuthorizationRequest:
        arguments: dict[str, Any] = {
            "action_id": "test_fixture.action", "action_type": EFFECT,
            "trace_id": "test_fixture.trace", "payload": {"value": 1},
            "principal": FieldValue("test_fixture.subject", source_id="test_fixture.source",
                                    trust=FieldTrust.AUTHORITATIVE),
        }
        if dimension in {"principal", "resource", "purpose", "scope", "context_facts"}:
            field = FieldValue(value, source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE)
            arguments[dimension] = [field] if dimension == "scope" else (
                {"test_fixture.fact": field} if dimension == "context_facts" else field
            )
        else:
            arguments[dimension] = value
        return AuthorizationRequest(**arguments)

    original, changed = item(before), item(after)
    harness = Harness(fingerprint=ConstantFingerprint())
    assert harness.issuer._fingerprint(original, frozenset()) == harness.issuer._fingerprint(changed, frozenset())
    assert canonical_request_bytes(original, frozenset()) != canonical_request_bytes(changed, frozenset())
    invocation, evaluation = bound_evaluation(harness, original)
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer._issue(invocation, evaluation, changed, frozenset(), evaluation.decision)
    assert reason(raised) == "authorization.request_mismatch"
    assert harness.store.issued == harness.store.consumed == harness.effects == 0


# Manufactured observations are private fault injections, not public APIs.
def test_exact_type_evaluation_copy_is_unrecognized() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    forged = object.__new__(PolicyEvaluation)
    for name in PolicyEvaluation.__slots__:
        object.__setattr__(forged, name, getattr(evaluation, name))
    assert type(forged) is type(evaluation)
    assert forged is not evaluation
    assert all(getattr(forged, name) == getattr(evaluation, name) for name in PolicyEvaluation.__slots__)
    record, failure = claim(harness, invocation, forged)
    assert record is None
    assert failure == "authorization.policy_evaluation_unrecognized"
    assert harness.store.issued == harness.effects == 0
    record, failure = claim(harness, invocation, evaluation)
    assert record is not None and failure is None


def test_evaluation_subclass_is_invalid() -> None:
    class TestFixtureEvaluation(PolicyEvaluation):
        pass

    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    forged = object.__new__(TestFixtureEvaluation)
    for name in PolicyEvaluation.__slots__:
        object.__setattr__(forged, name, getattr(evaluation, name))
    record, failure = claim(harness, invocation, forged)
    assert record is None
    assert failure == "authorization.policy_evaluation_invalid"
    assert harness.store.issued == harness.effects == 0


@pytest.mark.parametrize("variant", ["wrong-type", "subclass"])
def test_authorize_rejects_invalid_bound_result(variant: str, monkeypatch: Any) -> None:
    class TestFixtureEvaluation(PolicyEvaluation):
        pass

    harness = Harness()
    original = harness.owner._evaluate_bound

    def substitute(*args: Any, **kwargs: Any) -> Any:
        evaluation = original(*args, **kwargs)
        if variant == "wrong-type":
            return object()
        forged = object.__new__(TestFixtureEvaluation)
        for name in PolicyEvaluation.__slots__:
            object.__setattr__(forged, name, getattr(evaluation, name))
        return forged

    monkeypatch.setattr(harness.owner, "_evaluate_bound", substitute)
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())
    assert reason(raised) == "authorization.policy_evaluation_invalid"
    assert harness.store.issued == harness.store.consumed == harness.effects == 0
    assert harness.owner._live == {}


def test_same_receiver_other_invocation_has_its_own_diagnostic() -> None:
    harness = Harness()
    item = request()
    first, _evaluation = bound_evaluation(harness, item)
    second, other = bound_evaluation(harness, item)
    assert first is not second and first.link is second.link
    assert harness.owner._live[first].request_bytes == harness.owner._live[second].request_bytes
    record, failure = claim(harness, first, other)
    assert record is None
    assert harness.store.issued == harness.effects == 0
    assert failure == "authorization.policy_invocation_mismatch"


@pytest.mark.parametrize("method", ["replace", "json-constructor"])
def test_public_reconstruction_helpers_cannot_construct_evaluation(method: str) -> None:
    harness = Harness()
    _invocation, evaluation = bound_evaluation(harness, request())
    with pytest.raises(TypeError):
        if method == "replace":
            replace(evaluation, evaluation_id=evaluation.evaluation_id)
        else:
            data = json.loads(json.dumps({"evaluation_id": evaluation.evaluation_id}))
            PolicyEvaluation(**data)
    assert harness.store.issued == harness.effects == 0


# The known synthetic producer's registration is the schema reference.
@pytest.mark.parametrize("slot_name", PRODUCED_SLOT_NAMES)
@pytest.mark.parametrize("fault", ["schema", "missing-key", "failed-key"])
def test_incomplete_verified_slot_never_becomes_claimable(
    slot_name: str, fault: str, monkeypatch: Any,
) -> None:
    from sett.exceptions import SETTPolicyConfigurationError

    harness = Harness()
    producer = harness.producers[slot_name]
    original = producer.produce

    def produce(context: Any) -> VerifiedStateSlot | None:
        slot = original(context)
        if fault == "schema":
            return replace(slot, schema_revision="test_fixture.unregistered_revision")
        if fault == "missing-key":
            return replace(slot, required_keys=("test_fixture.required",), observed_keys=())
        return None

    monkeypatch.setattr(producer, "produce", produce)
    invocation = harness.owner._open_invocation(harness.issuer._link, request=request(), checkpoint_id=ENTRY)
    with pytest.raises(SETTPolicyConfigurationError) as raised:
        harness.owner._evaluate_bound(invocation, verified_approvals=frozenset(), receiver_generation=0)
    assert reason(raised) == "policy.evaluation_incomplete"
    assert harness.owner._live[invocation] is None
    harness.owner._close_invocation(invocation)
    with pytest.raises(SETTAuthorizationError) as translated:
        harness.issuer.authorize(request())
    assert reason(translated) == "authorization.policy_evaluation_incomplete"
    assert harness.owner._live == {}
    assert harness.store.issued == harness.store.consumed == harness.effects == 0


@pytest.mark.parametrize("cleanup", ["expired", "terminal"])
def test_purged_evaluation_is_unrecognized_and_live_sibling_survives(cleanup: str) -> None:
    harness = Harness(capacity=2, evaluation_ttl=30, receipt_ttl=60)
    first, expired = bound_evaluation(harness, request(action_id="test_fixture.first"))
    harness.clock.current += timedelta(seconds=10)
    sibling, live = bound_evaluation(harness, request(action_id="test_fixture.sibling"))
    if cleanup == "expired":
        harness.clock.current += timedelta(seconds=20)
    else:
        harness.owner._terminate(harness.owner._live[first])
    replacement, _new = bound_evaluation(harness, request(action_id="test_fixture.new"))
    assert first not in harness.owner._live
    assert sibling in harness.owner._live and replacement in harness.owner._live
    record, failure = claim(harness, first, expired)
    assert record is None
    assert failure == "authorization.policy_evaluation_unrecognized"
    record, failure = claim(harness, sibling, live)
    assert record is not None and failure is None
    assert harness.store.issued == harness.effects == 0


# ── one complete bound handoff ───────────────────────────────────────────


def test_one_bound_handoff_issues_consumes_and_reaches_one_effect() -> None:
    harness = Harness()
    item = request()

    outcome = harness.issuer.authorize(item)
    assert outcome.decision.verdict is AuthorizationVerdict.GRANT
    assert harness.store.issued == 1
    assert harness.effects == 0

    harness.issuer.verify_and_consume(outcome.receipt, item)
    harness.effect()

    assert harness.store.consumed == 1
    assert harness.effects == 1
    assert harness.policy.calls == 1


def copied_receipt(receipt: Any, method: str) -> Any:
    """Test-only copy adapters, not a SETT serialization or transport API."""
    if method == "shallow":
        return copy.copy(receipt)
    if method == "deep":
        return copy.deepcopy(receipt)
    if method == "replace":
        return replace(receipt)
    if method == "pickle":
        # Decode only bytes just produced from this synthetic receipt.
        return pickle.loads(pickle.dumps(receipt))

    def encode(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        raise TypeError(type(value).__name__)

    fields = json.loads(json.dumps(asdict(receipt), default=encode))
    for name in ("issued_at", "expires_at"):
        fields[name] = datetime.fromisoformat(fields[name])
    fields["execution_mode"] = ExecutionMode(fields["execution_mode"])
    fields["receipt_use"] = ReceiptUse(fields["receipt_use"])
    return type(receipt)(**fields)


@pytest.mark.parametrize("method", ["shallow", "deep", "replace", "json", "pickle"])
@pytest.mark.parametrize("replay", [False, True])
def test_equal_copy_cannot_use_the_original_objects_live_binding(
    method: str, replay: bool,
) -> None:
    """By a later maintainer decision, the issuer DOES retain the original binding."""
    harness = Harness(decision=PolicyDecision.grant(
        "test_fixture.policy", receipt_use=(
            ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY if replay else ReceiptUse.SINGLE_USE
        ),
        conditions=(),
        review_obligations=(),
    ))
    item = request(
        mode=ExecutionMode.CONTROLLED if replay else ExecutionMode.PLAIN,
        idempotency_key="test_fixture.operation" if replay else None,
    )
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    rebuilt = copied_receipt(receipt, method)
    assert rebuilt == receipt
    assert rebuilt is not receipt
    record = next(iter(harness.owner._live.values()))
    assert record.receipt is receipt
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(rebuilt, item)
    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert harness.store.consumed == 0
    assert next(iter(harness.owner._live.values())) is record

    # Rejecting a copy neither transfers nor destroys the original association.
    harness.issuer.verify_and_consume(receipt, item)
    assert harness.store.consumed == 1
    if replay:
        with pytest.raises(SETTAuthorizationError) as repeated_copy:
            harness.issuer.verify_and_consume(rebuilt, item)
        assert reason(repeated_copy) == "authorization.policy_binding_unavailable"
        assert harness.store.consumed == 1
        harness.issuer.verify_and_consume(receipt, item)
        assert harness.store.consumed == 2


@pytest.mark.parametrize("transfer", ["direct", "alias", "reference_queue"])
def test_original_reference_can_still_be_consumed(transfer: str) -> None:
    """Same-object transfer before consumption is not person authentication."""
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    if transfer == "reference_queue":
        channel = Queue()
        channel.put(receipt)
        presented = channel.get_nowait()
    elif transfer == "alias":
        holder = {"test_fixture.reference": receipt}
        presented = holder["test_fixture.reference"]
    else:
        presented = receipt
    assert presented is receipt
    harness.issuer.verify_and_consume(presented, item)
    assert harness.store.consumed == 1
    assert harness.owner._live == {}


@pytest.mark.parametrize("field", ["issuer_id", "action_id", "request_fingerprint"])
def test_changed_receipt_fields_keep_the_existing_mismatch(field: str) -> None:
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    changed = replace(receipt, **{field: "test_fixture.changed"})
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(changed, item)
    assert reason(raised) == "authorization.receipt_mismatch"
    assert harness.store.consumed == 0
    harness.issuer.verify_and_consume(receipt, item)
    assert harness.store.consumed == 1


def test_backend_value_copy_does_not_replace_the_callers_receipt() -> None:
    harness = Harness()
    issue = harness.store.issue
    retained = []

    def store_copy(receipt: Any) -> None:
        copied = copy.deepcopy(receipt)
        retained.append(copied)
        issue(copied)

    harness.store.issue = store_copy
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert retained[0] == receipt
    assert retained[0] is not receipt
    harness.issuer.verify_and_consume(receipt, item)
    assert harness.store.consumed == 1


def test_original_reference_without_its_issuer_binding_still_blocks() -> None:
    first, second = Harness(), Harness()
    item = request()
    receipt = first.issuer.authorize(item).receipt
    # A backend recognizing the original object cannot restore issuer identity.
    second.issuer._receipt_store = first.store
    with pytest.raises(SETTAuthorizationError) as raised:
        second.issuer.verify_and_consume(receipt, item)
    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert first.store.consumed == 0
    first.issuer.verify_and_consume(receipt, item)
    assert first.store.consumed == 1


def test_evaluation_alone_invokes_no_effect_and_no_store_operation() -> None:
    harness = Harness()

    harness.owner.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert harness.store.issued == 0
    assert harness.store.consumed == 0
    assert harness.effects == 0


# ── the integration hold ─────────────────────────────────────────────────


@pytest.mark.parametrize("omitted", PRODUCED_SLOT_NAMES)
def test_each_missing_real_producer_blocks_on_its_own(omitted: str) -> None:
    present = tuple(name for name in PRODUCED_SLOT_NAMES if name != omitted)
    harness = Harness(producers=present)

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_evaluation_incomplete"
    assert harness.store.issued == 0
    assert harness.effects == 0


def test_a_public_grant_does_not_survive_an_incomplete_state() -> None:
    harness = Harness(producers=())

    evaluation = harness.owner.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )
    assert evaluation.decision.verdict is AuthorizationVerdict.GRANT

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_evaluation_incomplete"


# ── only the registered object is recognized ─────────────────────────────


def bound_evaluation(harness: Harness, item: AuthorizationRequest) -> Any:
    invocation = harness.owner._open_invocation(
        harness.issuer._link, request=item, checkpoint_id=ENTRY
    )
    evaluation = harness.owner._evaluate_bound(
        invocation, verified_approvals=frozenset(), receiver_generation=0
    )
    return invocation, evaluation


def claim(
    harness: Harness,
    invocation: Any,
    evaluation: Any,
    *,
    receiver: Any = None,
    state: Any = None,
    selection_bytes: Any = None,
    request_bytes: Any = None,
    now: datetime | None = None,
) -> tuple[Any, str | None]:
    # Test callbacks follow the production callback/guard boundary too.
    current_selection = (
        selection_bytes if selection_bytes is not None
        else harness.owner._current_selection(invocation.request)[1]
    )
    current_state = state if state is not None else harness.owner._current_state(
        invocation, verified_approvals=frozenset(), receiver_generation=0,
    )
    confirmed = harness.owner._confirm_invocation_state(invocation)
    phase = harness.owner._begin_critical()
    try:
        return harness.owner._validate_and_claim(
            invocation,
            evaluation,
            receiver=receiver or harness.issuer,
            request_bytes=(
                request_bytes
                if request_bytes is not None
                else canonical_request_bytes(invocation.request, frozenset())
            ),
            current_selection_bytes=current_selection,
            current_state=current_state,
            state_confirmed=confirmed,
            receiver_generation=0,
            now=now or harness.clock.current,
        )
    finally:
        harness.owner._end_critical(phase)


def test_a_foreign_or_inspection_result_is_never_recognized() -> None:
    harness = Harness()
    item = request()
    invocation, _evaluation = bound_evaluation(harness, item)
    inspection = harness.owner.evaluate(
        item, verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    record, failure = claim(harness, invocation, inspection)

    assert record is None
    assert failure == "authorization.policy_evaluation_unrecognized"


def test_a_result_from_another_ruler_is_never_recognized() -> None:
    first = Harness()
    second = Harness()
    item = request()
    invocation, _evaluation = bound_evaluation(first, item)
    _other_invocation, foreign = bound_evaluation(second, item)

    record, failure = claim(first, invocation, foreign)

    assert record is None
    assert failure == "authorization.policy_evaluation_unrecognized"


def test_a_wrong_result_type_is_rejected_before_the_ledger_lookup() -> None:
    harness = Harness()
    invocation, _evaluation = bound_evaluation(harness, request())

    record, failure = claim(harness, invocation, object())

    assert record is None
    assert failure == "authorization.policy_evaluation_invalid"


def test_another_receiver_cannot_borrow_a_registered_result() -> None:
    harness = Harness()
    other = AuthorizationRuler(
        issuer_id="test_fixture.issuer",
        receipt_ttl_seconds=3_600,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=CountingStore(),
        evidence_sink=harness.evidence,
        clock=harness.clock,
        policy_ruler=harness.owner,
    )
    invocation, evaluation = bound_evaluation(harness, request())

    record, failure = claim(harness, invocation, evaluation, receiver=other)

    assert record is None
    assert failure == "authorization.policy_invocation_mismatch"


def test_a_claimed_result_cannot_be_claimed_again() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())

    first, failure = claim(harness, invocation, evaluation)
    assert first is not None and failure is None

    second, failure = claim(harness, invocation, evaluation)

    assert second is None
    assert failure == "authorization.policy_evaluation_used"


# ── complete state before authority ──────────────────────────────────────


def test_changed_selection_content_under_one_revision_label_is_rejected() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    harness.resolver.authority_ref = "test_fixture.other_authority"

    record, failure = claim(harness, invocation, evaluation)

    assert record is None
    assert failure == "authorization.policy_selection_changed"


def test_a_registry_change_invalidates_a_pending_binding() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    harness.owner.register_policy(
        "test_fixture.other",
        StaticPolicy("test_fixture.second"),
        binding=binding("test_fixture.second"),
    )

    record, failure = claim(harness, invocation, evaluation)

    assert record is None
    assert failure == "authorization.policy_generation_changed"


def test_a_receiver_configuration_change_invalidates_its_binding() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())

    selection = harness.owner._current_selection(invocation.request)[1]
    state = harness.owner._current_state(
        invocation, verified_approvals=frozenset(), receiver_generation=0,
    )
    confirmed = harness.owner._confirm_invocation_state(invocation)
    phase = harness.owner._begin_critical()
    try:
        record, failure = harness.owner._validate_and_claim(
            invocation,
            evaluation,
            receiver=harness.issuer,
            request_bytes=canonical_request_bytes(invocation.request, frozenset()),
            current_selection_bytes=selection,
            current_state=state,
            state_confirmed=confirmed,
            receiver_generation=1,
            now=harness.clock.current,
        )
    finally:
        harness.owner._end_critical(phase)

    assert record is None
    assert failure == "authorization.policy_generation_changed"


def test_an_incomplete_current_state_blocks_a_public_grant() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    state = harness.owner._current_state(
        invocation, verified_approvals=frozenset(), receiver_generation=0
    )
    del state["review"]

    record, failure = claim(harness, invocation, evaluation, state=state)

    assert record is None
    assert failure == "authorization.policy_evaluation_incomplete"


def test_a_resolvable_reference_does_not_replace_the_value() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    harness.producers["conditions"].value = b'{"changed":true}'

    record, failure = claim(harness, invocation, evaluation)

    assert record is None
    assert failure == "authorization.policy_evidence_changed"


def test_a_changed_epoch_alone_is_enough_to_reject() -> None:
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    harness.producers["durability"].epoch = 2

    record, failure = claim(harness, invocation, evaluation)

    assert record is None
    assert failure == "authorization.policy_evidence_changed"


@pytest.mark.parametrize("offset", [0, 1])
def test_reaching_the_binding_deadline_ends_usable_authority(offset: int) -> None:
    harness = Harness(evaluation_ttl=60)
    invocation, evaluation = bound_evaluation(harness, request())

    record, failure = claim(
        harness,
        invocation,
        evaluation,
        now=harness.clock.current + timedelta(seconds=60 + offset),
    )

    assert record is None
    assert failure == "authorization.policy_evaluation_expired"


def test_a_state_validity_bound_shortens_the_deadline() -> None:
    harness = Harness(evaluation_ttl=3_600)
    harness.producers["consent"].valid_until = harness.clock.current + timedelta(
        seconds=30
    )
    _invocation, evaluation = bound_evaluation(harness, request())

    assert evaluation.valid_until == harness.clock.current + timedelta(seconds=30)


# ── no binding, no authority ─────────────────────────────────────────────


def test_a_restored_receipt_cannot_be_used_in_a_fresh_incarnation() -> None:
    first = Harness()
    item = request()
    receipt = first.issuer.authorize(item).receipt
    assert receipt is not None

    second = Harness()
    second.store.issue(receipt)

    with pytest.raises(SETTAuthorizationError) as raised:
        second.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert second.store.consumed == 0


def test_a_backend_orphan_receipt_cannot_authorize() -> None:
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    harness.issuer.verify_and_consume(receipt, item)

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert harness.store.consumed == 1


# ── full canonical request bytes ─────────────────────────────────────────


@pytest.mark.parametrize(
    "changed",
    [
        {"payload": {"value": 2}},
        {"payload": {"value": True}},
        {"payload": {"value": 1.0}},
        {"payload": {"value": -0.0}},
        {"payload": {"value": "1"}},
        {"trace_id": "test_fixture.other_trace"},
        {"principal": "test_fixture.other_subject"},
        {"idempotency_key": "test_fixture.operation"},
    ],
)
def test_a_permissive_fingerprint_cannot_hide_a_changed_request(
    changed: dict[str, Any]
) -> None:
    harness = Harness(fingerprint=ConstantFingerprint())
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, request(**changed))

    assert reason(raised) == "authorization.request_mismatch"
    assert harness.store.consumed == 0


def test_the_same_request_still_consumes_under_that_fingerprint() -> None:
    harness = Harness(fingerprint=ConstantFingerprint())
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None

    harness.issuer.verify_and_consume(receipt, item)

    assert harness.store.consumed == 1


def test_an_altered_public_receipt_field_keeps_the_store_diagnostic() -> None:
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    forged = replace(receipt, request_fingerprint="0" * 64)

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(forged, item)

    assert reason(raised) == "authorization.receipt_mismatch"


# ── identity is never a label ────────────────────────────────────────────


def test_identical_declared_identifiers_do_not_share_authority() -> None:
    first = Harness()
    second = Harness()
    item = request()
    receipt = first.issuer.authorize(item).receipt
    assert receipt is not None

    with pytest.raises(SETTAuthorizationError) as raised:
        second.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_binding_unavailable"


def test_two_receivers_share_one_registry_with_disjoint_invocations() -> None:
    harness = Harness()
    second_store = CountingStore()
    second = AuthorizationRuler(
        issuer_id="test_fixture.issuer",
        receipt_ttl_seconds=3_600,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=second_store,
        evidence_sink=harness.evidence,
        clock=harness.clock,
        policy_ruler=harness.owner,
    )
    item = request()

    first_receipt = harness.issuer.authorize(item).receipt
    second_receipt = second.authorize(item).receipt

    assert first_receipt is not None and second_receipt is not None
    assert first_receipt.receipt_id != second_receipt.receipt_id
    assert list(harness.owner._registry[EFFECT]) == ["test_fixture.policy"]

    with pytest.raises(SETTAuthorizationError) as raised:
        second.verify_and_consume(first_receipt, item)

    assert reason(raised) == "authorization.policy_binding_unavailable"


# ── preserved outcomes ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "decision",
    [
        PolicyDecision.deny("test_fixture.policy", conditions=(), review_obligations=()),
        PolicyDecision.require_approval(
            "test_fixture.policy",
            requirements=(
                __import__(
                    "sett.authorization_ruler", fromlist=["x"]
                ).ApprovalRequirement("test_fixture.requirement", "test_fixture.verifier"),
            ),
            conditions=(),
            review_obligations=(),
        ),
    ],
)
def test_a_non_grant_outcome_issues_nothing(decision: PolicyDecision) -> None:
    harness = Harness(decision=decision)

    outcome = harness.issuer.authorize(request())

    assert outcome.receipt is None
    assert harness.store.issued == 0
    assert harness.effects == 0
    assert harness.owner._live == {}


def test_controlled_replay_keeps_its_binding_checks() -> None:
    harness = Harness(
        decision=PolicyDecision.grant(
            "test_fixture.policy",
            receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
            conditions=(),
            review_obligations=(),
        )
    )
    item = request(mode=ExecutionMode.CONTROLLED, idempotency_key="test_fixture.op")
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    assert receipt.receipt_use is ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY

    harness.issuer.verify_and_consume(receipt, item)
    harness.issuer.verify_and_consume(receipt, item)

    assert harness.store.consumed == 2

    harness.producers["review"].value = b'{"changed":true}'
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_evidence_changed"
    assert harness.store.consumed == 2


def test_a_producer_that_reports_stale_state_blocks_consumption() -> None:
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    harness.producers["conditions"].current = False

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)

    assert reason(raised) == "authorization.policy_evidence_changed"
    assert harness.store.consumed == 0


# Explicit currentness is distinct from equal bytes. All producers here are
# synthetic; none of these cases installs a production verification adapter.
@pytest.mark.parametrize("confirmation", [False, None, "raises"])
@pytest.mark.parametrize("slot_name", PRODUCED_SLOT_NAMES)
def test_preissue_rejects_unconfirmed_retained_state(
    confirmation: Any, slot_name: str,
) -> None:
    harness = Harness()
    producer = harness.producers[slot_name]
    produced = []
    confirmed = []
    original = producer.produce

    def produce(context: Any) -> Any:
        slot = original(context)
        produced.append(slot)
        return slot

    def confirm(slot: Any) -> Any:
        confirmed.append(slot)
        if confirmation == "raises":
            raise RuntimeError("test_fixture.unavailable")
        return confirmation

    producer.produce = produce
    producer.confirm = confirm
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert reason(raised) == "authorization.policy_evidence_changed"
    assert confirmed == [produced[0]]
    assert confirmed[0] is produced[0]
    assert produced[0] == produced[1]
    assert produced[0] is not produced[1]
    assert harness.store.issued == 0
    assert harness.owner._live == {}


def test_preissue_confirmation_keeps_retained_proof_and_deadline() -> None:
    harness = Harness()
    producer = harness.producers["conditions"]
    bound = harness.clock.current + timedelta(seconds=20)
    producer.valid_until = bound
    produced = []
    confirmed = []
    original = producer.produce

    def produce(context: Any) -> Any:
        slot = original(context)
        produced.append(slot)
        return slot

    def confirm(slot: Any) -> bool:
        confirmed.append(slot)
        harness.clock.current += timedelta(seconds=1)
        return True

    producer.produce = produce
    producer.confirm = confirm
    receipt = harness.issuer.authorize(request()).receipt
    assert receipt is not None
    assert len(confirmed) == 1
    assert confirmed[0] is produced[0]
    record = next(iter(harness.owner._live.values()))
    assert record.state["conditions"] is produced[0]
    assert record.valid_until == bound
    assert harness.store.issued == 1


def test_preissue_confirmation_runs_outside_guard_and_rechecks_generation() -> None:
    harness = Harness()

    def confirm(slot: Any) -> bool:
        # Registration would poison a critical phase. As a callback it is
        # permitted, but the subsequent generation comparison must reject.
        harness.owner.register_policy(
            "test_fixture.other", StaticPolicy("test_fixture.second"),
            binding=binding("test_fixture.second"),
        )
        return True

    harness.producers["conditions"].confirm = confirm
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())
    assert reason(raised) == "authorization.policy_generation_changed"
    assert harness.store.issued == 0


@pytest.mark.parametrize("earlier_fault", ["selection", "generation"])
def test_preissue_confirmation_failure_does_not_mask_earlier_fault(
    earlier_fault: str,
) -> None:
    harness = Harness()

    def confirm(slot: Any) -> bool:
        if earlier_fault == "generation":
            harness.owner.register_policy(
                "test_fixture.other", StaticPolicy("test_fixture.second"),
                binding=binding("test_fixture.second"),
            )
        return False

    harness.producers["conditions"].confirm = confirm
    resolve = harness.resolver.resolve
    calls = 0

    def selection(item: AuthorizationRequest) -> PolicySelection:
        nonlocal calls
        calls += 1
        if calls == 2 and earlier_fault == "selection":
            harness.resolver.authority_ref = "test_fixture.changed"
        return resolve(item)

    harness.resolver.resolve = selection
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())
    assert reason(raised) == {
        "selection": "authorization.policy_selection_changed",
        "generation": "authorization.policy_generation_changed",
    }[earlier_fault]
    assert harness.store.issued == 0


@pytest.mark.parametrize("earlier_fault", ["selection", "generation", None])
def test_private_checkpoint_fault_follows_selection_and_generation(
    earlier_fault: str | None,
) -> None:
    """Private fault injection, not a public alternate-checkpoint API."""
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    invocation.checkpoint_id = "test_fixture.other_checkpoint"
    if earlier_fault == "selection":
        harness.resolver.authority_ref = "test_fixture.other_authority"
    elif earlier_fault == "generation":
        harness.owner.register_policy(
            "test_fixture.other", StaticPolicy("test_fixture.second"),
            binding=binding("test_fixture.second"),
        )
    record, failure = claim(harness, invocation, evaluation)
    expected = {
        "selection": "authorization.policy_selection_changed",
        "generation": "authorization.policy_generation_changed",
        None: "authorization.policy_checkpoint_mismatch",
    }
    assert record is None
    assert failure == expected[earlier_fault]
    assert harness.store.issued == 0


def test_private_checkpoint_must_match_configured_entry_even_if_witnesses_agree() -> None:
    """Corrupt both private witnesses; the configured entry stays independent."""
    harness = Harness()
    invocation, evaluation = bound_evaluation(harness, request())
    invocation.checkpoint_id = "test_fixture.other_checkpoint"
    harness.owner._live[invocation].checkpoint_id = invocation.checkpoint_id
    record, failure = claim(harness, invocation, evaluation)
    assert record is None
    assert failure == "authorization.policy_checkpoint_mismatch"
    assert harness.store.issued == 0


def test_consumption_checkpoint_is_the_configured_entry() -> None:
    """Private fault injection before consume; no transport/checkpoint API."""
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None
    record = next(iter(harness.owner._live.values()))
    record.checkpoint_id = "test_fixture.other_checkpoint"
    record.invocation.checkpoint_id = record.checkpoint_id
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)
    assert reason(raised) == "authorization.policy_checkpoint_mismatch"
    assert harness.store.consumed == 0


@pytest.mark.parametrize("stage", ["issue", "consume"])
@pytest.mark.parametrize("shorter", ["receipt", "evaluation"])
@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("replay", [False, True])
def test_backend_crossing_either_deadline_terminates_authority(
    stage: str, shorter: str, offset: int, replay: bool,
) -> None:
    receipt_ttl, evaluation_ttl = (2, 30) if shorter == "receipt" else (30, 2)
    assert receipt_ttl != evaluation_ttl
    receipt_use = (
        ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY if replay else ReceiptUse.SINGLE_USE
    )
    harness = Harness(
        receipt_ttl=receipt_ttl, evaluation_ttl=evaluation_ttl,
        decision=PolicyDecision.grant("test_fixture.policy", receipt_use=receipt_use, conditions=(), review_obligations=()),
    )
    item = request(
        mode=ExecutionMode.CONTROLLED if replay else ExecutionMode.PLAIN,
        idempotency_key="test_fixture.operation" if replay else None,
    )
    captured = []
    issue = harness.store.issue
    consume = harness.store.verify_and_consume

    def slow_issue(receipt: Any) -> None:
        captured.append(receipt)
        issue(receipt)
        harness.clock.current += timedelta(seconds=2 + offset)

    def slow_consume(receipt: Any, **kwargs: Any) -> None:
        consume(receipt, **kwargs)
        harness.clock.current += timedelta(seconds=2 + offset)

    if stage == "issue":
        harness.store.issue = slow_issue

        def operation() -> Any:
            return harness.issuer.authorize(item)
    else:
        receipt = harness.issuer.authorize(item).receipt
        assert receipt is not None
        captured.append(receipt)
        if replay:
            harness.issuer.verify_and_consume(receipt, item)
        harness.store.verify_and_consume = slow_consume

        def operation() -> Any:
            return harness.issuer.verify_and_consume(receipt, item)

    with pytest.raises(SETTAuthorizationError) as raised:
        operation()
    expected = (
        "authorization.receipt_expired" if shorter == "receipt"
        else "authorization.policy_evaluation_expired"
    )
    assert reason(raised) == expected
    assert harness.store.issued == 1
    consumed = 0 if stage == "issue" else 1 + int(replay)
    assert harness.store.consumed == consumed
    assert harness.owner._live == {}
    with pytest.raises(SETTAuthorizationError) as retry:
        harness.issuer.verify_and_consume(captured[0], item)
    assert reason(retry) == "authorization.policy_binding_unavailable"
    assert harness.store.issued == 1
    assert harness.store.consumed == consumed
    assert harness.effects == 0


# Residual cases use synthetic closed slots; no real producer is installed.
@pytest.mark.parametrize("checkpoint", ["test_fixture.unsupported", ENTRY])
def test_residual_unsupported_checkpoint_translation_at_authorization(monkeypatch: Any, checkpoint: str) -> None:
    """Fault injection forwards a value to real private admission."""
    harness = Harness()
    original = harness.owner._open_invocation
    calls = []

    def forward(link: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["checkpoint_id"])
        kwargs["checkpoint_id"] = checkpoint
        return original(link, **kwargs)

    monkeypatch.setattr(harness.owner, "_open_invocation", forward)
    if checkpoint == ENTRY:
        result = harness.issuer.authorize(request())
        assert result.receipt is not None and harness.store.issued == 1
    else:
        with pytest.raises(SETTAuthorizationError) as raised:
            harness.issuer.authorize(request())
        assert reason(raised) == "authorization.policy_checkpoint_mismatch"
        assert not harness.owner._live
        assert harness.store.issued == harness.policy.calls == 0
    assert calls == [ENTRY]


@pytest.mark.parametrize("stage", ["claim", "consume"])
@pytest.mark.parametrize("slot", ["approvals", *PRODUCED_SLOT_NAMES])
@pytest.mark.parametrize("dimension", ["action_id", "checkpoint_id", "equal"])
def test_residual_changed_state_scope_at_final_comparison(monkeypatch: Any, stage: str, slot: str, dimension: str) -> None:
    """Inject current scope only at the private final comparison."""
    harness = Harness()
    item = request()
    original = (harness.owner._validate_and_claim if stage == "claim"
                else harness.owner._validate_receipt_binding)
    observed = []

    def compare(*args: Any, **kwargs: Any) -> Any:
        current = kwargs["current_state"]
        before = current[slot]
        changed = (before if dimension == "equal" else
                   replace(before, **{dimension: "test_fixture.changed_scope"}))
        for name in before.__dataclass_fields__:
            if name != dimension:
                assert getattr(before, name) == getattr(changed, name)
        retained = tuple((id(record), record.state_signature) for record in harness.owner._live.values())
        kwargs["current_state"] = {**current, slot: changed}
        result = original(*args, **kwargs)
        assert tuple((id(record), record.state_signature) for record in harness.owner._live.values()) == retained
        observed.append(result[1])
        return result

    receipt = harness.issuer.authorize(item).receipt if stage == "consume" else None
    monkeypatch.setattr(harness.owner, "_validate_and_claim" if stage == "claim" else "_validate_receipt_binding", compare)
    if dimension == "equal":
        if stage == "claim":
            receipt = harness.issuer.authorize(item).receipt
            assert receipt is not None
        else:
            harness.issuer.verify_and_consume(receipt, item)
        assert observed == [None]
        assert harness.store.issued == 1
        assert harness.store.consumed == (1 if stage == "consume" else 0)
    else:
        with pytest.raises(SETTAuthorizationError) as raised:
            if stage == "claim":
                harness.issuer.authorize(item)
            else:
                harness.issuer.verify_and_consume(receipt, item)
        assert reason(raised) == "authorization.policy_evidence_changed"
        assert observed == ["authorization.policy_evidence_changed"]
        assert harness.store.issued == (1 if stage == "consume" else 0)
        assert harness.store.consumed == 0
    assert harness.effects == 0


def residual_record_snapshot(record: Any) -> Any:
    """Capture complete retained values and object identities, not only a hash."""
    return (
        id(record), id(record.incarnation), id(record.link), id(record.invocation),
        id(record.evaluation), record.evaluation_id, record.verification_ref,
        record.request_bytes, record.selection_bytes, record.policy_generation,
        record.receiver_generation, record.checkpoint_id, record.state_signature,
        tuple((name, slot.signature()) for name, slot in record.state.items()),
        record.evaluated_at, record.valid_until, record.lifecycle,
        id(record.receipt), id(record.receiver),
    )


class ResidualInformativeResolver(Resolver):
    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        base = super().resolve(request)
        extra = [binding("test_fixture.info_ok"), binding("test_fixture.info_missing")]
        return replace(
            base, informative=extra,
            applicability=[*base.applicability, *[
                PolicyApplicability(policy_id=item.policy_id, policy_version=item.policy_version,
                    state=PolicyApplicabilityState.APPLIES, justification="Synthetic test declaration",
                    fact_names=["test_fixture.fact"], evidence_refs=[]) for item in extra]],
            facts={"test_fixture.fact": FieldValue({"nested": [1]},
                source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE)},
        )


@pytest.mark.parametrize("mutation", ["assign", "delete", "required", "informative", "diagnostics", "nested-selection"])
def test_residual_bound_result_views_cannot_mutate_retained_binding(monkeypatch: Any, mutation: str) -> None:
    """Ordinary mutation attempts cannot alter a real pending binding."""
    harness = Harness()
    resolver = ResidualInformativeResolver()
    # Configure synthetic selection before the invocation snapshot is taken.
    monkeypatch.setattr(harness.resolver, "resolve", resolver.resolve)
    harness.issuer.register_policy(EFFECT, StaticPolicy("test_fixture.info_ok"), binding=binding("test_fixture.info_ok"))
    invocation, evaluation = bound_evaluation(harness, request())
    record = harness.owner._live[invocation]
    before = residual_record_snapshot(record)
    observation = (evaluation.selection.canonical_view(), evaluation.policy_decisions,
                   evaluation.informative_decisions, evaluation.informative_diagnostics,
                   evaluation.action_id, evaluation.decision)
    assert evaluation.policy_decisions and evaluation.informative_decisions and evaluation.informative_diagnostics
    if mutation == "assign":
        with pytest.raises(AttributeError):
            evaluation.action_id = "test_fixture.changed"
    elif mutation == "delete":
        with pytest.raises(AttributeError):
            del evaluation.action_id
    elif mutation == "required":
        with pytest.raises(TypeError):
            evaluation.policy_decisions[0] = PolicyDecision.grant("test_fixture.changed", conditions=(), review_obligations=())
    elif mutation == "informative":
        with pytest.raises(TypeError):
            evaluation.informative_decisions[0] = PolicyDecision.grant("test_fixture.changed", conditions=(), review_obligations=())
    elif mutation == "diagnostics":
        with pytest.raises(TypeError):
            evaluation.informative_diagnostics[0] = ("test_fixture.changed", "test_fixture.code")
    else:
        projection = evaluation.selection.canonical_view()
        projection["required"][0]["policy_id"] = "test_fixture.changed"
        projection["informative"].clear()
        projection["facts"]["test_fixture.fact"]["value"]["nested"].append(2)
        assert projection != observation[0]
    assert residual_record_snapshot(record) == before
    assert (evaluation.selection.canonical_view(), evaluation.policy_decisions,
            evaluation.informative_decisions, evaluation.informative_diagnostics,
            evaluation.action_id, evaluation.decision) == observation
    receipt = harness.issuer._issue(invocation, evaluation, invocation.request, frozenset(), evaluation.decision)
    assert receipt is not None and harness.store.issued == 1
    harness.issuer.verify_and_consume(receipt, invocation.request)
    assert harness.store.consumed == 1


class ResidualVerifier:
    verifier_id = "test_fixture.residual.verifier"

    def verify(self, approval_input: Any, request: Any) -> None:
        return None


def residual_receiver_group() -> Any:
    first, independent = Harness(), Harness()
    second_store = CountingStore()
    second = AuthorizationRuler(
        issuer_id="test_fixture.issuer", receipt_ttl_seconds=3600,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=second_store,
        evidence_sink=first.evidence, clock=first.clock, policy_ruler=first.owner,
    )
    # The sibling has an unrelated action type under the same policy owner.
    first.issuer.register_policy("test_fixture.other_effect", StaticPolicy(), binding=binding())
    items = [request(action_id="test_fixture.first_action"),
             AuthorizationRequest(action_id="test_fixture.other_action", action_type="test_fixture.other_effect",
                 trace_id="test_fixture.trace", payload={"value": 1}, principal=request().principal),
             request(action_id="test_fixture.independent_action")]
    return first, independent, [first.issuer, second, independent.issuer], [first.store, second_store, independent.store], items


def residual_pending(receiver: Any, item: Any, stage: str) -> Any:
    if stage == "consume":
        return receiver.authorize(item).receipt
    ruler = receiver._policy_ruler
    invocation = ruler._open_invocation(receiver._link, request=item, checkpoint_id=ENTRY)
    evaluation = ruler._evaluate_bound(invocation, verified_approvals=frozenset(), receiver_generation=receiver._generation())
    return invocation, evaluation


def residual_continue(receiver: Any, pending: Any, item: Any, stage: str) -> None:
    if stage == "consume":
        receiver.verify_and_consume(pending, item)
    else:
        invocation, evaluation = pending
        receipt = receiver._issue(invocation, evaluation, item, frozenset(), evaluation.decision)
        assert receipt is not None
        receiver.verify_and_consume(receipt, item)


@pytest.mark.parametrize("stage", ["claim", "consume"])
@pytest.mark.parametrize("change", ["local", "shared"])
def test_residual_configuration_change_has_exact_affected_scope(stage: str, change: str) -> None:
    """Actual local and shared mutations, with independent control."""
    first, independent, receivers, stores, items = residual_receiver_group()
    pendings = [residual_pending(receiver, item, stage) for receiver, item in zip(receivers, items, strict=True)]
    generations = [receiver._generation() for receiver in receivers]
    shared_generation, separate_generation = first.owner._generation, independent.owner._generation
    registry_before = first.owner._snapshot()
    selection_before = [receiver._policy_ruler._current_selection(item)[1] for receiver, item in zip(receivers, items, strict=True)]
    if change == "local":
        receivers[0].register_approval_verifier(ResidualVerifier())
        assert [receiver._generation() for receiver in receivers] == [generations[0] + 1, *generations[1:]]
        assert first.owner._generation == shared_generation
        assert first.owner._snapshot() == registry_before
    else:
        receivers[0].register_policy("test_fixture.new_effect", StaticPolicy("test_fixture.new_policy"), binding=binding("test_fixture.new_policy"))
        assert first.owner._generation == shared_generation + 1
        assert [receiver._generation() for receiver in receivers] == generations
    assert independent.owner._generation == separate_generation
    assert [receiver._policy_ruler._current_selection(item)[1] for receiver, item in zip(receivers, items, strict=True)] == selection_before
    for index, (receiver, pending, item, store) in enumerate(zip(receivers, pendings, items, stores, strict=True)):
        affected = index == 0 or (change == "shared" and index == 1)
        if affected:
            with pytest.raises(SETTAuthorizationError) as raised:
                residual_continue(receiver, pending, item, stage)
            assert reason(raised) == "authorization.policy_generation_changed"
            assert store.issued == (1 if stage == "consume" else 0)
            assert store.consumed == 0
        else:
            residual_continue(receiver, pending, item, stage)
            assert store.issued == store.consumed == 1
    # New explicit requests continue under current generations, without retry.
    for receiver, item in zip(receivers[:2], items[:2], strict=True):
        receipt = receiver.authorize(item).receipt
        assert receipt is not None
        receiver.verify_and_consume(receipt, item)


@pytest.mark.parametrize("stage", ["claim", "consume"])
@pytest.mark.parametrize("change", ["local", "shared"])
def test_residual_failed_configuration_change_preserves_both_receivers(stage: str, change: str) -> None:
    """Failed registration cannot advance either generation."""
    from sett.authorization_ruler import SETTAuthorizationConfigurationError

    first, independent, receivers, stores, items = residual_receiver_group()
    receivers[0].register_approval_verifier(ResidualVerifier())
    pendings = [residual_pending(receiver, item, stage) for receiver, item in zip(receivers, items, strict=True)]
    before = (first.owner._snapshot(), independent.owner._snapshot(), tuple(receiver._generation() for receiver in receivers))
    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        if change == "local":
            receivers[0].register_approval_verifier(ResidualVerifier())
        else:
            receivers[0].register_policy(EFFECT, StaticPolicy(), binding=binding())
    assert reason(raised) == ("authorization.verifier_duplicate" if change == "local" else "authorization.policy_duplicate")
    assert (first.owner._snapshot(), independent.owner._snapshot(), tuple(receiver._generation() for receiver in receivers)) == before
    for receiver, pending, item, store in zip(receivers, pendings, items, stores, strict=True):
        residual_continue(receiver, pending, item, stage)
        assert store.issued == store.consumed == 1


def test_residual_configuration_change_preserves_committed_store_facts() -> None:
    """A shared mutation cannot rewrite a completed single use."""
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    harness.issuer.verify_and_consume(receipt, item)
    assert harness.store.issued == harness.store.consumed == 1
    harness.issuer.register_policy("test_fixture.other_effect", StaticPolicy("test_fixture.new_policy"), binding=binding("test_fixture.new_policy"))
    assert harness.store.issued == harness.store.consumed == 1
    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.verify_and_consume(receipt, item)
    assert reason(raised) == "authorization.policy_binding_unavailable"
    assert harness.store.issued == harness.store.consumed == 1
    # Observe the store's own historical fact independently of the dead link.
    with pytest.raises(SETTAuthorizationError) as stored:
        harness.store.verify_and_consume(receipt, request_fingerprint=receipt.request_fingerprint, now=harness.clock.current)
    assert reason(stored) == "authorization.receipt_consumed"
    assert harness.effects == 0 and harness.store.consumed == 1
