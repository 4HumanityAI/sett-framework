"""Following up declared review obligations, with durable review records.

The application calls ``PolicyRuler.follow_up_reviews()`` when it chooses.
SETT runs no scheduler and measures no time: when an obligation is due, and
whether it is overdue, belong to the implementation, which reports it
through its review verifier. For each review obligation the decision records
declare, the configured verifier is asked once, and every newly verified fact
is written to the governance journal as a review record, confirmed at the
journal's required durability before it is reported.

A recorded fact is evidence, never authority. A recorded favorable review
does not lift a restriction: the review producer asks the verifier again
before every effect. An overdue report has no consequence by itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sett._governance.validation import canonical_json_bytes
from sett.policy_ruler import _handoff
from sett.policy_ruler._review_state import (
    RESULT_INVALID,
    STATE_UNAVAILABLE,
    VERIFIER_FAILED,
    VERIFIER_MISSING,
)
from sett.policy_ruler.decision_record import read_decision_records
from sett.policy_ruler.persistence import GovernanceJournal, GovernanceRecord
from sett.policy_ruler.review import (
    DeadlineStatus,
    DeadlineStatusFact,
    NotificationDeliveredFact,
    PolicyReviewObligation,
    ReviewCompletedFact,
    ReviewMode,
    ReviewOutcome,
    ReviewVerification,
    ReviewVerificationRequest,
)

REVIEW_SCHEMA_ID = "sett.governance.review"
REVIEW_SCHEMA_VERSION = 1
UNCONFIRMED = "policy.review_record_unconfirmed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ObligationFollowUp:
    """What one follow-up observed for one declared obligation; never authority.

    ``reason_code`` is set when the obligation could not be verified, and the
    facts are then empty. ``deadline`` is only what the implementation's
    verifier reported; SETT calculates no deadline.
    """

    decision_operation_id: str
    obligation_id: str
    mode: ReviewMode
    action_id: str
    action_type: str
    notified: bool
    completed: ReviewOutcome | None
    deadline: DeadlineStatus | None
    evidence_refs: tuple[str, ...] = field(repr=False)
    reason_code: str | None

    @property
    def open(self) -> bool:
        """Whether it was verified and no completed review was reported."""
        return self.reason_code is None and self.completed is None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewFollowUp:
    """One follow-up: each declared obligation and how many new records it wrote."""

    obligations: tuple[ObligationFollowUp, ...]
    recorded: int


def _rebuild(
    binding_for: Callable[[str, str, str, Any], Any], row: Mapping[str, Any],
) -> PolicyReviewObligation | None:
    """Rebuild a recorded obligation, of any mode, with the binding still registered."""
    try:
        binding = binding_for(row["policy_id"], row["policy_version"], row["action_type"], row["binding"])
        if binding is None:
            return None
        return PolicyReviewObligation(
            obligation_id=row["obligation_id"], binding=binding, mode=ReviewMode(row["mode"]),
            verifier_id=row["verifier_id"], verifier_version=row["verifier_version"],
            action_id=row["action_id"], action_type=row["action_type"],
            future_action_types=tuple(row["future_action_types"]),
        )
    except Exception:  # noqa: BLE001 -- a record that cannot be rebuilt is reported, not raised
        return None


def _checked(
    verifier: Any, obligation: PolicyReviewObligation, operation_id: str,
) -> tuple[ReviewVerification | None, str | None]:
    """Ask the verifier once about the declaring action; check what it returns."""
    failed = False
    result: object = None
    try:
        result = verifier.verify(ReviewVerificationRequest(
            obligation=obligation, action_id=obligation.action_id,
            action_type=obligation.action_type, evidence_ref=operation_id,
        ))
    except Exception:  # noqa: BLE001 -- sanitize application verifier failures
        failed = True
    if failed:
        return None, VERIFIER_FAILED
    if (
        type(result) is not ReviewVerification
        or result.obligation_id != obligation.obligation_id
        or json.dumps(result.binding.canonical_view(), sort_keys=True)
        != json.dumps(obligation.binding.canonical_view(), sort_keys=True)
        or result.action_id != obligation.action_id
        or result.action_type != obligation.action_type
    ):
        return None, RESULT_INVALID
    return result, None


def _fact_rows(facts: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows = []
    for fact in facts:
        if type(fact) is ReviewCompletedFact:
            rows.append({"fact": "review_completed", "outcome": fact.outcome.value,
                         "deadline_status": None, "evidence_ref": fact.evidence_ref})
        elif type(fact) is NotificationDeliveredFact:
            rows.append({"fact": "notification_delivered", "outcome": None,
                         "deadline_status": None, "evidence_ref": fact.evidence_ref})
        elif type(fact) is DeadlineStatusFact:
            rows.append({"fact": "deadline_status", "outcome": None,
                         "deadline_status": fact.status.value, "evidence_ref": fact.evidence_ref})
    return rows


def _identity(decision: str, obligation: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (decision, obligation, row["fact"], row["outcome"], row["deadline_status"], row["evidence_ref"])


def _recorded_identities(journal: GovernanceJournal, lock: Any) -> set[tuple[Any, ...]]:
    """The identities of the review records already in the journal, once they are durable.

    A record can reach the stream while its durability is not confirmed, and
    a later read returns it like any other. Before trusting the records found,
    the journal's durability is confirmed through the last of them; if it is
    not, the follow-up stops with ``policy.review_record_unconfirmed``.
    """
    result = journal.read()
    if not result.complete:
        raise _handoff.policy_error(STATE_UNAVAILABLE)
    found = set()
    last = None
    for entry in result.entries:
        record = entry.record
        if record.schema_id != REVIEW_SCHEMA_ID:
            continue
        try:
            value = json.loads(record.payload)
            found.add(_identity(value["decision_operation_id"], value["obligation_id"], value))
        except Exception as error:
            raise _handoff.policy_error(STATE_UNAVAILABLE) from error
        last = entry.position
    if last is not None:
        try:
            with lock:
                reason = journal.confirm(last)
        except Exception:  # noqa: BLE001 -- storage failure; reported below
            reason = UNCONFIRMED
        if reason is not None:
            raise _handoff.policy_error(UNCONFIRMED)
    return found


def _write(journal: GovernanceJournal, lock: Any, payload: dict[str, Any]) -> None:
    """Write one review record and require its confirmation."""
    identity = canonical_json_bytes(
        list(_identity(payload["decision_operation_id"], payload["obligation_id"], payload)),
        field="review record identity",
    )
    confirmed = False
    try:
        record = GovernanceRecord(
            operation_id="review." + hashlib.sha256(identity).hexdigest(),
            schema_id=REVIEW_SCHEMA_ID, schema_version=REVIEW_SCHEMA_VERSION,
            payload=canonical_json_bytes(payload, field="review record"),
        )
        with lock:
            scan = journal.read()
            if scan.complete and scan.observed_head is not None:
                written = journal.append(journal.prepare(record, expected_head=scan.observed_head))
                confirmed = (
                    written.state == "confirmed" and written.entry is not None
                    and written.durability_receipt is not None
                )
    except Exception:  # noqa: BLE001 -- journal or encoding failure; reported below
        confirmed = False
    if not confirmed:
        raise _handoff.policy_error(UNCONFIRMED)


def follow_up(
    verifiers: Mapping[tuple[str, str], Any],
    binding_for: Callable[[str, str, str, Any], Any],
    journal: GovernanceJournal,
    lock: Any,
    now: Callable[[], datetime],
) -> ReviewFollowUp:
    """Ask each declared obligation's verifier once and record its new facts."""
    try:
        recovered = read_decision_records(journal)
    except Exception as error:
        raise _handoff.policy_error(STATE_UNAVAILABLE) from error
    if recovered.requires_reconciliation:
        raise _handoff.policy_error(STATE_UNAVAILABLE)
    recorded = _recorded_identities(journal, lock)
    items: list[ObligationFollowUp] = []
    written = 0
    seen = set()
    for decision in recovered.decisions:
        for row in decision.review_obligations:
            key = (decision.operation_id, row.get("obligation_id"))
            if key in seen:
                continue
            seen.add(key)
            verifier = verifiers.get((row.get("verifier_id"), row.get("verifier_version")))
            obligation = _rebuild(binding_for, row) if verifier is not None else None
            result, code = (None, VERIFIER_MISSING if verifier is None else STATE_UNAVAILABLE)
            if obligation is not None:
                result, code = _checked(verifier, obligation, decision.operation_id)
            facts = _fact_rows(tuple(result.facts)) if result is not None else []
            for fact in facts:
                identity = _identity(decision.operation_id, row["obligation_id"], fact)
                if identity in recorded:
                    continue
                payload = {
                    "decision_operation_id": decision.operation_id,
                    "obligation_id": row["obligation_id"],
                    "mode": row["mode"],
                    **fact,
                    "recorded_at": now().isoformat(timespec="microseconds").replace("+00:00", "Z"),
                }
                _write(journal, lock, payload)
                recorded.add(identity)
                written += 1
            items.append(ObligationFollowUp(
                decision_operation_id=decision.operation_id,
                obligation_id=str(row.get("obligation_id")),
                mode=ReviewMode(row["mode"]),
                action_id=str(row.get("action_id")),
                action_type=str(row.get("action_type")),
                notified=any(fact["fact"] == "notification_delivered" for fact in facts),
                completed=next((ReviewOutcome(fact["outcome"]) for fact in facts
                                if fact["fact"] == "review_completed"), None),
                deadline=next((DeadlineStatus(fact["deadline_status"]) for fact in facts
                               if fact["fact"] == "deadline_status"), None),
                evidence_refs=tuple(fact["evidence_ref"] for fact in facts),
                reason_code=code,
            ))
    return ReviewFollowUp(obligations=tuple(items), recorded=written)
