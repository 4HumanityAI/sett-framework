"""Following up declared review obligations, with durable review records.

The harness of the restriction tests is reused: declared consent, a real
governance journal, a review verifier test double and synthetic conditions.
The application decides when to follow up; no clock or scheduler is involved.
"""

import json
from datetime import timedelta

import pytest
from test_review_restrictions import (
    LATER,
    FailingStream,
    Harness,
    declaring,
    journal,
    obligation,
    request,
    ruler,
)

from sett.exceptions import SETTAuthorizationError, SETTPolicyConfigurationError
from sett.persistence import InMemoryEvidenceStream
from sett.policy_ruler import (
    DeadlineStatus,
    DeadlineStatusFact,
    NotificationDeliveredFact,
    ReviewCompletedFact,
    ReviewMode,
    ReviewOutcome,
    ReviewVerification,
)
from sett.policy_ruler.decision_record import read_decision_records
from sett.policy_ruler.review_follow_up import REVIEW_SCHEMA_ID, REVIEW_SCHEMA_VERSION


def reporting(*facts):
    """A verifier answer carrying exactly these facts for the asked obligation."""
    return lambda review_request: ReviewVerification(
        obligation_id=review_request.obligation.obligation_id, binding=review_request.obligation.binding,
        action_id=review_request.action_id, action_type=review_request.action_type, facts=facts,
    )


def review_records(governance):
    return [json.loads(entry.record.payload) for entry in governance.read().entries
            if entry.record.schema_id == REVIEW_SCHEMA_ID]


def declared(mode, **options):
    harness = Harness(declared=declaring(obligation(mode)), **options)
    harness.issuer.authorize(request())
    return harness


# ── configuration ────────────────────────────────────────────────────────


@pytest.mark.parametrize("options", [{}, {"governance_journal": journal()}])
def test_the_follow_up_needs_a_journal_and_review_verifiers(options):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ruler(**options).follow_up_reviews()
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


def test_without_declared_obligations_nothing_is_recorded():
    harness = Harness()
    harness.issuer.authorize(request(LATER))
    report = harness.owner.follow_up_reviews()
    assert report.obligations == () and report.recorded == 0
    assert harness.reviewer.calls == []


# ── what is recorded ─────────────────────────────────────────────────────


def test_a_delivered_notification_is_recorded_once():
    harness = declared(ReviewMode.NOTIFY_AFTER_ACTION)
    harness.reviewer.result = reporting(NotificationDeliveredFact(evidence_ref="test_fixture.notice"))
    first = harness.owner.follow_up_reviews()
    (item,) = first.obligations
    assert item.notified and item.open and item.reason_code is None
    assert item.mode is ReviewMode.NOTIFY_AFTER_ACTION
    assert first.recorded == 1
    (record,) = review_records(harness.journal)
    assert record["fact"] == "notification_delivered"
    assert record["obligation_id"] == "test_fixture.restriction"
    assert record["decision_operation_id"] == item.decision_operation_id
    (call,) = harness.reviewer.calls
    assert call.evidence_ref == item.decision_operation_id
    assert call.action_id == "test_fixture.action"
    second = harness.owner.follow_up_reviews()
    assert second.recorded == 0 and second.obligations[0].notified
    assert len(review_records(harness.journal)) == 1


def test_an_overdue_report_is_recorded_and_blocks_nothing_by_itself():
    harness = declared(ReviewMode.REQUIRE_HUMAN_REVIEW_WITH_DEADLINE)
    harness.reviewer.result = reporting(
        DeadlineStatusFact(evidence_ref="test_fixture.deadline", status=DeadlineStatus.OVERDUE),
    )
    (item,) = harness.owner.follow_up_reviews().obligations
    assert item.deadline is DeadlineStatus.OVERDUE and item.open
    assert harness.issuer.authorize(request(action_id="test_fixture.next")).receipt is not None


def test_a_completed_review_is_reported_with_its_outcome():
    harness = declared(ReviewMode.REQUIRE_HUMAN_REVIEW_WITH_DEADLINE)
    harness.reviewer.outcome = ReviewOutcome.UNFAVORABLE
    (item,) = harness.owner.follow_up_reviews().obligations
    assert item.completed is ReviewOutcome.UNFAVORABLE and not item.open
    (record,) = review_records(harness.journal)
    assert record["fact"] == "review_completed" and record["outcome"] == "unfavorable"


def test_review_records_have_their_own_schema_and_leave_decision_records_alone():
    harness = declared(ReviewMode.NOTIFY_AFTER_ACTION)
    harness.reviewer.result = reporting(NotificationDeliveredFact(evidence_ref="test_fixture.notice"))
    harness.owner.follow_up_reviews()
    (entry,) = [item for item in harness.journal.read().entries if item.record.schema_id == REVIEW_SCHEMA_ID]
    assert entry.record.schema_version == REVIEW_SCHEMA_VERSION
    assert entry.record.operation_id.startswith("review.")
    recovered = read_decision_records(harness.journal)
    assert len(recovered.decisions) == 1 and recovered.other_records == 1
    assert not recovered.requires_reconciliation


# ── no unlock by a record, a restart or elapsed time ─────────────────────


def test_a_recorded_favorable_review_does_not_lift_a_restriction():
    harness = declared(ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS)
    harness.reviewer.outcome = ReviewOutcome.FAVORABLE
    (item,) = harness.owner.follow_up_reviews().obligations
    assert item.completed is ReviewOutcome.FAVORABLE
    harness.reviewer.outcome = None
    with pytest.raises(SETTAuthorizationError) as caught:
        harness.issuer.authorize(request(LATER, "test_fixture.later_action"))
    assert caught.value.reason_code == "authorization.review_restriction_active"


def test_after_a_restart_restrictions_still_apply_and_records_are_not_written_again():
    first = declared(ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS)
    first.reviewer.outcome = ReviewOutcome.FAVORABLE
    first.owner.follow_up_reviews()
    restarted = Harness(governance=first.journal)
    restarted.clock.current += timedelta(days=400)
    with pytest.raises(SETTAuthorizationError) as caught:
        restarted.issuer.authorize(request(LATER, "test_fixture.later_action"))
    assert caught.value.reason_code == "authorization.review_restriction_active"
    restarted.reviewer.outcome = ReviewOutcome.FAVORABLE
    assert restarted.owner.follow_up_reviews().recorded == 0
    assert len(review_records(first.journal)) == 1


# ── failures ─────────────────────────────────────────────────────────────


def test_an_obligation_that_cannot_be_verified_does_not_stop_the_others():
    absent = obligation(ReviewMode.NOTIFY_AFTER_ACTION, verifier="test_fixture.absent", identifier="test_fixture.absent")
    present = obligation(ReviewMode.NOTIFY_AFTER_ACTION, identifier="test_fixture.present")
    harness = Harness(declared=declaring(absent, present))
    harness.issuer.authorize(request())
    harness.reviewer.result = reporting(NotificationDeliveredFact(evidence_ref="test_fixture.notice"))
    items = {item.obligation_id: item for item in harness.owner.follow_up_reviews().obligations}
    assert items["test_fixture.absent"].reason_code == "policy.review_verifier_missing"
    assert not items["test_fixture.absent"].open
    assert items["test_fixture.present"].notified


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda reviewer: setattr(reviewer, "error", RuntimeError("test_fixture.secret")),
         "policy.review_verifier_failed"),
        (lambda reviewer: setattr(reviewer, "result", lambda review_request: object()),
         "policy.review_result_invalid"),
    ],
)
def test_a_failing_or_invalid_verifier_marks_only_its_obligation(setup, code):
    harness = declared(ReviewMode.NOTIFY_AFTER_ACTION)
    setup(harness.reviewer)
    report = harness.owner.follow_up_reviews()
    (item,) = report.obligations
    assert item.reason_code == code and item.evidence_refs == ()
    assert report.recorded == 0


def test_an_unreadable_journal_stops_the_follow_up():
    harness = Harness(governance=journal(FailingStream(backend_id="backend.reviews", stream_id="reviews")))
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        harness.owner.follow_up_reviews()
    assert caught.value.reason_code == "policy.review_state_unavailable"


class SwitchingStream(InMemoryEvidenceStream):
    """Confirm writes until told to stop."""

    refuse = False

    def flush(self, *, through):
        if self.refuse:
            raise RuntimeError("test_fixture.storage_down")
        return super().flush(through=through)


def test_a_record_that_cannot_be_confirmed_stops_the_follow_up():
    stream = SwitchingStream(backend_id="backend.reviews", stream_id="reviews")
    harness = declared(ReviewMode.NOTIFY_AFTER_ACTION, governance=journal(stream))
    harness.reviewer.result = reporting(NotificationDeliveredFact(evidence_ref="test_fixture.notice"))
    stream.refuse = True
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        harness.owner.follow_up_reviews()
    assert caught.value.reason_code == "policy.review_record_unconfirmed"


def test_only_facts_of_known_kinds_are_recorded():
    harness = declared(ReviewMode.REQUIRE_HUMAN_REVIEW_WITH_DEADLINE)
    harness.reviewer.result = reporting(
        ReviewCompletedFact(evidence_ref="test_fixture.review", outcome=ReviewOutcome.FAVORABLE),
        NotificationDeliveredFact(evidence_ref="test_fixture.notice"),
        DeadlineStatusFact(evidence_ref="test_fixture.deadline", status=DeadlineStatus.NOT_OVERDUE),
    )
    report = harness.owner.follow_up_reviews()
    (item,) = report.obligations
    assert (item.completed, item.notified, item.deadline) == (
        ReviewOutcome.FAVORABLE, True, DeadlineStatus.NOT_OVERDUE,
    )
    assert report.recorded == 3
    assert sorted(record["fact"] for record in review_records(harness.journal)) == [
        "deadline_status", "notification_delivered", "review_completed",
    ]
