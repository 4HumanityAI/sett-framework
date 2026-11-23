"""Policy identity cases, without effect authority."""

import json
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, replace
from inspect import Parameter, signature

import pytest

import sett
from sett.authorization_ruler import (
    PolicyBinding,
    PolicyOrigin,
    PolicySourceReference,
    SETTAuthorizationConfigurationError,
)


def source(**changes):
    fields = {
        "source_id": "instrument",
        "source_version": "edition-1",
        "locator": "4.2",
    }
    return PolicySourceReference(**(fields | changes))


def origin(**changes):
    fields = {
        "requirement_id": "requirement",
        "requirement_revision": "r1",
        "layer_id": "internal",
        "source": None,
        "source_absence_reason": "Synthetic test fixture",
    }
    return PolicyOrigin(**(fields | changes))


def binding(**changes):
    fields = {
        "policy_id": "policy",
        "policy_version": "edition-blue",
        "contract_version": "2.0",
        "origins": [origin()],
        "profile_id": None,
        "profile_revision": None,
    }
    return PolicyBinding(**(fields | changes))


def assert_error(suffix, constructor, **fields):
    with pytest.raises(SETTAuthorizationConfigurationError) as caught:
        constructor(**fields)
    error = caught.value
    assert error.reason_code == f"authorization.{suffix}"
    assert error.verdict == "block"
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


INVALID_IDS = [
    None,
    7,
    True,
    b"id",
    "",
    " x",
    "x ",
    "a\x00b",
    "a\x7fb",
    "x" * 129,
    "\u00e9" * 65,
    "\ud800",
]
INVALID_TEXT = [
    None,
    7,
    b"text",
    "",
    " x",
    "x ",
    "a\nb",
    "a\x7fb",
    "x" * 1025,
    "\u00e9" * 513,
    "\ud800",
]


def test_internal_origin_has_explicit_absence():
    assert binding().canonical_view() == {
        "policy_id": "policy",
        "policy_version": "edition-blue",
        "contract_version": "2.0",
        "profile_id": None,
        "profile_revision": None,
        "origins": [
            {
                "requirement_id": "requirement",
                "requirement_revision": "r1",
                "layer_id": "internal",
                "source": None,
                "source_absence_reason": "Synthetic test fixture",
            }
        ],
    }


def test_institutional_origin_records_declarations_only():
    item = origin(source=source(), source_absence_reason=None)
    value = binding(origins=[item], profile_id="profile", profile_revision="draft-1")
    assert value.canonical_view()["origins"][0]["source"] == {
        "source_id": "instrument",
        "source_version": "edition-1",
        "locator": "4.2",
    }
    assert value.profile_id == "profile"
    assert value.profile_revision == "draft-1"
    assert not hasattr(value, "receipt")
    assert not hasattr(value, "verdict")
    assert not hasattr(value, "evaluate")


@pytest.mark.parametrize("has_source,reason", [(False, None), (True, "not absent")])
def test_source_and_absence_are_exclusive(has_source, reason):
    assert_error(
        "policy_origin_invalid",
        origin,
        source=source() if has_source else None,
        source_absence_reason=reason,
    )


def test_shared_source_preserves_requirements_and_layers():
    first = origin(source=source(), source_absence_reason=None)
    second = replace(first, layer_id="voluntary", requirement_id="another")
    value = binding(origins=[second, first])
    assert value.origins == (first, second)


def test_conflicting_requirement_revisions_are_not_selected():
    first = origin(source=source(), source_absence_reason=None)
    second = replace(first, requirement_revision="r2", source=source(locator="5.1"))
    assert_error("policy_binding_invalid", binding, origins=[first, second])
    assert len(binding(origins=[first, replace(second, layer_id="other")]).origins) == 2


@pytest.mark.parametrize("version", ["1.0", "2.1", "two", "\u00e9" * 64])
def test_well_formed_unsupported_version_has_own_code(version):
    assert_error("policy_contract_unsupported", binding, contract_version=version)


@pytest.mark.parametrize("version", INVALID_IDS)
def test_malformed_contract_version_is_binding_invalid(version):
    assert_error("policy_binding_invalid", binding, contract_version=version)


@pytest.mark.parametrize("name", ["profile_id", "profile_revision"])
@pytest.mark.parametrize("value", INVALID_IDS)
def test_profile_fields_must_be_valid_and_paired(name, value):
    fields = {"profile_id": "profile", "profile_revision": "r1", name: value}
    assert_error("policy_binding_invalid", binding, **fields)


@pytest.mark.parametrize("name", ["source_id", "source_version"])
@pytest.mark.parametrize("value", INVALID_IDS)
def test_source_identifiers_have_source_error(name, value):
    assert_error("policy_source_invalid", source, **{name: value})


@pytest.mark.parametrize("name", ["requirement_id", "requirement_revision", "layer_id"])
@pytest.mark.parametrize("value", INVALID_IDS)
def test_origin_identifiers_have_origin_error(name, value):
    assert_error("policy_origin_invalid", origin, **{name: value})


@pytest.mark.parametrize("name", ["policy_id", "policy_version"])
@pytest.mark.parametrize("value", INVALID_IDS)
def test_binding_identifiers_have_binding_error(name, value):
    assert_error("policy_binding_invalid", binding, **{name: value})


@pytest.mark.parametrize("value", INVALID_TEXT)
def test_locator_has_source_error(value):
    assert_error("policy_source_invalid", source, locator=value)


@pytest.mark.parametrize("value", INVALID_TEXT)
def test_absence_reason_has_origin_error(value):
    assert_error("policy_origin_invalid", origin, source_absence_reason=value)


@pytest.mark.parametrize(
    "values",
    [
        None,
        [],
        "origins",
        b"origins",
        bytearray(b"a"),
        memoryview(b"a"),
        {},
        [None],
        ["origin"],
    ],
)
def test_origins_reject_invalid_containers_and_members(values):
    assert_error("policy_binding_invalid", binding, origins=values)


def test_generator_is_rejected_without_consumption():
    consumed = []

    def generate():
        consumed.append(True)
        yield origin()

    assert_error("policy_binding_invalid", binding, origins=generate())
    assert consumed == []


def test_sequence_failure_does_not_leak_raw_error():
    class BrokenSequence(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            raise RuntimeError("confidential input")

    error = assert_error("policy_binding_invalid", binding, origins=BrokenSequence())
    assert "confidential input" not in str(error)


def test_inconsistent_sequence_is_bounded():
    calls = []

    class EndlessSequence(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            calls.append(index)
            return origin()

    assert_error("policy_binding_invalid", binding, origins=EndlessSequence())
    assert len(calls) == 257


def test_nested_subclasses_cannot_run_overridden_projections():
    class OtherSource(PolicySourceReference):
        def canonical_view(self):
            pytest.fail("Subclass projection must never execute")

    class OtherOrigin(PolicyOrigin):
        def canonical_view(self):
            pytest.fail("Subclass projection must never execute")

    child_source = OtherSource(**source().canonical_view())
    assert_error(
        "policy_origin_invalid", origin, source=child_source, source_absence_reason=None
    )
    child_origin = OtherOrigin(**origin().canonical_view())
    assert_error("policy_binding_invalid", binding, origins=[child_origin])
    assert_error("policy_origin_invalid", origin, source={}, source_absence_reason=None)


def test_canonical_byte_boundary_includes_the_complete_value():
    items = [origin(requirement_id=f"r{i:03d}") for i in range(64)]
    fields = binding(origins=items).canonical_view()
    size = len(
        json.dumps(
            fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    remaining = 65_536 - size
    for index, item in enumerate(items):
        extra = min(remaining, 1024 - len(item.source_absence_reason))
        items[index] = replace(
            item, source_absence_reason=item.source_absence_reason + "x" * extra
        )
        remaining -= extra
    assert remaining == 0
    exact = binding(origins=items)
    assert (
        len(
            json.dumps(
                exact.canonical_view(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        == 65_536
    )
    items[-1] = replace(
        items[-1], source_absence_reason=items[-1].source_absence_reason + "x"
    )
    assert_error("policy_binding_invalid", binding, origins=items)


def test_inputs_and_every_projection_are_detached():
    ref = source()
    item = origin(source=ref, source_absence_reason=None)
    caller = [item]
    value = binding(origins=caller)
    expected = value.canonical_view()
    caller.clear()
    view = value.canonical_view()
    view["origins"][0]["source"]["locator"] = "changed"
    view["origins"].clear()
    ref.canonical_view()["source_version"] = "changed"
    item.canonical_view()["requirement_id"] = "changed"
    assert value.canonical_view() == expected
    assert value.origins == (item,)
    for model, name in [(ref, "locator"), (item, "layer_id"), (value, "policy_id")]:
        with pytest.raises(FrozenInstanceError):
            setattr(model, name, "changed")
        assert not hasattr(model, "__dict__")


def test_canonical_order_is_independent_of_input_order():
    absent = origin()
    first = origin(source=source(), source_absence_reason=None)
    second = replace(first, source=source(locator="9"))
    third = replace(first, source=source(source_version="edition-2"))
    fourth = replace(first, source=source(source_id="z"))
    fifth = replace(first, requirement_id="z")
    sixth = replace(first, layer_id="z")
    expected = (absent, first, second, third, fourth, fifth, sixth)
    assert binding(origins=list(reversed(expected))).origins == expected
    assert (
        binding(origins=expected).canonical_view()
        == binding(origins=list(reversed(expected))).canonical_view()
    )


def test_exact_utf8_field_limits_are_accepted():
    identifier = "\u00e9" * 64
    text = "\u00e9" * 512
    ref = source(source_id=identifier, source_version=identifier, locator=text)
    item = origin(
        requirement_id=identifier,
        requirement_revision=identifier,
        layer_id=identifier,
        source_absence_reason=text,
    )
    value = binding(
        policy_id=identifier,
        policy_version=identifier,
        origins=[item],
        profile_id=identifier,
        profile_revision=identifier,
    )
    assert value.origins[0].source_absence_reason == text
    assert ref.locator == text


def test_origin_count_boundary():
    items = [
        origin(requirement_id=f"r{i:03d}", source_absence_reason="internal")
        for i in range(257)
    ]
    assert len(binding(origins=items[:256]).origins) == 256
    assert_error("policy_binding_invalid", binding, origins=items)


@pytest.mark.parametrize("change", ["source", "requirement", "policy", "profile"])
def test_revision_changes_are_recorded_without_rewriting_history(change):
    item = origin(source=source(), source_absence_reason=None)
    old = binding(origins=[item], profile_id="profile", profile_revision="r1")
    snapshot = old.canonical_view()
    if change == "source":
        new = replace(
            old, origins=[replace(item, source=source(source_version="edition-2"))]
        )
    elif change == "requirement":
        new = replace(old, origins=[replace(item, requirement_revision="r2")])
    elif change == "policy":
        new = replace(old, policy_version="next")
    else:
        new = replace(old, profile_revision="r2")
    assert old.canonical_view() == snapshot
    assert new.canonical_view() != snapshot


def test_distinct_provisions_survive_but_duplicate_links_fail():
    first = origin(source=source(), source_absence_reason=None)
    second = replace(first, source=source(locator="5.1"))
    assert len(binding(origins=[first, second]).origins) == 2
    assert_error("policy_binding_invalid", binding, origins=[first, first])
    assert_error(
        "policy_binding_invalid",
        binding,
        origins=[origin(), origin(source_absence_reason="Other reason")],
    )


def test_error_messages_never_echo_supplied_values():
    secret = "private-source-value "
    for constructor, fields, suffix in [
        (source, {"source_id": secret}, "policy_source_invalid"),
        (origin, {"requirement_id": secret}, "policy_origin_invalid"),
        (binding, {"policy_id": secret}, "policy_binding_invalid"),
        (binding, {"contract_version": secret.strip()}, "policy_contract_unsupported"),
    ]:
        assert "private-source-value" not in str(
            assert_error(suffix, constructor, **fields)
        )


def test_owning_field_validation_precedes_cross_field_rules():
    assert_error(
        "policy_binding_invalid", binding, contract_version="1.0", profile_id=7
    )
    assert_error(
        "policy_binding_invalid", binding, contract_version="1.0", origins=[{}]
    )
    assert_error(
        "policy_origin_invalid", origin, source=source(), source_absence_reason="\ud800"
    )


@pytest.mark.parametrize("model", [PolicySourceReference, PolicyOrigin, PolicyBinding])
def test_public_constructors_require_explicit_keyword_fields(model):
    for parameter in signature(model).parameters.values():
        assert parameter.kind is Parameter.KEYWORD_ONLY
        assert parameter.default is Parameter.empty
    assert not hasattr(sett, model.__name__)
