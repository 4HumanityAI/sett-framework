"""Deterministic SETT Authorization Ruler."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, NoReturn
from uuid import uuid4

from sett.authorization_ruler._validation import validate_identifier
from sett.authorization_ruler.contracts import (
    ApprovalVerifier,
    AuthorizationClock,
    AuthorizationFingerprintPolicy,
    AuthorizationPolicy,
)
from sett.authorization_ruler.errors import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
)
from sett.authorization_ruler.evidence import (
    AuthorizationEvent,
    AuthorizationEvidenceSink,
)
from sett.authorization_ruler.models import (
    ApprovalInput,
    ApprovalRequirement,
    AuthorizationConstraint,
    AuthorizationDecision,
    AuthorizationOutcome,
    AuthorizationReceipt,
    AuthorizationRequest,
    AuthorizationVerdict,
    ExecutionMode,
    PolicyDecision,
    ReceiptUse,
    VerifiedApproval,
)
from sett.authorization_ruler.store import AuthorizationReceiptStore
from sett.exceptions import SETTValidationError


class SystemAuthorizationClock:
    """Timezone-aware system clock for production wiring."""

    def now(self) -> datetime:
        """Return the current UTC authorization time."""
        return datetime.now(timezone.utc)


class AuthorizationRuler:
    """Evaluate exact policies and issue store-backed authorization receipts."""

    def __init__(
        self,
        *,
        issuer_id: str,
        receipt_ttl_seconds: float,
        fingerprint_policy: AuthorizationFingerprintPolicy,
        receipt_store: AuthorizationReceiptStore,
        evidence_sink: AuthorizationEvidenceSink,
        clock: AuthorizationClock | None = None,
    ) -> None:
        """Configure explicit policy, storage, evidence, lifetime, and time."""
        if (
            isinstance(receipt_ttl_seconds, bool)
            or not isinstance(receipt_ttl_seconds, int | float)
            or not math.isfinite(receipt_ttl_seconds)
            or receipt_ttl_seconds <= 0
        ):
            raise SETTValidationError(
                "receipt_ttl_seconds must be an explicit positive number."
            )
        if not isinstance(fingerprint_policy, AuthorizationFingerprintPolicy):
            raise SETTValidationError(
                "fingerprint_policy must implement AuthorizationFingerprintPolicy."
            )
        if not isinstance(receipt_store, AuthorizationReceiptStore):
            raise SETTValidationError(
                "receipt_store must implement AuthorizationReceiptStore."
            )
        if not isinstance(evidence_sink, AuthorizationEvidenceSink):
            raise SETTValidationError(
                "evidence_sink must implement AuthorizationEvidenceSink."
            )
        selected_clock = clock or SystemAuthorizationClock()
        if not isinstance(selected_clock, AuthorizationClock):
            raise SETTValidationError("clock must implement AuthorizationClock.")
        validate_identifier(fingerprint_policy.policy_id, field="fingerprint policy_id")
        self._issuer_id = validate_identifier(issuer_id, field="issuer_id")
        self._ttl = float(receipt_ttl_seconds)
        self._fingerprint_policy = fingerprint_policy
        self._receipt_store = receipt_store
        self._evidence_sink = evidence_sink
        self._clock = selected_clock
        self._policies: dict[str, dict[str, AuthorizationPolicy]] = {}
        self._verifiers: dict[str, ApprovalVerifier] = {}
        self._lock = RLock()

    def register_policy(self, action_type: str, policy: AuthorizationPolicy) -> None:
        """Register one policy for one exact action type."""

        validate_identifier(action_type, field="action_type")
        if not isinstance(policy, AuthorizationPolicy):
            raise SETTAuthorizationConfigurationError(
                "Policy does not implement the authorization contract.",
                reason_code="authorization.policy_invalid",
            )
        policy_id = validate_identifier(policy.policy_id, field="policy_id")
        with self._lock:
            policies = self._policies.setdefault(action_type, {})
            if policy_id in policies:
                raise SETTAuthorizationConfigurationError(
                    "Policy identifier is already registered for this action type.",
                    reason_code="authorization.policy_duplicate",
                )
            policies[policy_id] = policy

    def register_approval_verifier(self, verifier: ApprovalVerifier) -> None:
        """Register one deterministic additional-authority verifier."""

        if not isinstance(verifier, ApprovalVerifier):
            raise SETTAuthorizationConfigurationError(
                "Approval verifier does not implement the verifier contract.",
                reason_code="authorization.verifier_invalid",
            )
        verifier_id = validate_identifier(verifier.verifier_id, field="verifier_id")
        with self._lock:
            if verifier_id in self._verifiers:
                raise SETTAuthorizationConfigurationError(
                    "Approval verifier identifier is already registered.",
                    reason_code="authorization.verifier_duplicate",
                )
            self._verifiers[verifier_id] = verifier

    def authorize(
        self,
        request: AuthorizationRequest,
        *,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> AuthorizationOutcome:
        """Evaluate all exact policies and issue a receipt only for GRANT."""

        if not isinstance(request, AuthorizationRequest):
            raise SETTValidationError("request must be an AuthorizationRequest.")
        self._emit(
            "authorization.requested",
            attributes={
                "action_id": request.action_id,
                "action_type": request.action_type,
                "execution_mode": request.execution_mode.value,
            },
        )
        non_authoritative = request.non_authoritative_fields()
        if non_authoritative:
            self._fail(
                "authorization.provenance_not_authoritative",
                field_count=len(non_authoritative),
            )
        verified_approvals = self._verify_approval_inputs(request, approval_inputs)
        policies = self._policies_for(request.action_type)
        decisions: list[PolicyDecision] = []
        for policy in policies:
            try:
                decision = policy.evaluate(request, verified_approvals)
            except Exception as error:  # noqa: BLE001
                self._fail(
                    "authorization.policy_failed",
                    error_type=type(error).__name__,
                    policy_id=policy.policy_id,
                )
            if not isinstance(decision, PolicyDecision):
                self._fail(
                    "authorization.policy_result_invalid",
                    policy_id=policy.policy_id,
                )
            if decision.policy_id != policy.policy_id:
                self._fail(
                    "authorization.policy_identity_mismatch",
                    policy_id=policy.policy_id,
                )
            decisions.append(decision)
            self._emit(
                "authorization.policy_checked",
                reason_codes=decision.reason_codes,
                attributes={
                    "action_id": request.action_id,
                    "policy_id": policy.policy_id,
                    "receipt_use": decision.receipt_use.value,
                    "verdict": decision.verdict.value,
                },
            )
        aggregate = self._compose(decisions)
        if aggregate.verdict is AuthorizationVerdict.DENY:
            self._emit(
                "authorization.denied",
                reason_codes=aggregate.reason_codes,
                attributes={
                    "action_id": request.action_id,
                    "policy_count": len(aggregate.policy_ids),
                },
            )
            return AuthorizationOutcome(aggregate)
        if aggregate.verdict is AuthorizationVerdict.REQUIRE_APPROVAL:
            self._emit(
                "authorization.approval_required",
                reason_codes=aggregate.reason_codes,
                attributes={
                    "action_id": request.action_id,
                    "policy_count": len(aggregate.policy_ids),
                    "requirement_count": len(aggregate.requirements),
                },
            )
            return AuthorizationOutcome(aggregate)
        fingerprint = self._fingerprint(request, verified_approvals)
        now = self._now()
        receipt = AuthorizationReceipt(
            receipt_id=str(uuid4()),
            issuer_id=self._issuer_id,
            action_id=request.action_id,
            fingerprint_policy_id=self._fingerprint_policy.policy_id,
            request_fingerprint=fingerprint,
            policy_ids=aggregate.policy_ids,
            reason_codes=aggregate.reason_codes,
            constraint_ids=tuple(
                constraint.constraint_id for constraint in aggregate.constraints
            ),
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            execution_mode=request.execution_mode,
            receipt_use=(
                ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
                if (
                    aggregate.receipt_use
                    is ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
                    and request.execution_mode is ExecutionMode.CONTROLLED
                    and request.idempotency_key is not None
                )
                else ReceiptUse.SINGLE_USE
            ),
        )
        try:
            self._receipt_store.issue(receipt)
        except SETTAuthorizationError:
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(
                "authorization.receipt_store_failed",
                error_type=type(error).__name__,
            )
        self._emit(
            "authorization.granted",
            reason_codes=aggregate.reason_codes,
            attributes={
                "action_id": request.action_id,
                "constraint_count": len(aggregate.constraints),
                "policy_count": len(aggregate.policy_ids),
            },
        )
        self._emit(
            "authorization.receipt_issued",
            attributes={
                "action_id": request.action_id,
                "execution_mode": request.execution_mode.value,
                "receipt_id": receipt.receipt_id,
                "receipt_use": receipt.receipt_use.value,
            },
        )
        return AuthorizationOutcome(aggregate, receipt)

    def verify_and_consume(
        self,
        receipt: AuthorizationReceipt,
        request: AuthorizationRequest,
        *,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> None:
        """Verify issuer-owned state and atomically consume effect authority."""

        if not isinstance(receipt, AuthorizationReceipt):
            raise SETTValidationError("receipt must be an AuthorizationReceipt.")
        if not isinstance(request, AuthorizationRequest):
            raise SETTValidationError("request must be an AuthorizationRequest.")
        verified_approvals = self._verify_approval_inputs(request, approval_inputs)
        fingerprint = self._fingerprint(request, verified_approvals)
        try:
            self._receipt_store.verify_and_consume(
                receipt,
                request_fingerprint=fingerprint,
                now=self._now(),
            )
        except SETTAuthorizationError as error:
            self._emit(
                "authorization.receipt_rejected",
                reason_codes=(error.reason_code,),
                attributes={
                    "action_id": request.action_id,
                    "receipt_id": receipt.receipt_id,
                },
            )
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(
                "authorization.receipt_store_failed",
                error_type=type(error).__name__,
            )
        self._emit(
            "authorization.receipt_verified",
            attributes={
                "action_id": request.action_id,
                "receipt_id": receipt.receipt_id,
            },
        )

    def revoke(self, receipt_id: str) -> None:
        """Revoke issuer-owned receipt state before any later effect."""

        validate_identifier(receipt_id, field="receipt_id")
        try:
            self._receipt_store.revoke(receipt_id)
        except SETTAuthorizationError:
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(
                "authorization.receipt_store_failed",
                error_type=type(error).__name__,
            )
        self._emit(
            "authorization.receipt_revoked",
            attributes={"receipt_id": receipt_id},
        )

    def _policies_for(self, action_type: str) -> tuple[AuthorizationPolicy, ...]:
        with self._lock:
            policies = self._policies.get(action_type)
            if not policies:
                self._fail("authorization.policy_missing")
            return tuple(policies[key] for key in sorted(policies))

    def _verify_approval_inputs(
        self,
        request: AuthorizationRequest,
        approval_inputs: Sequence[ApprovalInput],
    ) -> frozenset[VerifiedApproval]:
        inputs = tuple(approval_inputs)
        if any(not isinstance(item, ApprovalInput) for item in inputs):
            raise SETTValidationError(
                "approval_inputs must contain ApprovalInput instances."
            )
        verified: dict[tuple[str, str], VerifiedApproval] = {}
        with self._lock:
            verifiers = dict(self._verifiers)
        for approval_input in inputs:
            verifier = verifiers.get(approval_input.verifier_id)
            if verifier is None:
                self._fail(
                    "authorization.verifier_missing",
                    verifier_id=approval_input.verifier_id,
                )
            try:
                result = verifier.verify(approval_input, request)
            except Exception as error:  # noqa: BLE001
                self._fail(
                    "authorization.verifier_failed",
                    error_type=type(error).__name__,
                    verifier_id=approval_input.verifier_id,
                )
            if result is None:
                continue
            if not isinstance(result, VerifiedApproval):
                self._fail(
                    "authorization.verifier_result_invalid",
                    verifier_id=approval_input.verifier_id,
                )
            if (
                result.requirement_id != approval_input.requirement_id
                or result.verifier_id != approval_input.verifier_id
            ):
                self._fail(
                    "authorization.verifier_identity_mismatch",
                    verifier_id=approval_input.verifier_id,
                )
            key = (result.requirement_id, result.verifier_id)
            previous = verified.get(key)
            if previous is not None and previous != result:
                self._fail(
                    "authorization.approval_conflict",
                    verifier_id=approval_input.verifier_id,
                )
            verified[key] = result
        return frozenset(verified.values())

    def _fingerprint(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> str:
        try:
            fingerprint = self._fingerprint_policy.fingerprint(
                request,
                verified_approvals,
            )
            return validate_identifier(fingerprint, field="request_fingerprint")
        except SETTValidationError:
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(
                "authorization.fingerprint_failed",
                error_type=type(error).__name__,
            )

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            self._fail("authorization.clock_invalid")
        return now

    def _compose(self, decisions: list[PolicyDecision]) -> AuthorizationDecision:
        policy_ids = tuple(decision.policy_id for decision in decisions)
        reason_codes = tuple(
            dict.fromkeys(
                reason
                for decision in decisions
                for reason in decision.reason_codes
            )
        )
        denied = [
            decision
            for decision in decisions
            if decision.verdict is AuthorizationVerdict.DENY
        ]
        if denied:
            return AuthorizationDecision(
                AuthorizationVerdict.DENY,
                policy_ids,
                reason_codes=reason_codes,
            )
        requirements_by_id: dict[str, ApprovalRequirement] = {}
        for decision in decisions:
            for requirement in decision.requirements:
                previous = requirements_by_id.get(requirement.requirement_id)
                if previous is not None and previous != requirement:
                    self._fail(
                        "authorization.approval_requirement_conflict",
                        requirement_id=requirement.requirement_id,
                    )
                requirements_by_id[requirement.requirement_id] = requirement
        requirements = tuple(requirements_by_id.values())
        if requirements:
            return AuthorizationDecision(
                AuthorizationVerdict.REQUIRE_APPROVAL,
                policy_ids,
                reason_codes=reason_codes,
                requirements=requirements,
            )
        constraints_by_id: dict[str, AuthorizationConstraint] = {}
        for decision in decisions:
            for constraint in decision.constraints:
                if constraint.constraint_id in constraints_by_id:
                    self._fail(
                        "authorization.constraint_duplicate",
                        constraint_id=constraint.constraint_id,
                    )
                constraints_by_id[constraint.constraint_id] = constraint
        constraints = tuple(constraints_by_id.values())
        receipt_use = (
            ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
            if all(
                decision.receipt_use
                is ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
                for decision in decisions
            )
            else ReceiptUse.SINGLE_USE
        )
        return AuthorizationDecision(
            AuthorizationVerdict.GRANT,
            policy_ids,
            receipt_use=receipt_use,
            reason_codes=reason_codes,
            constraints=constraints,
        )

    def _emit(
        self,
        kind: str,
        *,
        reason_codes: tuple[str, ...] = (),
        attributes: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._evidence_sink.record(
                AuthorizationEvent(
                    kind,
                    reason_codes=reason_codes,
                    attributes=attributes,
                )
            )
        except SETTAuthorizationConfigurationError:
            raise
        except Exception as error:
            raise SETTAuthorizationConfigurationError(
                "Authorization evidence could not be recorded.",
                reason_code="authorization.evidence_failed",
            ) from error

    def _fail(self, reason_code: str, **attributes: Any) -> NoReturn:
        self._emit(
            "authorization.failed",
            reason_codes=(reason_code,),
            attributes=attributes,
        )
        raise SETTAuthorizationConfigurationError(
            "Authorization could not produce a trustworthy result.",
            reason_code=reason_code,
        )
