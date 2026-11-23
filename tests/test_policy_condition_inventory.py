"""PolicyDecision condition declaration cases; no real producer or governed effect.

These cases construct decisions that carry declared conditions and observe how
the declaration is admitted. SETT verifies no condition here: no verifier is
registered or invoked, and no declaration is compared with an observation. The
policy callbacks below are synthetic and exercise only the existing boundary.
"""

import ast
import dataclasses
import inspect
import json
import os
import subprocess
import sys
import traceback
import typing
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sett
import sett.authorization_ruler as authorization
import sett.policy_ruler as policy
from sett.authorization_ruler import (
    ApprovalRequirement,
    AuthorizationConstraint,
    AuthorizationRequest,
    AuthorizationVerdict,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicySelection,
    ReceiptUse,
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
)
from sett.exceptions import (
    SETTConfigurationError,
    SETTError,
    SETTPolicyConfigurationError,
    SETTValidationError,
)
from sett.policy_ruler import (
    ConditionValidityMode,
    PolicyBinding,
    PolicyCondition,
    PolicyDecision,
    PolicyOrigin,
    PolicyRuler,
    models,
)

POLICY = "test_fixture.policy"
VERSION = "test_fixture.version"
CODE = "policy.condition_inventory_invalid"
MESSAGE = "The policy condition inventory is invalid."
SECRET = "test_fixture.secret"
CALLER_SECRET = "test_fixture.caller_secret"
LIMIT = 65_536
EXISTING = ("policy_id", "verdict", "receipt_use", "reason_codes", "requirements", "constraints")
ENTRY = "test_fixture.entry"
EFFECT = "test_fixture.effect"
OMITTED = object()


def origin():
    return PolicyOrigin(
        requirement_id="test_fixture.requirement", requirement_revision="test_fixture.revision",
        layer_id="test_fixture.layer", source=None, source_absence_reason="test_fixture.synthetic",
    )


def binding(policy_id=POLICY, version=VERSION):
    return PolicyBinding(
        policy_id=policy_id, policy_version=version, contract_version="2.0",
        origins=[origin()], profile_id=None, profile_revision=None,
    )


def condition_values(condition_id="test_fixture.condition", policy_id=POLICY, version=VERSION):
    bound = binding(policy_id, version)
    return dict(
        condition_id=condition_id, binding=bound, origins=list(bound.origins),
        verifier_id="test_fixture.verifier", verifier_version="test_fixture.version",
        required=True, action_id="test_fixture.action", action_type="test_fixture.type",
        checkpoint_id="test_fixture.checkpoint", validity_mode=ConditionValidityMode.RECHECK,
        attestation_not_after=None,
    )


def condition(**changes):
    return PolicyCondition(**condition_values(**changes))


def forged(**changes):
    """Frozen data assembled without the constructor, as a hostile caller could."""
    value = object.__new__(PolicyCondition)
    for name, item in {**condition_values(), **changes}.items():
        object.__setattr__(value, name, item)
    return value


def requirement():
    return ApprovalRequirement("test_fixture.requirement", "test_fixture.verifier")


BUILDERS = {
    "constructor": lambda **kwargs: PolicyDecision(POLICY, AuthorizationVerdict.GRANT, **kwargs, review_obligations=()),
    "grant": lambda **kwargs: PolicyDecision.grant(POLICY, **kwargs, review_obligations=()),
    "deny": lambda **kwargs: PolicyDecision.deny(POLICY, **kwargs, review_obligations=()),
    "require_approval": lambda **kwargs: PolicyDecision.require_approval(
        POLICY, requirements=[requirement()], **kwargs, review_obligations=()),
}


def invalid(build):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        build()
    error = caught.value
    assert type(error) is SETTPolicyConfigurationError
    assert error.reason_code == CODE
    assert error.args == (MESSAGE,)
    assert str(error) == MESSAGE
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.verdict is None and not hasattr(error, "receipt_id")
    return error


class CountedSequence(Sequence):
    def __init__(self, items, size=None, failure=None):
        self.items = items
        self.size = len(items) if size is None else size
        self.failure = failure
        self.indices = []

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        self.indices.append(index)
        if self.failure is not None:
            raise self.failure
        return self.items[index]


class Endless(Sequence):
    def __init__(self, item, size):
        self.item = item
        self.size = size
        self.reads = 0

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        self.reads += 1
        return self.item


class CountedIterator:
    def __init__(self, reads):
        self.reads = reads

    def __iter__(self):
        return self

    def __next__(self):
        self.reads.append(1)
        return condition()


class ExplodingBinding:
    def __init__(self, failure):
        self.failure = failure

    def canonical_view(self):
        raise self.failure


def generated(reads):
    reads.append(1)
    yield condition()


@pytest.fixture
def projections(monkeypatch):
    calls = []
    original = PolicyCondition.canonical_view

    def counted(self):
        calls.append(self)
        return original(self)

    monkeypatch.setattr(PolicyCondition, "canonical_view", counted)
    return calls


def aggregate_bytes(values):
    views = [value.canonical_view() for value in values]
    return len(json.dumps(
        views, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8"))


# The declaration is mandatory, keyword-only and without default.
@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_omission_is_an_ordinary_type_error_and_explicit_empty_is_kept(name):
    with pytest.raises(TypeError) as caught:
        BUILDERS[name]()
    assert type(caught.value) is TypeError and not isinstance(caught.value, SETTError)
    assert "conditions" in str(caught.value)
    decision = BUILDERS[name](conditions=())
    assert decision.conditions == () and type(decision.conditions) is tuple


def test_decision_signatures_keep_existing_parameters_and_append_declarations():
    parameters = inspect.signature(PolicyDecision).parameters
    assert tuple(parameters) == (*EXISTING, "conditions", "review_obligations")
    assert all(parameters[name].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for name in EXISTING)
    assert [parameters[name].default for name in EXISTING] == [
        inspect.Parameter.empty, inspect.Parameter.empty, ReceiptUse.SINGLE_USE, (), (), ()]
    for factory, before in (
        (PolicyDecision.grant, ("policy_id", "receipt_use", "reason_codes", "constraints")),
        (PolicyDecision.deny, ("policy_id", "reason_codes")),
        (PolicyDecision.require_approval, ("policy_id", "requirements", "reason_codes")),
        (PolicyDecision, EXISTING),
    ):
        parameters = inspect.signature(factory).parameters
        assert tuple(parameters) == (*before, "conditions", "review_obligations")
        assert parameters["conditions"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["conditions"].default is inspect.Parameter.empty


def test_the_declaration_cannot_be_passed_positionally():
    with pytest.raises(TypeError):
        PolicyDecision(POLICY, AuthorizationVerdict.GRANT, ReceiptUse.SINGLE_USE, (), (), (), (), review_obligations=())
    with pytest.raises(TypeError):
        PolicyDecision.grant(POLICY, (), review_obligations=())
    with pytest.raises(TypeError):
        PolicyDecision.deny(POLICY, (), review_obligations=())


# None and non-sequence or text-like containers, rejected without traversal.
REJECTED_CONTAINERS = {
    "none": lambda reads: None,
    "text": lambda reads: "test_fixture.text",
    "bytes": lambda reads: b"test_fixture.bytes",
    "bytearray": lambda reads: bytearray(b"test_fixture.bytes"),
    "memoryview": lambda reads: memoryview(b"test_fixture.bytes"),
    "mapping": lambda reads: {"test_fixture.key": condition()},
    "set": lambda reads: frozenset({condition()}),
    "generator": generated,
    "iterator": CountedIterator,
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
@pytest.mark.parametrize("kind", sorted(REJECTED_CONTAINERS))
def test_none_and_non_sequence_containers_are_rejected_unread(name, kind):
    reads = []
    values = REJECTED_CONTAINERS[kind](reads)
    invalid(lambda: BUILDERS[name](conditions=values))
    assert reads == []


# Exact entries are admitted before any projection.
@pytest.mark.parametrize("kind", ["subclass", "mapping_view", "binding", "none"])
def test_inexact_entries_are_rejected_before_any_projection(kind, projections):
    class TestFixtureCondition(PolicyCondition):
        pass

    entry = {
        "subclass": lambda: TestFixtureCondition(**condition_values()),
        "mapping_view": lambda: condition().canonical_view(),
        "binding": binding,
        "none": lambda: None,
    }[kind]()
    values = [condition(condition_id="test_fixture.first"), entry]
    projections.clear()
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
    assert projections == []


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_exact_values_are_retained_as_the_same_objects(name):
    value = condition()
    decision = BUILDERS[name](conditions=[value])
    assert decision.conditions == (value,) and decision.conditions[0] is value


# Nested revalidation through the canonical view and canonical bounds.
@pytest.mark.parametrize("kind", ["binding", "origin", "mode", "oversized", "non_finite"])
def test_forged_values_failing_their_view_or_bounds_are_rejected(kind):
    changes = {
        "binding": {"binding": None},
        "origin": {"origins": [object()]},
        "mode": {"validity_mode": "recheck"},
        "oversized": {"condition_id": "test_fixture." + "x" * LIMIT},
        "non_finite": {"required": float("nan")},
    }[kind]
    value = forged(**changes)
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=[value], review_obligations=()))


def test_revalidation_relies_on_the_constructor_for_field_rules():
    # Measured limit of the approved mechanism: the canonical view and bounds are
    # rechecked, the declaration constructor's field rules are not rerun.
    value = forged(condition_id="test_fixture.\u0001")
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyCondition(**condition_values(condition_id="test_fixture.\u0001"))
    assert caught.value.reason_code == "policy.condition_invalid"
    decision = PolicyDecision.grant(POLICY, conditions=[value], review_obligations=())
    assert decision.conditions[0] is value


# Every entry names the enclosing policy.
@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_entries_must_name_the_enclosing_policy(name):
    other = condition(policy_id="test_fixture.other")
    invalid(lambda: BUILDERS[name](conditions=[other]))
    invalid(lambda: BUILDERS[name](conditions=[condition(), other]))
    assert BUILDERS[name](conditions=[condition()]).conditions[0].binding.policy_id == POLICY


# Duplicate logical identity is rejected, never deduplicated.
def test_duplicate_identity_is_rejected_and_distinct_versions_are_kept():
    value = condition()
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=[value, value], review_obligations=()))
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=[value, condition()], review_obligations=()))
    versions = [condition(version="test_fixture.v1"), condition(version="test_fixture.v2")]
    assert len(PolicyDecision.grant(POLICY, conditions=versions, review_obligations=()).conditions) == 2
    identifiers = [condition(condition_id="test_fixture.a"), condition(condition_id="test_fixture.b")]
    assert len(PolicyDecision.grant(POLICY, conditions=identifiers, review_obligations=()).conditions) == 2


# Count limit, honest length and bounded traversal.
def test_count_limit_is_checked_before_reading_any_entry(projections):
    values = CountedSequence([], size=257)
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
    assert values.indices == [] and projections == []


def test_two_hundred_fifty_six_entries_pass_the_count_gate(projections):
    value = condition()
    values = CountedSequence([value] * 256)
    projections.clear()
    # The count and type gates admit them; the duplicate rule then rejects them.
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
    assert len(projections) == 256


@pytest.mark.parametrize("reported", [1, 3])
def test_inconsistent_length_is_rejected(reported, projections):
    values = CountedSequence([condition(condition_id="test_fixture.a"),
                              condition(condition_id="test_fixture.b")], size=reported)
    projections.clear()
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
    assert projections == []


def test_traversal_of_a_dishonest_sequence_is_bounded():
    values = Endless(condition(), size=3)
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
    assert values.reads == 257


# Detached tuple, deterministic order, equality, hashing and repr.
def test_detached_tuple_in_deterministic_key_order():
    late = condition(condition_id="test_fixture.b", version="test_fixture.v1")
    early = condition(condition_id="test_fixture.a", version="test_fixture.v1")
    first = condition(condition_id="test_fixture.z", version="test_fixture.v0")
    supplied = [late, early, first]
    decision = PolicyDecision.grant(POLICY, conditions=supplied, review_obligations=())
    supplied.clear()
    assert decision.conditions == (first, early, late)
    assert type(decision.conditions) is tuple
    assert PolicyDecision.grant(POLICY, conditions=[first, late, early], review_obligations=()) == decision
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.conditions = ()


def test_declaration_takes_part_in_equality_hashing_and_repr():
    field = {item.name: item for item in dataclasses.fields(PolicyDecision)}["conditions"]
    assert field.kw_only and field.compare and field.repr and field.hash is None
    value = condition()
    plain = PolicyDecision.grant(POLICY, conditions=(), review_obligations=())
    declared = PolicyDecision.grant(POLICY, conditions=[value], review_obligations=())
    assert plain != declared
    assert hash(declared) == hash(PolicyDecision.grant(POLICY, conditions=[value], review_obligations=()))
    shown = repr(declared)
    assert "test_fixture.synthetic" in shown and "test_fixture.condition" in shown


# The aggregate bound covers the collection only, without truncation.
def test_aggregate_bound_at_and_over_the_limit_without_truncation():
    values = []
    while aggregate_bytes([*values, condition(condition_id=f"test_fixture.c{len(values):03d}")]) <= LIMIT:
        values.append(condition(condition_id=f"test_fixture.c{len(values):03d}"))
    slack = LIMIT - aggregate_bytes(values)
    padded = []
    for value in values:
        extra = min(slack, 128 - len(value.condition_id))
        padded.append(condition(condition_id=value.condition_id + "x" * extra) if extra else value)
        slack -= extra
    assert slack == 0 and aggregate_bytes(padded) == LIMIT
    decision = PolicyDecision.grant(POLICY, conditions=padded, review_obligations=())
    assert len(decision.conditions) == len(padded) and set(decision.conditions) == set(padded)
    extra = condition(condition_id="test_fixture.over")
    assert PolicyDecision.grant(POLICY, conditions=[extra], review_obligations=()).conditions == (extra,)
    invalid(lambda: PolicyDecision.grant(POLICY, conditions=[*padded, extra], review_obligations=()))


# Exact class, code and message; no verdict or receipt.
@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_exact_diagnostic_surface(name):
    error = invalid(lambda: BUILDERS[name](conditions=None))
    assert isinstance(error, SETTConfigurationError) and isinstance(error, SETTError)
    assert not isinstance(error, ValueError) and not isinstance(error, SETTValidationError)
    assert not isinstance(error, SETTAuthorizationError)


# No cause, context or private input, also inside a caller's handler.
@pytest.mark.parametrize("kind", ["length", "item", "projection", "value"])
def test_ordinary_failures_keep_no_cause_context_or_private_input(kind):
    class Length(CountedSequence):
        def __len__(self):
            raise RuntimeError(SECRET)

    values = {
        "length": lambda: Length([condition()]),
        "item": lambda: CountedSequence([condition()], failure=RuntimeError(SECRET)),
        "projection": lambda: [forged(binding=ExplodingBinding(RuntimeError(SECRET)))],
        "value": lambda: [forged(condition_id=SECRET + "x" * LIMIT)],
    }[kind]()
    error = invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
    shown = "".join(traceback.format_exception(error))
    assert SECRET not in repr(error) and SECRET not in str(error) and SECRET not in shown


@pytest.mark.parametrize("kind", ["none", "container", "caller_with_cause"])
def test_exception_handled_by_the_caller_is_not_chained(kind):
    values = {
        "none": None,
        "container": CountedSequence([condition()], failure=RuntimeError(SECRET)),
        "caller_with_cause": None,
    }[kind]
    caller = RuntimeError(CALLER_SECRET)
    cause = ValueError("test_fixture.caller_cause")
    try:
        if kind == "caller_with_cause":
            raise caller from cause
        raise caller
    except RuntimeError as handled:
        error = invalid(lambda: PolicyDecision.grant(POLICY, conditions=values, review_obligations=()))
        assert handled is caller and sys.exception() is caller
    assert caller.args == (CALLER_SECRET,) and caller.__context__ is None
    assert caller.__cause__ is (cause if kind == "caller_with_cause" else None)
    shown = "".join(traceback.format_exception(error))
    assert CALLER_SECRET not in shown and SECRET not in shown


# Termination signals propagate as the same object.
def signal_values(boundary, instance):
    class Length(CountedSequence):
        def __len__(self):
            raise instance

    return {
        "length": lambda: Length([condition()]),
        "item": lambda: CountedSequence([condition()], failure=instance),
        "projection": lambda: [forged(binding=ExplodingBinding(instance))],
    }[boundary]()


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("boundary", ["length", "item", "projection"])
def test_termination_signals_propagate_unchanged(signal, boundary):
    instance = signal("test_fixture.termination")
    values = signal_values(boundary, instance)
    with pytest.raises(signal) as caught:
        PolicyDecision.grant(POLICY, conditions=values, review_obligations=())
    assert caught.value is instance


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_termination_signals_propagate_inside_a_caller_handler(signal):
    instance = signal("test_fixture.termination")
    values = signal_values("item", instance)
    with pytest.raises(signal) as caught:
        try:
            raise RuntimeError(CALLER_SECRET)
        except RuntimeError:
            PolicyDecision.grant(POLICY, conditions=values, review_obligations=())
    assert caught.value is instance


# Existing-field diagnostics keep class, message and precedence.
PRIOR = {
    "policy_id": (lambda c: PolicyDecision(" test_fixture.policy", AuthorizationVerdict.GRANT, conditions=c, review_obligations=()),
                  "policy_id must be non-empty and trimmed."),
    "verdict": (lambda c: PolicyDecision(POLICY, "grant", conditions=c, review_obligations=()),
                "verdict must be an AuthorizationVerdict."),
    "receipt_use": (lambda c: PolicyDecision(POLICY, AuthorizationVerdict.GRANT, "single_use", conditions=c, review_obligations=()),
                    "receipt_use must be a ReceiptUse member."),
    "reason_codes": (lambda c: PolicyDecision.grant(POLICY, reason_codes=[" test_fixture.reason"], conditions=c, review_obligations=()),
                     "reason_code must be non-empty and trimmed."),
    "requirements": (lambda c: PolicyDecision.require_approval(POLICY, requirements=["test_fixture"], conditions=c, review_obligations=()),
                     "requirements must contain ApprovalRequirement instances."),
    "constraints": (lambda c: PolicyDecision.grant(POLICY, constraints=["test_fixture"], conditions=c, review_obligations=()),
                    "constraints must contain AuthorizationConstraint instances."),
    "approval_empty": (lambda c: PolicyDecision.require_approval(POLICY, requirements=[], conditions=c, review_obligations=()),
                       "REQUIRE_APPROVAL needs at least one declared requirement."),
    "approval_replay": (lambda c: PolicyDecision(
        POLICY, AuthorizationVerdict.REQUIRE_APPROVAL, ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
        requirements=(requirement(),), conditions=c, review_obligations=()), "REQUIRE_APPROVAL cannot authorize receipt replay."),
    "grant_requirements": (lambda c: PolicyDecision(
        POLICY, AuthorizationVerdict.GRANT, requirements=(requirement(),), conditions=c, review_obligations=()),
        "Only REQUIRE_APPROVAL can carry approval requirements."),
    "deny_constraints": (lambda c: PolicyDecision(
        POLICY, AuthorizationVerdict.DENY, constraints=(AuthorizationConstraint("test_fixture.constraint"),),
        conditions=c, review_obligations=()), "DENY cannot carry grant constraints."),
}


@pytest.mark.parametrize("field", sorted(PRIOR))
@pytest.mark.parametrize("declaration", ["empty", "none", "failing_container"])
def test_existing_field_diagnostics_are_never_masked(field, declaration):
    build, message = PRIOR[field]
    values = {
        "empty": (),
        "none": None,
        "failing_container": CountedSequence([condition()], failure=RuntimeError(SECRET)),
    }[declaration]
    with pytest.raises(SETTValidationError) as caught:
        build(values)
    assert type(caught.value) is SETTValidationError
    assert caught.value.args == (message,)
    if declaration == "failing_container":
        assert values.indices == []


# Every verdict carries its declaration; constraints stay separate; factories forward.
def test_every_verdict_carries_its_declaration_separately_from_constraints():
    value = condition()
    constraint = AuthorizationConstraint("test_fixture.constraint")
    granted = PolicyDecision.grant(POLICY, constraints=[constraint], conditions=[value], review_obligations=())
    assert granted.conditions == (value,) and granted.constraints == (constraint,)
    assert PolicyDecision.deny(POLICY, conditions=[value], review_obligations=()).conditions == (value,)
    pending = PolicyDecision.require_approval(POLICY, requirements=[requirement()], conditions=[value], review_obligations=())
    assert pending.conditions == (value,) and pending.constraints == ()
    direct = PolicyDecision(POLICY, AuthorizationVerdict.GRANT, conditions=[value], review_obligations=())
    assert direct.conditions == (value,) and direct.constraints == ()


@pytest.mark.parametrize("name", ["grant", "deny", "require_approval"])
def test_factories_forward_the_same_declaration_object(name, monkeypatch):
    seen = []
    original = models._policy_conditions

    def spy(values, policy_id, condition_type):
        seen.append(values)
        return original(values, policy_id, condition_type)

    monkeypatch.setattr(models, "_policy_conditions", spy)
    supplied = [condition()]
    BUILDERS[name](conditions=supplied)
    assert len(seen) == 1 and seen[0] is supplied
    invalid(lambda: BUILDERS[name](conditions=None))
    reads = []
    invalid(lambda: BUILDERS[name](conditions=generated(reads)))
    assert reads == []


def test_inspection_shows_the_declaration_and_the_aggregate_carries_none():
    # This inspection uses a declaration matching its own registration, request,
    # and entry checkpoint. Value construction alone remains context-free.
    bound = binding(POLICY, "1")
    current_request = request()
    value = dataclasses.replace(
        condition(), binding=bound, action_id=current_request.action_id,
        action_type=current_request.action_type, checkpoint_id=ENTRY,
    )
    ruler = ruler_for([bound])
    ruler.register_policy(EFFECT, DeclaringPolicy(POLICY, [value]), binding=bound)
    evaluation = ruler.evaluate(current_request, verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert evaluation.policy_decisions[0].conditions == (value,)
    assert evaluation.decision.policy_ids == (POLICY,)
    assert not hasattr(evaluation.decision, "conditions")


# Same objects, both import orders, no module-level cycle.
def test_same_object_through_every_namespace():
    assert sett.PolicyDecision is authorization.PolicyDecision is policy.PolicyDecision
    assert policy.PolicyDecision is models.PolicyDecision
    assert "PolicyCondition" not in vars(models)


@pytest.mark.parametrize("first", ["conditions", "models"])
def test_both_import_orders_construct_in_a_fresh_interpreter(first):
    order = ["sett.policy_ruler.conditions", "sett.policy_ruler.models"]
    if first == "models":
        order.reverse()
    script = "\n".join([
        "import importlib",
        f"first = importlib.import_module({order[0]!r})",
        f"second = importlib.import_module({order[1]!r})",
        "from sett.policy_ruler import PolicyBinding, PolicyCondition, PolicyDecision, PolicyOrigin",
        "from sett.policy_ruler import ConditionValidityMode",
        "origin = PolicyOrigin(requirement_id='test_fixture.requirement',",
        "    requirement_revision='test_fixture.revision', layer_id='test_fixture.layer',",
        "    source=None, source_absence_reason='test_fixture.synthetic')",
        "bound = PolicyBinding(policy_id='test_fixture.policy', policy_version='1',",
        "    contract_version='2.0', origins=[origin], profile_id=None, profile_revision=None)",
        "value = PolicyCondition(condition_id='test_fixture.condition', binding=bound,",
        "    origins=[origin], verifier_id='test_fixture.verifier', verifier_version='1',",
        "    required=True, action_id='test_fixture.action', action_type='test_fixture.type',",
        "    checkpoint_id='test_fixture.checkpoint', validity_mode=ConditionValidityMode.RECHECK,",
        "    attestation_not_after=None)",
        "decision = PolicyDecision.grant('test_fixture.policy', conditions=[value], review_obligations=())",
        "assert decision.conditions == (value,)",
        "print('ok')",
    ])
    root = str(Path(sett.__file__).resolve().parents[1])
    environment = dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script], env=environment, capture_output=True,
        text=True, check=False, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_conditions_are_imported_only_inside_the_constructor_hook():
    tree = ast.parse(Path(models.__file__).read_text(encoding="utf-8"))

    def names_conditions(node):
        return isinstance(node, ast.ImportFrom) and node.module == "sett.policy_ruler.conditions"

    module_level = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert not any(names_conditions(node) for node in module_level)
    typing_only = [node for node in tree.body if isinstance(node, ast.If)
                   and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"]
    assert len(typing_only) == 1 and all(names_conditions(node) for node in typing_only[0].body)
    decision = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "PolicyDecision")
    hook = next(node for node in decision.body
                if isinstance(node, ast.FunctionDef) and node.name == "__post_init__")
    runtime = [node for node in ast.walk(tree) if names_conditions(node)
               and node not in typing_only[0].body]
    assert len(runtime) == 1 and runtime[0] in hook.body
    handlers = [node for node in hook.body if isinstance(node, ast.Try)]
    assert all(runtime[0] not in list(ast.walk(node)) for node in handlers)


def test_runtime_annotation_resolution_needs_the_name_supplied():
    with pytest.raises(NameError):
        typing.get_type_hints(PolicyDecision)
    hints = typing.get_type_hints(PolicyDecision, localns={"PolicyCondition": PolicyCondition})
    assert hints["conditions"] == Sequence[PolicyCondition]


# An unmigrated policy fails through the existing boundary, with no new code.
class FixedClock:
    def now(self):
        return datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


class Resolver:
    resolver_id = "test_fixture.resolver"

    def __init__(self, required, informative):
        self.required = list(required)
        self.informative = list(informative)

    def resolve(self, request):
        items = self.required + self.informative
        return PolicySelection(
            selection_id="test_fixture.selection", selection_revision="1", contract_version="2.0",
            authority_ref="test_fixture.authority",
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id, action_type=request.action_type,
            required=list(self.required), informative=list(self.informative),
            applicability=[
                PolicyApplicability(
                    policy_id=item.policy_id, policy_version=item.policy_version,
                    state=PolicyApplicabilityState.APPLIES,
                    justification="Synthetic test declaration", fact_names=[], evidence_refs=[],
                )
                for item in items
            ],
            facts={},
        )


class DeclaringPolicy:
    """A synthetic policy that records what its own decision construction raised."""

    def __init__(self, policy_id, conditions=OMITTED):
        self.policy_id = policy_id
        self.conditions = conditions
        self.calls = 0
        self.raised = None

    def evaluate(self, request, verified_approvals):
        self.calls += 1
        try:
            if self.conditions is OMITTED:
                return PolicyDecision.grant(self.policy_id, review_obligations=())
            return PolicyDecision.grant(self.policy_id, conditions=self.conditions, review_obligations=())
        except Exception as error:
            self.raised = error
            raise


def ruler_for(required, informative=()):
    return PolicyRuler(
        ruler_id="test_fixture.policy_ruler", entry_checkpoint_id=ENTRY,
        evaluation_ttl_seconds=3_600, max_live_evaluations=16, clock=FixedClock(),
        evidence_sink=InMemoryAuthorizationEvidence(),
        selection_resolver=Resolver(required, informative),
    )


def request():
    return AuthorizationRequest(
        action_id="test_fixture.action", trace_id="test_fixture.trace", action_type=EFFECT,
        payload={"value": 1},
        principal=FieldValue(
            "test_fixture.subject", source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE,
        ),
    )


UNMIGRATED = {"omitted": (OMITTED, TypeError), "none": (None, SETTPolicyConfigurationError)}


@pytest.mark.parametrize("mode", sorted(UNMIGRATED))
def test_required_unmigrated_policy_fails_through_the_existing_boundary(mode):
    declared, raised = UNMIGRATED[mode]
    bound = binding(POLICY, "1")
    unmigrated = DeclaringPolicy(POLICY, declared)
    ruler = ruler_for([bound])
    ruler.register_policy(EFFECT, unmigrated, binding=bound)
    with pytest.raises(SETTAuthorizationConfigurationError) as caught:
        ruler.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    error = caught.value
    assert type(error) is SETTAuthorizationConfigurationError
    assert error.reason_code == "authorization.policy_failed"
    assert not isinstance(error, SETTPolicyConfigurationError)
    assert error.__cause__ is None and error.__context__ is None
    assert CODE not in str(error) and CODE not in repr(error)
    assert unmigrated.calls == 1 and type(unmigrated.raised) is raised
    if raised is SETTPolicyConfigurationError:
        assert unmigrated.raised.reason_code == CODE


@pytest.mark.parametrize("mode", sorted(UNMIGRATED))
def test_informative_unmigrated_policy_yields_the_existing_diagnostic_pair(mode):
    declared, raised = UNMIGRATED[mode]
    required = binding(POLICY, "1")
    informative = binding("test_fixture.informative", "1")
    ruler = ruler_for([required], [informative])
    ruler.register_policy(EFFECT, DeclaringPolicy(POLICY, ()), binding=required)
    unmigrated = DeclaringPolicy("test_fixture.informative", declared)
    ruler.register_policy(EFFECT, unmigrated, binding=informative)
    evaluation = ruler.evaluate(request(), verified_approvals=frozenset(), checkpoint_id=ENTRY)
    assert evaluation.decision.verdict is AuthorizationVerdict.GRANT
    assert evaluation.informative_diagnostics == (
        ("test_fixture.informative", "authorization.policy_failed"),
    )
    assert evaluation.informative_decisions == ()
    assert unmigrated.calls == 1 and type(unmigrated.raised) is raised
