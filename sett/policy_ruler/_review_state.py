"""Private review-state production: restrictions declared by earlier decisions.

Before an effect, only ``RESTRICT_DECLARED_FUTURE_ACTIONS`` blocks, and only
a later action whose type is one of the declared future types. The earlier
obligations come from the decision records in the governance journal. A
restriction is lifted only when its configured verifier reports a favorable
completed review for it; an unfavorable review, a notification or a deadline
status does not lift it. Each collection asks the verifiers again.

The obligations an authorization declared itself never block that
authorization: a notification and a review with deadline are post-action,
and a restriction binds later actions. The exemption covers only the
decision record the authorization confirmed, found by the position its
retained durability slot names, never by a caller-supplied action
identifier. A separately submitted action, or a new authorization of the
same request, is a later action even when it reuses the identifier.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from itertools import islice
from typing import Any

from sett._governance.validation import canonical_json_bytes, validate_identifier
from sett.policy_ruler import _handoff
from sett.policy_ruler.decision_record import (
    DecisionRecordProducer,
    read_decision_records,
)
from sett.policy_ruler.models import PolicyBinding
from sett.policy_ruler.persistence import GovernanceJournal
from sett.policy_ruler.review import (
    PolicyReviewObligation,
    ReviewCompletedFact,
    ReviewMode,
    ReviewOutcome,
    ReviewVerification,
    ReviewVerificationRequest,
)

RESTRICTION_ACTIVE = "policy.review_restriction_active"
VERIFIER_MISSING = "policy.review_verifier_missing"
VERIFIER_FAILED = "policy.review_verifier_failed"
RESULT_INVALID = "policy.review_result_invalid"
STATE_UNAVAILABLE = "policy.review_state_unavailable"
REVIEW_CODES = frozenset({RESTRICTION_ACTIVE, VERIFIER_MISSING, VERIFIER_FAILED, RESULT_INVALID, STATE_UNAVAILABLE})


def _admit_review_verifiers(values: Sequence[Any]) -> dict[tuple[str, str], Any]:
    """Freeze bounded verifier identities at the constructor boundary only."""
    admitted = False
    registry: dict[tuple[str, str], Any] = {}
    try:
        if not isinstance(values, Sequence) or isinstance(
            values, str | bytes | bytearray | memoryview | Mapping
        ):
            raise ValueError
        size = len(values)
        if not 0 <= size <= 256:
            raise ValueError
        items = tuple(islice(values, 257))
        if len(items) != size:
            raise ValueError
        for verifier in items:
            if not callable(getattr(verifier, "verify", None)):
                raise ValueError
            identifier = verifier.verifier_id
            version = verifier.verifier_version
            validate_identifier(identifier, field="verifier_id")
            validate_identifier(version, field="verifier_version")
            key = (str(identifier), str(version))
            if key in registry:
                raise ValueError
            registry[key] = verifier
        admitted = True
    except Exception:  # noqa: BLE001 -- sanitize constructor verifier admission failures
        pass
    if not admitted:
        raise _handoff.policy_error("policy.ruler_configuration_invalid")
    return registry


def _own_decision_position(slot: object) -> tuple[str, str, str] | None:
    """The journal position of the decision record an authorization confirmed.

    The policy ruler retains the durability slot of each bound authorization;
    nothing here comes from the caller. Without a retained slot of the
    decision record producer there is no position, and nothing is exempt.
    """
    if (
        type(slot) is not _handoff.VerifiedStateSlot
        or slot.slot != DecisionRecordProducer.slot
        or slot.producer_id != DecisionRecordProducer.producer_id
    ):
        return None
    try:
        position = json.loads(slot.value_bytes)["position"]
        own = (position["backend_id"], position["stream_id"], position["token"])
    except (ValueError, KeyError, TypeError):
        return None
    return own if all(type(part) is str for part in own) else None


class ReviewStateProducer:
    """The ``review`` slot producer installed with review verifiers and a journal."""

    slot = "review"
    producer_id = "sett.policy_ruler.review_restrictions"
    schema_revision = "1"

    def __init__(
        self,
        verifiers: Sequence[Any],
        journal: GovernanceJournal,
        binding_for: Callable[[str, str, str, Any], PolicyBinding | None],
    ) -> None:
        """Freeze the verifiers and keep the journal and the binding lookup."""
        self._verifiers = _admit_review_verifiers(verifiers)
        self._journal = journal
        self._binding_for = binding_for

    def _restrictions(self, context: _handoff.StateContext) -> list[tuple[str, dict[str, Any]]] | None:
        """Earlier restriction rows naming this action type, or None if unreadable.

        Only the authorization's own decision record is skipped, identified by
        its retained durability slot. Before that record exists, at the first
        collection, nothing is skipped.
        """
        request = context.request
        own = _own_decision_position(context.retained_durability_slot)
        recovered = read_decision_records(self._journal)
        if recovered.requires_reconciliation:
            return None
        found = []
        seen = set()
        for record in recovered.decisions:
            position = record.position
            if own is not None and (position.backend_id, position.stream_id, position.token) == own:
                continue
            for row in record.review_obligations:
                if row.get("mode") != ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS.value:
                    continue
                if request.action_type not in row.get("future_action_types", ()):
                    continue
                key = (record.operation_id, row.get("obligation_id"))
                if key in seen:
                    continue
                seen.add(key)
                found.append((record.operation_id, row))
        return found

    def produce(
        self, context: _handoff.StateContext,
    ) -> _handoff.VerifiedStateSlot | _handoff.ConditionRejection | None:
        """Verify that every restriction naming this action is lifted."""
        request = context.request
        try:
            restrictions = self._restrictions(context)
        except Exception:  # noqa: BLE001 -- an unreadable journal blocks the attempt
            restrictions = None
        if restrictions is None:
            return _handoff.ConditionRejection(STATE_UNAVAILABLE)
        if any((row.get("verifier_id"), row.get("verifier_version")) not in self._verifiers
               for _operation, row in restrictions):
            return _handoff.ConditionRejection(VERIFIER_MISSING)
        rows = []
        keys = []
        references = set()
        for operation_id, row in restrictions:
            obligation = self._obligation(row)
            if obligation is None:
                return _handoff.ConditionRejection(STATE_UNAVAILABLE)
            verifier = self._verifiers[(row["verifier_id"], row["verifier_version"])]
            outcome, evidence = self._verify(verifier, obligation, request, operation_id)
            if outcome != "favorable":
                return _handoff.ConditionRejection(outcome)
            keys.append(f"{operation_id}/{obligation.obligation_id}")
            references.add(evidence)
            rows.append({
                "operation_id": operation_id, "obligation_id": obligation.obligation_id,
                "outcome": ReviewOutcome.FAVORABLE.value, "evidence_ref": evidence,
            })
        value = canonical_json_bytes({"restrictions": rows}, field="review state")
        normalized = tuple(sorted(keys))
        return _handoff.VerifiedStateSlot(
            slot=self.slot, producer_id=self.producer_id, schema_revision=self.schema_revision,
            epoch=0, action_id=request.action_id, checkpoint_id=context.checkpoint_id,
            required_keys=normalized, observed_keys=normalized, value_bytes=value,
            evidence_refs=tuple(sorted(references)), valid_until=None,
        )

    def _obligation(self, row: Mapping[str, Any]) -> PolicyReviewObligation | None:
        """Rebuild the declared obligation with the binding still registered for it."""
        try:
            binding = self._binding_for(
                row["policy_id"], row["policy_version"], row["action_type"], row["binding"],
            )
            if binding is None:
                return None
            return PolicyReviewObligation(
                obligation_id=row["obligation_id"], binding=binding,
                mode=ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS,
                verifier_id=row["verifier_id"], verifier_version=row["verifier_version"],
                action_id=row["action_id"], action_type=row["action_type"],
                future_action_types=tuple(row["future_action_types"]),
            )
        except Exception:  # noqa: BLE001 -- a record that cannot be rebuilt blocks
            return None

    @staticmethod
    def _verify(verifier: Any, obligation: PolicyReviewObligation, request: Any, operation_id: str) -> tuple[str, str]:
        """Ask the verifier once; return ``favorable`` and its evidence, or a code."""
        failed = False
        result: object = None
        try:
            result = verifier.verify(ReviewVerificationRequest(
                obligation=obligation, action_id=request.action_id,
                action_type=request.action_type, evidence_ref=operation_id,
            ))
        except Exception:  # noqa: BLE001 -- sanitize application verifier failures
            failed = True
        if failed:
            return VERIFIER_FAILED, ""
        if (
            type(result) is not ReviewVerification
            or result.obligation_id != obligation.obligation_id
            or json.dumps(result.binding.canonical_view(), sort_keys=True)
            != json.dumps(obligation.binding.canonical_view(), sort_keys=True)
            or result.action_id != request.action_id
            or result.action_type != request.action_type
        ):
            return RESULT_INVALID, ""
        for fact in result.facts:
            if type(fact) is ReviewCompletedFact and fact.outcome is ReviewOutcome.FAVORABLE:
                return "favorable", fact.evidence_ref
        return RESTRICTION_ACTIVE, ""

    def confirm(self, slot: _handoff.VerifiedStateSlot) -> bool:
        """Accept a record with this producer's identity; later stages verify again."""
        return (
            type(slot) is _handoff.VerifiedStateSlot
            and slot.slot == self.slot
            and slot.producer_id == self.producer_id
            and slot.schema_revision == self.schema_revision
            and slot.epoch == 0
        )


def review_rows(slot: object) -> list[dict[str, Any]]:
    """The restriction checks recorded in a review slot of this producer, for the decision record."""
    if type(slot) is not _handoff.VerifiedStateSlot or slot.producer_id != ReviewStateProducer.producer_id:
        return []
    try:
        return list(json.loads(slot.value_bytes)["restrictions"])
    except Exception:  # noqa: BLE001
        return []
