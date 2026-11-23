"""PolicyRuler configuration, registration, declared selection and inspection.

Every fixture here is synthetic and neutral. A synthetic state producer
exercises the real handoff validation; it is not a condition, consent,
review or durability verifier and establishes none.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import sett
from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
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
from sett.exceptions import (
    SETTConfigurationError,
    SETTError,
    SETTPolicyConfigurationError,
)
from sett.policy_ruler import PolicyEvaluation, PolicyRuler
from sett.policy_ruler._handoff import PRODUCED_SLOT_NAMES, VerifiedStateSlot

ENTRY = "test_fixture.entry"
EFFECT = "test_fixture.effect"


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
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


def applicability(
    item: PolicyBinding,
    state: PolicyApplicabilityState = PolicyApplicabilityState.APPLIES,
    fact_names: tuple[str, ...] = (),
) -> PolicyApplicability:
    return PolicyApplicability(
        policy_id=item.policy_id,
        policy_version=item.policy_version,
        state=state,
        justification="Synthetic test declaration",
        fact_names=list(fact_names),
        evidence_refs=[],
    )


class Resolver:
    """A test-owned resolver returning one fully declared selection."""

    resolver_id = "test_fixture.resolver"

    def __init__(self) -> None:
        self.required: list[PolicyBinding] = [binding()]
        self.informative: list[PolicyBinding] = []
        self.entries: dict[tuple[str, str], PolicyApplicability] = {}
        self.facts: dict[str, FieldValue | None] = {}
        self.effective_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.action_id: str | None = None
        self.action_type: str | None = None
        self.result: Any = None
        self.explode = False
        self.calls = 0

    def entry(self, item: PolicyBinding) -> PolicyApplicability:
        return self.entries.get(
            (item.policy_id, item.policy_version), applicability(item)
        )

    def resolve(self, request: AuthorizationRequest) -> Any:
        self.calls += 1
        if self.explode:
            raise RuntimeError("resolver secret detail")
        if self.result is not None:
            return self.result
        items = self.required + self.informative
        return PolicySelection(
            selection_id="test_fixture.selection",
            selection_revision="1",
            contract_version="2.0",
            authority_ref="test_fixture.authority",
            effective_at=self.effective_at,
            action_id=self.action_id or request.action_id,
            action_type=self.action_type or request.action_type,
            required=list(self.required),
            informative=list(self.informative),
            applicability=[self.entry(item) for item in items],
            facts=dict(self.facts),
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


def owner(
    resolver: Resolver | None = None,
    *,
    clock: FixedClock | None = None,
    evidence: InMemoryAuthorizationEvidence | None = None,
    capacity: int = 16,
    producers: tuple[str, ...] = PRODUCED_SLOT_NAMES,
) -> PolicyRuler:
    ruler = PolicyRuler(
        ruler_id="test_fixture.policy_ruler",
        entry_checkpoint_id=ENTRY,
        evaluation_ttl_seconds=3_600,
        max_live_evaluations=capacity,
        clock=clock or FixedClock(),
        evidence_sink=evidence or InMemoryAuthorizationEvidence(),
        selection_resolver=resolver or Resolver(),
    )
    for name in producers:
        ruler._register_state_producer(SyntheticStateProducer(name))
    return ruler


def request(action_id: str = "test_fixture.action") -> AuthorizationRequest:
    return AuthorizationRequest(
        action_id=action_id,
        trace_id="test_fixture.trace",
        action_type=EFFECT,
        payload={"value": 1},
        principal=FieldValue(
            "test_fixture.subject",
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
    )


# Exact admission is stricter than the preserved value constructors.
@pytest.mark.parametrize("variant", ["request", "container", "approval"])
def test_exact_input_subclasses_fail_before_callback(variant: str) -> None:
    from sett.authorization_ruler import VerifiedApproval

    class TestFixtureRequest(AuthorizationRequest):
        pass

    class TestFixtureSet(frozenset):
        pass

    class TestFixtureApproval(VerifiedApproval):
        pass

    ruler = owner()
    policy = StaticPolicy()
    ruler.register_policy(EFFECT, policy, binding=binding())
    item = request()
    approvals = frozenset()
    if variant == "request":
        item = TestFixtureRequest(action_id=item.action_id, action_type=EFFECT,
                                  trace_id=item.trace_id, payload=item.payload_copy(), principal=item.principal)
    elif variant == "container":
        approvals = TestFixtureSet()
    else:
        approvals = frozenset({TestFixtureApproval("test_fixture.requirement", "test_fixture.verifier", "test_fixture.authority")})
    with pytest.raises(SETTPolicyConfigurationError) as raised:
        ruler.evaluate(item, verified_approvals=approvals, checkpoint_id=ENTRY)
    assert raised.value.reason_code == "policy.evaluation_input_invalid"
    assert policy.calls == 0 and ruler._live == {}


@pytest.mark.parametrize("field_name", ["principal", "resource", "purpose", "scope", "data_labels", "context_facts"])
def test_nested_request_subclass_cannot_supply_binding_proof(field_name: str) -> None:
    class TestFixtureField(FieldValue):
        calls = 0

        def canonical_view(self) -> dict[str, Any]:
            type(self).calls += 1
            return super().canonical_view()

    ruler = owner()
    policy = StaticPolicy()
    ruler.register_policy(EFFECT, policy, binding=binding())
    field = TestFixtureField("test_fixture.value", source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE)
    arguments: dict[str, Any] = dict(action_id="test_fixture.action", action_type=EFFECT,
                                    trace_id="test_fixture.trace", payload={"value": 1}, principal=request().principal)
    arguments[field_name] = (field,) if field_name in {"scope", "data_labels"} else (
        {"test_fixture.fact": field} if field_name == "context_facts" else field
    )
    item = AuthorizationRequest(**arguments)
    issuer = AuthorizationRuler(**issuer_arguments(policy_ruler=ruler, clock=ruler._clock,
                                                   evidence_sink=ruler._evidence_sink))
    # The existing constructor accepts this subtype; the new boundary must not.
    with pytest.raises(SETTAuthorizationError) as raised:
        issuer.authorize(item)
    assert raised.value.reason_code == "authorization.policy_evaluation_invalid"
    assert policy.calls == TestFixtureField.calls == 0
    assert ruler._live == {}


@pytest.mark.parametrize("variant", ["decision", "constraint", "requirement"])
def test_nested_result_types_are_rejected_before_composition(variant: str) -> None:
    from sett.authorization_ruler import ApprovalRequirement, AuthorizationConstraint

    class TestFixtureDecision(PolicyDecision):
        pass

    class TestFixtureConstraint(AuthorizationConstraint):
        pass

    class TestFixtureRequirement(ApprovalRequirement):
        pass

    if variant == "decision":
        decision = TestFixtureDecision.grant("test_fixture.policy", conditions=(), review_obligations=())
    elif variant == "constraint":
        decision = PolicyDecision.grant("test_fixture.policy", constraints=[TestFixtureConstraint("test_fixture.constraint")], conditions=(), review_obligations=())
    else:
        decision = PolicyDecision.require_approval("test_fixture.policy", requirements=[
            TestFixtureRequirement("test_fixture.requirement", "test_fixture.verifier"),
        ], conditions=(), review_obligations=())
    ruler = owner()
    policy = StaticPolicy(decision=decision)
    ruler.register_policy(EFFECT, policy, binding=binding())
    with pytest.raises(SETTAuthorizationError) as raised:
        ruler.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert raised.value.reason_code == "authorization.policy_result_invalid"
    assert policy.calls == 1 and ruler._live == {}


def test_invalid_new_boundary_input_translates_before_callbacks() -> None:
    class TestFixtureRequest(AuthorizationRequest):
        pass

    ruler = owner()
    policy = StaticPolicy()
    ruler.register_policy(EFFECT, policy, binding=binding())
    issuer = AuthorizationRuler(**issuer_arguments(policy_ruler=ruler, clock=ruler._clock,
                                                   evidence_sink=ruler._evidence_sink))
    original = request()
    item = TestFixtureRequest(action_id=original.action_id, trace_id=original.trace_id,
                              action_type=EFFECT, payload=original.payload_copy(), principal=original.principal)
    with pytest.raises(SETTAuthorizationError) as raised:
        issuer.authorize(item)
    assert raised.value.reason_code == "authorization.policy_evaluation_invalid"
    assert policy.calls == 0 and ruler._live == {}


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, Exception])
def test_unexpected_private_exception_is_translated_without_raw_context(
    error_type: type[Exception], monkeypatch: Any,
) -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    store = InMemoryAuthorizationReceiptStore()
    issuer = AuthorizationRuler(**issuer_arguments(policy_ruler=ruler, clock=ruler._clock,
                                                   evidence_sink=ruler._evidence_sink, receipt_store=store))
    issued = []
    monkeypatch.setattr(store, "issue", lambda receipt: issued.append(receipt))

    def internal_failure(*args: Any, **kwargs: Any) -> None:
        raise error_type("test_fixture.private_snapshot_secret")

    monkeypatch.setattr(ruler, "_evaluate_bound", internal_failure)
    with pytest.raises(SETTAuthorizationError) as raised:
        issuer.authorize(request())
    assert raised.value.reason_code == "authorization.policy_evaluation_failed"
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert "test_fixture.private_snapshot_secret" not in str(raised.value)
    assert issued == [] and ruler._live == {}


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_termination_signals_propagate_unchanged(error_type: type[BaseException], monkeypatch: Any) -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    issuer = AuthorizationRuler(**issuer_arguments(policy_ruler=ruler, clock=ruler._clock,
                                                   evidence_sink=ruler._evidence_sink))
    failure = error_type("test_fixture.termination")

    def internal_signal(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(ruler, "_evaluate_bound", internal_signal)
    with pytest.raises(error_type) as raised:
        issuer.authorize(request())
    assert raised.value is failure
    assert ruler._live == {}


@pytest.mark.parametrize("family", ["policy", "authorization"])
def test_existing_structured_diagnostics_keep_precedence(family: str, monkeypatch: Any) -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    issuer = AuthorizationRuler(**issuer_arguments(policy_ruler=ruler, clock=ruler._clock,
                                                   evidence_sink=ruler._evidence_sink))
    failure = (SETTPolicyConfigurationError("Synthetic failure.", reason_code="policy.evaluation_incomplete")
               if family == "policy" else SETTAuthorizationError("Synthetic failure.", reason_code="authorization.policy_failed"))

    def structured_failure(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(ruler, "_evaluate_bound", structured_failure)
    with pytest.raises(SETTAuthorizationError) as raised:
        issuer.authorize(request())
    assert raised.value.reason_code == ("authorization.policy_evaluation_incomplete" if family == "policy"
                                        else "authorization.policy_failed")
    if family == "authorization":
        assert raised.value is failure
    assert ruler._live == {}


# ── explicit configuration, no hidden defaults ───────────────────────────


@pytest.mark.parametrize(
    "overrides",
    [
        {"ruler_id": ""},
        {"ruler_id": " padded "},
        {"ruler_id": 7},
        {"entry_checkpoint_id": ""},
        {"evaluation_ttl_seconds": 0},
        {"evaluation_ttl_seconds": -1},
        {"evaluation_ttl_seconds": True},
        {"evaluation_ttl_seconds": float("nan")},
        {"evaluation_ttl_seconds": float("inf")},
        {"evaluation_ttl_seconds": None},
        {"max_live_evaluations": 0},
        {"max_live_evaluations": 257},
        {"max_live_evaluations": True},
        {"max_live_evaluations": 1.0},
        {"max_live_evaluations": None},
        {"clock": object()},
        {"evidence_sink": object()},
    ],
)
def test_policy_ruler_configuration_is_explicit(overrides: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "ruler_id": "test_fixture.policy_ruler",
        "entry_checkpoint_id": ENTRY,
        "evaluation_ttl_seconds": 60,
        "max_live_evaluations": 4,
        "clock": FixedClock(),
        "evidence_sink": InMemoryAuthorizationEvidence(),
        "selection_resolver": Resolver(),
    }
    arguments.update(overrides)

    with pytest.raises(SETTPolicyConfigurationError) as raised:
        PolicyRuler(**arguments)

    assert raised.value.reason_code == "policy.ruler_configuration_invalid"


def test_missing_resolver_keeps_its_existing_runtime_diagnostic() -> None:
    with pytest.raises(SETTAuthorizationError) as raised:
        PolicyRuler(
            ruler_id="test_fixture.policy_ruler",
            entry_checkpoint_id=ENTRY,
            evaluation_ttl_seconds=60,
            max_live_evaluations=4,
            clock=FixedClock(),
            evidence_sink=InMemoryAuthorizationEvidence(),
        )

    assert (
        raised.value.reason_code == "authorization.policy_selection_resolver_missing"
    )


@pytest.mark.parametrize("resolver", [object(), "resolver"])
def test_invalid_resolver_is_a_configuration_failure(resolver: object) -> None:
    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        PolicyRuler(
            ruler_id="test_fixture.policy_ruler",
            entry_checkpoint_id=ENTRY,
            evaluation_ttl_seconds=60,
            max_live_evaluations=4,
            clock=FixedClock(),
            evidence_sink=InMemoryAuthorizationEvidence(),
            selection_resolver=resolver,  # type: ignore[arg-type]
        )

    assert (
        raised.value.reason_code == "authorization.policy_selection_resolver_invalid"
    )


def test_resolver_identifier_is_validated() -> None:
    resolver = Resolver()
    resolver.resolver_id = ""  # type: ignore[assignment]

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        owner(resolver)

    assert (
        raised.value.reason_code == "authorization.policy_selection_resolver_invalid"
    )


def test_policy_configuration_error_is_not_an_authorization_error() -> None:
    assert issubclass(SETTPolicyConfigurationError, SETTConfigurationError)
    assert issubclass(SETTPolicyConfigurationError, SETTError)
    assert not issubclass(SETTPolicyConfigurationError, SETTAuthorizationError)
    error = SETTPolicyConfigurationError("m", reason_code="policy.clock_invalid")
    assert error.reason_code == "policy.clock_invalid"
    assert not hasattr(error, "receipt_id")
    assert error.verdict is None


# ── registration ─────────────────────────────────────────────────────────


def test_registration_requires_its_declared_binding() -> None:
    ruler = owner()

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        ruler.register_policy(EFFECT, StaticPolicy())

    assert raised.value.reason_code == "authorization.policy_binding_required"
    assert ruler._generation == len(PRODUCED_SLOT_NAMES)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"binding": object()}, "authorization.policy_binding_invalid"),
        ({"binding": binding()}, "authorization.policy_invalid"),
    ],
)
def test_registration_rejects_invalid_inputs(
    kwargs: dict[str, Any], code: str
) -> None:
    ruler = owner()
    policy = StaticPolicy() if code.endswith("binding_invalid") else object()

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        ruler.register_policy(EFFECT, policy, **kwargs)  # type: ignore[arg-type]

    assert raised.value.reason_code == code


def test_registration_requires_matching_identifiers() -> None:
    ruler = owner()

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        ruler.register_policy(
            EFFECT, StaticPolicy("test_fixture.other"), binding=binding()
        )

    assert raised.value.reason_code == "authorization.policy_binding_mismatch"


def test_duplicate_registration_leaves_the_registry_unchanged() -> None:
    ruler = owner()
    first = StaticPolicy()
    ruler.register_policy(EFFECT, first, binding=binding())
    generation = ruler._generation

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    assert raised.value.reason_code == "authorization.policy_duplicate"
    assert ruler._generation == generation
    assert ruler._registry[EFFECT]["test_fixture.policy"].policy is first


def test_successful_registration_advances_the_generation_once() -> None:
    ruler = owner()
    generation = ruler._generation

    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    assert ruler._generation == generation + 1


def test_authorization_facade_registers_in_the_single_registry() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)
    issuer = AuthorizationRuler(
        issuer_id="test_fixture.issuer",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=evidence,
        clock=clock,
        policy_ruler=ruler,
    )
    generation = ruler._generation

    issuer.register_policy(EFFECT, StaticPolicy(), binding=binding())

    assert ruler._generation == generation + 1
    assert list(ruler._registry[EFFECT]) == ["test_fixture.policy"]
    assert not hasattr(issuer, "_policies")


# ── inspection only ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [
        {"request": object()},
        {"verified_approvals": set()},
        {"verified_approvals": frozenset({object()})},
    ],
)
def test_evaluation_input_must_be_the_exact_expected_types(
    arguments: dict[str, Any]
) -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    call: dict[str, Any] = {
        "request": request(),
        "verified_approvals": frozenset(),
        "checkpoint_id": ENTRY,
    }
    call.update(arguments)

    with pytest.raises(SETTPolicyConfigurationError) as raised:
        ruler.evaluate(
            call["request"],
            verified_approvals=call["verified_approvals"],
            checkpoint_id=call["checkpoint_id"],
        )

    assert raised.value.reason_code == "policy.evaluation_input_invalid"


@pytest.mark.parametrize("checkpoint", ["", " padded ", 7, "test_fixture.unknown"])
def test_unsupported_checkpoint_is_rejected(checkpoint: object) -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    with pytest.raises(SETTPolicyConfigurationError) as raised:
        ruler.evaluate(
            request(),
            verified_approvals=frozenset(),
            checkpoint_id=checkpoint,  # type: ignore[arg-type]
        )

    assert raised.value.reason_code == "policy.evaluation_checkpoint_invalid"


def test_public_inspection_registers_no_claimable_result() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    store = InMemoryAuthorizationReceiptStore()
    ruler = owner(resolver, clock=clock, evidence=evidence)
    callback = StaticPolicy()
    ruler.register_policy(EFFECT, callback, binding=binding())
    AuthorizationRuler(
        issuer_id="test_fixture.issuer",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=store,
        evidence_sink=evidence,
        clock=clock,
        policy_ruler=ruler,
    )

    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert isinstance(evaluation, PolicyEvaluation)
    assert evaluation.verification_ref is None
    assert evaluation.decision.verdict is AuthorizationVerdict.GRANT
    assert evaluation.checkpoint_id == ENTRY
    assert evaluation.policy_decisions[0].policy_id == "test_fixture.policy"
    assert callback.calls == 1
    assert ruler._live == {}
    assert store._records == {}


def test_observation_has_no_public_constructor_or_replacement() -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    with pytest.raises(TypeError):
        PolicyEvaluation()
    with pytest.raises(TypeError):
        PolicyEvaluation(evaluation_id="x")
    with pytest.raises(AttributeError):
        evaluation.decision = None  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del evaluation._selection  # type: ignore[attr-defined]
    assert not hasattr(evaluation, "__dict__")
    assert evaluation.evaluation_id in repr(evaluation)
    assert "test_fixture.policy" not in repr(evaluation)


def test_authorize_has_no_evaluation_parameter() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    issuer = AuthorizationRuler(
        issuer_id="test_fixture.issuer",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=evidence,
        clock=clock,
        policy_ruler=ruler,
    )
    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    with pytest.raises(TypeError):
        issuer.authorize(request(), evaluation=evaluation)  # type: ignore[call-arg]


def test_returned_collections_are_detached() -> None:
    ruler = owner()
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    view = evaluation.selection.canonical_view()
    view["required"].clear()

    assert len(evaluation.selection.canonical_view()["required"]) == 1
    assert isinstance(evaluation.policy_decisions, tuple)
    assert isinstance(evaluation.informative_diagnostics, tuple)


# ── declared selection ───────────────────────────────────────────────────


def evaluate_error(ruler: PolicyRuler, code: str) -> None:
    with pytest.raises(SETTError) as raised:
        ruler.evaluate(
            request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
        )
    assert raised.value.reason_code == code  # type: ignore[attr-defined]


def test_resolver_failure_never_leaks_its_exception() -> None:
    resolver = Resolver()
    resolver.explode = True
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    with pytest.raises(SETTAuthorizationError) as raised:
        ruler.evaluate(
            request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
        )

    assert raised.value.reason_code == "authorization.policy_selection_failed"
    assert "resolver secret detail" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_policy_boundary_reports_generation_change_directly() -> None:
    """Assert the policy code, without authorization translation."""
    resolver = Resolver()
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    resolve = resolver.resolve

    def mutate(item: AuthorizationRequest) -> PolicySelection:
        ruler.register_policy(
            "test_fixture.other", StaticPolicy("test_fixture.second"),
            binding=binding("test_fixture.second"),
        )
        return resolve(item)

    resolver.resolve = mutate
    with pytest.raises(SETTPolicyConfigurationError) as raised:
        ruler.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert raised.value.reason_code == "policy.evaluation_state_changed"


def test_policy_boundary_reports_missing_producers_directly() -> None:
    """A real private evaluation boundary without synthetic slots."""
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(clock=clock, evidence=evidence, producers=())
    issuer = AuthorizationRuler(
        **issuer_arguments(policy_ruler=ruler, clock=clock, evidence_sink=evidence)
    )
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    invocation = ruler._open_invocation(issuer._link, request=request(), checkpoint_id=ENTRY)
    try:
        with pytest.raises(SETTPolicyConfigurationError) as raised:
            ruler._evaluate_bound(
                invocation, verified_approvals=frozenset(), receiver_generation=0,
            )
        assert raised.value.reason_code == "policy.evaluation_incomplete"
    finally:
        ruler._close_invocation(invocation)
    assert ruler._live == {}


def test_policy_boundary_reports_reentry_and_poisoning_directly() -> None:
    """A rejected mutation poisons the existing private phase."""
    ruler = owner()
    before = ruler._generation
    phase = ruler._begin_critical()
    try:
        with pytest.raises(SETTPolicyConfigurationError) as raised:
            ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
        assert raised.value.reason_code == "policy.transition_reentrant"
        assert phase.poisoned is True
        assert ruler._generation == before
    finally:
        ruler._end_critical(phase)


def test_resolver_result_must_be_an_exact_selection() -> None:
    resolver = Resolver()
    resolver.result = object()
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluate_error(ruler, "authorization.policy_selection_result_invalid")


@pytest.mark.parametrize(
    "attribute", ["action_id", "action_type"]
)
def test_selection_scope_must_match_the_request(attribute: str) -> None:
    resolver = Resolver()
    setattr(resolver, attribute, "test_fixture.divergent")
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluate_error(ruler, "authorization.policy_selection_scope_mismatch")


def test_future_selection_is_not_yet_effective() -> None:
    resolver = Resolver()
    resolver.effective_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluate_error(ruler, "authorization.policy_selection_not_effective")


def test_undetermined_required_applicability_blocks() -> None:
    resolver = Resolver()
    item = resolver.required[0]
    resolver.entries[(item.policy_id, item.policy_version)] = applicability(
        item, PolicyApplicabilityState.UNDETERMINED
    )
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluate_error(ruler, "authorization.policy_applicability_undetermined")


@pytest.mark.parametrize(
    "fact",
    [
        None,
        FieldValue("v", source_id="test_fixture.source", trust=FieldTrust.PROPOSED),
        FieldValue("v", source_id="test_fixture.source", trust=FieldTrust.UNKNOWN),
    ],
)
def test_unverified_facts_block_a_required_applicability(fact: object) -> None:
    resolver = Resolver()
    item = resolver.required[0]
    resolver.entries[(item.policy_id, item.policy_version)] = applicability(
        item, fact_names=("test_fixture.fact",)
    )
    resolver.facts = {"test_fixture.fact": fact}  # type: ignore[dict-item]
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluate_error(ruler, "authorization.policy_selection_facts_unverified")


def test_an_entirely_inapplicable_required_set_blocks() -> None:
    resolver = Resolver()
    item = resolver.required[0]
    resolver.entries[(item.policy_id, item.policy_version)] = applicability(
        item, PolicyApplicabilityState.DOES_NOT_APPLY
    )
    ruler = owner(resolver)

    evaluate_error(ruler, "authorization.policy_required_empty")


def test_declared_inapplicability_needs_no_installed_policy() -> None:
    resolver = Resolver()
    first = binding("test_fixture.policy")
    second = binding("test_fixture.retired")
    resolver.required = [first, second]
    resolver.entries[(second.policy_id, second.policy_version)] = applicability(
        second, PolicyApplicabilityState.DOES_NOT_APPLY
    )
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=first)

    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert evaluation.decision.policy_ids == ("test_fixture.policy",)


def test_missing_registration_blocks_before_any_callback() -> None:
    resolver = Resolver()
    ruler = owner(resolver)
    other = StaticPolicy("test_fixture.installed")
    ruler.register_policy(EFFECT, other, binding=binding("test_fixture.installed"))

    evaluate_error(ruler, "authorization.policy_missing")
    assert other.calls == 0


def test_wrong_registered_version_blocks() -> None:
    resolver = Resolver()
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding(version="2"))

    evaluate_error(ruler, "authorization.policy_version_mismatch")


def test_same_version_with_other_metadata_blocks_at_runtime() -> None:
    resolver = Resolver()
    ruler = owner(resolver)
    divergent = PolicyBinding(
        policy_id="test_fixture.policy",
        policy_version="1",
        contract_version="2.0",
        origins=[
            PolicyOrigin(
                requirement_id="test_fixture.requirement",
                requirement_revision="2",
                layer_id="test_fixture.layer",
                source=None,
                source_absence_reason="Synthetic test fixture",
            )
        ],
        profile_id=None,
        profile_revision=None,
    )
    ruler.register_policy(EFFECT, StaticPolicy(), binding=divergent)

    with pytest.raises(SETTAuthorizationError) as raised:
        ruler.evaluate(
            request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
        )

    assert raised.value.reason_code == "authorization.policy_binding_mismatch"
    assert not isinstance(raised.value, SETTAuthorizationConfigurationError)


# ── informative entries carry no authority ───────────────────────────────


def test_informative_failures_are_diagnostics_only() -> None:
    resolver = Resolver()
    missing = binding("test_fixture.informative")
    resolver.informative = [missing]
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert evaluation.decision.verdict is AuthorizationVerdict.GRANT
    assert evaluation.informative_diagnostics == (
        ("test_fixture.informative", "authorization.policy_missing"),
    )
    assert evaluation.informative_decisions == ()


def test_an_informative_denial_does_not_deny() -> None:
    resolver = Resolver()
    informative = binding("test_fixture.informative")
    resolver.informative = [informative]
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    ruler.register_policy(
        EFFECT,
        StaticPolicy(
            "test_fixture.informative",
            PolicyDecision.deny("test_fixture.informative", conditions=(), review_obligations=()),
        ),
        binding=informative,
    )

    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert evaluation.decision.verdict is AuthorizationVerdict.GRANT
    assert evaluation.decision.policy_ids == ("test_fixture.policy",)
    assert evaluation.informative_decisions[0].verdict is AuthorizationVerdict.DENY


def test_a_wrong_version_informative_policy_is_never_executed() -> None:
    resolver = Resolver()
    informative = binding("test_fixture.informative", version="9")
    resolver.informative = [informative]
    ruler = owner(resolver)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    never = StaticPolicy("test_fixture.informative")
    ruler.register_policy(
        EFFECT, never, binding=binding("test_fixture.informative", version="1")
    )

    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert never.calls == 0
    assert evaluation.informative_diagnostics == (
        ("test_fixture.informative", "authorization.policy_version_mismatch"),
    )


# ── the held wording survives the prepared foundation ────────────────────


def test_public_namespace_keeps_its_non_enforcement_wording() -> None:
    import sett.policy_ruler as package

    assert "govern no decision" in (package.__doc__ or "")
    assert "held" in (package.__doc__ or "")
    assert not hasattr(sett, "PolicyRuler")
    assert not hasattr(sett, "PolicyEvaluation")


def test_evaluation_deadline_never_exceeds_the_configured_lifetime() -> None:
    clock = FixedClock()
    ruler = owner(clock=clock)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())

    evaluation = ruler.evaluate(
        request(), verified_approvals=frozenset(), checkpoint_id=ENTRY
    )

    assert evaluation.valid_until == clock.current + timedelta(seconds=3_600)
    assert evaluation.evaluated_at == clock.current


# ── mandatory injection ──────────────────────────────────────────────────


def issuer_arguments(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "issuer_id": "test_fixture.issuer",
        "receipt_ttl_seconds": 60,
        "fingerprint_policy": StrictJSONAuthorizationFingerprint(),
        "receipt_store": InMemoryAuthorizationReceiptStore(),
        "evidence_sink": InMemoryAuthorizationEvidence(),
    }
    arguments.update(overrides)
    return arguments


def test_authorization_cannot_be_configured_without_a_policy_ruler() -> None:
    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        AuthorizationRuler(**issuer_arguments())

    assert raised.value.reason_code == "authorization.policy_ruler_missing"


@pytest.mark.parametrize("injected", [object(), "policy", 7])
def test_only_the_exact_policy_ruler_class_is_accepted(injected: object) -> None:
    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        AuthorizationRuler(**issuer_arguments(policy_ruler=injected))

    assert raised.value.reason_code == "authorization.policy_ruler_invalid"


@pytest.mark.parametrize("field", ["clock", "evidence_sink"])
def test_a_different_clock_or_sink_object_is_rejected(field: str) -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)
    arguments = issuer_arguments(
        policy_ruler=ruler, clock=clock, evidence_sink=evidence
    )
    arguments[field] = FixedClock() if field == "clock" else (
        InMemoryAuthorizationEvidence()
    )

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        AuthorizationRuler(**arguments)

    assert (
        raised.value.reason_code
        == "authorization.policy_ruler_configuration_mismatch"
    )


def test_an_omitted_clock_uses_the_injected_rulers_clock() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)

    issuer = AuthorizationRuler(
        **issuer_arguments(policy_ruler=ruler, evidence_sink=evidence)
    )

    assert issuer._clock is clock


def test_a_second_resolver_object_cannot_be_configured() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        AuthorizationRuler(
            **issuer_arguments(
                policy_ruler=ruler,
                clock=clock,
                evidence_sink=evidence,
                selection_resolver=Resolver(),
            )
        )

    assert (
        raised.value.reason_code
        == "authorization.policy_selection_resolver_conflict"
    )


def test_the_same_resolver_object_is_accepted_as_a_facade() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)

    issuer = AuthorizationRuler(
        **issuer_arguments(
            policy_ruler=ruler,
            clock=clock,
            evidence_sink=evidence,
            selection_resolver=resolver,
        )
    )

    assert issuer._policy_ruler is ruler


# ── an unmapped new-path failure has a fixed diagnostic ──────────────────


def test_an_unmapped_policy_failure_becomes_the_generic_new_path_code() -> None:
    resolver = Resolver()
    clock = FixedClock()
    evidence = InMemoryAuthorizationEvidence()
    ruler = owner(resolver, clock=clock, evidence=evidence)
    issuer = AuthorizationRuler(
        **issuer_arguments(policy_ruler=ruler, clock=clock, evidence_sink=evidence)
    )

    def unmapped() -> None:
        raise SETTPolicyConfigurationError(
            "Policy evaluation could not establish a valid binding.",
            reason_code="policy.unknown_internal_case",
        )

    with pytest.raises(SETTAuthorizationError) as raised:
        issuer._translate(unmapped)

    assert raised.value.reason_code == "authorization.policy_evaluation_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


# Residual evidence: synthetic state exercises the approved local boundary only.
class ResidualCountingStore(InMemoryAuthorizationReceiptStore):
    def __init__(self) -> None:
        super().__init__()
        self.issue_calls = 0
        self.consume_calls = 0

    def issue(self, receipt: Any) -> None:
        self.issue_calls += 1
        super().issue(receipt)

    def verify_and_consume(self, receipt: Any, **kwargs: Any) -> None:
        self.consume_calls += 1
        super().verify_and_consume(receipt, **kwargs)


def residual_receiver(ruler: PolicyRuler) -> tuple[AuthorizationRuler, Any]:
    store = ResidualCountingStore()
    receiver = AuthorizationRuler(
        issuer_id="test_fixture.residual.issuer", receipt_ttl_seconds=1800,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=store,
        evidence_sink=ruler._evidence_sink, clock=ruler._clock, policy_ruler=ruler,
    )
    return receiver, store


@pytest.mark.parametrize("bound", [False, True], ids=["reserved", "bound-unclaimed"])
def test_residual_pending_capacity_preserves_existing_invocations(bound: bool) -> None:
    """Full real reservations and full unclaimed records are distinct."""
    ruler = owner(capacity=2)
    receiver, store = residual_receiver(ruler)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    first = ruler._open_invocation(receiver._link, request=request("test_fixture.first"), checkpoint_id=ENTRY)
    second = ruler._open_invocation(receiver._link, request=request("test_fixture.second"), checkpoint_id=ENTRY)
    second_result = None
    if bound:
        ruler._evaluate_bound(first, verified_approvals=frozenset(), receiver_generation=0)
        second_result = ruler._evaluate_bound(second, verified_approvals=frozenset(), receiver_generation=0)
    retained = tuple(ruler._live.items())
    with pytest.raises(SETTPolicyConfigurationError) as raised:
        ruler._open_invocation(receiver._link, request=request("test_fixture.third"), checkpoint_id=ENTRY)
    assert raised.value.reason_code == "policy.evaluation_capacity_exceeded"
    assert tuple(ruler._live) == (first, second)
    assert all(ruler._live[invocation] is record for invocation, record in retained)
    assert not first.closed and not second.closed
    assert store.issue_calls == 0
    ruler._close_invocation(first)
    third = ruler._open_invocation(receiver._link, request=request("test_fixture.third"), checkpoint_id=ENTRY)
    assert tuple(ruler._live) == (second, third)
    assert ruler._live[third] is None
    if second_result is None:
        second_result = ruler._evaluate_bound(second, verified_approvals=frozenset(), receiver_generation=0)
    assert ruler._live[second].evaluation is second_result
    receipt = receiver._issue(second, second_result, second.request, frozenset(), second_result.decision)
    assert receipt is not None and store.issue_calls == 1
    receiver.verify_and_consume(receipt, second.request)
    assert store.consume_calls == 1
    ruler._close_invocation(third)
    assert not ruler._live


@pytest.mark.parametrize("clock_value", [None, 7, "test_fixture.invalid_time"])
def test_residual_non_datetime_clock_is_rejected_at_policy_boundary(clock_value: Any) -> None:
    """Invalid policy observation, not temporal inspection history."""
    clock = FixedClock()
    ruler = owner(clock=clock)
    ruler.register_policy(EFFECT, StaticPolicy(), binding=binding())
    stable = clock.current
    clock.current = clock_value
    with pytest.raises(SETTPolicyConfigurationError) as raised:
        ruler.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert raised.value.reason_code == "policy.clock_invalid"
    assert not ruler._live
    clock.current = stable
    observation = ruler.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert observation.evaluated_at == stable
    assert observation.valid_until == stable + timedelta(seconds=3600)
    assert observation.verification_ref is None and not ruler._live


def test_residual_subclassed_policy_owner_rejected_before_attachment() -> None:
    """An ordinarily constructed subclass must not reach _attach."""
    class ResidualPolicySubclass(PolicyRuler):
        def _attach(self, *args: Any, **kwargs: Any) -> Any:
            self.attachment_calls += 1
            return super()._attach(*args, **kwargs)

    clock, evidence = FixedClock(), InMemoryAuthorizationEvidence()
    ruler = ResidualPolicySubclass(
        ruler_id="test_fixture.residual.owner", entry_checkpoint_id=ENTRY,
        evaluation_ttl_seconds=3600, max_live_evaluations=2,
        clock=clock, evidence_sink=evidence, selection_resolver=Resolver(),
    )
    ruler.attachment_calls = 0
    before = ruler._snapshot(), dict(ruler._live), ruler._incarnation
    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        residual_receiver(ruler)
    assert raised.value.reason_code == "authorization.policy_ruler_invalid"
    assert ruler.attachment_calls == 0
    assert (ruler._snapshot(), dict(ruler._live), ruler._incarnation) == before
    base = owner(clock=clock, evidence=evidence)
    receiver, _store = residual_receiver(base)
    assert receiver._link.receiver is receiver
    assert receiver._link.incarnation is base._incarnation


def test_residual_composer_runs_once_per_inspection_or_authority_evaluation(monkeypatch: Any) -> None:
    """Count the real composer separately from policy callbacks."""
    ruler = owner()
    policy = StaticPolicy()
    ruler.register_policy(EFFECT, policy, binding=binding())
    receiver, store = residual_receiver(ruler)
    compose = ruler._compose
    calls = []

    def observed_compose(*args: Any, **kwargs: Any) -> Any:
        result = compose(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(ruler, "_compose", observed_compose)
    item = request()
    inspection = ruler.evaluate(item, verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert len(calls) == policy.calls == 1
    assert calls[0] is inspection.decision
    assert inspection.verification_ref is None and not ruler._live
    assert store.issue_calls == store.consume_calls == 0
    result = receiver.authorize(item)
    assert len(calls) == policy.calls == 2
    assert calls[1] is result.decision
    assert result.receipt is not None and store.issue_calls == 1
    receiver.verify_and_consume(result.receipt, item)
    assert len(calls) == policy.calls == 2
    assert store.consume_calls == 1
