"""Reading the governance journal and the restored trace together, without acting.

``read_governance_recovery()`` reports, for each decision record, what the
durable evidence can vouch for: a denial, an approval requirement, a grant
whose trace evidence is insufficient, an effect whose outcome is known,
or an effect that may or may not have happened. It also reports each
declared review obligation with its review records and the actions that
cleared each restriction.

The journal is read once, so the decision records and the review records
describe the same stable prefix and the reported completeness covers both.
A missing event cannot establish that a grant was not used: even an intact
prefix of the correct trace may stop before its use. Such grants stay
unknown. Positive effect observations require an intact chain and a link
to the decision's own trace and authorization attempt.

It restores nothing. No receipt becomes usable, no consumption, review or
effect is resumed, and nothing is retried. Continuing is always a new
submission with a fresh evaluation; an uncertain effect is documented with
``sett.audit_ruler.recovery.record_boundary_disposition()``, which documents
it without closing it or authorizing anything.

The report holds no pending request to restore. An approval-required outcome
ends its attempt and keeps nothing alive, and SETT decides no expiry for it:
how long a request or an authorization lasts belongs to the implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sett.audit_ruler.trace import RecoveryReport, UnresolvedBoundary
from sett.policy_ruler.decision_record import DecisionRecord, _recovery_from
from sett.policy_ruler.persistence import GovernanceJournal, GovernanceReadResult
from sett.policy_ruler.review import ReviewMode
from sett.policy_ruler.review_follow_up import REVIEW_SCHEMA_ID

_TERMINALS = {
    "handler.entry_blocked": "blocked",
    "handler.completed": "completed",
    "handler.failed": "failed",
    "handler.cancelled": "cancelled",
    "handler.timed_out": "timed_out",
}
_COVERAGE_UNESTABLISHED = "trace_coverage_unestablished"


class RecoveredOutcome(str, Enum):
    """What the durable evidence can vouch for about one decision."""

    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    # Retained for value compatibility; missing events never establish non-use.
    GRANTED_NOT_USED = "granted_not_used"
    EFFECT_KNOWN = "effect_known"
    EFFECT_UNCERTAIN = "effect_uncertain"
    GRANTED_TRACE_UNKNOWN = "granted_trace_unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveredDecision:
    """One decision record and what the trace shows of it; never authority.

    ``effect`` names how a known effect ended (``completed``, ``failed``,
    ``cancelled``, ``timed_out`` or ``blocked``). ``boundary`` is the trace's
    unresolved boundary for an uncertain effect, with its dispositions.
    """

    operation_id: str
    action_id: str
    action_type: str
    verdict: str
    recorded_at: str
    outcome: RecoveredOutcome
    effect: str | None
    boundary: UnresolvedBoundary | None = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewRecord:
    """One review record written by the follow-up; evidence, never authority."""

    decision_operation_id: str
    obligation_id: str
    mode: ReviewMode
    fact: str
    outcome: str | None
    deadline_status: str | None
    evidence_ref: str
    recorded_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveredObligation:
    """One declared review obligation, its review records and its clearings."""

    decision_operation_id: str
    obligation_id: str
    mode: ReviewMode
    action_id: str
    action_type: str
    future_action_types: tuple[str, ...]
    review_records: tuple[ReviewRecord, ...] = field(repr=False)
    cleared_by: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceRecovery:
    """What one read of the journal, and of a restored trace if given, can vouch for."""

    decisions: tuple[RecoveredDecision, ...] = field(repr=False)
    obligations: tuple[RecoveredObligation, ...] = field(repr=False)
    complete: bool
    trace_given: bool
    diagnostics: tuple[str, ...]

    @property
    def requires_reconciliation(self) -> bool:
        """Whether the read was incomplete or an uncertain effect is not documented."""
        return (
            not self.complete
            or bool(self.diagnostics)
            or any(
                item.outcome is RecoveredOutcome.EFFECT_UNCERTAIN
                and (item.boundary is None or item.boundary.requires_reconciliation)
                for item in self.decisions
            )
        )


def _review_records(result: GovernanceReadResult) -> tuple[list[ReviewRecord], list[str]]:
    """Decode the review records of the same journal read as the decisions."""
    records, diagnostics = [], []
    for index, entry in enumerate(result.entries):
        if entry.record.schema_id != REVIEW_SCHEMA_ID:
            continue
        try:
            value = json.loads(entry.record.payload)
            records.append(ReviewRecord(
                decision_operation_id=value["decision_operation_id"],
                obligation_id=value["obligation_id"], mode=ReviewMode(value["mode"]),
                fact=value["fact"], outcome=value["outcome"],
                deadline_status=value["deadline_status"], evidence_ref=value["evidence_ref"],
                recorded_at=value["recorded_at"],
            ))
        except Exception:  # noqa: BLE001 -- an unreadable record is reported, not raised
            diagnostics.append(f"review_record_unreadable:{index}")
    return records, diagnostics


class _Trace:
    """The restored events, indexed for the joins the report needs."""

    def __init__(self, report: RecoveryReport) -> None:
        self.verified: dict[str, Any] = {}
        self.by_cause: dict[str, list[Any]] = {}
        for event in report.events:
            if event.kind == "authorization.receipt_verified":
                reference = event.attributes.get("decision_ref")
                if isinstance(reference, str):
                    self.verified[reference] = event
            if event.cause_id is not None:
                self.by_cause.setdefault(event.cause_id, []).append(event)
        self.unresolved = {
            boundary.start_event_id: boundary
            for trace in report.traces
            for boundary in trace.unresolved_boundaries
        }

    def child(self, event: Any, *kinds: str) -> Any:
        return next((item for item in self.by_cause.get(event.event_id, ()) if item.kind in kinds), None)

    def classify(self, record: DecisionRecord) -> tuple[RecoveredOutcome, str | None, UnresolvedBoundary | None]:
        verified = self.verified.get(record.authorization_attempt_id or "")
        if verified is None or verified.trace_id != record.trace_id:
            # A stable prefix can predate receipt use, even for this trace.
            return RecoveredOutcome.GRANTED_TRACE_UNKNOWN, None, None
        authorized = self.child(verified, "handler.authorized")
        if authorized is None:
            return RecoveredOutcome.GRANTED_TRACE_UNKNOWN, None, None
        # Each step is followed through the causal links the executor records.
        if self.child(authorized, "handler.blocked") is not None:
            return RecoveredOutcome.EFFECT_KNOWN, "blocked", None
        started = self.child(authorized, "handler.started")
        if started is None:
            return RecoveredOutcome.EFFECT_UNCERTAIN, None, self.unresolved.get(authorized.event_id)
        terminal = self.child(started, *_TERMINALS)
        if terminal is None:
            return RecoveredOutcome.EFFECT_UNCERTAIN, None, self.unresolved.get(started.event_id)
        return RecoveredOutcome.EFFECT_KNOWN, _TERMINALS[terminal.kind], None


def read_governance_recovery(
    journal: GovernanceJournal, *, trace: RecoveryReport | None = None,
) -> GovernanceRecovery:
    """Read the journal, and the restored trace if given, and report; act on nothing."""
    if type(journal) is not GovernanceJournal:
        raise TypeError("journal must be a GovernanceJournal.")
    if trace is not None and not isinstance(trace, RecoveryReport):
        raise TypeError("trace must be a RecoveryReport.")
    # One read: both record families come from the same stable prefix.
    result = journal.read()
    recovered = _recovery_from(result)
    reviews, review_diagnostics = _review_records(result)
    diagnostics = [*recovered.diagnostics, *review_diagnostics]
    if trace is not None and not trace.chain_integrity:
        diagnostics.append("trace_chain_integrity_failed")
    joined = _Trace(trace) if trace is not None and trace.chain_integrity else None
    decisions = []
    for record in recovered.decisions:
        verdict = str(record.decision.get("verdict"))
        effect: str | None = None
        boundary: UnresolvedBoundary | None = None
        if verdict == "deny":
            outcome = RecoveredOutcome.DENIED
        elif verdict == "require_approval":
            outcome = RecoveredOutcome.APPROVAL_REQUIRED
        elif joined is None:
            outcome = RecoveredOutcome.GRANTED_TRACE_UNKNOWN
        else:
            outcome, effect, boundary = joined.classify(record)
        decisions.append(RecoveredDecision(
            operation_id=record.operation_id, action_id=record.action_id,
            action_type=record.action_type, verdict=verdict, recorded_at=record.recorded_at,
            outcome=outcome, effect=effect, boundary=boundary,
        ))
    if trace is not None and any(
        item.outcome is RecoveredOutcome.GRANTED_TRACE_UNKNOWN for item in decisions
    ):
        diagnostics.append(_COVERAGE_UNESTABLISHED)
    cleared: dict[tuple[str, str], list[str]] = {}
    for record in recovered.decisions:
        for row in record.review:
            key = (str(row.get("operation_id")), str(row.get("obligation_id")))
            cleared.setdefault(key, []).append(record.operation_id)
    obligations = []
    seen = set()
    for record in recovered.decisions:
        for row in record.review_obligations:
            key = (record.operation_id, str(row.get("obligation_id")))
            if key in seen:
                continue
            seen.add(key)
            obligations.append(RecoveredObligation(
                decision_operation_id=record.operation_id, obligation_id=key[1],
                mode=ReviewMode(row["mode"]), action_id=str(row.get("action_id")),
                action_type=str(row.get("action_type")),
                future_action_types=tuple(row.get("future_action_types", ())),
                review_records=tuple(
                    item for item in reviews
                    if (item.decision_operation_id, item.obligation_id) == key
                ),
                cleared_by=tuple(cleared.get(key, ())),
            ))
    return GovernanceRecovery(
        decisions=tuple(decisions), obligations=tuple(obligations),
        complete=recovered.complete, trace_given=trace is not None,
        diagnostics=tuple(diagnostics),
    )
