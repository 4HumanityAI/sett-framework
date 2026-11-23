"""Requirements are never compared: alike conditions of two policies stay separate.

Two required policies declare conditions with the same identifier and the
same verifier, under different origins. Each is verified on its own, a
failure of either blocks the action, and the decision record keeps both.
"""

import pytest
from test_policy_condition_producer import condition
from test_policy_explanations import EFFECT, Harness, grant
from test_policy_origins import bound, origin

from sett.exceptions import SETTAuthorizationError
from sett.policy_ruler import ConditionVerificationState

FIRST = bound("test_fixture.first", origin("test_fixture.retention", "test_fixture.organization"))
SECOND = bound("test_fixture.second", origin("test_fixture.access_control", "test_fixture.regulation"))


def alike():
    """Two policies whose conditions share an identifier and a verifier."""
    return Harness({EFFECT: [
        (binding, grant(binding.policy_id, conditions=(condition("test_fixture.shared_check", bound=binding),)))
        for binding in (FIRST, SECOND)
    ]})


def test_alike_conditions_of_two_policies_are_each_verified_and_recorded():
    harness = alike()
    harness.authorize()
    checked = [checked_condition.binding.policy_id for _request, checked_condition in harness.verifier.calls]
    # One call for each condition at every site where conditions are verified.
    assert checked[:2] == ["test_fixture.first", "test_fixture.second"]
    assert checked.count("test_fixture.first") == checked.count("test_fixture.second") == len(checked) // 2
    (record,) = harness.records()
    assert [(row["policy_id"], row["condition_id"], row["requirements"]) for row in record.conditions] == [
        ("test_fixture.first", "test_fixture.shared_check",
         [{"layer_id": "test_fixture.organization", "requirement_id": "test_fixture.retention"}]),
        ("test_fixture.second", "test_fixture.shared_check",
         [{"layer_id": "test_fixture.regulation", "requirement_id": "test_fixture.access_control"}]),
    ]


@pytest.mark.parametrize("failing", [0, 1])
def test_a_passed_alike_condition_does_not_cover_a_failed_one(failing):
    harness = alike()
    states = [ConditionVerificationState.SATISFIED, ConditionVerificationState.SATISFIED]
    states[failing] = ConditionVerificationState.UNSATISFIED
    harness.verifier.states = states
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.authorize()
    error = caught.value
    assert error.reason_code == "authorization.condition_unsatisfied"
    failed_policy = ("test_fixture.first", "test_fixture.second")[failing]
    assert error.failed_condition == (failed_policy, "1", "test_fixture.shared_check")
    assert len(error.attempted_conditions) == failing + 1
    assert harness.records() == ()
