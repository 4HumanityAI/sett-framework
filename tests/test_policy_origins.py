"""Policy origins in decision records and in their explanations.

Each test runs the four real state producers with a real governance journal.
A decision record keeps each policy's declared origins and profile, copied
when the policy's result was admitted, and the requirements each verified
condition declared. Origins are declared metadata: SETT records them as
declared and verifies nothing about them.
"""

import json

import pytest
import test_policy_explanations as explanations
import test_policy_handoff as foundation
from test_policy_condition_producer import condition
from test_policy_explanations import EFFECT, Harness, grant
from test_review_restrictions import journal

from sett._governance.validation import canonical_json_bytes
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.exceptions import SETTAuthorizationError
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream
from sett.policy_ruler import (
    GovernanceJournal,
    GovernanceRecord,
    PolicyBinding,
    PolicyOrigin,
    PolicySourceReference,
)
from sett.policy_ruler.decision_record import DECISION_SCHEMA_ID, read_decision_records
from sett.policy_ruler.explanation import PublicValues, explain


def origin(requirement, layer, *, source=None, reason="Internal requirement", revision="1"):
    return PolicyOrigin(
        requirement_id=requirement, requirement_revision=revision, layer_id=layer,
        source=source, source_absence_reason=None if source is not None else reason,
    )


def bound(policy_id, *origins, profile=None):
    return PolicyBinding(
        policy_id=policy_id, policy_version="1", contract_version="2.0", origins=list(origins),
        profile_id=None if profile is None else profile[0],
        profile_revision=None if profile is None else profile[1],
    )


REGULATION = PolicySourceReference(
    source_id="test_fixture.regulation_source", source_version="2024", locator="Article 5",
)


def text(view):
    return json.dumps(view, sort_keys=True)


def test_each_policy_row_keeps_its_declared_origins_and_profile():
    first = bound("test_fixture.first", origin("test_fixture.retention", "test_fixture.organization"),
                  profile=("test_fixture.profile", "3"))
    second = bound("test_fixture.second",
                   origin("test_fixture.lawful_basis", "test_fixture.regulation", source=REGULATION))
    harness = Harness({EFFECT: [(first, None), (second, None)]})
    harness.authorize()
    (record,) = harness.records()
    first_row, second_row = record.policies
    assert first_row["origins"] == first.canonical_view()["origins"]
    assert first_row["profile"] == {"profile_id": "test_fixture.profile", "profile_revision": "3"}
    assert second_row["origins"] == second.canonical_view()["origins"]
    assert second_row["profile"] is None
    # Only the declared provenance is added; the row repeats nothing else.
    assert set(first_row) == {"policy_id", "policy_version", "verdict", "reason_codes", "origins", "profile"}

    operational = explain(record).operational_view()["policies"]
    assert [row["origins"][0]["layer_id"] for row in operational] == [
        "test_fixture.organization", "test_fixture.regulation",
    ]
    assert operational[1]["origins"][0]["source"] == {
        "source_id": "test_fixture.regulation_source", "source_version": "2024", "locator": "Article 5",
    }


def test_related_conditions_keep_their_own_requirements():
    shared = origin("test_fixture.access_review", "test_fixture.organization")
    other = origin("test_fixture.retention", "test_fixture.organization")
    first = bound("test_fixture.first", shared, other)
    second = bound("test_fixture.second",
                   origin("test_fixture.access_control", "test_fixture.regulation", source=REGULATION))
    harness = Harness({EFFECT: [
        (first, grant(first.policy_id, conditions=(
            condition("test_fixture.first_check", bound=first, origins=(shared,)),
        ))),
        (second, grant(second.policy_id, conditions=(
            condition("test_fixture.second_check", bound=second),
        ))),
    ]})
    harness.authorize()
    (record,) = harness.records()
    assert [(row["condition_id"], row["requirements"]) for row in record.conditions] == [
        ("test_fixture.first_check",
         [{"layer_id": "test_fixture.organization", "requirement_id": "test_fixture.access_review"}]),
        ("test_fixture.second_check",
         [{"layer_id": "test_fixture.regulation", "requirement_id": "test_fixture.access_control"}]),
    ]
    view = explain(record).operational_view()
    assert [row["requirements"] for row in view["conditions"]] == [row["requirements"] for row in record.conditions]


def test_an_absent_source_stays_an_absence():
    internal = bound("test_fixture.internal", origin("test_fixture.house_rule", "test_fixture.organization",
                                                     reason="Owner-defined rule"))
    sourced = bound("test_fixture.sourced",
                    origin("test_fixture.lawful_basis", "test_fixture.regulation", source=REGULATION))
    harness = Harness({EFFECT: [(internal, None), (sourced, None)]})
    harness.authorize()
    (record,) = harness.records()
    explanation = explain(record)
    rows = explanation.operational_view()["policies"]
    assert rows[0]["origins"][0]["source"] is None
    assert rows[0]["origins"][0]["source_absence_reason"] == "Owner-defined rule"
    assert rows[1]["origins"][0]["source_absence_reason"] is None
    public = explanation.public_view(PublicValues(
        policy_ids={"test_fixture.internal", "test_fixture.sourced"},
        source_ids={"test_fixture.regulation_source"},
    ))["policies"]
    assert [row["origins"][0]["source_declared"] for row in public] == [False, True]
    assert [row["origins"][0]["source_id"] for row in public] == [None, "test_fixture.regulation_source"]


def test_a_record_keeps_the_origins_that_were_evaluated(monkeypatch):
    shared = journal()
    monkeypatch.setattr(explanations, "journal", lambda: shared)
    before = bound("test_fixture.policy", origin("test_fixture.requirement", "test_fixture.before"))
    after = bound("test_fixture.policy", origin("test_fixture.requirement", "test_fixture.after"))
    Harness({EFFECT: [(before, None)]}).authorize()
    # A restarted process registers the same policy with other origins.
    Harness({EFFECT: [(after, None)]}).authorize(action_id="test_fixture.second_action")
    first, second = read_decision_records(shared).decisions
    assert first.policies[0]["origins"][0]["layer_id"] == "test_fixture.before"
    assert second.policies[0]["origins"][0]["layer_id"] == "test_fixture.after"


@pytest.mark.parametrize("version", [1, 2])
def test_earlier_records_report_origins_as_not_recorded(version):
    governance = journal()
    payload = {
        "authorization_attempt_id": None, "action_id": "test_fixture.action", "action_type": EFFECT,
        "trace_id": "test_fixture.trace", "checkpoint_id": foundation.ENTRY,
        "selection": {"selection_id": "test_fixture.selection", "selection_revision": "1"},
        "policies": [{"policy_id": "test_fixture.policy", "policy_version": "1", "verdict": "grant",
                      "reason_codes": []}],
        "decision": {"verdict": "grant"},
        "conditions": [{"policy_id": "test_fixture.policy", "policy_version": "1",
                        "condition_id": "test_fixture.condition", "state": "satisfied"}],
        "approvals": [], "consent": None, "request_fingerprint": None, "fingerprint_policy_id": None,
        "recorded_at": "2026-08-30T12:00:00.000000Z",
    }
    if version == 2:
        payload.update(review_obligations=[], review=[])
    record = GovernanceRecord(
        operation_id=f"decision.test_fixture_v{version}", schema_id=DECISION_SCHEMA_ID,
        schema_version=version, payload=canonical_json_bytes(payload, field="test fixture"),
    )
    governance.append(governance.prepare(record, expected_head=governance.read().observed_head))
    (decoded,) = read_decision_records(governance).decisions
    explanation = explain(decoded)
    view = explanation.operational_view()
    assert (view["policies"][0]["origins"], view["policies"][0]["profile"]) == (None, None)
    assert view["conditions"][0]["requirements"] is None
    public = explanation.public_view(PublicValues(policy_ids={"test_fixture.policy"}))
    assert public["policies"][0]["origins"] is None


def test_planted_origin_values_never_reach_the_public_view_unless_approved():
    planted = {
        "layer": "planted.layer.secret-1a7c", "requirement": "planted.requirement.secret-6d02",
        "revision": "planted-revision-secret-c419", "source": "planted.source.secret-8b3e",
        "edition": "planted-edition-secret-27fa", "locator": "planted locator secret-e5d1",
        "reason": "planted absence reason secret-90bc",
    }
    binding = bound(
        "test_fixture.policy",
        origin(planted["requirement"], planted["layer"], revision=planted["revision"],
               source=PolicySourceReference(source_id=planted["source"], source_version=planted["edition"],
                                            locator=planted["locator"])),
        origin("test_fixture.other", planted["layer"], reason=planted["reason"]),
    )
    harness = Harness({EFFECT: [(binding, None)]})
    harness.authorize()
    (record,) = harness.records()
    explanation = explain(record)
    operational = text(explanation.operational_view())
    assert all(value in operational for value in planted.values())
    shown = PublicValues(policy_ids={"test_fixture.policy"})
    assert not [value for value in planted.values() if value in text(explanation.public_view(shown))]
    approved = text(explanation.public_view(PublicValues(
        policy_ids={"test_fixture.policy"}, layer_ids={planted["layer"]},
        requirement_ids={planted["requirement"]}, source_ids={planted["source"]},
    )))
    assert all(planted[key] in approved for key in ("layer", "requirement", "source"))
    # Revisions, editions, locators and absence reasons are never public.
    assert not [planted[key] for key in ("revision", "edition", "locator", "reason") if planted[key] in approved]


def test_a_journal_too_small_for_the_origins_blocks_the_attempt(monkeypatch):
    stream = InMemoryEvidenceStream(backend_id="backend.reviews", stream_id="reviews")
    small = GovernanceJournal(
        namespace_id="namespace.test", journal_id="journal.reviews", stream=stream,
        origin=stream.head(), required_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(), historical_integrity_policies=(),
        max_frame_bytes=8192, max_records=64, max_total_bytes=4_194_304,
    )
    monkeypatch.setattr(explanations, "journal", lambda: small)
    many = bound("test_fixture.policy", *(
        origin(f"test_fixture.requirement_{index}", "test_fixture.regulation",
               source=PolicySourceReference(source_id="test_fixture.source", source_version="1",
                                            locator=f"Provision {index} " + "x" * 512))
        for index in range(32)
    ))
    Harness({EFFECT: [(foundation.binding(), None)]}).authorize()  # a record with one origin fits
    with pytest.raises(SETTAuthorizationError) as caught:
        Harness({EFFECT: [(many, None)]}).authorize(action_id="test_fixture.second_action")
    assert caught.value.reason_code == "authorization.decision_record_unconfirmed"
    assert len(read_decision_records(small).decisions) == 1
