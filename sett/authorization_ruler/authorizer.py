"""Deterministic SETT Authorization Ruler.

Authorization keeps final authority and receipts. Policy registration,
declared selection, evaluation and composition belong to the injected
PolicyRuler, which this module drives through a private link. There is no
second registry, no second engine and no legacy route.

``authorize()`` deliberately has no parameter for a policy evaluation
result. Authority comes from the private invocation this ruler opens on
the injected policy owner, and every receipt operation revalidates the
exact request, selection, both generations, the checkpoint and the
complete verified state before the store is touched.

When the executor consumes a receipt, it keeps what the consumption
checked and compares it again after the effect barrier, immediately
before the handler: the trace callbacks that run in between are
application code, and a configuration change or an expiry there blocks
the effect. That final gate calls no application code.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, NoReturn
from uuid import uuid4

from sett._governance.evidence import _ATTEMPT_ID, _with_attempt_id
from sett.authorization_ruler._validation import validate_identifier
from sett.authorization_ruler.contracts import (
    ApprovalVerifier,
    AuthorizationClock,
    AuthorizationFingerprintPolicy,
    AuthorizationPolicy,
    PolicySelectionResolver,
)
from sett.authorization_ruler.errors import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
)
from sett.authorization_ruler.evidence import (
    AuthorizationEvent,
    AuthorizationEvidenceSink,
)
from sett.authorization_ruler.fingerprint import StrictJSONAuthorizationFingerprint
from sett.authorization_ruler.models import (
    ApprovalInput,
    AuthorizationOutcome,
    AuthorizationReceipt,
    AuthorizationRequest,
    AuthorizationVerdict,
    ExecutionMode,
    PolicyBinding,
    ReceiptUse,
    VerifiedApproval,
)
from sett.authorization_ruler.store import AuthorizationReceiptStore
from sett.exceptions import SETTPolicyConfigurationError, SETTValidationError
from sett.policy_ruler import _handoff
from sett.policy_ruler._condition_state import ConditionStateProducer
from sett.policy_ruler.evaluation import PolicyEvaluation
from sett.policy_ruler.evaluator import PolicyRuler

_POLICY_TRANSLATION = {
    "policy.condition_verifier_missing": "authorization.condition_verifier_missing",
    "policy.condition_verifier_failed": "authorization.condition_verifier_failed",
    "policy.condition_result_invalid": "authorization.condition_result_invalid",
    "policy.condition_unsatisfied": "authorization.condition_unsatisfied",
    "policy.condition_evidence_not_present": "authorization.condition_evidence_not_present",
    "policy.condition_evidence_invalid": "authorization.condition_evidence_invalid",
    "policy.condition_evidence_unavailable": "authorization.condition_evidence_unavailable",
    "policy.condition_inventory_invalid": "authorization.policy_result_invalid",
    "policy.evaluation_input_invalid": "authorization.policy_evaluation_invalid",
    "policy.evaluation_checkpoint_invalid": "authorization.policy_checkpoint_mismatch",
    "policy.evaluation_capacity_exceeded": (
        "authorization.policy_evaluation_capacity_exceeded"
    ),
    "policy.clock_invalid": "authorization.clock_invalid",
    "policy.evaluation_state_changed": "authorization.policy_generation_changed",
    "policy.evaluation_incomplete": "authorization.policy_evaluation_incomplete",
    "policy.transition_reentrant": "authorization.policy_handoff_reentrant",
    "policy.decision_record_unconfirmed": "authorization.decision_record_unconfirmed",
    "policy.review_restriction_active": "authorization.review_restriction_active",
    "policy.review_verifier_missing": "authorization.review_verifier_missing",
    "policy.review_verifier_failed": "authorization.review_verifier_failed",
    "policy.review_result_invalid": "authorization.review_result_invalid",
    "policy.review_state_unavailable": "authorization.review_state_unavailable",
}

_UNKNOWN_NEW_PATH = "authorization.policy_evaluation_failed"

_BINDING_FAILURES = frozenset(
    {
        "authorization.condition_unsatisfied",
        "authorization.condition_evidence_not_present",
        "authorization.condition_evidence_invalid",
        "authorization.condition_evidence_unavailable",
        "authorization.policy_binding_unavailable",
        "authorization.policy_checkpoint_mismatch",
        "authorization.policy_evaluation_capacity_exceeded",
        "authorization.policy_evaluation_expired",
        "authorization.policy_evaluation_failed",
        "authorization.policy_evaluation_incomplete",
        "authorization.policy_evaluation_invalid",
        "authorization.policy_evaluation_unrecognized",
        "authorization.policy_evaluation_used",
        "authorization.policy_evidence_changed",
        "authorization.policy_generation_changed",
        "authorization.policy_handoff_reentrant",
        "authorization.policy_invocation_mismatch",
        "authorization.policy_selection_changed",
        "authorization.review_restriction_active",
    }
)

_PRESERVED_RUNTIME_FAILURES = frozenset(
    {
        "authorization.receipt_mismatch",
        "authorization.receipt_expired",
        "authorization.request_mismatch",
    }
)

# Codes for which condition verification can stop on one specific condition.
_CONDITION_FAILURES = frozenset(
    {
        "authorization.condition_verifier_missing",
        "authorization.condition_verifier_failed",
        "authorization.condition_result_invalid",
        "authorization.condition_unsatisfied",
        "authorization.condition_evidence_not_present",
        "authorization.condition_evidence_invalid",
        "authorization.condition_evidence_unavailable",
        "authorization.clock_invalid",
    }
)


def _strict_fingerprint(
    request: AuthorizationRequest, verified_approvals: frozenset[VerifiedApproval],
) -> tuple[str, str] | None:
    """Return the StrictJSON request fingerprint for a decision record, or None.

    It is computed by SETT's own fingerprint, not the configured policy, so no
    application callback runs for it. It equals the receipt's fingerprint when
    the issuer uses the default StrictJSON policy.
    """
    policy = StrictJSONAuthorizationFingerprint()
    try:
        return policy.policy_id, policy.fingerprint(request, verified_approvals)
    except Exception:  # noqa: BLE001 -- the record then carries no fingerprint
        return None


def _attach_invocation_diagnostics(
    error: SETTAuthorizationError, invocation: _handoff.Invocation
) -> None:
    """Copy what the invocation observed onto the error, as detached values."""
    error.policy_observations = invocation.policy_observations
    error.policy_observation_reason_codes = invocation.policy_observation_reason_codes
    reported = (
        error.original_reason_code
        if error.reason_code == "authorization.evidence_failed"
        else error.reason_code
    )
    if invocation.failed_condition is not None and reported in _CONDITION_FAILURES:
        error.failed_condition = invocation.failed_condition
        error.attempted_conditions = invocation.attempted_conditions


@dataclass(frozen=True, slots=True)
class _EffectHandoff:
    """What one consumption checked, kept for the final gate before the effect."""

    receipt: AuthorizationReceipt
    request: AuthorizationRequest
    policy_generation: int
    receiver_generation: int
    consumed_at: datetime
    valid_until: datetime
    condition_entries: tuple[Any, ...] = ()
    verified_conditions: tuple[Any, ...] = ()
    checkpoints: tuple[str, ...] = ()


def _verified_conditions(record: Any) -> tuple[Any, ...]:
    """The conditions the consumed grant verified, as (key, condition, observation)."""
    proof = record.condition_proof
    observed = dict(proof.observations) if proof is not None else {}
    return tuple(
        (key, condition, observed[key])
        for key, condition, _coverage in record.condition_entries
        if key in observed
    )


class SystemAuthorizationClock:
    """Timezone-aware system clock for production wiring."""

    def now(self) -> datetime:
        """Return the current UTC authorization time."""
        return datetime.now(timezone.utc)


class AuthorizationRuler:
    """Issue store-backed receipts from one injected policy owner.

    Construction requires an explicit ``policy_ruler`` built with the very
    same clock and evidence sink objects. There is no default policy ruler,
    no default policy set and no fallback registry inside this ruler.
    Several issuers may share one policy owner, but their receiver
    identities, verifier generations, invocations and receipt bindings stay
    disjoint.
    """

    def __init__(
        self,
        *,
        issuer_id: str,
        receipt_ttl_seconds: float,
        fingerprint_policy: AuthorizationFingerprintPolicy,
        receipt_store: AuthorizationReceiptStore,
        evidence_sink: AuthorizationEvidenceSink,
        clock: AuthorizationClock | None = None,
        policy_ruler: PolicyRuler | None = None,
        selection_resolver: PolicySelectionResolver | None = None,
    ) -> None:
        """Configure explicit storage, evidence, lifetime, time and policy."""
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
        if clock is not None and not isinstance(clock, AuthorizationClock):
            raise SETTValidationError("clock must implement AuthorizationClock.")
        validate_identifier(fingerprint_policy.policy_id, field="fingerprint policy_id")
        issuer = validate_identifier(issuer_id, field="issuer_id")
        if policy_ruler is None:
            raise SETTAuthorizationConfigurationError(
                _handoff.AUTHORIZATION_FAILURE_MESSAGE,
                reason_code="authorization.policy_ruler_missing",
            )
        if type(policy_ruler) is not PolicyRuler:
            raise SETTAuthorizationConfigurationError(
                _handoff.AUTHORIZATION_FAILURE_MESSAGE,
                reason_code="authorization.policy_ruler_invalid",
            )
        selected_clock = clock if clock is not None else policy_ruler._clock
        link = policy_ruler._attach(
            self,
            clock=selected_clock,
            evidence_sink=evidence_sink,
            selection_resolver=selection_resolver,
        )
        self._issuer_id = issuer
        self._ttl = float(receipt_ttl_seconds)
        self._fingerprint_policy = fingerprint_policy
        self._receipt_store = receipt_store
        self._evidence_sink = evidence_sink
        self._clock = selected_clock
        self._policy_ruler = policy_ruler
        self._link = link
        self._verifiers: dict[str, ApprovalVerifier] = {}
        self._configuration_generation = 0
        self._lock = RLock()

    def register_policy(
        self,
        action_type: str,
        policy: AuthorizationPolicy,
        *,
        binding: PolicyBinding | None = None,
    ) -> None:
        """Delegate one registration to the injected single policy owner.

        This facade keeps no dictionary of its own: it forwards once and
        preserves the owner's result and diagnostics exactly.
        """
        self._policy_ruler.register_policy(action_type, policy, binding=binding)

    def register_approval_verifier(self, verifier: ApprovalVerifier) -> None:
        """Register one deterministic additional-authority verifier."""

        if not isinstance(verifier, ApprovalVerifier):
            raise SETTAuthorizationConfigurationError(
                "Approval verifier does not implement the verifier contract.",
                reason_code="authorization.verifier_invalid",
            )
        verifier_id = validate_identifier(verifier.verifier_id, field="verifier_id")
        # The change and its generation increment happen under the policy
        # guard, so a final transition already in progress cannot observe a
        # half-applied configuration or a stale generation.
        with self._policy_ruler._receiver_configuration_change(), self._lock:
            if verifier_id in self._verifiers:
                raise SETTAuthorizationConfigurationError(
                    "Approval verifier identifier is already registered.",
                    reason_code="authorization.verifier_duplicate",
                )
            self._verifiers[verifier_id] = verifier
            self._configuration_generation += 1

    def authorize(
        self,
        request: AuthorizationRequest,
        *,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> AuthorizationOutcome:
        """Evaluate the declared policies and issue a receipt only for GRANT.

        Each call is one authorization attempt with a fresh random
        identifier. The returned outcome, every ``SETTAuthorizationError``
        raised by the call and every evidence event it writes carry it.
        """

        if not isinstance(request, AuthorizationRequest):
            raise SETTValidationError("request must be an AuthorizationRequest.")
        attempt_id = str(uuid4())
        token = _ATTEMPT_ID.set(attempt_id)
        try:
            outcome = self._authorize_attempt(request, approval_inputs)
        except SETTAuthorizationError as error:
            # The error leaving this attempt carries this attempt's identifier.
            error.authorization_attempt_id = attempt_id
            raise
        finally:
            _ATTEMPT_ID.reset(token)
        object.__setattr__(outcome, "authorization_attempt_id", attempt_id)
        return outcome

    def _authorize_attempt(
        self,
        request: AuthorizationRequest,
        approval_inputs: Sequence[ApprovalInput],
    ) -> AuthorizationOutcome:
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
        invocation = self._translate(
            self._policy_ruler._open_invocation,
            self._link,
            request=request,
            checkpoint_id=self._policy_ruler.entry_checkpoint_id,
        )
        if self._policy_ruler._durability_producer is not None:
            invocation.request_fingerprint = _strict_fingerprint(request, verified_approvals)
        try:
            evaluation = self._translate(
                self._policy_ruler._evaluate_bound,
                invocation,
                verified_approvals=verified_approvals,
                receiver_generation=self._generation(),
                observation_invocation=invocation,
            )
            if type(evaluation) is not PolicyEvaluation:
                self._fail("authorization.policy_evaluation_invalid")
            aggregate = evaluation.decision
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
            receipt = self._issue(
                invocation, evaluation, request, verified_approvals, aggregate
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
        finally:
            self._policy_ruler._close_invocation(invocation)

    def verify_and_consume(
        self,
        receipt: AuthorizationReceipt,
        request: AuthorizationRequest,
        *,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> None:
        """Verify issuer-owned state and atomically consume effect authority.

        Each call is an authorization attempt of its own, with an identifier
        different from the one of the attempt that issued the receipt.
        """

        if not isinstance(receipt, AuthorizationReceipt):
            raise SETTValidationError("receipt must be an AuthorizationReceipt.")
        if not isinstance(request, AuthorizationRequest):
            raise SETTValidationError("request must be an AuthorizationRequest.")
        attempt_id = str(uuid4())
        token = _ATTEMPT_ID.set(attempt_id)
        try:
            self._verify_and_consume_attempt(receipt, request, approval_inputs)
        except SETTAuthorizationError as error:
            # The error leaving this attempt carries this attempt's identifier.
            error.authorization_attempt_id = attempt_id
            raise
        finally:
            _ATTEMPT_ID.reset(token)

    def _consume_for_effect(
        self,
        receipt: AuthorizationReceipt,
        request: AuthorizationRequest,
        *,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> _EffectHandoff:
        """Consume exactly as ``verify_and_consume()`` does, for the executor.

        The returned value lets the executor run the final gate after the
        effect barrier; it is not authority and cannot be presented again.
        """

        if not isinstance(receipt, AuthorizationReceipt):
            raise SETTValidationError("receipt must be an AuthorizationReceipt.")
        if not isinstance(request, AuthorizationRequest):
            raise SETTValidationError("request must be an AuthorizationRequest.")
        attempt_id = str(uuid4())
        token = _ATTEMPT_ID.set(attempt_id)
        try:
            return self._verify_and_consume_attempt(receipt, request, approval_inputs)
        except SETTAuthorizationError as error:
            error.authorization_attempt_id = attempt_id
            raise
        finally:
            _ATTEMPT_ID.reset(token)

    def _confirm_effect_handoff(self, handoff: _EffectHandoff) -> None:
        """Check the configured clock and retained authority before the effect.

        Both generations must be the ones the consumption checked, the
        configured clock must not have gone back, and neither the
        evaluation deadline nor the receipt's expiry may have been reached.
        A failure is reported like a rejected receipt; the receipt stays
        consumed.
        """

        failure: str | None = None
        # Sample the configured clock before reading generations: its callback
        # can itself register a policy. Generation loss has diagnostic precedence.
        now = self._observe()
        if (
            handoff.policy_generation != self._policy_ruler._generation
            or handoff.receiver_generation != self._generation()
        ):
            failure = "authorization.policy_generation_changed"
        elif now is None or now < handoff.consumed_at:
            failure = "authorization.clock_invalid"
        elif now >= handoff.valid_until:
            failure = "authorization.policy_evaluation_expired"
        elif now >= handoff.receipt.expires_at:
            failure = "authorization.receipt_expired"
        if failure is not None:
            self._reject(failure, handoff.request, handoff.receipt)

    def _check_controlled_checkpoint(
        self, handoff: _EffectHandoff, checkpoint_id: str, verified: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        """Pass one controlled checkpoint of a consumed grant; no receipt is involved.

        Both generations must be unchanged. The conditions declared for this
        checkpoint are verified, then every earlier ``RECHECK`` condition
        again, and an earlier ``ATTESTATION`` must still be within its
        limit. Policies, selection, approvals, review and durability are not
        run again. Returns the conditions verified so far.
        """

        if (
            handoff.policy_generation != self._policy_ruler._generation
            or handoff.receiver_generation != self._generation()
        ):
            raise self._failure("authorization.policy_generation_changed")
        pending = tuple(
            (key, condition)
            for key, condition, _coverage in handoff.condition_entries
            if condition.checkpoint_id == checkpoint_id
        )
        if not pending and not verified:
            return verified
        producer = self._policy_ruler._condition_producer
        if type(producer) is not ConditionStateProducer:
            raise self._failure("authorization.policy_evaluation_incomplete")
        result = producer.verify_at_checkpoint(handoff.request, pending, verified)
        if type(result) is _handoff.ConditionRejection:
            error = self._failure(_POLICY_TRANSLATION.get(result.code, _UNKNOWN_NEW_PATH))
            error.failed_condition = result.failed_condition
            error.attempted_conditions = result.attempted_conditions
            raise error
        return result

    def _verify_and_consume_attempt(
        self,
        receipt: AuthorizationReceipt,
        request: AuthorizationRequest,
        approval_inputs: Sequence[ApprovalInput],
    ) -> _EffectHandoff:
        verified_approvals = self._verify_approval_inputs(request, approval_inputs)
        # This reading scopes the verified-state producers only. The
        # authority check re-reads the generation inside the guard.
        producer_generation = self._generation()
        selection_bytes = self._translate(
            self._current_selection_bytes, request
        )
        state, confirmation = self._translate(
            self._policy_ruler._current_state_for_consumption,
            self,
            receipt.receipt_id,
            request=request,
            verified_approvals=verified_approvals,
            receiver_generation=producer_generation,
        )
        if confirmation is not None:
            self._reject(confirmation, request, receipt)
        request_bytes = _handoff.canonical_request_bytes(request, verified_approvals)
        fingerprint = self._fingerprint(request, verified_approvals)
        failure: str | None = None
        store_error: BaseException | None = None
        handoff: _EffectHandoff | None = None
        phase = self._translate(self._policy_ruler._begin_critical)
        try:
            now = self._observe()
            if now is None:
                failure = "authorization.clock_invalid"
            else:
                record, failure = self._policy_ruler._validate_receipt_binding(
                    self,
                    receipt,
                    request_bytes=request_bytes,
                    current_selection_bytes=selection_bytes,
                    current_state=state,
                    # Read the live generation here, not the value captured
                    # before the application callbacks ran.
                    receiver_generation=self._generation(),
                    now=now,
                )
                if failure is None and record is not None:
                    try:
                        self._receipt_store.verify_and_consume(
                            receipt,
                            request_fingerprint=fingerprint,
                            now=now,
                        )
                    except SETTAuthorizationError as error:
                        store_error = error
                    except Exception as error:  # noqa: BLE001 -- backend boundary
                        store_error = error
                        failure = "authorization.receipt_store_failed"
                    after = self._observe()
                    if phase.poisoned:
                        # A forbidden reentry poisons the outer transition
                        # even when the backend caught the rejection.
                        failure = "authorization.policy_handoff_reentrant"
                        store_error = None
                    elif store_error is None:
                        if after is None or after < now:
                            # A backward reading is as unusable as an invalid
                            # one. The pre-backend observation is the
                            # reference, and it is never earlier than the
                            # retained evaluation time.
                            failure = "authorization.clock_invalid"
                        elif after >= record.valid_until:
                            failure = "authorization.policy_evaluation_expired"
                        elif after >= receipt.expires_at:
                            failure = "authorization.receipt_expired"
                    if failure is not None or store_error is not None:
                        # A consumption the store already performed stays
                        # performed. It is not undone and not retried.
                        self._policy_ruler._terminate(record)
                    elif receipt.receipt_use is ReceiptUse.SINGLE_USE:
                        self._policy_ruler._terminate(record)
                    if failure is None and store_error is None and after is not None:
                        handoff = _EffectHandoff(
                            receipt=receipt, request=request,
                            policy_generation=record.policy_generation,
                            receiver_generation=record.receiver_generation,
                            consumed_at=after, valid_until=record.valid_until,
                            condition_entries=record.condition_entries,
                            verified_conditions=_verified_conditions(record),
                            checkpoints=self._policy_ruler._controlled_checkpoints_for(
                                request.action_type
                            ),
                        )
        finally:
            self._policy_ruler._end_critical(phase)
        if store_error is not None and failure is None:
            self._report_failure(
                "authorization.receipt_rejected",
                getattr(store_error, "reason_code", "unknown"),
                {
                    "action_id": request.action_id,
                    "receipt_id": receipt.receipt_id,
                },
            )
            raise store_error
        if failure is not None:
            self._reject(failure, request, receipt)
        if handoff is None:
            self._reject(_UNKNOWN_NEW_PATH, request, receipt)
        self._emit(
            "authorization.receipt_verified",
            attributes={
                "action_id": request.action_id,
                "receipt_id": receipt.receipt_id,
            },
        )
        return handoff

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

    # ── Internal coordination ────────────────────────────────────────

    def _generation(self) -> int:
        with self._lock:
            return self._configuration_generation

    def _current_selection_bytes(self, request: AuthorizationRequest) -> bytes | None:
        _selection, encoded = self._policy_ruler._current_selection(request)
        return encoded

    def _translate(
        self, operation: Any, *args: Any,
        observation_invocation: _handoff.Invocation | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run one policy-owner operation and translate its diagnostics."""
        try:
            return operation(*args, **kwargs)
        except SETTPolicyConfigurationError as error:
            code = _POLICY_TRANSLATION.get(error.reason_code, _UNKNOWN_NEW_PATH)
        except SETTAuthorizationError as error:
            if observation_invocation is not None:
                _attach_invocation_diagnostics(error, observation_invocation)
            raise
        except SETTValidationError:
            raise
        except Exception:  # noqa: BLE001 -- new private boundary only
            code = _UNKNOWN_NEW_PATH
        # Raise outside the handler so no private exception or context is retained.
        if observation_invocation is None:
            self._fail(code)
        try:
            self._fail(code)
        except SETTAuthorizationError as error:
            _attach_invocation_diagnostics(error, observation_invocation)
            raise

    def _issue(
        self,
        invocation: Any,
        evaluation: Any,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
        aggregate: Any,
    ) -> AuthorizationReceipt:
        # This reading scopes the verified-state producers only. The
        # authority check re-reads the generation inside the guard.
        producer_generation = self._generation()
        request_bytes = _handoff.canonical_request_bytes(request, verified_approvals)
        selection_bytes = self._translate(self._current_selection_bytes, request)
        state = self._translate(
            self._policy_ruler._current_state,
            invocation,
            verified_approvals=verified_approvals,
            receiver_generation=producer_generation,
        )
        state_confirmed = self._policy_ruler._confirm_invocation_state(invocation)
        fingerprint = self._fingerprint(request, verified_approvals)
        failure: str | None = None
        store_error: BaseException | None = None
        receipt: AuthorizationReceipt | None = None
        phase = self._translate(self._policy_ruler._begin_critical)
        try:
            now = self._observe()
            if now is None:
                failure = "authorization.clock_invalid"
            else:
                record, failure = self._policy_ruler._validate_and_claim(
                    invocation,
                    evaluation,
                    receiver=self,
                    request_bytes=request_bytes,
                    current_selection_bytes=selection_bytes,
                    current_state=state,
                    state_confirmed=state_confirmed,
                    # Read the live generation here, not the value captured
                    # before the application callbacks ran.
                    receiver_generation=self._generation(),
                    now=now,
                )
                if failure is None and record is not None:
                    receipt = self._build_receipt(request, aggregate, fingerprint, now)
                    try:
                        self._receipt_store.issue(receipt)
                    except SETTAuthorizationError as error:
                        store_error = error
                    except Exception as error:  # noqa: BLE001 -- backend boundary
                        store_error = error
                        failure = "authorization.receipt_store_failed"
                    after = self._observe()
                    if phase.poisoned:
                        # A forbidden reentry poisons the outer transition
                        # even when the backend caught the rejection.
                        failure = "authorization.policy_handoff_reentrant"
                        store_error = None
                    elif store_error is None:
                        if after is None or after < now:
                            # A backward reading is as unusable as an invalid
                            # one. The pre-backend observation is the
                            # reference, and it is never earlier than the
                            # retained evaluation time.
                            failure = "authorization.clock_invalid"
                        elif after >= record.valid_until:
                            failure = "authorization.policy_evaluation_expired"
                        elif after >= receipt.expires_at:
                            failure = "authorization.receipt_expired"
                        else:
                            self._policy_ruler._bind_receipt(record, receipt)
                    if failure is not None or store_error is not None:
                        # Whatever the backend already wrote stays written.
                        # It is not undone, reinterpreted or retried.
                        receipt = None
                        self._policy_ruler._terminate(record)
        finally:
            self._policy_ruler._end_critical(phase)
        if store_error is not None and failure is None:
            raise store_error
        if failure is not None or receipt is None:
            self._fail(failure or _UNKNOWN_NEW_PATH)
        return receipt

    def _build_receipt(
        self,
        request: AuthorizationRequest,
        aggregate: Any,
        fingerprint: str,
        now: datetime,
    ) -> AuthorizationReceipt:
        return AuthorizationReceipt(
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
                    aggregate.receipt_use is ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
                    and request.execution_mode is ExecutionMode.CONTROLLED
                    and request.idempotency_key is not None
                )
                else ReceiptUse.SINGLE_USE
            ),
        )

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
            result: object = None
            failed = False
            try:
                result = verifier.verify(approval_input, request)
            except Exception:  # noqa: BLE001 -- application verifier boundary
                failed = True
            # Raise outside the handler so no verifier exception is retained.
            if failed:
                self._fail(
                    "authorization.verifier_failed",
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

    def _observe(self) -> datetime | None:
        """Read the configured clock inside a guarded section, or report None."""
        observed: object = None
        failed = False
        try:
            observed = self._clock.now()
        except Exception:  # noqa: BLE001 -- caller clock, fixed diagnostic
            failed = True
        if failed:
            return None
        return _handoff.aware_utc(observed)

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
                    attributes=_with_attempt_id(attributes),
                )
            )
        except SETTAuthorizationConfigurationError:
            raise
        except Exception as error:
            raise SETTAuthorizationConfigurationError(
                "Authorization evidence could not be recorded.",
                reason_code="authorization.evidence_failed",
            ) from error

    def _report_failure(
        self, kind: str, reason_code: str, attributes: dict[str, Any]
    ) -> None:
        """Write the event that reports one failure before it is raised.

        If that write fails, ``authorization.evidence_failed`` stays the
        primary code and the error keeps the code it was reporting.
        """
        try:
            self._emit(kind, reason_codes=(reason_code,), attributes=attributes)
        except SETTAuthorizationConfigurationError as error:
            if error.reason_code == "authorization.evidence_failed":
                error.original_reason_code = reason_code
            raise

    def _fail(self, reason_code: str, **attributes: Any) -> NoReturn:
        self._report_failure("authorization.failed", reason_code, attributes)
        raise self._failure(reason_code)

    def _reject(
        self,
        reason_code: str,
        request: AuthorizationRequest,
        receipt: AuthorizationReceipt,
    ) -> NoReturn:
        self._report_failure(
            "authorization.receipt_rejected",
            reason_code,
            {
                "action_id": request.action_id,
                "receipt_id": receipt.receipt_id,
            },
        )
        raise self._failure(reason_code)

    def _failure(self, reason_code: str) -> SETTAuthorizationError:
        if reason_code in _BINDING_FAILURES:
            return SETTAuthorizationError(
                _handoff.AUTHORIZATION_FAILURE_MESSAGE, reason_code=reason_code
            )
        if reason_code in _PRESERVED_RUNTIME_FAILURES:
            return SETTAuthorizationError(
                "Authorization could not produce a trustworthy result.",
                reason_code=reason_code,
            )
        return SETTAuthorizationConfigurationError(
            "Authorization could not produce a trustworthy result.",
            reason_code=reason_code,
        )
