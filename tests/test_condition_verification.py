"""Condition verification value/protocol cases; no real producer or governed effect.

These cases construct values and inspect a declared protocol. SETT calls no
verifier here: a callback invoked below is invoked directly by the test, and
nothing in these cases admits, schedules or rejects a callback at runtime.
"""

import dataclasses
import importlib
import inspect
import json
import sys
import traceback
import typing
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum

import pytest

import sett
import sett.authorization_ruler as authorization
import sett.policy_ruler as policy
from sett.authorization_ruler import AuthorizationRequest, FieldTrust, FieldValue
from sett.exceptions import (
    SETTConfigurationError,
    SETTError,
    SETTPolicyConfigurationError,
)
from sett.policy_ruler import (
    ConditionValidityMode,
    ConditionVerification,
    ConditionVerificationState,
    ConditionVerifier,
    PolicyBinding,
    PolicyCondition,
    PolicyOrigin,
    condition_results,
    conditions,
    contracts,
)

CODE = "policy.condition_verification_invalid"
MESSAGE = "The condition verification result is invalid."
FIELDS = ("condition", "state", "checked_at", "valid_until", "evidence_refs", "reason_codes")
State = ConditionVerificationState
NEGATIVE = (
    State.UNSATISFIED, State.EVIDENCE_NOT_PRESENT, State.EVIDENCE_INVALID,
    State.EVIDENCE_UNAVAILABLE,
)
CHECKED = datetime(2026, 9, 23, 12, 0, 0, 1, tzinfo=timezone.utc)
LIMIT = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
SECRET = "test_fixture.secret"
CALLER_SECRET = "test_fixture.caller_secret"


def origin():
    return PolicyOrigin(
        requirement_id="test_fixture.requirement", requirement_revision="test_fixture.revision",
        layer_id="test_fixture.layer", source=None, source_absence_reason="test_fixture.synthetic",
    )


def condition(mode=ConditionValidityMode.RECHECK, limit=None):
    bound = PolicyBinding(
        policy_id="test_fixture.policy", policy_version="test_fixture.version",
        contract_version="2.0", origins=[origin()], profile_id=None, profile_revision=None,
    )
    return PolicyCondition(
        condition_id="test_fixture.condition", binding=bound, origins=list(bound.origins),
        verifier_id="test_fixture.verifier", verifier_version="test_fixture.version",
        required=True, action_id="test_fixture.action", action_type="test_fixture.type",
        checkpoint_id="test_fixture.checkpoint", validity_mode=mode, attestation_not_after=limit,
    )


def attested(limit=LIMIT):
    return condition(ConditionValidityMode.ATTESTATION, limit)


def result(**changes):
    values = dict(
        condition=condition(), state=State.SATISFIED, checked_at=CHECKED, valid_until=None,
        evidence_refs=["test_fixture.observation"], reason_codes=[],
    )
    values.update(changes)
    return values


def negative(state, **changes):
    values = dict(state=state, reason_codes=["test_fixture.reason"], evidence_refs=[])
    values.update(changes)
    return result(**values)


def invalid(values):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ConditionVerification(**values)
    error = caught.value
    assert type(error) is SETTPolicyConfigurationError
    assert error.reason_code == CODE
    assert error.args == (MESSAGE,)
    assert str(error) == MESSAGE
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


def stamp(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def expected_projection(values):
    return {
        "condition": values["condition"].canonical_view(),
        "state": values["state"].value,
        "checked_at": stamp(values["checked_at"]),
        "valid_until": stamp(values["valid_until"]),
        "evidence_refs": sorted(values["evidence_refs"]),
        "reason_codes": sorted(values["reason_codes"]),
    }


def encoded_size(values):
    return len(json.dumps(expected_projection(values), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode())


def request():
    return AuthorizationRequest(
        action_id="test_fixture.action", trace_id="test_fixture.trace",
        action_type="test_fixture.type", payload={"test_fixture.key": "test_fixture.value"},
        principal=FieldValue("test_fixture.principal", source_id="test_fixture.source",
                             trust=FieldTrust.PROPOSED),
    )


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


class BrokenZone(tzinfo):
    def __init__(self, failure):
        self.failure = failure

    def utcoffset(self, value):
        raise self.failure


def test_positive_recheck_is_a_frozen_observation_without_deadline():
    values = result()
    value = ConditionVerification(**values)
    assert value.valid_until is None
    assert value.evidence_refs == ("test_fixture.observation",)
    assert value.reason_codes == ()
    assert value.canonical_view() == expected_projection(values)
    assert not hasattr(value, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.state = State.UNSATISFIED
    for name in ("verify", "confirm", "authorize", "execute", "receipt_id", "verdict", "is_valid"):
        assert not hasattr(value, name)


def test_positive_attestation_within_declared_bounds():
    declared = attested()
    before = declared.canonical_view()
    values = result(condition=declared, valid_until=LIMIT - timedelta(hours=1))
    value = ConditionVerification(**values)
    assert value.condition is declared
    assert declared.canonical_view() == before
    assert value.canonical_view() == expected_projection(values)
    assert tuple(value.canonical_view()) == FIELDS
    assert value.canonical_view()["condition"] == before


def test_exact_five_member_vocabulary():
    assert [(member.name, member.value) for member in State] == [
        ("SATISFIED", "satisfied"), ("UNSATISFIED", "unsatisfied"),
        ("EVIDENCE_NOT_PRESENT", "evidence_not_present"),
        ("EVIDENCE_INVALID", "evidence_invalid"),
        ("EVIDENCE_UNAVAILABLE", "evidence_unavailable"),
    ]
    assert all(type(member.value) is str for member in State)
    assert issubclass(State, str) and State("satisfied") is State.SATISFIED


@pytest.mark.parametrize("refs", [[], ["test_fixture.observation"]])
def test_unsatisfied_is_a_valid_negative_observation_without_authority(refs):
    value = ConditionVerification(**negative(State.UNSATISFIED, evidence_refs=refs))
    assert value.state is State.UNSATISFIED and value.valid_until is None
    assert value.evidence_refs == tuple(refs)
    assert not hasattr(value, "verdict") and not hasattr(value, "receipt_id")


def test_evidence_not_present_is_a_distinguished_absence():
    value = ConditionVerification(**negative(State.EVIDENCE_NOT_PRESENT))
    assert value.evidence_refs == () and value.reason_codes == ("test_fixture.reason",)


def test_evidence_invalid_retains_its_identified_reference():
    value = ConditionVerification(**negative(State.EVIDENCE_INVALID, evidence_refs=["test_fixture.presented"]))
    assert value.evidence_refs == ("test_fixture.presented",)


@pytest.mark.parametrize("refs", [[], ["test_fixture.dependency"]])
def test_evidence_unavailable_is_a_distinguished_availability_failure(refs):
    value = ConditionVerification(**negative(State.EVIDENCE_UNAVAILABLE, evidence_refs=refs))
    assert value.state is State.EVIDENCE_UNAVAILABLE and value.valid_until is None


@pytest.mark.parametrize("declared", ["recheck", "attestation"])
def test_positive_observation_requires_a_reference(declared):
    changes = {} if declared == "recheck" else dict(condition=attested(), valid_until=LIMIT)
    invalid(result(evidence_refs=[], **changes))


@pytest.mark.parametrize("state", NEGATIVE)
@pytest.mark.parametrize("fault", ["empty_reasons", "deadline"])
def test_negative_states_require_reasons_and_no_deadline(state, fault):
    if fault == "empty_reasons":
        invalid(negative(state, reason_codes=[]))
    else:
        invalid(negative(state, condition=attested(), valid_until=LIMIT))


def test_absence_state_cannot_present_a_reference():
    invalid(negative(State.EVIDENCE_NOT_PRESENT, evidence_refs=["test_fixture.presented"]))


@pytest.mark.parametrize("kind", ["mapping", "none", "projection", "subclass"])
def test_nested_declaration_is_admitted_before_any_read(kind):
    calls = []

    class WrongCondition(PolicyCondition):
        def canonical_view(self):
            calls.append("projection")
            raise RuntimeError(SECRET)

        @property
        def validity_mode(self):
            calls.append("validity_mode")
            raise RuntimeError(SECRET)

        @property
        def attestation_not_after(self):
            calls.append("attestation_not_after")
            raise RuntimeError(SECRET)

    wrong = {
        "mapping": {}, "none": None, "projection": condition().canonical_view(),
        "subclass": object.__new__(WrongCondition),
    }[kind]
    invalid(result(condition=wrong))
    assert calls == []


class OtherState(str, Enum):
    SATISFIED = "satisfied"


@pytest.mark.parametrize("bad", ["satisfied", OtherState.SATISFIED, ConditionValidityMode.RECHECK, None, 1])
def test_state_requires_the_exact_enum(bad):
    invalid(result(state=bad))


@pytest.mark.parametrize("field", ["checked_at", "valid_until"])
@pytest.mark.parametrize("kind", ["naive", "text", "broken_zone", "overflow"])
def test_naive_or_malformed_timestamps_reject_without_raw_error(field, kind):
    bad = {
        "naive": datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        "text": "2026-09-23T12:00:00Z",
        "broken_zone": datetime(2026, 9, 23, tzinfo=BrokenZone(RuntimeError(SECRET))),
        "overflow": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    }[kind]
    changes = dict(condition=attested(), valid_until=LIMIT)
    changes[field] = bad
    error = invalid(result(**changes))
    assert SECRET not in repr(error)


def test_checked_at_is_mandatory_not_optional():
    invalid(result(checked_at=None))


@pytest.mark.parametrize("micros", [0, 123456])
def test_equivalent_offsets_share_one_utc_representation(micros):
    checked = datetime(2026, 1, 1, 0, 30, 0, micros, tzinfo=timezone.utc)
    until = checked + timedelta(hours=1)
    shifted = timezone(timedelta(hours=-3))
    one = ConditionVerification(**result(condition=attested(), checked_at=checked, valid_until=until))
    two = ConditionVerification(**result(
        condition=attested(), checked_at=checked.astimezone(shifted), valid_until=until.astimezone(shifted)))
    assert one.canonical_view() == two.canonical_view()
    assert two.checked_at.tzinfo is timezone.utc and two.valid_until.tzinfo is timezone.utc
    assert one.canonical_view()["checked_at"] == f"2026-01-01T00:30:00.{micros:06}Z"
    assert one.canonical_view()["valid_until"] == f"2026-01-01T01:30:00.{micros:06}Z"


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(microseconds=1)])
def test_attestation_deadline_must_follow_checked_at(offset):
    invalid(result(condition=attested(), valid_until=CHECKED + offset))


def test_deadline_equal_to_declared_bound_is_an_exclusive_expiry_declaration():
    value = ConditionVerification(**result(condition=attested(), valid_until=LIMIT))
    assert value.valid_until == value.condition.attestation_not_after
    assert not hasattr(value, "is_current") and not hasattr(value, "expired")


def test_deadline_later_than_declared_bound_is_rejected():
    invalid(result(condition=attested(), valid_until=LIMIT + timedelta(microseconds=1)))


def test_positive_recheck_cannot_carry_a_reusable_deadline():
    invalid(result(valid_until=LIMIT))


def test_historical_values_are_representable():
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    value = ConditionVerification(**result(
        condition=attested(past + timedelta(days=1)), checked_at=past, valid_until=past + timedelta(hours=1)))
    assert value.canonical_view()["checked_at"] == "2000-01-01T00:00:00.000000Z"
    recheck = ConditionVerification(**result(checked_at=past))
    assert recheck.canonical_view()["valid_until"] is None


@pytest.mark.parametrize("field", ["evidence_refs", "reason_codes"])
@pytest.mark.parametrize("kind", ["str", "bytes", "bytearray", "memoryview", "mapping", "generator", "set", "none", "broken", "short", "long", "infinite"])
def test_sequence_fields_reject_unsuitable_containers_without_unbounded_consumption(field, kind):
    seen = []

    def stream():
        seen.append("consumed")
        yield "test_fixture.item"

    class Infinite(CountedSequence):
        def __getitem__(self, index):
            self.indices.append(index)
            return f"test_fixture.item.{index}"

    values = {
        "str": "test_fixture.item", "bytes": b"x", "bytearray": bytearray(b"x"),
        "memoryview": memoryview(b"x"), "mapping": {"test_fixture.item": 1},
        "generator": stream(), "set": {"test_fixture.item"}, "none": None,
        "broken": CountedSequence(["test_fixture.item"], failure=RuntimeError(SECRET)),
        "short": CountedSequence([], size=1),
        "long": CountedSequence(["test_fixture.a", "test_fixture.b"], size=1),
        "infinite": Infinite([], size=1),
    }[kind]
    invalid(result(**{field: values}))
    assert seen == []
    if kind == "infinite":
        assert values.indices == list(range(257))


def test_mapping_sequence_hybrid_is_rejected_without_access():
    calls = []

    class Hybrid(Sequence, Mapping):
        def __len__(self):
            calls.append("len")
            return 1

        def __getitem__(self, key):
            calls.append(key)
            return "test_fixture.item"

        def __iter__(self):
            calls.append("iter")
            return iter(["test_fixture.item"])

    invalid(result(evidence_refs=Hybrid()))
    assert calls == []


@pytest.mark.parametrize("field", ["evidence_refs", "reason_codes"])
@pytest.mark.parametrize("bad", [None, 1, "", " x", "x ", "x\n", "x\x00", "x\x7f", "\u00e9" * 65, "\ud800"])
def test_each_identifier_follows_the_existing_rule(field, bad):
    invalid(result(**{field: ["test_fixture.valid", bad]}))


@pytest.mark.parametrize("field", ["evidence_refs", "reason_codes"])
def test_utf8_identifier_byte_boundary(field):
    assert len(("\u00e9" * 64).encode()) == 128
    value = ConditionVerification(**result(**{field: ["\u00e9" * 64, "test_fixture.x"]}))
    assert "\u00e9" * 64 in getattr(value, field)
    invalid(result(**{field: ["\u00e9" * 64 + "x"]}))


@pytest.mark.parametrize("field", ["evidence_refs", "reason_codes"])
def test_duplicates_are_rejected_not_dropped(field):
    item = "test_fixture.item"
    invalid(result(**{field: [item, item]}))
    invalid(result(**{field: [item, "".join(["test_fixture.", "item"])]}))


@pytest.mark.parametrize("field", ["evidence_refs", "reason_codes"])
def test_count_boundary_and_sorted_detached_retention(field):
    items = [f"test_fixture.{index:03}" for index in reversed(range(256))]
    value = ConditionVerification(**result(**{field: CountedSequence(items)}))
    assert getattr(value, field) == tuple(sorted(items))
    excess = CountedSequence(items + ["test_fixture.extra"])
    invalid(result(**{field: excess}))
    assert excess.indices == []


def test_inputs_and_returned_views_are_detached():
    refs, reasons = ["test_fixture.b", "test_fixture.a"], ["test_fixture.reason"]
    value = ConditionVerification(**result(evidence_refs=refs, reason_codes=reasons))
    before = value.canonical_view()
    refs.clear()
    reasons.append("test_fixture.changed")
    view = value.canonical_view()
    view["evidence_refs"].clear()
    view["condition"]["origins"].clear()
    view["condition"]["binding"]["origins"].clear()
    assert value.canonical_view() == before
    assert value.evidence_refs == ("test_fixture.a", "test_fixture.b")
    assert isinstance(value.evidence_refs, tuple) and isinstance(value.reason_codes, tuple)


def sized_result(target):
    # Each identifier starts at the 128-byte limit and keeps a unique prefix when trimmed.
    refs = [f"test_fixture.e{index:03}." + "x" * 110 for index in range(256)]
    reasons = [f"test_fixture.r{index:03}." + "x" * 110 for index in range(256)]
    values = result(evidence_refs=refs, reason_codes=reasons)
    excess = encoded_size(values) - target
    assert excess > 0
    for rows in (refs, reasons):
        for index, item in enumerate(rows):
            cut = min(excess, 100)
            rows[index] = item[:len(item) - cut]
            excess -= cut
    assert excess == 0
    assert encoded_size(values) == target
    return values


def test_exact_aggregate_limit_and_one_excess_byte():
    exact = sized_result(65536)
    value = ConditionVerification(**exact)
    assert len(json.dumps(value.canonical_view(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) == 65536
    excess = sized_result(65537)
    assert len(json.dumps(excess["condition"].canonical_view(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) < 65536
    invalid(excess)


def test_owning_modules_and_forbidden_locations():
    assert policy.ConditionVerification is condition_results.ConditionVerification
    assert policy.ConditionVerificationState is condition_results.ConditionVerificationState
    assert policy.ConditionVerifier is contracts.ConditionVerifier
    authorization_contracts = importlib.import_module("sett.authorization_ruler.contracts")
    for name in ("ConditionVerification", "ConditionVerificationState", "ConditionVerifier"):
        assert not hasattr(authorization, name)
        assert not hasattr(authorization_contracts, name)
        assert not hasattr(sett, name)
        assert not hasattr(conditions, name)


def test_exact_final_protocol_signatures():
    verify = inspect.signature(ConditionVerifier.verify)
    confirm = inspect.signature(ConditionVerifier.confirm)
    assert [(p.name, p.kind) for p in verify.parameters.values()] == [
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("request", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("condition", inspect.Parameter.KEYWORD_ONLY),
    ]
    assert [(p.name, p.kind) for p in confirm.parameters.values()] == [
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("verification", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("request", inspect.Parameter.KEYWORD_ONLY),
    ]
    for signature in (verify, confirm):
        assert all(p.default is inspect.Parameter.empty for p in signature.parameters.values())
    assert typing.get_type_hints(ConditionVerifier.verify) == {
        "request": AuthorizationRequest, "condition": PolicyCondition, "return": ConditionVerification,
    }
    assert typing.get_type_hints(ConditionVerifier.confirm) == {
        "verification": ConditionVerification, "request": AuthorizationRequest, "return": bool,
    }
    assert typing.get_type_hints(ConditionVerifier) == {"verifier_id": str, "verifier_version": str}


class NeutralVerifier:
    verifier_id = "test_fixture.verifier"
    verifier_version = "test_fixture.version"

    def __init__(self):
        self.seen = []

    def verify(self, request, *, condition):
        self.seen.append((request.payload_copy(), condition.checkpoint_id))
        return ConditionVerification(
            condition=condition, state=State.SATISFIED, checked_at=CHECKED, valid_until=None,
            evidence_refs=["test_fixture.local_observation"], reason_codes=[],
        )

    def confirm(self, verification, *, request):
        self.seen.append((verification.condition.checkpoint_id, request.action_id))
        return True


def test_neutral_callback_shape_observation_only():
    callback = NeutralVerifier()
    assert isinstance(callback, ConditionVerifier)
    declared = condition()
    observed = callback.verify(request(), condition=declared)
    assert callback.confirm(observed, request=request()) is True
    assert callback.seen == [
        ({"test_fixture.key": "test_fixture.value"}, "test_fixture.checkpoint"),
        ("test_fixture.checkpoint", "test_fixture.action"),
    ]
    assert observed.condition is declared


class MalformedVerifier:
    verifier_id = "test_fixture.verifier"
    verifier_version = "test_fixture.version"

    def verify(self):
        return {"state": "satisfied"}

    def confirm(self, *args, **kwargs):
        return 1


def test_protocol_membership_is_not_runtime_validation():
    callback = MalformedVerifier()
    assert isinstance(callback, ConditionVerifier)
    assert not isinstance(callback.verify(), ConditionVerification)
    assert type(callback.confirm()) is not bool
    assert not isinstance(object(), ConditionVerifier)


def test_exact_class_message_and_code():
    error = invalid(result(state="satisfied"))
    assert error.reason_code == "policy.condition_verification_invalid"
    assert error.args == ("The condition verification result is invalid.",)


@pytest.mark.parametrize("kind", ["length", "item", "zone", "value", "repr"])
def test_ordinary_failures_keep_no_cause_context_or_private_input(kind):
    class Length(CountedSequence):
        def __len__(self):
            raise RuntimeError(SECRET)

    class BadRepresentation:
        def __str__(self):
            raise RuntimeError(SECRET)

        def __repr__(self):
            raise RuntimeError(SECRET)

    changes = {
        "length": {"evidence_refs": Length(["test_fixture.item"])},
        "item": {"reason_codes": CountedSequence(["test_fixture.item"], failure=RuntimeError(SECRET))},
        "zone": {"checked_at": datetime(2026, 9, 23, tzinfo=BrokenZone(RuntimeError(SECRET)))},
        "value": {"evidence_refs": [" " + SECRET]},
        "repr": {"evidence_refs": [BadRepresentation()]},
    }[kind]
    error = invalid(result(**changes))
    assert SECRET not in repr(error) and SECRET not in str(error)


@pytest.mark.parametrize("kind", ["scalar", "container"])
def test_exception_handled_by_the_caller_is_not_chained(kind):
    changes = {
        "scalar": {"state": None},
        "container": {"evidence_refs": CountedSequence(["test_fixture.item"], failure=RuntimeError(SECRET))},
    }[kind]
    caller = RuntimeError(CALLER_SECRET)
    try:
        raise caller
    except RuntimeError as handled:
        error = invalid(result(**changes))
        assert handled is caller and sys.exception() is caller
    assert caller.args == (CALLER_SECRET,) and caller.__context__ is None
    shown = "".join(traceback.format_exception(error))
    assert CALLER_SECRET not in shown and SECRET not in shown


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("boundary", ["refs_length", "refs_item", "reasons_length", "reasons_item", "checked_zone", "until_zone"])
def test_termination_signals_propagate_unchanged(signal, boundary):
    instance = signal("test_fixture.termination")

    class Length(CountedSequence):
        def __len__(self):
            raise instance

    zone = datetime(2026, 9, 23, tzinfo=BrokenZone(instance))
    changes = {
        "refs_length": {"evidence_refs": Length(["test_fixture.item"])},
        "refs_item": {"evidence_refs": CountedSequence(["test_fixture.item"], failure=instance)},
        "reasons_length": {"reason_codes": Length(["test_fixture.item"])},
        "reasons_item": {"reason_codes": CountedSequence(["test_fixture.item"], failure=instance)},
        "checked_zone": {"checked_at": zone},
        "until_zone": {"condition": attested(), "valid_until": zone},
    }[boundary]
    with pytest.raises(signal) as caught:
        ConditionVerification(**result(**changes))
    assert caught.value is instance


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_termination_signals_propagate_inside_a_caller_handler(signal):
    instance = signal("test_fixture.termination")
    with pytest.raises(signal) as caught:
        try:
            raise RuntimeError(CALLER_SECRET)
        except RuntimeError:
            ConditionVerification(**result(
                evidence_refs=CountedSequence(["test_fixture.item"], failure=instance)))
    assert caught.value is instance


@pytest.mark.parametrize("field", FIELDS)
def test_every_keyword_is_required(field):
    parameters = inspect.signature(ConditionVerification).parameters
    assert tuple(parameters) == FIELDS
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for p in parameters.values())
    values = result()
    del values[field]
    with pytest.raises(TypeError):
        ConditionVerification(**values)


def test_error_surface_and_unchanged_condition_diagnostic():
    with pytest.raises(TypeError):
        ConditionVerification(**result(), unexpected=True)
    error = invalid(result(evidence_refs=[]))
    assert isinstance(error, SETTConfigurationError) and isinstance(error, SETTError)
    assert not isinstance(error, authorization.SETTAuthorizationError)
    assert error.verdict is None and not hasattr(error, "receipt_id")
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        attested(limit=None)
    assert caught.value.reason_code == "policy.condition_invalid"
    assert caught.value.args == ("The policy condition declaration is invalid.",)


def test_public_names_and_owning_modules():
    new = {"ConditionVerification", "ConditionVerificationState", "ConditionVerifier"}
    assert new <= set(policy.__all__)
    assert ConditionVerification.__module__ == "sett.policy_ruler.condition_results"
    assert ConditionVerificationState.__module__ == "sett.policy_ruler.condition_results"
    assert ConditionVerifier.__module__ == "sett.policy_ruler.contracts"
    assert policy.PolicyCondition is conditions.PolicyCondition
    assert policy.ConditionValidityMode is conditions.ConditionValidityMode
    assert policy.AuthorizationPolicy is contracts.AuthorizationPolicy is authorization.AuthorizationPolicy
    assert policy.PolicySelectionResolver is authorization.PolicySelectionResolver
    assert policy.AuthorizationPolicy.__module__ == "sett.policy_ruler.contracts"
