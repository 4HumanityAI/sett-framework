"""Policy selection value-contract cases using synthetic test_fixture identifiers.

These types do not select policies, govern decisions or authorize effects yet.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone, tzinfo
from inspect import Parameter, signature

import pytest

import sett
import sett.authorization_ruler as authorization
from sett.authorization_ruler import (
    FieldTrust,
    FieldValue,
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyOrigin,
    PolicySelection,
    SETTAuthorizationConfigurationError,
)


def binding(**changes):
    return PolicyBinding(
        **(
            {
                "policy_id": "test_fixture.policy",
                "policy_version": "test_fixture.v1",
                "contract_version": "2.0",
                "profile_id": None,
                "profile_revision": None,
                "origins": [
                    PolicyOrigin(
                        requirement_id="test_fixture.requirement",
                        requirement_revision="test_fixture.r1",
                        layer_id="test_fixture.layer",
                        source=None,
                        source_absence_reason="Synthetic test data only",
                    )
                ],
            }
            | changes
        )
    )


def applicability(**changes):
    return PolicyApplicability(
        **(
            {
                "policy_id": "test_fixture.policy",
                "policy_version": "test_fixture.v1",
                "state": PolicyApplicabilityState.APPLIES,
                "justification": "Synthetic test declaration",
                "fact_names": ["test_fixture.fact"],
                "evidence_refs": ["test_fixture.evidence"],
            }
            | changes
        )
    )


def fact(value=None, trust=FieldTrust.AUTHORITATIVE):
    return FieldValue(value, source_id="test_fixture.source", trust=trust)


def selection(**changes):
    return PolicySelection(
        **(
            {
                "selection_id": "test_fixture.selection",
                "selection_revision": "test_fixture.r1",
                "contract_version": "2.0",
                "authority_ref": "test_fixture.authority",
                "effective_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "action_id": "test_fixture.action",
                "action_type": "test_fixture.action_type",
                "required": [binding()],
                "informative": [],
                "applicability": [applicability()],
                "facts": {"test_fixture.fact": fact({"test_fixture.value": [1]})},
            }
            | changes
        )
    )


def error(code, factory, **changes):
    with pytest.raises(SETTAuthorizationConfigurationError) as caught:
        factory(**changes)
    value = caught.value
    assert value.reason_code == "authorization." + code
    assert value.verdict == "block"
    assert value.__context__ is None and value.__cause__ is None
    assert "test_fixture.secret" not in str(value)
    return value


def size(value):
    return len(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def test_full_projection_and_no_authority():
    value = selection()
    assert set(value.canonical_view()) == set(signature(PolicySelection).parameters)
    assert value.canonical_view()["effective_at"] == "2026-01-01T00:00:00.000000Z"
    assert value.canonical_view()["facts"]["test_fixture.fact"] == {
        "value": {"test_fixture.value": [1]},
        "source_id": "test_fixture.source",
        "trust": "authoritative",
    }
    for model in (value, applicability()):
        assert not hasattr(model, "evaluate")
        assert not hasattr(model, "receipt")
        assert not hasattr(model, "verdict")


@pytest.mark.parametrize("state", list(PolicyApplicabilityState))
def test_all_states_represent_declarations(state):
    assert applicability(state=state).canonical_view()["state"] == state.value


INVALID_ID = [
    None,
    True,
    1,
    b"test_fixture",
    "",
    " test_fixture.secret",
    "test_fixture.secret ",
    "test_fixture.\x00",
    "test_fixture.\x7f",
    "x" * 129,
    "\u00e9" * 65,
    "\ud800",
]


@pytest.mark.parametrize("name", ["policy_id", "policy_version"])
@pytest.mark.parametrize("value", INVALID_ID)
def test_applicability_invalid_identifiers(name, value):
    error("policy_applicability_invalid", applicability, **{name: value})


@pytest.mark.parametrize("value", [None, "applies", "unknown", 1, True])
def test_enum_is_not_coerced(value):
    error("policy_applicability_invalid", applicability, state=value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "",
        " test_fixture.secret",
        "test_fixture.secret\n",
        "test_fixture.\x7f",
        "x" * 1025,
        "\u00e9" * 513,
        "\ud800",
    ],
)
def test_justification_is_bounded(value):
    error("policy_applicability_invalid", applicability, justification=value)


@pytest.mark.parametrize("value", INVALID_ID)
def test_malformed_version_has_selection_error(value):
    error("policy_selection_invalid", selection, contract_version=value)


@pytest.mark.parametrize("value", ["1.0", "3.0", "test_fixture.unsupported"])
def test_well_formed_unsupported_version_has_distinct_error(value):
    error("policy_contract_unsupported", selection, contract_version=value)


@pytest.mark.parametrize(
    "name",
    ["selection_id", "selection_revision", "authority_ref", "action_id", "action_type"],
)
@pytest.mark.parametrize("value", INVALID_ID)
def test_selection_identifier_contract(name, value):
    error("policy_selection_invalid", selection, **{name: value})


@pytest.mark.parametrize(
    "change",
    [
        {"required": [binding(), binding()]},
        {"informative": [binding()]},
        {"informative": [binding(policy_version="test_fixture.v2")]},
        {"applicability": []},
        {"applicability": [applicability(), applicability()]},
        {"applicability": [applicability(policy_id="test_fixture.other")]},
        {"applicability": [applicability(policy_version="test_fixture.v2")]},
        {"facts": {}},
        {"facts": {"test_fixture.fact": None, "test_fixture.extra": None}},
    ],
)
def test_cross_references_never_infer_a_repair(change):
    error("policy_selection_invalid", selection, **change)


@pytest.mark.parametrize("name", ["fact_names", "evidence_refs"])
def test_duplicate_reference_is_invalid(name):
    error(
        "policy_applicability_invalid", applicability, **{name: ["test_fixture.x"] * 2}
    )


def test_input_order_and_mutation_cannot_change_values():
    one, two = binding(), binding(policy_id="test_fixture.other")
    first, second = applicability(), applicability(policy_id=two.policy_id)
    req = [one, two]
    apps = [first, second]
    facts = {"test_fixture.fact": fact({"test_fixture.nested": [1]})}
    a = selection(required=req, applicability=apps, facts=facts)
    b = selection(
        required=list(reversed(req)), applicability=list(reversed(apps)), facts=facts
    )
    expected = a.canonical_view()
    req.clear()
    apps.clear()
    facts.clear()
    projection = a.canonical_view()
    projection["required"][0]["origins"].clear()
    projection["facts"]["test_fixture.fact"]["value"]["test_fixture.nested"].append(2)
    projection["applicability"].clear()
    a.facts["test_fixture.fact"].value["test_fixture.nested"].append(3)
    assert a.canonical_view() == b.canonical_view() == expected
    with pytest.raises(TypeError):
        a.facts["test_fixture.fact"] = None
    for model, name in [(a, "selection_id"), (first, "state")]:
        with pytest.raises(FrozenInstanceError):
            setattr(model, name, "test_fixture.changed")
        assert not hasattr(model, "__dict__")
    assert applicability(
        fact_names=["test_fixture.b", "test_fixture.a"]
    ).fact_names == ("test_fixture.a", "test_fixture.b")


@pytest.mark.parametrize("name", ["required", "informative", "applicability"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        "test_fixture",
        b"test_fixture",
        bytearray(b"x"),
        memoryview(b"x"),
        {},
        [None],
    ],
)
def test_reject_invalid_selection_collections(name, value):
    error("policy_selection_invalid", selection, **{name: value})


@pytest.mark.parametrize("name", ["fact_names", "evidence_refs"])
@pytest.mark.parametrize(
    "value", [None, "test_fixture", b"test_fixture", {}, [None], ["\ud800"]]
)
def test_reject_invalid_reference_collections(name, value):
    error("policy_applicability_invalid", applicability, **{name: value})


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {1: None},
        {"test_fixture.fact": {}},
        {"test_fixture.fact": "test_fixture"},
    ],
)
def test_reject_invalid_facts(value):
    error("policy_selection_invalid", selection, facts=value)


def test_custom_container_failures_and_endless_iteration_are_bounded():
    class BrokenSequence(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            raise RuntimeError("test_fixture.secret")

    class EndlessSequence(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            calls.append(index)
            return binding()

    class BrokenMapping(Mapping):
        def __len__(self):
            return 1

        def __iter__(self):
            return iter(["test_fixture.fact"])

        def __getitem__(self, key):
            raise RuntimeError("test_fixture.secret")

    calls = []
    error("policy_selection_invalid", selection, required=BrokenSequence())
    error("policy_selection_invalid", selection, required=EndlessSequence())
    assert len(calls) == 257
    error("policy_selection_invalid", selection, facts=BrokenMapping())
    consumed = []

    def generate():
        consumed.append(True)
        yield binding()

    error("policy_selection_invalid", selection, required=generate())
    assert consumed == []


def test_nested_subclasses_do_not_execute_overrides():
    class OtherBinding(PolicyBinding):
        def canonical_view(self):
            pytest.fail("Must not execute an overridden projection")

    class OtherApplicability(PolicyApplicability):
        def canonical_view(self):
            pytest.fail("Must not execute an overridden projection")

    class OtherFact(FieldValue):
        def canonical_view(self):
            pytest.fail("Must not execute an overridden projection")

    item = binding()
    child = OtherBinding(
        **{k: getattr(item, k) for k in signature(PolicyBinding).parameters}
    )
    error("policy_selection_invalid", selection, required=[child])
    item = applicability()
    child = OtherApplicability(
        **{k: getattr(item, k) for k in signature(PolicyApplicability).parameters}
    )
    error("policy_selection_invalid", selection, applicability=[child])
    error(
        "policy_selection_invalid",
        selection,
        facts={
            "test_fixture.fact": OtherFact(
                None, source_id="test_fixture.source", trust=FieldTrust.AUTHORITATIVE
            )
        },
    )


def test_exact_field_and_collection_limits():
    identifier, text = "\u00e9" * 64, "\u00e9" * 512
    value = applicability(
        policy_id=identifier, policy_version=identifier, justification=text
    )
    assert value.policy_id == identifier and value.justification == text
    keys = [f"test_fixture.f{i:03}" for i in range(257)]
    app = applicability(fact_names=keys[:256], evidence_refs=keys[:256])
    assert (
        len(selection(applicability=[app], facts=dict.fromkeys(keys[:256])).facts)
        == 256
    )
    error("policy_applicability_invalid", applicability, fact_names=keys)
    error("policy_applicability_invalid", applicability, evidence_refs=keys)
    error("policy_selection_invalid", selection, facts=dict.fromkeys(keys))
    error("policy_selection_invalid", selection, required=[binding()] * 257)
    error(
        "policy_selection_invalid",
        selection,
        required=[binding()] * 128,
        informative=[binding()] * 129,
    )


def test_total_byte_limit_includes_nested_valid_values():
    empty_size = size(selection(facts={"test_fixture.fact": fact("")}).canonical_view())
    payload = "x" * (65_536 - empty_size)
    exact = selection(facts={"test_fixture.fact": fact(payload)})
    assert size(exact.canonical_view()) == 65_536
    error(
        "policy_selection_invalid",
        selection,
        facts={"test_fixture.fact": fact(payload + "x")},
    )


def test_aggregate_depth_does_not_reset_at_nested_fact():
    nested = None
    for _ in range(9):
        nested = [nested]
    assert selection(facts={"test_fixture.fact": fact(nested)})
    error(
        "policy_selection_invalid",
        selection,
        facts={"test_fixture.fact": fact([nested])},
    )


@pytest.mark.parametrize("trust", list(FieldTrust))
def test_unknown_and_null_provenance_is_preserved(trust):
    declared = selection(facts={"test_fixture.fact": fact(None, trust)})
    missing = selection(facts={"test_fixture.fact": None})
    assert (
        declared.canonical_view()["facts"]["test_fixture.fact"]["trust"] == trust.value
    )
    assert missing.canonical_view()["facts"]["test_fixture.fact"] is None
    assert declared.canonical_view() != missing.canonical_view()
    assert declared.applicability[0].state is PolicyApplicabilityState.APPLIES


def test_empty_declaration_is_representable_without_authority():
    value = selection(required=[], informative=[], applicability=[], facts={})
    assert value.required == () and value.canonical_view()["facts"] == {}
    assert not hasattr(value, "permits_effect")


def test_scalar_validation_precedes_version_and_cross_reference_checks():
    error("policy_selection_invalid", selection, contract_version="3.0", action_id=7)
    error(
        "policy_contract_unsupported",
        selection,
        contract_version="3.0",
        required=[None],
    )
    error("policy_selection_invalid", selection, action_id=7, applicability=[])
    error(
        "policy_applicability_invalid",
        applicability,
        state="applies",
        fact_names=[None],
    )


def test_namespace_and_required_keywords_without_resolver_activation():
    for model in (PolicyApplicability, PolicySelection):
        for parameter in signature(model).parameters.values():
            assert parameter.kind is Parameter.KEYWORD_ONLY
            assert parameter.default is Parameter.empty
    for model in (PolicyApplicabilityState, PolicyApplicability, PolicySelection):
        assert model.__module__ == "sett.policy_ruler.models"
        assert model.__name__ in authorization.__all__
        assert not hasattr(sett, model.__name__)
    assert hasattr(authorization, "PolicySelectionResolver")
    assert (
        "selection_resolver"
        in signature(authorization.AuthorizationRuler).parameters
    )
    assert (
        "binding"
        in signature(authorization.AuthorizationRuler.register_policy).parameters
    )


@pytest.mark.parametrize(
    "value",
    [None, "2026-01-01", datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)],
)
def test_invalid_time_is_selection_error(value):
    error("policy_selection_invalid", selection, effective_at=value)


def test_equivalent_offsets_and_calendar_limits():
    utc = datetime(2026, 1, 1, 3, 0, 0, 42, tzinfo=timezone.utc)
    offset = utc.astimezone(timezone(timedelta(hours=-3)))
    assert (
        selection(effective_at=utc).canonical_view()
        == selection(effective_at=offset).canonical_view()
    )
    assert selection(effective_at=offset).effective_at.tzinfo is timezone.utc
    assert (
        selection(effective_at=utc)
        .canonical_view()["effective_at"]
        .endswith(".000042Z")
    )
    assert (
        selection(effective_at=datetime(1, 1, 1, tzinfo=timezone.utc))
        .canonical_view()["effective_at"]
        .startswith("0001-")
    )
    error(
        "policy_selection_invalid",
        selection,
        effective_at=datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    )

    class BrokenTimezone(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("test_fixture.secret")

    error(
        "policy_selection_invalid",
        selection,
        effective_at=datetime(2026, 1, 1, tzinfo=BrokenTimezone()),
    )


def test_revision_and_fact_changes_leave_old_projection_unchanged():
    old = selection()
    snapshot = old.canonical_view()
    assert (
        replace(old, selection_revision="test_fixture.r2").canonical_view() != snapshot
    )
    assert replace(old, facts={"test_fixture.fact": None}).canonical_view() != snapshot
    assert old.canonical_view() == snapshot
