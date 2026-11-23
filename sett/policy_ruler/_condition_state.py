"""Private condition production; it does not supply the other verified state a governed attempt needs.

At entry only the conditions of the entry checkpoint are verified; a
condition of a controlled checkpoint is verified when the handler reaches
that checkpoint, after the receipt was consumed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from itertools import islice
from typing import Any

from sett._governance.validation import canonical_json_bytes, validate_identifier
from sett.exceptions import SETTPolicyConfigurationError, SETTValidationError
from sett.policy_ruler import _handoff
from sett.policy_ruler.condition_results import (
    ConditionVerification,
    ConditionVerificationState,
)
from sett.policy_ruler.conditions import ConditionValidityMode, PolicyCondition
from sett.policy_ruler.contracts import ConditionVerifier

_NEGATIVE = {
    ConditionVerificationState.UNSATISFIED: "policy.condition_unsatisfied",
    ConditionVerificationState.EVIDENCE_NOT_PRESENT: "policy.condition_evidence_not_present",
    ConditionVerificationState.EVIDENCE_INVALID: "policy.condition_evidence_invalid",
    ConditionVerificationState.EVIDENCE_UNAVAILABLE: "policy.condition_evidence_unavailable",
}


def _admit_condition_verifiers(
    values: Sequence[ConditionVerifier],
) -> dict[tuple[str, str], ConditionVerifier]:
    """Freeze bounded verifier identities at the constructor boundary only."""
    admitted = False
    registry: dict[tuple[str, str], ConditionVerifier] = {}
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
            if not isinstance(verifier, ConditionVerifier):
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


def _identity(condition: PolicyCondition) -> tuple[str, str, str]:
    """Return the declared identity of one condition as detached base strings."""
    return (
        str.__str__(condition.binding.policy_id),
        str.__str__(condition.binding.policy_version),
        str.__str__(condition.condition_id),
    )


class _CheckpointScope:
    """The only part of a state context a verification after consumption uses."""

    __slots__ = ("request",)

    def __init__(self, request: Any) -> None:
        self.request = request


def _call_verify(
    verifier: ConditionVerifier, request: Any, condition: PolicyCondition,
) -> object:
    """Sanitize only the application verification lookup and call."""
    failed = False
    result: object = None
    try:
        result = verifier.verify(request, condition=condition)
    except Exception:  # noqa: BLE001 -- sanitize application verification failures
        failed = True
    if failed:
        return _handoff.ConditionRejection("policy.condition_verifier_failed")
    return result


def _call_confirm(
    verifier: ConditionVerifier, observation: ConditionVerification, request: Any,
) -> bool:
    """Sanitize only the application confirmation lookup and call."""
    result: object = None
    try:
        result = verifier.confirm(observation, request=request)
    except Exception:  # noqa: BLE001 -- sanitize application confirmation failures
        pass
    return result is True


class ConditionStateProducer:
    """The configured private producer, bound through ruler-owned invocation data.

    Its observations do not supply the other required state producers. The
    callback clock must be consistent with the injected ruler clock. Internal
    or external verifier changes are not automatically detected by SETT.
    """

    slot = "conditions"
    producer_id = "sett.policy_ruler.conditions"
    schema_revision = "1"

    def __init__(
        self, verifiers: Sequence[ConditionVerifier], now: Callable[[], datetime],
    ) -> None:
        """Freeze declared verifier identities and retain the injected clock."""
        self._verifiers = _admit_condition_verifiers(verifiers)
        self._now = now

    def _entries(self, context: _handoff.StateContext) -> tuple[Any, ...] | None:
        """Check independent expected entries and coverage before observation."""
        if context.condition_inventory is None or context.condition_coverage is None:
            return None
        entries = context.condition_entries
        expected = []
        for key, condition, coverage in entries:
            if type(condition) is not PolicyCondition:
                return None
            expected.append({"key": key, "condition": condition.canonical_view(),
                             "role": "required", "coverage": json.loads(coverage)})
        try:
            encoded = canonical_json_bytes(expected, field="condition inventory")
        except (SETTValidationError, UnicodeError):
            return None
        if encoded != context.condition_inventory:
            return None
        for row_bytes in context.condition_coverage:
            row = json.loads(row_bytes)
            if (
                row["role"] == "required"
                and row["applicability"] == "applies"
                and row["status"] not in {"admitted", "explicit_empty"}
            ):
                return None
        return entries

    def _prepass(self, entries: tuple[Any, ...]) -> bool:
        return all(
            (condition.verifier_id, condition.verifier_version) in self._verifiers
            for _key, condition, _coverage in entries
        )

    def _verify_one(
        self, context: _handoff.StateContext, condition: PolicyCondition,
    ) -> tuple[ConditionVerification | _handoff.ConditionRejection, bool]:
        """Verify one condition; the flag says whether its verifier was called."""
        verifier = self._verifiers[(condition.verifier_id, condition.verifier_version)]
        # Clock errors keep their own structured diagnostic, not a verifier failure.
        try:
            t0 = self._now()
        except SETTPolicyConfigurationError:
            return _handoff.ConditionRejection("policy.clock_invalid"), False
        result = _call_verify(verifier, context.request, condition)
        try:
            t1 = self._now()
        except SETTPolicyConfigurationError:
            return _handoff.ConditionRejection("policy.clock_invalid"), True
        if type(result) is _handoff.ConditionRejection:
            return result, True
        invalid = _handoff.ConditionRejection("policy.condition_result_invalid")
        if type(result) is not ConditionVerification:
            return invalid, True
        if type(result.condition) is not PolicyCondition:
            return invalid, True
        if type(result.state) is not ConditionVerificationState:
            return invalid, True
        if result.condition.canonical_view() != condition.canonical_view():
            return invalid, True
        if condition.validity_mode is ConditionValidityMode.RECHECK:
            if not t0 <= result.checked_at <= t1:
                return invalid, True
        elif result.checked_at > t1:
            return invalid, True
        if result.state is not ConditionVerificationState.SATISFIED:
            code = _NEGATIVE.get(result.state)
            return (_handoff.ConditionRejection(code) if code is not None else invalid), True
        if (
            condition.validity_mode is ConditionValidityMode.ATTESTATION
            and (result.valid_until is None or t1 >= result.valid_until)
        ):
            return invalid, True
        return result, True

    def produce(
        self, context: _handoff.StateContext,
    ) -> _handoff.VerifiedStateSlot | _handoff.ConditionRejection:
        """Retain one initial proof, or return that proof unchanged later."""
        incomplete = _handoff.ConditionRejection("policy.evaluation_incomplete")
        entries = self._entries(context)
        if entries is None or context.condition_proof is None:
            return incomplete
        if context.stage != "initial":
            slot = context.retained_condition_slot
            if slot is None or not _handoff.condition_slot_matches(slot, context):
                return incomplete
            if context.condition_proof.slot is not slot:
                return incomplete
            return slot
        if context.condition_proof.slot is not None:
            return incomplete
        if not self._prepass(entries):
            # Every verifier is checked before any is called, so none was
            # attempted; report the first condition, in verification order,
            # whose verifier is missing.
            missing = next(
                condition for _key, condition, _coverage in entries
                if (condition.verifier_id, condition.verifier_version) not in self._verifiers
            )
            return _handoff.ConditionRejection(
                "policy.condition_verifier_missing",
                failed_condition=_identity(missing),
                attempted_conditions=(),
            )
        # Conditions of a controlled checkpoint wait for that checkpoint.
        active = tuple(entry for entry in entries if entry[1].checkpoint_id == context.checkpoint_id)
        observations = []
        attempted: list[tuple[str, str, str]] = []
        for key, condition, _coverage in active:
            result, called = self._verify_one(context, condition)
            if called:
                attempted.append(_identity(condition))
            if type(result) is _handoff.ConditionRejection:
                return _handoff.ConditionRejection(
                    result.code,
                    failed_condition=_identity(condition),
                    attempted_conditions=tuple(attempted),
                )
            observations.append((key, result))
        try:
            value = canonical_json_bytes({
                "inventory": json.loads(context.condition_inventory),
                "observations": {key: item.canonical_view() for key, item in observations},
            }, field="condition state")
        except (SETTValidationError, UnicodeError):
            return incomplete
        references = tuple(sorted({ref for _key, item in observations for ref in item.evidence_refs}))
        if len(references) > 256:
            return incomplete
        deadlines = [item.valid_until for _key, item in observations if item.valid_until is not None]
        keys = tuple(key for key, _condition, _coverage in active)
        slot = _handoff.VerifiedStateSlot(
            slot=self.slot, producer_id=self.producer_id, schema_revision=self.schema_revision,
            epoch=0, action_id=context.request.action_id, checkpoint_id=context.checkpoint_id,
            required_keys=keys, observed_keys=keys, value_bytes=value,
            evidence_refs=references, valid_until=min(deadlines) if deadlines else None,
        )
        context.condition_proof.slot = slot
        context.condition_proof.observations = tuple(observations)
        return slot

    def confirm(self, slot: _handoff.VerifiedStateSlot) -> bool:
        """Check the retained slot identity fields; contextual proof is separate."""
        return (
            type(slot) is _handoff.VerifiedStateSlot
            and slot.slot == self.slot and slot.producer_id == self.producer_id
            and slot.schema_revision == self.schema_revision and slot.epoch == 0
        )

    def verify_at_checkpoint(
        self, request: Any, pending: tuple[Any, ...], verified: tuple[Any, ...],
    ) -> tuple[Any, ...] | _handoff.ConditionRejection:
        """Verify one controlled checkpoint after consumption, without a new proof.

        ``pending`` holds the conditions declared for this checkpoint as
        ``(key, condition)``; ``verified`` those already verified as
        ``(key, condition, observation)``. Every verifier is checked before
        any is called; then this checkpoint's conditions are verified, every
        earlier ``RECHECK`` condition is verified again, and an earlier
        ``ATTESTATION`` must still be within its limit. The result is the
        new verified tuple, or a rejection naming the failed condition.
        """
        needed = [condition for _key, condition in pending] + [
            condition for _key, condition, _observation in verified
            if condition.validity_mode is ConditionValidityMode.RECHECK
        ]
        for condition in needed:
            if (condition.verifier_id, condition.verifier_version) not in self._verifiers:
                return _handoff.ConditionRejection(
                    "policy.condition_verifier_missing",
                    failed_condition=_identity(condition), attempted_conditions=(),
                )
        scope = _CheckpointScope(request)
        attempted: list[tuple[str, str, str]] = []
        fresh = []
        for key, condition in pending:
            result, called = self._verify_one(scope, condition)  # type: ignore[arg-type]
            if called:
                attempted.append(_identity(condition))
            if type(result) is _handoff.ConditionRejection:
                return _handoff.ConditionRejection(
                    result.code, failed_condition=_identity(condition),
                    attempted_conditions=tuple(attempted),
                )
            fresh.append((key, condition, result))
        for _key, condition, observation in verified:
            if condition.validity_mode is ConditionValidityMode.RECHECK:
                result, called = self._verify_one(scope, condition)  # type: ignore[arg-type]
                if called:
                    attempted.append(_identity(condition))
                code = result.code if type(result) is _handoff.ConditionRejection else None
            else:
                try:
                    now = self._now()
                except SETTPolicyConfigurationError:
                    now = None
                if now is None:
                    code = "policy.clock_invalid"
                elif observation.valid_until is None or now >= observation.valid_until:
                    code = "policy.condition_result_invalid"
                else:
                    code = None
            if code is not None:
                return _handoff.ConditionRejection(
                    code, failed_condition=_identity(condition),
                    attempted_conditions=tuple(attempted),
                )
        return (*verified, *fresh)

    def confirm_context(self, context: _handoff.StateContext) -> bool:
        """Perform the real later check without changing the original proof."""
        entries = self._entries(context)
        proof = context.condition_proof
        if entries is None or proof is None or proof.slot is not context.retained_condition_slot:
            return False
        if context.stage not in {"pre_issue", "pre_consumption"}:
            return False
        if proof.slot is None or not _handoff.condition_slot_matches(proof.slot, context):
            return False
        active = tuple(entry for entry in entries if entry[1].checkpoint_id == context.checkpoint_id)
        if not self._prepass(entries) or len(proof.observations) != len(active):
            return False
        if any(type(item) is not ConditionVerification for _key, item in proof.observations):
            return False
        retained_values = json.loads(proof.slot.value_bytes)["observations"]
        if retained_values != {key: item.canonical_view() for key, item in proof.observations}:
            return False
        for (key, condition, _coverage), (observed_key, observation) in zip(active, proof.observations, strict=True):
            if key != observed_key or observation.condition.canonical_view() != condition.canonical_view():
                return False
            if condition.validity_mode is ConditionValidityMode.RECHECK:
                fresh, _called = self._verify_one(context, condition)
                if type(fresh) is _handoff.ConditionRejection:
                    return False
                proof.history += ((context.stage, key, fresh),)
            else:
                verifier = self._verifiers[(condition.verifier_id, condition.verifier_version)]
                if not _call_confirm(verifier, observation, context.request):
                    return False
        return True
