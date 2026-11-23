"""Decision records: durable evidence of completed policy evaluations.

``PolicyRuler(governance_journal=...)`` installs the producer of the
``durability`` state slot. For every completed bound evaluation it writes one
decision record to the configured ``GovernanceJournal`` and requires that
record confirmed at the journal's required durability before the slot exists.
A receipt can be issued only after that, so a receipt never exists without its
decision record. Nothing joins the journal, the receipt store, the trace and
the external effect in one transaction: the order is the guarantee.

A record carries only settled facts: the attempt and action identity, the
selection, each governing policy's verdict and reason codes, the aggregate
decision, the verified condition and approval identities, how consent was
supplied, the StrictJSON request fingerprint and the time it was recorded.
Version 2 adds the review obligations the decision declared and the earlier
restrictions checked for the action. Version 3 adds each policy's declared
origins and profile, copied when its result was admitted, and the
requirements each verified condition declared. Version 4 adds the
results of the informative policies of the attempt, which never decide
anything. It carries no request payload and no approval authority identity.
Records of versions 1 to 3 stay readable.

``read_decision_records()`` reads a journal and returns the decisions it
holds as values. It restores no receipt, consumption, review or effect.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import uuid4

from sett._governance.validation import canonical_json_bytes
from sett.persistence.formats import StreamPosition
from sett.policy_ruler import _handoff
from sett.policy_ruler.persistence import (
    GovernanceJournal,
    GovernanceReadResult,
    GovernanceRecord,
)

DECISION_SCHEMA_ID = "sett.governance.decision"
DECISION_SCHEMA_VERSION = 4
UNCONFIRMED = "policy.decision_record_unconfirmed"

_FIELDS_V1 = (
    "authorization_attempt_id", "action_id", "action_type", "trace_id", "checkpoint_id",
    "selection", "policies", "decision", "conditions", "approvals", "consent",
    "request_fingerprint", "fingerprint_policy_id", "recorded_at",
)
_FIELDS_V2 = (*_FIELDS_V1, "review_obligations", "review")
_FIELDS = {1: _FIELDS_V1, 2: _FIELDS_V2, 3: _FIELDS_V2, 4: (*_FIELDS_V2, "informative")}


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRecord:
    """One decision as recorded; a value to read, never authority.

    It says that an evaluation completed with this outcome. It does not say
    that a receipt was issued or used, or that an effect happened.
    """

    operation_id: str
    position: StreamPosition
    authorization_attempt_id: str | None
    action_id: str
    action_type: str
    trace_id: str
    checkpoint_id: str
    selection: dict[str, Any] = field(repr=False)
    policies: tuple[dict[str, Any], ...] = field(repr=False)
    decision: dict[str, Any] = field(repr=False)
    conditions: tuple[dict[str, Any], ...] = field(repr=False)
    approvals: tuple[dict[str, Any], ...] = field(repr=False)
    consent: str | None
    request_fingerprint: str | None = field(repr=False)
    fingerprint_policy_id: str | None
    recorded_at: str
    review_obligations: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    review: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    informative: tuple[dict[str, Any], ...] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRecovery:
    """What one read of a journal can vouch for, shaped like a recovery report.

    ``decisions`` come only from the stable prefix the journal read admitted.
    ``complete`` is the journal read's completeness; when it is false, or when
    a decision record could not be decoded, ``requires_reconciliation`` is
    true. Records of other schemas are counted, not interpreted.
    """

    decisions: tuple[DecisionRecord, ...] = field(repr=False)
    complete: bool
    last_position: StreamPosition | None
    other_records: int
    diagnostics: tuple[str, ...]

    @property
    def requires_reconciliation(self) -> bool:
        """Whether the read was incomplete or a decision record was unreadable."""
        return not self.complete or bool(self.diagnostics)


def read_decision_records(journal: GovernanceJournal) -> DecisionRecovery:
    """Read the journal and return its decision records as values."""
    if type(journal) is not GovernanceJournal:
        raise TypeError("journal must be a GovernanceJournal.")
    return _recovery_from(journal.read())


def _recovery_from(result: GovernanceReadResult) -> DecisionRecovery:
    """Decode the decision records of one journal read; the read is not repeated."""
    decisions: list[DecisionRecord] = []
    diagnostics: list[str] = []
    other = 0
    for index, entry in enumerate(result.entries):
        record = entry.record
        if record.schema_id != DECISION_SCHEMA_ID:
            other += 1
            continue
        decoded = _decode(record, entry.position)
        if decoded is None:
            diagnostics.append(f"decision_record_unreadable:{index}")
        else:
            decisions.append(decoded)
    if result.reason_code is not None:
        diagnostics.append(result.reason_code)
    return DecisionRecovery(
        decisions=tuple(decisions), complete=result.complete,
        last_position=result.observed_head, other_records=other,
        diagnostics=tuple(diagnostics),
    )


def _decode(record: GovernanceRecord, position: StreamPosition) -> DecisionRecord | None:
    fields = _FIELDS.get(record.schema_version)
    if fields is None:
        return None
    try:
        value = json.loads(record.payload)
        if type(value) is not dict or sorted(value) != sorted(fields):
            return None
        if canonical_json_bytes(value, field="decision record") != record.payload:
            return None
        return DecisionRecord(
            operation_id=record.operation_id, position=position,
            authorization_attempt_id=value["authorization_attempt_id"],
            action_id=value["action_id"], action_type=value["action_type"],
            trace_id=value["trace_id"], checkpoint_id=value["checkpoint_id"],
            selection=value["selection"], policies=tuple(value["policies"]),
            decision=value["decision"], conditions=tuple(value["conditions"]),
            approvals=tuple(value["approvals"]), consent=value["consent"],
            request_fingerprint=value["request_fingerprint"],
            fingerprint_policy_id=value["fingerprint_policy_id"],
            recorded_at=value["recorded_at"],
            review_obligations=tuple(value.get("review_obligations", ())),
            review=tuple(value.get("review", ())),
            informative=tuple(value["informative"]) if "informative" in value else None,
        )
    except Exception:  # noqa: BLE001 -- an unreadable record is reported, not raised
        return None


class DecisionRecordProducer:
    """The ``durability`` slot producer installed with a governance journal.

    At the initial stage it writes one decision record and returns a slot only
    when the journal confirms it at the required durability. At later stages
    it returns the record retained for the invocation and writes nothing more.
    Writes through one producer are serialized, so concurrent attempts in one
    process do not see each other as a stale journal head.
    """

    slot = "durability"
    producer_id = "sett.policy_ruler.decision_record"
    schema_revision = "1"

    def __init__(self, journal: GovernanceJournal, now: Callable[[], datetime]) -> None:
        """Bind the journal and the ruler's clock."""
        self._journal = journal
        self._now = now
        self._lock = RLock()

    def _own(self, slot: object) -> bool:
        return (
            type(slot) is _handoff.VerifiedStateSlot
            and slot.slot == self.slot
            and slot.producer_id == self.producer_id
            and slot.schema_revision == self.schema_revision
            and slot.epoch == 0
        )

    def produce(
        self, context: _handoff.StateContext,
    ) -> _handoff.VerifiedStateSlot | _handoff.ConditionRejection | None:
        """Write and confirm the decision record, or return the retained one."""
        if context.stage != "initial":
            retained = context.retained_durability_slot
            if (
                not self._own(retained)
                or retained.action_id != context.request.action_id
                or retained.checkpoint_id != context.checkpoint_id
            ):
                return None
            return retained
        facts = context.decision_facts
        if facts is None:
            return _handoff.ConditionRejection(UNCONFIRMED)
        try:
            recorded_at = self._now().isoformat(timespec="microseconds").replace("+00:00", "Z")
            payload = dict(facts)
            payload["conditions"] = _condition_rows(context)
            payload["recorded_at"] = recorded_at
            payload_bytes = canonical_json_bytes(payload, field="decision record")
            attempt = payload["authorization_attempt_id"]
            record = GovernanceRecord(
                operation_id="decision." + (attempt if attempt is not None else uuid4().hex),
                schema_id=DECISION_SCHEMA_ID, schema_version=DECISION_SCHEMA_VERSION,
                payload=payload_bytes,
            )
            with self._lock:
                scan = self._journal.read()
                if not scan.complete or scan.observed_head is None:
                    return _handoff.ConditionRejection(UNCONFIRMED)
                submission = self._journal.prepare(record, expected_head=scan.observed_head)
                written = self._journal.append(submission)
        except Exception:  # noqa: BLE001 -- journal, clock or encoding failure blocks the attempt
            return _handoff.ConditionRejection(UNCONFIRMED)
        if written.state != "confirmed" or written.entry is None or written.durability_receipt is None:
            return _handoff.ConditionRejection(UNCONFIRMED)
        position = written.entry.position
        value = canonical_json_bytes({
            "write_id": written.entry.write_id,
            "position": {
                "backend_id": position.backend_id, "stream_id": position.stream_id, "token": position.token,
            },
            "schema_id": DECISION_SCHEMA_ID,
            "schema_version": DECISION_SCHEMA_VERSION,
            "achieved_durability": written.durability_receipt.achieved_durability.value,
        }, field="decision record slot")
        return _handoff.VerifiedStateSlot(
            slot=self.slot, producer_id=self.producer_id, schema_revision=self.schema_revision,
            epoch=0, action_id=context.request.action_id, checkpoint_id=context.checkpoint_id,
            required_keys=("decision_record",), observed_keys=("decision_record",),
            value_bytes=value, evidence_refs=(written.entry.write_id,), valid_until=None,
        )

    def confirm(self, slot: _handoff.VerifiedStateSlot) -> bool:
        """Accept a retained record of this producer; a confirmed write stays confirmed."""
        return self._own(slot)


def _condition_rows(context: _handoff.StateContext) -> list[dict[str, Any]]:
    """Identities, states and declared requirements of the conditions the producer verified.

    Each requirement is named by its layer and requirement ID; its revision
    and source are in the policy's row of the same record.
    """
    proof = context.condition_proof
    if proof is None:
        return []
    rows = []
    for _key, observation in proof.observations:
        condition = observation.condition
        requirements: list[dict[str, str]] = []
        for origin in condition.origins:
            requirement = {
                "layer_id": str.__str__(origin.layer_id),
                "requirement_id": str.__str__(origin.requirement_id),
            }
            if requirement not in requirements:
                requirements.append(requirement)
        rows.append({
            "policy_id": str.__str__(condition.binding.policy_id),
            "policy_version": str.__str__(condition.binding.policy_version),
            "condition_id": str.__str__(condition.condition_id),
            "state": observation.state.value,
            "requirements": requirements,
        })
    return rows
