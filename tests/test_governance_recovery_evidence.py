"""What governance recovery may conclude from the evidence it was given.

A missing event proves nothing about a trace that does not contain the
decision's trace: such a grant stays unknown, and the report asks for
reconciliation. Decision records and review records come from one read of
the journal, so the completeness reported covers both. Each test runs the
governed executor or the policy layer with the real state producers.
"""

import dataclasses

import pytest
from test_governance_recovery import Durable, FailingIssueStore, only
from test_review_restrictions import declaring, journal, obligation, request

from sett import SETTAuthorizationError
from sett.audit_ruler.trace import TraceRecorder
from sett.persistence import (
    DurabilityLevel,
    InMemoryEvidenceStream,
    decode_trace_evidence_frame,
)
from sett.policy_ruler import ReviewOutcome
from sett.policy_ruler.governance_recovery import (
    RecoveredOutcome,
    read_governance_recovery,
)

UNESTABLISHED = "trace_coverage_unestablished"


class CountingStream(InMemoryEvidenceStream):
    """Count the journal's reads; from a chosen read on, the storage fails."""

    def __init__(self):
        super().__init__(backend_id="backend.reviews", stream_id="reviews")
        self.reads = 0
        self.fail_from = None

    def read(self, *, after=None):
        self.reads += 1
        if self.fail_from is not None and self.reads >= self.fail_from:
            raise OSError("test_fixture.read_failed")
        return super().read(after=after)


def reviewed(stream):
    """One restriction declared and one favorable review record written."""
    durable = Durable(decision=declaring(obligation()), governance=journal(stream))
    durable.issuer.authorize(request())
    durable.reviewer.outcome = ReviewOutcome.FAVORABLE
    durable.owner.follow_up_reviews()
    return durable


# ── trace coverage ───────────────────────────────────────────────────────


def test_a_trace_without_the_decisions_trace_leaves_a_used_grant_unknown():
    durable = Durable()
    durable.handler()
    durable.submit()
    assert durable.effects == [{"value": 1}]
    # A genuine restored trace of another deployment, with the same labels.
    unrelated = Durable().restored()
    recovery = read_governance_recovery(durable.journal, trace=unrelated)
    decision = only(recovery)
    assert decision.outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN
    assert (decision.effect, decision.boundary) == (None, None)
    assert recovery.trace_given and recovery.complete
    assert UNESTABLISHED in recovery.diagnostics
    assert recovery.requires_reconciliation


def test_the_decisions_own_trace_still_shows_the_effect():
    durable = Durable()
    durable.handler()
    durable.submit()
    recovery = durable.recovery()
    assert (only(recovery).outcome, only(recovery).effect) == (RecoveredOutcome.EFFECT_KNOWN, "completed")
    assert UNESTABLISHED not in recovery.diagnostics
    assert not recovery.requires_reconciliation


def test_a_trace_whose_chain_failed_cannot_show_that_a_grant_was_not_used():
    durable = Durable(store=FailingIssueStore())
    durable.handler()
    with pytest.raises(SETTAuthorizationError):
        durable.submit()
    restored = durable.restored()
    assert only(read_governance_recovery(durable.journal, trace=restored)).outcome is (
        RecoveredOutcome.GRANTED_TRACE_UNKNOWN
    )
    broken = dataclasses.replace(restored, chain_integrity=False)
    recovery = read_governance_recovery(durable.journal, trace=broken)
    assert only(recovery).outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN
    assert {"trace_chain_integrity_failed", UNESTABLISHED} <= set(recovery.diagnostics)
    assert recovery.requires_reconciliation


# ── one journal read ─────────────────────────────────────────────────────


def test_decisions_and_review_records_come_from_one_journal_read():
    stream = CountingStream()
    durable = reviewed(stream)
    stream.reads = 0
    stream.fail_from = 2  # a second read would fail
    recovery = read_governance_recovery(durable.journal)
    assert stream.reads == 1
    (item,) = recovery.obligations
    (record,) = item.review_records
    assert (record.fact, record.outcome) == ("review_completed", "favorable")
    assert recovery.complete and not recovery.requires_reconciliation


def test_a_failed_journal_read_reports_nothing_as_complete():
    stream = CountingStream()
    durable = reviewed(stream)
    stream.reads = 0
    stream.fail_from = 1
    recovery = read_governance_recovery(durable.journal)
    assert stream.reads == 1
    assert not recovery.complete and recovery.requires_reconciliation
    assert recovery.decisions == () and recovery.obligations == ()


@pytest.mark.parametrize(
    ("last_kind", "expected"),
    [
        (None, RecoveredOutcome.GRANTED_TRACE_UNKNOWN),
        ("authorization.receipt_verified", RecoveredOutcome.GRANTED_TRACE_UNKNOWN),
        ("handler.authorized", RecoveredOutcome.EFFECT_UNCERTAIN),
        ("handler.started", RecoveredOutcome.EFFECT_UNCERTAIN),
    ],
)
def test_an_intact_older_prefix_of_the_correct_trace_cannot_prove_non_use(last_kind, expected):
    durable = Durable()
    durable.handler()
    durable.submit()
    assert durable.effects == [{"value": 1}]
    prefix = InMemoryEvidenceStream(backend_id="backend.local", stream_id="evidence.local")
    found = False
    for entry in durable.stream.read():
        prefix.append(write_id=entry.write_id, payload=entry.payload, expected_head=prefix.head())
        # A direct executor submission does not emit trace.started. None
        # deliberately selects the first actual entry.
        if last_kind is None or decode_trace_evidence_frame(entry.payload).payload["kind"] == last_kind:
            found = True
            break
    assert found
    restored = TraceRecorder.restore(
        evidence_stream=prefix, namespace_id="namespace.local",
        evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    assert restored.chain_integrity
    assert {item.trace_id for item in restored.traces} == {
        item.trace_id for item in durable.restored().traces
    }
    recovery = read_governance_recovery(durable.journal, trace=restored)
    assert only(recovery).outcome is expected
    assert only(recovery).effect is None
    assert recovery.requires_reconciliation
    assert durable.effects == [{"value": 1}]


def test_a_failed_chain_cannot_establish_a_known_effect():
    durable = Durable()
    durable.handler()
    durable.submit()
    broken = dataclasses.replace(durable.restored(), chain_integrity=False)
    recovery = read_governance_recovery(durable.journal, trace=broken)
    assert only(recovery).outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN
    assert only(recovery).effect is None
    assert {"trace_chain_integrity_failed", UNESTABLISHED} <= set(recovery.diagnostics)
    assert recovery.requires_reconciliation


class PartialReadStream(CountingStream):
    """Inject a storage failure after the first admitted record, without editing it."""

    partial = False

    def read(self, *, after=None):
        snapshot = super().read(after=after)
        if not self.partial:
            return snapshot

        def interrupted():
            yield snapshot[0]
            raise OSError("test_fixture.partial_read")

        return interrupted()


def test_a_partial_read_keeps_only_the_admitted_prefix_and_reports_incompleteness():
    stream = PartialReadStream()
    durable = reviewed(stream)
    stream.reads = 0
    stream.partial = True
    recovery = read_governance_recovery(durable.journal)
    assert stream.reads == 1
    assert len(recovery.decisions) == 1
    assert len(recovery.obligations) == 1
    assert recovery.obligations[0].review_records == ()
    assert not recovery.complete
    assert recovery.diagnostics
    assert recovery.requires_reconciliation
