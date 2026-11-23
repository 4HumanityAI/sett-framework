"""Condition declaration cases; no real verifier, producer or governed effect."""

import dataclasses
import inspect
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum

import pytest

import sett
import sett.authorization_ruler as authorization
import sett.policy_ruler as policy
from sett.exceptions import (
    SETTConfigurationError,
    SETTError,
    SETTPolicyConfigurationError,
)
from sett.policy_ruler import (
    ConditionValidityMode,
    PolicyBinding,
    PolicyCondition,
    PolicyOrigin,
    PolicySourceReference,
    conditions,
)

CODE = "policy.condition_invalid"
MESSAGE = "The policy condition declaration is invalid."
IDENTIFIERS = (
    "condition_id", "verifier_id", "verifier_version", "action_id", "action_type",
    "checkpoint_id",
)
FIELDS = (
    "condition_id", "binding", "origins", "verifier_id", "verifier_version",
    "required", "action_id", "action_type", "checkpoint_id", "validity_mode",
    "attestation_not_after",
)


def origin(index=0, **changes):
    values = dict(
        requirement_id=f"test_fixture.requirement.{index:03}",
        requirement_revision="test_fixture.revision", layer_id="test_fixture.layer",
        source=None, source_absence_reason="test_fixture.synthetic",
    )
    values.update(changes)
    return PolicyOrigin(**values)


def binding(origins=None, **changes):
    values = dict(
        policy_id="test_fixture.policy", policy_version="test_fixture.version",
        contract_version="2.0", origins=[origin()] if origins is None else origins,
        profile_id=None, profile_revision=None,
    )
    values.update(changes)
    return PolicyBinding(**values)


def declaration(**changes):
    bound = binding()
    values = dict(
        condition_id="test_fixture.condition", binding=bound, origins=list(bound.origins),
        verifier_id="test_fixture.verifier", verifier_version="test_fixture.version",
        required=True, action_id="test_fixture.action", action_type="test_fixture.type",
        checkpoint_id="test_fixture.checkpoint", validity_mode=ConditionValidityMode.RECHECK,
        attestation_not_after=None,
    )
    values.update(changes)
    return values


def invalid(values):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        PolicyCondition(**values)
    error = caught.value
    assert error.reason_code == CODE
    assert error.args == (MESSAGE,)
    assert str(error) == MESSAGE
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


def expected_projection(values):
    result = dict(values)
    result["binding"] = values["binding"].canonical_view()
    result["origins"] = [item.canonical_view() for item in values["origins"]]
    result["validity_mode"] = values["validity_mode"].value
    timestamp = values["attestation_not_after"]
    result["attestation_not_after"] = (
        timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if timestamp is not None else None
    )
    return result


def encoded_size(values):
    return len(json.dumps(expected_projection(values), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def boundary_declaration(target):
    # Each absence string occurs in the binding and in the explicit origin subset.
    items = [origin(i) for i in range(48)]
    values = declaration(binding=binding(items), origins=items)
    remaining = target - encoded_size(values)
    assert remaining > 0
    if remaining % 2:
        values["condition_id"] += "x"
        remaining -= 1
    for i, item in enumerate(items):
        increment = min(remaining // 2, 1024 - len(item.source_absence_reason.encode()))
        items[i] = dataclasses.replace(item, source_absence_reason=item.source_absence_reason + "x" * increment)
        remaining -= increment * 2
    assert remaining == 0
    values["binding"] = binding(items)
    assert encoded_size(values) == target
    return values


def test_recheck_is_only_a_detached_declaration():
    values = declaration()
    value = PolicyCondition(**values)
    assert value.canonical_view() == expected_projection(values)
    assert set(value.canonical_view()) == set(FIELDS)
    assert value.canonical_view()["attestation_not_after"] is None
    for name in ("verify", "authorize", "execute", "receipt_id", "verdict"):
        assert not hasattr(value, name)


def test_attestation_is_not_a_verified_attestation():
    stamp = datetime(2026, 9, 23, 12, 1, 2, 3, tzinfo=timezone(timedelta(hours=2)))
    values = declaration(validity_mode=ConditionValidityMode.ATTESTATION, attestation_not_after=stamp)
    value = PolicyCondition(**values)
    assert value.attestation_not_after == datetime(2026, 9, 23, 10, 1, 2, 3, tzinfo=timezone.utc)
    assert value.attestation_not_after.tzinfo is timezone.utc
    assert value.canonical_view() == expected_projection(values)
    assert not hasattr(value, "verified")


@pytest.mark.parametrize("field", IDENTIFIERS)
@pytest.mark.parametrize("bad", [None, 1, "", " x", "x ", "x\n", "x\x00", "x\x7f", "\u00e9" * 65, "\ud800"])
def test_each_identifier_rejects_malformed_values(field, bad):
    invalid(declaration(**{field: bad}))


@pytest.mark.parametrize("kind", ["mapping", "none", "subclass"])
def test_binding_is_admitted_before_projection(kind):
    calls = []

    class WrongBinding(PolicyBinding):
        def canonical_view(self):
            calls.append("projection")
            raise RuntimeError("test_fixture.secret")

    wrong = {"mapping": {}, "none": None, "subclass": object.__new__(WrongBinding)}[kind]
    invalid(declaration(binding=wrong))
    assert calls == []


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


@pytest.mark.parametrize("kind", ["empty", "excess", "generator", "str", "bytes", "bytearray", "memoryview", "mapping", "broken", "short", "long", "infinite"])
def test_origin_container_admission_and_bounded_traversal(kind):
    seen = []

    def stream():
        seen.append("consumed")
        yield origin()

    class Infinite(CountedSequence):
        def __getitem__(self, index):
            self.indices.append(index)
            return origin()

    choices = {
        "empty": [], "excess": CountedSequence([origin()] * 257),
        "generator": stream(), "str": "test_fixture.origins", "bytes": b"x",
        "bytearray": bytearray(b"x"), "memoryview": memoryview(b"x"),
        "mapping": {"test_fixture.key": origin()},
        "broken": CountedSequence([origin()], failure=RuntimeError("test_fixture.secret")),
        "short": CountedSequence([], size=1), "long": CountedSequence([origin(), origin()], size=1),
        "infinite": Infinite([origin()], size=1),
    }
    values = choices[kind]
    invalid(declaration(origins=values))
    assert seen == []
    if isinstance(values, CountedSequence):
        assert len(values.indices) <= 257
    if kind == "excess":
        assert values.indices == []
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
            return origin()

        def __iter__(self):
            calls.append("iter")
            return iter([origin()])

    invalid(declaration(origins=Hybrid()))
    assert calls == []


@pytest.mark.parametrize("stage", ["length", "iterator"])
def test_container_protocol_failures_keep_fixed_diagnostic(stage):
    class BrokenSequence(CountedSequence):
        def __len__(self):
            if stage == "length":
                raise RuntimeError("test_fixture.secret")
            return 1

        def __iter__(self):
            raise RuntimeError("test_fixture.secret")

    invalid(declaration(origins=BrokenSequence([origin()])))


@pytest.mark.parametrize("kind", ["mapping", "subclass"])
def test_nested_origin_admission_precedes_projection(kind):
    calls = []

    class WrongOrigin(PolicyOrigin):
        def canonical_view(self):
            calls.append("projection")
            raise RuntimeError("test_fixture.secret")

    wrong = {} if kind == "mapping" else object.__new__(WrongOrigin)
    invalid(declaration(origins=[wrong]))
    assert calls == []


@pytest.mark.parametrize("separate", [False, True])
def test_canonical_duplicate_origins_are_not_deduplicated(separate):
    one = origin()
    other = dataclasses.replace(one) if separate else one
    if separate:
        assert one is not other and one == other
    invalid(declaration(origins=[one, other]))


@pytest.mark.parametrize("field", ["requirement_revision", "layer_id", "source", "source_absence_reason"])
def test_full_provenance_membership(field):
    values = {field: "test_fixture.changed"}
    if field == "source":
        values = dict(source=PolicySourceReference(source_id="test_fixture.source", source_version="test_fixture.edition", locator="test_fixture.section"), source_absence_reason=None)
    invalid(declaration(origins=[origin(**values)]))


def test_source_locator_is_part_of_membership():
    source = PolicySourceReference(source_id="test_fixture.source", source_version="test_fixture.edition", locator="test_fixture.section.1")
    one = origin(source=source, source_absence_reason=None)
    other = dataclasses.replace(one, source=dataclasses.replace(source, locator="test_fixture.section.2"))
    invalid(declaration(binding=binding([one]), origins=[other]))


def test_subset_follows_binding_order_without_losing_full_provenance():
    items = [origin(i) for i in range(3)]
    bound = binding(list(reversed(items)))
    value = PolicyCondition(**declaration(binding=bound, origins=[dataclasses.replace(items[2]), items[0]]))
    assert value.origins == (items[0], items[2])
    assert len(value.canonical_view()["binding"]["origins"]) == 3
    assert len(value.canonical_view()["origins"]) == 2


@pytest.mark.parametrize("required", [False, True])
def test_boolean_role_is_not_authority(required):
    value = PolicyCondition(**declaration(required=required))
    assert value.required is required
    assert value.canonical_view()["required"] is required
    assert not hasattr(value, "verdict")


@pytest.mark.parametrize("bad", [0, 1, None, "true", object()])
def test_only_an_exact_boolean_is_admitted(bad):
    invalid(declaration(required=bad))


class OtherMode(str, Enum):
    RECHECK = "recheck"


@pytest.mark.parametrize("bad", ["recheck", "attestation", OtherMode.RECHECK, None, 1])
def test_mode_requires_the_exact_enum(bad):
    invalid(declaration(validity_mode=bad))


def test_recheck_cannot_declare_an_attestation_limit():
    invalid(declaration(attestation_not_after=datetime(2026, 9, 23, tzinfo=timezone.utc)))


def test_attestation_requires_an_explicit_limit():
    invalid(declaration(validity_mode=ConditionValidityMode.ATTESTATION))


class BrokenZone(tzinfo):
    def utcoffset(self, value):
        raise RuntimeError("test_fixture.secret")


@pytest.mark.parametrize("kind", ["naive", "not_datetime", "broken_zone", "overflow"])
def test_invalid_time_or_conversion_rejects_without_raw_context(kind):
    choices = {
        "naive": datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None),
        "not_datetime": "2026-01-01T00:00:00Z",
        "broken_zone": datetime(2026, 1, 1, tzinfo=BrokenZone()),
        "overflow": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    }
    invalid(declaration(validity_mode=ConditionValidityMode.ATTESTATION, attestation_not_after=choices[kind]))


@pytest.mark.parametrize("micros", [0, 123456])
def test_equivalent_offsets_and_year_boundary(micros):
    utc = datetime(2026, 1, 1, 0, 30, 0, micros, tzinfo=timezone.utc)
    other = utc.astimezone(timezone(timedelta(hours=-2)))
    one = PolicyCondition(**declaration(validity_mode=ConditionValidityMode.ATTESTATION, attestation_not_after=utc))
    two = PolicyCondition(**declaration(validity_mode=ConditionValidityMode.ATTESTATION, attestation_not_after=other))
    assert one.canonical_view() == two.canonical_view()
    assert one.canonical_view()["attestation_not_after"] == f"2026-01-01T00:30:00.{micros:06}Z"


def test_past_timestamp_is_representable_without_clock_authority():
    value = PolicyCondition(**declaration(validity_mode=ConditionValidityMode.ATTESTATION, attestation_not_after=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert value.canonical_view()["attestation_not_after"] == "2000-01-01T00:00:00.000000Z"
    assert not hasattr(value, "is_valid")


def test_inputs_and_nested_views_are_detached_and_value_is_frozen():
    values = declaration()
    value = PolicyCondition(**values)
    before = value.canonical_view()
    values["origins"].clear()
    changed = value.canonical_view()
    changed["origins"][0]["requirement_id"] = "test_fixture.changed"
    changed["binding"]["origins"].clear()
    assert value.canonical_view() == before
    assert isinstance(value.origins, tuple)
    assert not hasattr(value, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.required = False


@pytest.mark.parametrize("field", IDENTIFIERS)
def test_utf8_identifier_boundary(field):
    assert len(("\u00e9" * 64).encode()) == 128
    assert getattr(PolicyCondition(**declaration(**{field: "\u00e9" * 64})), field) == "\u00e9" * 64
    invalid(declaration(**{field: "\u00e9" * 64 + "x"}))


def test_count_admission_boundary_is_distinct_from_total_budget(monkeypatch):
    items = [origin(i, requirement_revision="r", layer_id="l", source_absence_reason="s") for i in range(256)]
    bound = binding(items)
    seen = []
    original = conditions._condition_subset

    def observe(binding_value, subset):
        seen.append(len(subset))
        return original(binding_value, subset)

    monkeypatch.setattr(conditions, "_condition_subset", observe)
    values = declaration(binding=bound, origins=CountedSequence(items))
    assert encoded_size(values) > 65536
    invalid(values)
    assert seen == [256]  # admitted count, subsequently rejected aggregate bytes
    seen.clear()
    excess = CountedSequence(items + [items[0]])
    invalid(declaration(binding=bound, origins=excess))
    assert seen == [] and excess.indices == []
    assert PolicyCondition(**declaration(binding=bound, origins=[items[0]])).origins == (items[0],)


def test_exact_aggregate_byte_boundary_and_one_excess_byte():
    exact = boundary_declaration(65536)
    assert len(json.dumps(PolicyCondition(**exact).canonical_view(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) == 65536
    excess = boundary_declaration(65537)
    invalid(excess)


def test_valid_nested_values_do_not_waive_aggregate_limit():
    values = boundary_declaration(65537)
    assert isinstance(values["binding"], PolicyBinding)
    assert len(json.dumps(values["binding"].canonical_view(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) < 65536
    assert all(type(item) is PolicyOrigin for item in values["origins"])
    invalid(values)


@pytest.mark.parametrize("fault", ["condition_id", "verifier_id", "required", "action_id", "validity_mode", "attestation_not_after"])
def test_scalar_failure_precedes_nested_access(fault):
    sequence = CountedSequence([origin()])
    bad = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None) if fault == "attestation_not_after" else object()
    invalid(declaration(binding=None, origins=sequence, **{fault: bad}))
    assert sequence.indices == []


def test_duplicate_and_membership_precede_mode_consistency(monkeypatch):
    # Trap the next phase: an invalid mode/time combination must not hide origin faults.
    seen = []
    original = conditions._condition_subset

    def observe(bound, subset):
        seen.append("entered")
        result = original(bound, subset)
        seen.append("completed")
        return result

    monkeypatch.setattr(conditions, "_condition_subset", observe)
    invalid(declaration(origins=[origin(), origin()], validity_mode=ConditionValidityMode.ATTESTATION))
    assert seen == ["entered"]
    seen.clear()
    invalid(declaration(origins=[origin(2)], validity_mode=ConditionValidityMode.ATTESTATION))
    assert seen == ["entered"]
    seen.clear()
    invalid(declaration(validity_mode=ConditionValidityMode.ATTESTATION))
    assert seen == ["entered", "completed"]


def test_aggregate_projection_is_last(monkeypatch):
    calls = []
    original = conditions._condition_projection

    def observe(*args):
        calls.append("projection")
        return original(*args)

    monkeypatch.setattr(conditions, "_condition_projection", observe)
    invalid(declaration(validity_mode=ConditionValidityMode.ATTESTATION))
    assert calls == []
    PolicyCondition(**declaration())
    assert calls == ["projection"]


@pytest.mark.parametrize("failed_field", [*IDENTIFIERS, None])
def test_scalar_order_and_precedence_over_nested_admission(monkeypatch, failed_field):
    calls = []
    original_identifier = conditions.validate_identifier
    original_origins = conditions._condition_origins

    def identifier(value, *, field):
        calls.append(field)
        return original_identifier(value, field=field)

    def origins(values):
        calls.append("origins")
        return original_origins(values)

    monkeypatch.setattr(conditions, "validate_identifier", identifier)
    monkeypatch.setattr(conditions, "_condition_origins", origins)
    if failed_field is not None:
        invalid(declaration(**{failed_field: ""}, origins=None))
        assert calls == list(IDENTIFIERS[:IDENTIFIERS.index(failed_field) + 1])
    else:
        PolicyCondition(**declaration())
        assert calls == [*IDENTIFIERS, "origins"]


def test_identity_is_policy_scoped_not_a_collection_registry():
    one = PolicyCondition(**declaration())
    two = PolicyCondition(**declaration(binding=binding(policy_id="test_fixture.other")))
    assert one.condition_id == two.condition_id and one != two
    assert PolicyCondition(**declaration()) == one


def test_unregistered_named_dependencies_do_not_trigger_verification():
    value = PolicyCondition(**declaration(verifier_id="test_fixture.unregistered", checkpoint_id="test_fixture.unregistered"))
    assert value.verifier_id == value.checkpoint_id == "test_fixture.unregistered"
    assert not hasattr(value, "register") and not hasattr(value, "verify")


@pytest.mark.parametrize("kind", ["value", "container", "timezone", "repr"])
def test_fixed_diagnostic_does_not_retain_private_input(kind):
    class BadRepresentation:
        def __str__(self):
            raise RuntimeError("test_fixture.secret")

        def __repr__(self):
            raise RuntimeError("test_fixture.secret")

    changes = {
        "value": {"condition_id": " test_fixture.secret"},
        "container": {"origins": CountedSequence([origin()], failure=RuntimeError("test_fixture.secret"))},
        "timezone": {"attestation_not_after": datetime(2026, 1, 1, tzinfo=BrokenZone())},
        "repr": {"condition_id": BadRepresentation()},
    }
    error = invalid(declaration(**changes[kind]))
    assert "test_fixture.secret" not in repr(error)
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_termination_signals_are_not_configuration_errors(signal):
    instance = signal("test_fixture.termination")
    with pytest.raises(signal) as caught:
        PolicyCondition(**declaration(origins=CountedSequence([origin()], failure=instance)))
    assert caught.value is instance


def test_same_object_exports_and_unchanged_legacy_binding():
    assert policy.PolicyCondition is conditions.PolicyCondition
    assert policy.ConditionValidityMode is conditions.ConditionValidityMode
    assert authorization.PolicyBinding is PolicyBinding
    for name in ("PolicyCondition", "ConditionValidityMode"):
        assert name in policy.__all__
        assert not hasattr(authorization, name)
        assert not hasattr(sett, name)
    assert hasattr(policy.PolicyDecision, "conditions")


@pytest.mark.parametrize("field", FIELDS)
def test_all_eleven_arguments_are_required_keyword_arguments(field):
    parameters = inspect.signature(PolicyCondition).parameters
    assert tuple(parameters) == FIELDS
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters.values())
    values = declaration()
    del values[field]
    with pytest.raises(TypeError):
        PolicyCondition(**values)


def test_unexpected_keyword_direct_enum_and_nested_constructor_errors():
    with pytest.raises(TypeError):
        PolicyCondition(**declaration(), unexpected=True)
    with pytest.raises(ValueError):
        ConditionValidityMode("test_fixture.invalid")
    with pytest.raises(authorization.SETTAuthorizationConfigurationError) as caught:
        PolicyCondition(**declaration(binding=binding(policy_id="")))
    assert caught.value.reason_code == "authorization.policy_binding_invalid"


def test_existing_error_class_and_exact_diagnostic():
    from sett.policy_ruler.errors import SETTPolicyConfigurationError as facade

    assert facade is policy.SETTPolicyConfigurationError is SETTPolicyConfigurationError
    error = invalid(declaration(required=1))
    assert type(error) is SETTPolicyConfigurationError
    assert isinstance(error, SETTConfigurationError)
    assert isinstance(error, SETTError)
    assert not isinstance(error, authorization.SETTAuthorizationError)
    assert not hasattr(error, "receipt_id") and error.verdict is None
