"""Which action a review restriction exempts, by retained identity.

An authorization is never blocked by the obligations it declared itself.
That exemption is taken from the decision record the authorization itself
confirmed, which the policy ruler retains for it, never from an action
identifier the caller supplies. A later, separately submitted action that
reuses the identifier is a later action and stays restricted. The review
verifier is a test double standing for the application's own; no governed
effect is performed.
"""

import pytest
import test_policy_handoff as foundation
from test_governance_recovery import Durable
from test_review_restrictions import LATER, Harness, declaring, obligation, request

from sett import Action, SETTAuthorizationError
from sett.policy_ruler import ReviewOutcome

RESTRICTED = "authorization.review_restriction_active"


def test_a_later_action_reusing_the_declaring_identifier_stays_restricted():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    # Same action identifier as the declaring action, another action type.
    error = harness.blocked(request(LATER))
    assert error.reason_code == RESTRICTED
    (call,) = harness.reviewer.calls
    assert (call.action_id, call.action_type) == ("test_fixture.action", LATER)
    assert call.evidence_ref.startswith("decision.")
    assert harness.store.issued == 1


def test_a_favorable_review_lifts_the_restriction_for_a_reused_identifier():
    harness = Harness(declared=declaring(obligation()))
    harness.issuer.authorize(request())
    harness.reviewer.outcome = ReviewOutcome.FAVORABLE
    later = request(LATER)
    outcome = harness.issuer.authorize(later)
    assert outcome.receipt is not None
    harness.issuer.verify_and_consume(outcome.receipt, later)
    assert harness.store.consumed == 1


def test_the_declaring_authorization_is_not_blocked_by_its_own_restriction():
    # The restriction names the declaring action's own type, so every later
    # collection of the same authorization reads a restriction that matches it.
    harness = Harness(declared=declaring(obligation(future=(foundation.EFFECT,))))
    declared = request()
    outcome = harness.issuer.authorize(declared)
    assert outcome.receipt is not None
    harness.issuer.verify_and_consume(outcome.receipt, declared)
    assert harness.store.consumed == 1
    assert harness.reviewer.calls == []


def test_a_new_authorization_of_the_same_request_is_a_later_action():
    harness = Harness(declared=declaring(obligation(future=(foundation.EFFECT,))))
    harness.issuer.authorize(request())
    error = harness.blocked(request())
    assert error.reason_code == RESTRICTED
    assert len(harness.reviewer.calls) == 1
    assert harness.store.issued == 1


@pytest.mark.parametrize("action_id", ["test_fixture.later", "test_fixture.action"])
def test_the_executor_preserves_restrictions_even_when_an_identifier_is_reused(action_id):
    durable = Durable(decision=declaring(obligation()))
    durable.issuer.authorize(request())
    effects = []
    durable.executor.register_handler(LATER, lambda payload: effects.append(payload))
    with pytest.raises(SETTAuthorizationError) as caught:
        durable.executor.submit(Action(action_type=LATER, payload={"value": 1}, action_id=action_id))
    assert caught.value.reason_code == RESTRICTED
    assert effects == []
    assert len(durable.reviewer.calls) == 1
    assert durable.reviewer.outcome is None
    assert durable.store.issued == 1
    assert durable.store.consumed == 0
