"""Review records already in the journal are confirmed durable before they are trusted.

A follow-up's write can reach the stream while its durability is not
confirmed, and a later read returns it like any other record. The next
follow-up confirms the journal's durability through the last review record
before treating any as recorded, and stops if it cannot.
"""

import pytest
from test_review_follow_up import declared, reporting, review_records
from test_review_restrictions import journal

from sett.exceptions import SETTPersistenceFormatError, SETTPolicyConfigurationError
from sett.persistence import InMemoryEvidenceStream
from sett.persistence.formats import StreamPosition
from sett.policy_ruler import NotificationDeliveredFact, ReviewMode


class Flaky(InMemoryEvidenceStream):
    """Refuses to flush while told to, and remembers every flush."""

    def __init__(self):
        super().__init__(backend_id="backend.reviews", stream_id="reviews")
        self.refuse = False
        self.flushed = []

    def flush(self, *, through):
        self.flushed.append(through)
        if self.refuse:
            raise RuntimeError("test_fixture.storage_down")
        return super().flush(through=through)


def notifying(stream):
    harness = declared(ReviewMode.NOTIFY_AFTER_ACTION, governance=journal(stream))
    harness.reviewer.result = reporting(NotificationDeliveredFact(evidence_ref="test_fixture.notice"))
    return harness


def unconfirmed_record():
    """A review record that reached the stream without its durability confirmed."""
    stream = Flaky()
    harness = notifying(stream)
    stream.refuse = True
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        harness.owner.follow_up_reviews()
    assert caught.value.reason_code == "policy.review_record_unconfirmed"
    assert len(review_records(harness.journal)) == 1
    return stream, harness


def test_a_later_follow_up_confirms_an_unconfirmed_record_before_trusting_it():
    stream, harness = unconfirmed_record()
    stream.refuse = False
    stream.flushed.clear()
    last = harness.journal.read().entries[-1].position
    assert harness.owner.follow_up_reviews().recorded == 0
    assert last in stream.flushed
    assert len(review_records(harness.journal)) == 1


def test_a_later_follow_up_stops_while_the_record_cannot_be_confirmed():
    _stream, harness = unconfirmed_record()
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        harness.owner.follow_up_reviews()
    assert caught.value.reason_code == "policy.review_record_unconfirmed"
    assert len(review_records(harness.journal)) == 1


def test_durable_records_let_the_follow_up_proceed_as_before():
    harness = notifying(Flaky())
    assert harness.owner.follow_up_reviews().recorded == 1
    assert harness.owner.follow_up_reviews().recorded == 0
    assert len(review_records(harness.journal)) == 1


def test_the_journal_confirms_an_entry_without_appending():
    stream = Flaky()
    harness = notifying(stream)
    governance = harness.journal
    (entry,) = governance.read().entries
    before = governance.read().observed_head
    assert governance.confirm(entry.position) is None
    stream.refuse = True
    assert governance.confirm(entry.position) == "policy.journal_storage_failed"
    unknown = StreamPosition(backend_id="backend.reviews", stream_id="reviews", token="test_fixture.unknown")
    assert governance.confirm(unknown) == "policy.journal_identity_mismatch"
    assert governance.read().observed_head == before
    with pytest.raises(SETTPersistenceFormatError):
        governance.confirm("not a position")
