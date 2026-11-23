"""The single owner of policy registration, selection, evaluation and state.

PolicyRuler owns the authoritative policy registry, the declared selection,
the one evaluation engine and the one local serialization guard.
AuthorizationRuler keeps final authority and receipts and delegates to this
owner; there is no second registry, no second engine and no legacy route.

Public evaluation is inspection only. Its result cannot be promoted into
authorization: ``AuthorizationRuler.authorize()`` has no parameter for one,
and the private handoff recognizes only the exact object this ruler
registered for that exact invocation, together with the complete detached
request, selection, generations, checkpoint and verified state.

A governed attempt completes only when every verified-state slot has a
producer. The configured condition producer does not supply consent, review
or durability state or delivery; a condition of a declared controlled
checkpoint is verified when a controlled handler reaches it. An implementation can
declare that it owns consent, a governance journal supplies durability, and
review verifiers with that journal supply review. Without them the state is
incomplete. A bound evaluation that cannot obtain a complete record blocks;
a missing producer never becomes an empty requirement set. See
``docs/policy_handoff.md``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from itertools import islice
from typing import Any, NoReturn
from uuid import uuid4

from sett._governance.evidence import (
    _ATTEMPT_ID,
    AuthorizationEvent,
    AuthorizationEvidenceSink,
    _with_attempt_id,
)
from sett._governance.protocols import AuthorizationClock
from sett._governance.validation import canonical_json_bytes, validate_identifier
from sett._governance.values import (
    ApprovalRequirement,
    AuthorizationConstraint,
    AuthorizationDecision,
    AuthorizationRequest,
    FieldTrust,
    FieldValue,
    ReceiptUse,
    VerifiedApproval,
)
from sett._governance.verdict import AuthorizationVerdict
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    SETTValidationError,
)
from sett.policy_ruler import _handoff
from sett.policy_ruler._condition_state import ConditionStateProducer
from sett.policy_ruler._handoff import (
    BindingRecord,
    Invocation,
    PolicyGuard,
    ReceiverLink,
    RulerIncarnation,
    StateContext,
    VerifiedStateSlot,
)
from sett.policy_ruler._review_state import (
    REVIEW_CODES,
    ReviewStateProducer,
    review_rows,
)
from sett.policy_ruler.conditions import PolicyCondition
from sett.policy_ruler.contracts import (
    AuthorizationPolicy,
    ConditionVerifier,
    PolicySelectionResolver,
    ReviewVerifier,
)
from sett.policy_ruler.decision_record import (
    UNCONFIRMED as _DECISION_RECORD_UNCONFIRMED,
)
from sett.policy_ruler.decision_record import DecisionRecordProducer
from sett.policy_ruler.evaluation import PolicyEvaluation, _create_evaluation
from sett.policy_ruler.models import (
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyDecision,
    PolicySelection,
)
from sett.policy_ruler.persistence import GovernanceJournal
from sett.policy_ruler.review_follow_up import ReviewFollowUp, follow_up

_GENERIC_FAILURE = "Authorization could not produce a trustworthy result."

_CONFIGURATION_FAILURES = frozenset(
    {
        "authorization.approval_requirement_conflict",
        "authorization.constraint_duplicate",
        "authorization.policy_failed",
        "authorization.policy_identity_mismatch",
        "authorization.policy_missing",
        "authorization.policy_result_invalid",
    }
)


class _Registration:
    """One registered policy callback with its detached binding snapshot."""

    __slots__ = ("policy", "binding", "canonical")

    def __init__(self, policy: AuthorizationPolicy, binding: PolicyBinding) -> None:
        """Retain the callback and an immutable copy of its declared binding."""
        self.policy = policy
        self.binding = binding
        self.canonical = binding.canonical_view()


class PolicyRuler:
    """The single policy owner for one explicitly configured deployment.

    Construction is entirely explicit: there is no default policy set, no
    shared singleton, no default evaluation lifetime, no background
    cleanup service and no automatic eviction of still-valid authority.
    The declared ``ruler_id`` is a label for diagnostics; instance
    identity is the private incarnation this constructor creates.

    Several AuthorizationRuler instances may share one PolicyRuler. They
    then share its registry and its generation, so a registry change
    invalidates every pending binding of that ruler, including bindings
    belonging to the other issuers. That is stale-binding rejection, not
    a policy denial, and it is a cost of sharing an instance.
    """

    def __init__(
        self,
        *,
        ruler_id: str,
        entry_checkpoint_id: str,
        evaluation_ttl_seconds: float,
        max_live_evaluations: int,
        clock: AuthorizationClock,
        evidence_sink: AuthorizationEvidenceSink,
        selection_resolver: PolicySelectionResolver | None = None,
        condition_verifiers: Sequence[ConditionVerifier] | None = None,
        implementation_owned_consent: bool = False,
        governance_journal: GovernanceJournal | None = None,
        review_verifiers: Sequence[ReviewVerifier] | None = None,
        controlled_checkpoints: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """Configure one owner; None installs no condition producer and blocks.

        An explicit verifier sequence freezes its identities at construction.
        Empty supports only a complete empty governing inventory. There is no
        verifier mutator, and configuring verifiers alone does not let a
        governed attempt complete.

        ``implementation_owned_consent=True`` declares that the
        implementation built on SETT owns consent. An internal producer then
        records that declaration in the consent slot; SETT verifies no
        consent. The default ``False`` installs nothing, and the consent slot
        stays without a producer. The choice is fixed for the ruler's life.

        ``governance_journal`` installs the durability producer: every
        completed bound evaluation writes one decision record to the journal,
        and the durability slot exists only once the journal confirms that
        record at its required durability. ``None`` installs nothing.

        ``review_verifiers`` installs the review producer, which requires
        ``governance_journal``: before an effect, every restriction that an
        earlier decision declared on this action type must be lifted by a
        favorable completed review its configured verifier reports. ``None``
        installs nothing.

        ``controlled_checkpoints`` maps an action type to its ordered later
        checkpoint IDs. A condition may name one of them instead of the
        entry checkpoint; it is then verified when a controlled handler
        reaches that checkpoint, not at entry. ``None`` declares none.
        """
        self._ruler_id = _configured_identifier(ruler_id, "ruler_id")
        self._entry_checkpoint_id = _configured_identifier(
            entry_checkpoint_id, "entry_checkpoint_id"
        )
        if (
            isinstance(evaluation_ttl_seconds, bool)
            or not isinstance(evaluation_ttl_seconds, int | float)
            or not math.isfinite(evaluation_ttl_seconds)
            or evaluation_ttl_seconds <= 0
        ):
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        self._ttl = _configured_lifetime(evaluation_ttl_seconds)
        if (
            isinstance(max_live_evaluations, bool)
            or type(max_live_evaluations) is not int
            or not 1 <= max_live_evaluations <= 256
        ):
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        if not isinstance(clock, AuthorizationClock):
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        if not isinstance(evidence_sink, AuthorizationEvidenceSink):
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        if selection_resolver is None:
            raise _handoff.authorization_error(
                "authorization.policy_selection_resolver_missing"
            )
        if not isinstance(selection_resolver, PolicySelectionResolver):
            raise _handoff.authorization_configuration_error(
                "authorization.policy_selection_resolver_invalid"
            )
        if not _valid_resolver_id(selection_resolver):
            raise _handoff.authorization_configuration_error(
                "authorization.policy_selection_resolver_invalid"
            )
        if type(implementation_owned_consent) is not bool:
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        if governance_journal is not None and type(governance_journal) is not GovernanceJournal:
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        if review_verifiers is not None and governance_journal is None:
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        self._max_live = max_live_evaluations
        self._clock = clock
        self._evidence_sink = evidence_sink
        self._selection_resolver = selection_resolver
        self._incarnation = RulerIncarnation()
        self._generation = 0
        self._guard = PolicyGuard()
        self._registry: dict[str, dict[str, _Registration]] = {}
        self._state_producers: dict[str, Any] = {}
        self._live: dict[Invocation, BindingRecord | None] = {}
        self._condition_producer = (
            ConditionStateProducer(condition_verifiers, self._now)
            if condition_verifiers is not None else None
        )
        if self._condition_producer is not None:
            self._state_producers["conditions"] = self._condition_producer
        self._implementation_owned_consent = implementation_owned_consent
        if implementation_owned_consent:
            self._state_producers["consent"] = _handoff.ImplementationOwnedConsentProducer()
        self._durability_producer = (
            DecisionRecordProducer(governance_journal, self._now)
            if governance_journal is not None else None
        )
        if self._durability_producer is not None:
            self._state_producers["durability"] = self._durability_producer
        self._review_producer = (
            ReviewStateProducer(review_verifiers, governance_journal, self._registered_binding)
            if review_verifiers is not None else None
        )
        if self._review_producer is not None:
            self._state_producers["review"] = self._review_producer
        self._controlled_checkpoints = _admit_controlled_checkpoints(
            controlled_checkpoints, self._entry_checkpoint_id
        )

    # ── Public surface ───────────────────────────────────────────────

    @property
    def ruler_id(self) -> str:
        """Return the declared label; it is not proof of instance identity."""
        return self._ruler_id

    @property
    def implementation_owned_consent(self) -> bool:
        """Report whether this ruler was built declaring implementation-owned consent."""
        return self._implementation_owned_consent

    @property
    def entry_checkpoint_id(self) -> str:
        """Return the named authorization-entry checkpoint of this ruler."""
        return self._entry_checkpoint_id

    def follow_up_reviews(self) -> ReviewFollowUp:
        """Ask each declared review obligation's verifier once; record new facts.

        The application calls it when it chooses: SETT runs no scheduler and
        decides no deadline. Each newly verified fact is written to the
        governance journal and confirmed before it is reported. A recorded
        fact grants nothing: a restriction is still verified again before
        every effect. Requires a governance journal and review verifiers.
        """
        reviews = self._review_producer
        durability = self._durability_producer
        if reviews is None or durability is None:
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        return follow_up(
            reviews._verifiers, self._registered_binding, durability._journal,
            durability._lock, self._now,
        )

    def _controlled_checkpoints_for(self, action_type: str) -> tuple[str, ...]:
        """Return the declared later checkpoints of one action type, in order."""
        return self._controlled_checkpoints.get(action_type, ())

    def register_policy(
        self,
        action_type: str,
        policy: AuthorizationPolicy,
        *,
        binding: PolicyBinding | None = None,
    ) -> None:
        """Register one policy and its declared binding for one action type.

        The keyword default exists only to diagnose the old two-argument
        call: it never registers a policy without metadata. A successful
        registration advances this instance's generation exactly once; a
        failed registration changes nothing. There is no removal,
        replacement or dynamic resolver setter in this contract.
        """
        validate_identifier(action_type, field="action_type")
        if binding is None:
            raise SETTAuthorizationConfigurationError(
                "Policy registration requires its declared binding.",
                reason_code="authorization.policy_binding_required",
            )
        if type(binding) is not PolicyBinding:
            raise SETTAuthorizationConfigurationError(
                "binding must be an exact PolicyBinding.",
                reason_code="authorization.policy_binding_invalid",
            )
        if not isinstance(policy, AuthorizationPolicy):
            raise SETTAuthorizationConfigurationError(
                "Policy does not implement the authorization contract.",
                reason_code="authorization.policy_invalid",
            )
        policy_id = validate_identifier(policy.policy_id, field="policy_id")
        if policy_id != binding.policy_id:
            raise SETTAuthorizationConfigurationError(
                "The policy identifier does not match its declared binding.",
                reason_code="authorization.policy_binding_mismatch",
            )
        self._guard.acquire()
        try:
            self._guard.reject_reentrant_mutation()
            policies = self._registry.get(action_type, {})
            if policy_id in policies:
                raise SETTAuthorizationConfigurationError(
                    "Policy identifier is already registered for this action type.",
                    reason_code="authorization.policy_duplicate",
                )
            self._registry.setdefault(action_type, {})[policy_id] = _Registration(
                policy, binding
            )
            self._generation += 1
        finally:
            self._guard.release()

    def evaluate(
        self,
        request: AuthorizationRequest,
        *,
        verified_approvals: frozenset[VerifiedApproval],
        checkpoint_id: str,
    ) -> PolicyEvaluation:
        """Evaluate one request for inspection, granting nothing.

        The supplied approval values are declared inputs; this method
        verifies no authority and mints no receipt. The observation it
        returns is not registered in the private ledger, so no receiver
        can recognize it, however closely its fields match a bound
        result. ``verification_ref`` is always ``None`` here.
        """
        self._admit_evaluation_input(request, verified_approvals, checkpoint_id)
        self._guard.acquire()
        try:
            snapshot = self._snapshot()
        finally:
            self._guard.release()
        outcome = self._run_engine(request, verified_approvals, snapshot)
        evaluated_at = self._now()
        self._guard.acquire()
        try:
            if self._generation != snapshot[0]:
                raise _handoff.policy_error("policy.evaluation_state_changed")
        finally:
            self._guard.release()
        return _create_evaluation(
            ruler_id=self._ruler_id,
            action_id=request.action_id,
            action_type=request.action_type,
            trace_id=request.trace_id,
            selection=outcome["selection"],
            checkpoint_id=checkpoint_id,
            generation=snapshot[0],
            evaluated_at=evaluated_at,
            valid_until=self._deadline(evaluated_at),
            decision=outcome["aggregate"],
            policy_decisions=outcome["decisions"],
            informative_decisions=outcome["informative_decisions"],
            informative_diagnostics=outcome["informative_diagnostics"],
            verification_ref=None,
        )

    # ── Configuration and evidence helpers ───────────────────────────

    def _snapshot(self) -> tuple[int, dict[str, dict[str, _Registration]]]:
        return (
            self._generation,
            {key: dict(value) for key, value in self._registry.items()},
        )

    def _admit_evaluation_input(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
        checkpoint_id: str,
    ) -> None:
        if type(request) is not AuthorizationRequest:
            raise _handoff.policy_error("policy.evaluation_input_invalid")
        if (
            type(request.principal) is not FieldValue
            or (request.resource is not None and type(request.resource) is not FieldValue)
            or (request.purpose is not None and type(request.purpose) is not FieldValue)
            or any(type(item) is not FieldValue for item in request.scope)
            or any(type(item) is not FieldValue for item in request.data_labels)
            or any(type(item) is not FieldValue for _key, item in request.context_facts)
        ):
            raise _handoff.policy_error("policy.evaluation_input_invalid")
        if type(verified_approvals) is not frozenset:
            raise _handoff.policy_error("policy.evaluation_input_invalid")
        if any(type(item) is not VerifiedApproval for item in verified_approvals):
            raise _handoff.policy_error("policy.evaluation_input_invalid")
        if not isinstance(checkpoint_id, str):
            raise _handoff.policy_error("policy.evaluation_checkpoint_invalid")
        if not _valid_identifier(checkpoint_id):
            raise _handoff.policy_error("policy.evaluation_checkpoint_invalid")
        if checkpoint_id != self._entry_checkpoint_id:
            raise _handoff.policy_error("policy.evaluation_checkpoint_invalid")

    def _now(self) -> datetime:
        observed: object = None
        failed = False
        try:
            observed = self._clock.now()
        except Exception:  # noqa: BLE001 -- caller clock, fixed diagnostic
            failed = True
        # Decide outside the handler so no caller exception is retained.
        aware = None if failed else _handoff.aware_utc(observed)
        if aware is None:
            raise _handoff.policy_error("policy.clock_invalid")
        return aware

    def _deadline(self, evaluated_at: datetime) -> datetime:
        deadline = None
        try:
            deadline = evaluated_at + self._ttl
        except (OverflowError, ValueError):
            deadline = None
        if deadline is None:
            raise _handoff.policy_error("policy.clock_invalid")
        return deadline

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
                    kind, reason_codes=reason_codes, attributes=_with_attempt_id(attributes)
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
        try:
            self._emit(
                "authorization.failed",
                reason_codes=(reason_code,),
                attributes=attributes,
            )
        except SETTAuthorizationConfigurationError as error:
            # The failure report itself could not be written: keep the code
            # it was reporting, which is otherwise lost.
            if error.reason_code == "authorization.evidence_failed":
                error.original_reason_code = reason_code
            raise
        if reason_code in _CONFIGURATION_FAILURES:
            raise SETTAuthorizationConfigurationError(
                _GENERIC_FAILURE, reason_code=reason_code
            )
        raise SETTAuthorizationError(_GENERIC_FAILURE, reason_code=reason_code)

    # ── Selection and evaluation engine ──────────────────────────────

    def _resolve_selection(self, request: AuthorizationRequest) -> PolicySelection:
        selection: object = None
        failed = False
        try:
            selection = self._selection_resolver.resolve(request)
        except Exception:  # noqa: BLE001 -- application resolver boundary
            failed = True
        # Raise outside the handler so no resolver exception is retained.
        if failed:
            self._fail("authorization.policy_selection_failed")
        if type(selection) is not PolicySelection:
            self._fail("authorization.policy_selection_result_invalid")
        if (
            selection.action_id != request.action_id
            or selection.action_type != request.action_type
        ):
            self._fail("authorization.policy_selection_scope_mismatch")
        if self._now() < selection.effective_at:
            self._fail("authorization.policy_selection_not_effective")
        return selection

    def _facts_are_verified(
        self, selection: PolicySelection, names: tuple[str, ...]
    ) -> bool:
        for name in names:
            fact = selection.facts.get(name)
            if fact is None or fact.trust is not FieldTrust.AUTHORITATIVE:
                return False
        return True

    def _plan(
        self,
        selection: PolicySelection,
        registry: dict[str, dict[str, _Registration]],
        request: AuthorizationRequest,
    ) -> dict[str, Any]:
        entries = {
            (item.policy_id, item.policy_version): item
            for item in selection.applicability
        }
        installed = registry.get(request.action_type, {})
        applies: list[PolicyBinding] = []
        for binding in selection.required:
            entry = entries[(binding.policy_id, binding.policy_version)]
            if entry.state is PolicyApplicabilityState.UNDETERMINED:
                self._fail(
                    "authorization.policy_applicability_undetermined",
                    policy_id=binding.policy_id,
                )
            if not self._facts_are_verified(selection, tuple(entry.fact_names)):
                self._fail(
                    "authorization.policy_selection_facts_unverified",
                    policy_id=binding.policy_id,
                )
            if entry.state is PolicyApplicabilityState.APPLIES:
                applies.append(binding)
        if not applies:
            self._fail("authorization.policy_required_empty")
        required: list[tuple[PolicyBinding, _Registration]] = []
        for binding in applies:
            registration = installed.get(binding.policy_id)
            if registration is None:
                self._fail(
                    "authorization.policy_missing", policy_id=binding.policy_id
                )
            if registration.binding.policy_version != binding.policy_version:
                self._fail(
                    "authorization.policy_version_mismatch",
                    policy_id=binding.policy_id,
                )
            if registration.canonical != binding.canonical_view():
                self._fail(
                    "authorization.policy_binding_mismatch",
                    policy_id=binding.policy_id,
                )
            required.append((binding, registration))
        return {
            "required": tuple(required),
            "informative": tuple(selection.informative),
            "entries": entries,
            "installed": installed,
        }

    def _evaluate_required(
        self,
        plan: dict[str, Any],
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
        *,
        invocation: Invocation | None = None,
    ) -> tuple[PolicyDecision, ...]:
        decisions: list[PolicyDecision] = []
        for _binding, registration in plan["required"]:
            policy = registration.policy
            decision: object = None
            failed = False
            try:
                decision = policy.evaluate(request, verified_approvals)
            except Exception:  # noqa: BLE001 -- application policy boundary
                failed = True
            # Raise outside the handler so no callback exception is retained.
            if failed:
                self._fail("authorization.policy_failed", policy_id=policy.policy_id)
            if (
                type(decision) is not PolicyDecision
                or any(type(item) is not ApprovalRequirement for item in decision.requirements)
                or any(type(item) is not AuthorizationConstraint for item in decision.constraints)
            ):
                self._fail(
                    "authorization.policy_result_invalid",
                    policy_id=policy.policy_id,
                )
            if decision.policy_id != policy.policy_id:
                self._fail(
                    "authorization.policy_identity_mismatch",
                    policy_id=policy.policy_id,
                )
            if not self._condition_declarations_match(decision, registration.binding, request):
                self._fail("authorization.policy_result_invalid", policy_id=policy.policy_id)
            decisions.append(decision)
            if invocation is not None:
                # Copy admitted provenance, not mutable callback metadata.
                # Base str copies retain characters without subclass state.
                invocation.policy_observations += ((
                    str.__str__(registration.canonical["policy_id"]),
                    str.__str__(registration.canonical["policy_version"]),
                    decision.verdict.value,
                ),)
                invocation.policy_observation_reason_codes += (
                    tuple(str.__str__(code) for code in decision.reason_codes),
                )
                invocation.policy_observation_provenance += (_provenance(registration.canonical),)
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
        return tuple(decisions)

    def _evaluate_informative(
        self,
        plan: dict[str, Any],
        selection: PolicySelection,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> tuple[tuple[PolicyDecision, ...], tuple[tuple[str, str], ...]]:
        results: list[PolicyDecision] = []
        diagnostics: list[tuple[str, str]] = []
        entries = plan["entries"]
        installed = plan["installed"]
        for binding in plan["informative"]:
            policy_id = binding.policy_id
            entry = entries[(policy_id, binding.policy_version)]
            if entry.state is PolicyApplicabilityState.UNDETERMINED:
                diagnostics.append(
                    (policy_id, "authorization.policy_applicability_undetermined")
                )
                continue
            if not self._facts_are_verified(selection, tuple(entry.fact_names)):
                diagnostics.append(
                    (policy_id, "authorization.policy_selection_facts_unverified")
                )
                continue
            if entry.state is not PolicyApplicabilityState.APPLIES:
                continue
            registration = installed.get(policy_id)
            if registration is None:
                diagnostics.append((policy_id, "authorization.policy_missing"))
                continue
            if registration.binding.policy_version != binding.policy_version:
                diagnostics.append(
                    (policy_id, "authorization.policy_version_mismatch")
                )
                continue
            if registration.canonical != binding.canonical_view():
                diagnostics.append(
                    (policy_id, "authorization.policy_binding_mismatch")
                )
                continue
            try:
                decision = registration.policy.evaluate(request, verified_approvals)
            except Exception:  # noqa: BLE001 -- informative boundary, no authority
                diagnostics.append((policy_id, "authorization.policy_failed"))
                continue
            if type(decision) is not PolicyDecision:
                diagnostics.append(
                    (policy_id, "authorization.policy_result_invalid")
                )
                continue
            if decision.policy_id != policy_id:
                diagnostics.append(
                    (policy_id, "authorization.policy_identity_mismatch")
                )
                continue
            if not self._condition_declarations_match(decision, binding, request):
                diagnostics.append((policy_id, "authorization.policy_result_invalid"))
                continue
            results.append(decision)
        return tuple(results), tuple(diagnostics)

    def _run_engine(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
        snapshot: tuple[int, dict[str, dict[str, _Registration]]],
        *,
        invocation: Invocation | None = None,
    ) -> dict[str, Any]:
        selection = self._resolve_selection(request)
        plan = self._plan(selection, snapshot[1], request)
        decisions = self._evaluate_required(
            plan, request, verified_approvals, invocation=invocation
        )
        informative, diagnostics = self._evaluate_informative(
            plan, selection, request, verified_approvals
        )
        aggregate = self._compose(decisions)
        inventory = self._condition_inventory(plan, selection, decisions, informative, diagnostics)
        return {
            "condition_data": inventory,
            "selection": selection,
            "decisions": decisions,
            "aggregate": aggregate,
            "informative_decisions": informative,
            "informative_diagnostics": diagnostics + inventory[4],
        }

    def _condition_declarations_match(
        self, decision: PolicyDecision, binding: PolicyBinding, request: AuthorizationRequest,
    ) -> bool:
        for condition in decision.conditions:
            if type(condition) is not PolicyCondition:
                return False
            if (condition.binding.canonical_view() != binding.canonical_view()
                    or condition.action_id != request.action_id
                    or condition.action_type != request.action_type
                    or (condition.checkpoint_id != self._entry_checkpoint_id
                        and condition.checkpoint_id
                        not in self._controlled_checkpoints_for(request.action_type))):
                return False
        return True

    def _registered_binding(
        self, policy_id: str, policy_version: str, action_type: str, canonical: Any,
    ) -> PolicyBinding | None:
        """Return the binding still registered for a recorded obligation, or None."""
        self._guard.acquire()
        try:
            registration = self._registry.get(action_type, {}).get(policy_id)
        finally:
            self._guard.release()
        if registration is None or registration.binding.policy_version != policy_version:
            return None
        try:
            same = canonical_json_bytes(
                registration.binding.canonical_view(), field="registered binding"
            ) == canonical_json_bytes(canonical, field="recorded binding")
        except SETTValidationError:
            return None
        return registration.binding if same else None

    def _decision_facts(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
        invocation: Invocation,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        """Collect the settled facts of one bound evaluation for its decision record.

        No request payload and no approval authority identity are included.
        Condition rows are added by the durability producer from the
        condition producer's observations.
        """
        aggregate = outcome["aggregate"]
        selection = outcome["selection"]
        consent_producer = self._state_producers.get("consent")
        fingerprint = invocation.request_fingerprint
        return {
            "authorization_attempt_id": _ATTEMPT_ID.get(),
            "action_id": request.action_id,
            "action_type": request.action_type,
            "trace_id": request.trace_id,
            "checkpoint_id": invocation.checkpoint_id,
            "selection": {
                "selection_id": selection.selection_id,
                "selection_revision": selection.selection_revision,
            },
            "policies": [
                {"policy_id": policy_id, "policy_version": version, "verdict": verdict,
                 "reason_codes": list(codes), **provenance}
                for (policy_id, version, verdict), codes, provenance in zip(
                    invocation.policy_observations, invocation.policy_observation_reason_codes,
                    invocation.policy_observation_provenance, strict=True,
                )
            ],
            "decision": {
                "verdict": aggregate.verdict.value,
                "policy_ids": list(aggregate.policy_ids),
                "reason_codes": list(aggregate.reason_codes),
                "receipt_use": aggregate.receipt_use.value,
                "requirement_ids": [item.requirement_id for item in aggregate.requirements],
                "constraint_ids": [item.constraint_id for item in aggregate.constraints],
            },
            "approvals": sorted(
                ({"requirement_id": item.requirement_id, "verifier_id": item.verifier_id}
                 for item in verified_approvals),
                key=lambda row: (row["requirement_id"], row["verifier_id"]),
            ),
            "consent": (
                None if consent_producer is None
                else "owned_by_implementation" if self._implementation_owned_consent
                else consent_producer.producer_id
            ),
            "request_fingerprint": None if fingerprint is None else fingerprint[1],
            "fingerprint_policy_id": None if fingerprint is None else fingerprint[0],
            "review_obligations": [
                {
                    "obligation_id": item.obligation_id,
                    "policy_id": str.__str__(item.binding.policy_id),
                    "policy_version": str.__str__(item.binding.policy_version),
                    "binding": item.binding.canonical_view(),
                    "mode": item.mode.value,
                    "verifier_id": item.verifier_id,
                    "verifier_version": item.verifier_version,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "future_action_types": list(item.future_action_types),
                }
                for decision in outcome["decisions"]
                for item in decision.review_obligations
            ],
            "review": [],
            "informative": _informative_rows(
                selection, outcome["informative_decisions"], outcome["informative_diagnostics"],
            ),
        }

    def _condition_inventory(
        self, plan: dict[str, Any], selection: PolicySelection,
        decisions: tuple[PolicyDecision, ...], informative: tuple[PolicyDecision, ...],
        diagnostics: tuple[tuple[str, str], ...],
    ) -> tuple[Any, ...]:
        """Derive complete coverage independently of any condition observation."""
        required_results = {item.policy_id: item for item in decisions}
        informative_results = {item.policy_id: item for item in informative}
        coverage = []
        governing = []
        informational = []
        for role, bindings, results in (
            ("required", selection.required, required_results),
            ("informative", selection.informative, informative_results),
        ):
            for binding in bindings:
                applicability = plan["entries"][(binding.policy_id, binding.policy_version)]
                result = results.get(binding.policy_id)
                reasons = [code for policy_id, code in diagnostics if policy_id == binding.policy_id]
                status = ("explicit_empty" if not result.conditions else "admitted") if result is not None else (
                    "unavailable" if reasons or applicability.state is PolicyApplicabilityState.UNDETERMINED
                    else "not_applicable"
                )
                row = {"binding": binding.canonical_view(), "role": role,
                       "applicability": applicability.state.value, "status": status,
                       "count": len(result.conditions) if result is not None else None,
                       "diagnostics": reasons}
                row_bytes = canonical_json_bytes(row, field="condition coverage")
                coverage.append(row_bytes)
                if result is None:
                    continue
                for condition in result.conditions:
                    if role == "required" and condition.required:
                        governing.append((condition, row_bytes))
                    else:
                        informational.append({"condition": condition.canonical_view(),
                                              "role": role, "coverage": row})
        governing.sort(key=lambda item: (item[0].binding.policy_id,
                                         item[0].binding.policy_version, item[0].condition_id))
        entries = tuple((f"condition.{index:06d}", condition, row)
                        for index, (condition, row) in enumerate(governing, 1))
        view = [{"key": key, "condition": condition.canonical_view(),
                 "role": "required", "coverage": json.loads(row)}
                for key, condition, row in entries]
        invalid = False
        try:
            inventory = canonical_json_bytes(view, field="condition inventory")
        except (SETTValidationError, UnicodeError):
            invalid = True
        if invalid:
            raise _handoff.policy_error("policy.condition_inventory_invalid")
        info_bytes = None
        info_diagnostics: tuple[tuple[str, str], ...] = ()
        try:
            info_bytes = canonical_json_bytes(informational, field="informational conditions")
        except (SETTValidationError, UnicodeError):
            info_diagnostics = tuple((identifier, "policy.condition_inventory_invalid")
                                     for identifier in sorted({item["condition"]["binding"]["policy_id"]
                                                               for item in informational}))
        return inventory, entries, tuple(coverage), info_bytes, info_diagnostics

    def _compose(
        self, decisions: tuple[PolicyDecision, ...]
    ) -> AuthorizationDecision:
        policy_ids = tuple(decision.policy_id for decision in decisions)
        reason_codes = tuple(
            dict.fromkeys(
                reason for decision in decisions for reason in decision.reason_codes
            )
        )
        denied = [
            decision
            for decision in decisions
            if decision.verdict is AuthorizationVerdict.DENY
        ]
        if denied:
            return AuthorizationDecision(
                AuthorizationVerdict.DENY, policy_ids, reason_codes=reason_codes
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
        receipt_use = (
            ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
            if all(
                decision.receipt_use is ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY
                for decision in decisions
            )
            else ReceiptUse.SINGLE_USE
        )
        return AuthorizationDecision(
            AuthorizationVerdict.GRANT,
            policy_ids,
            receipt_use=receipt_use,
            reason_codes=reason_codes,
            constraints=tuple(constraints_by_id.values()),
        )

    # ── Private handoff, driven by an attached authorization receiver ─

    def _attach(
        self,
        receiver: object,
        *,
        clock: AuthorizationClock,
        evidence_sink: AuthorizationEvidenceSink,
        selection_resolver: PolicySelectionResolver | None,
    ) -> ReceiverLink:
        if clock is not self._clock or evidence_sink is not self._evidence_sink:
            raise _handoff.authorization_configuration_error(
                "authorization.policy_ruler_configuration_mismatch"
            )
        if (
            selection_resolver is not None
            and selection_resolver is not self._selection_resolver
        ):
            raise _handoff.authorization_configuration_error(
                "authorization.policy_selection_resolver_conflict"
            )
        return ReceiverLink(self._incarnation, receiver)

    def _register_state_producer(self, producer: object) -> None:
        """Install one internal verified-state producer for a closed slot.

        Private by design: there is no public slot writer, loader or
        generic evidence API. Condition production is configured explicitly; a
        producer installed by a test is synthetic and proves nothing
        about a real condition, consent, review or durability verifier.
        """
        slot = getattr(producer, "slot", None)
        if slot not in _handoff.PRODUCED_SLOT_NAMES:
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        if not isinstance(producer, _handoff.VerifiedStateProducer):
            raise _handoff.policy_error("policy.ruler_configuration_invalid")
        self._guard.acquire()
        try:
            self._guard.reject_reentrant_mutation()
            if slot in self._state_producers:
                raise _handoff.policy_error("policy.ruler_configuration_invalid")
            self._state_producers[slot] = producer
            self._generation += 1
        finally:
            self._guard.release()

    @contextmanager
    def _receiver_configuration_change(self) -> Iterator[None]:
        """Hold the guard across one attached receiver's configuration change.

        The mutation and its generation increment must be serialized with
        the final receipt transition, not merely preceded by a guard the
        caller has already released. A same-thread attempt from inside a
        critical phase is still rejected before anything changes.
        """
        self._guard.acquire()
        try:
            self._guard.reject_reentrant_mutation()
            yield
        finally:
            self._guard.release()

    def _open_invocation(
        self,
        link: ReceiverLink,
        *,
        request: AuthorizationRequest,
        checkpoint_id: str,
    ) -> Invocation:
        self._admit_evaluation_input(request, frozenset(), checkpoint_id)
        now = self._now()
        invocation = Invocation(link, request, checkpoint_id)
        self._guard.acquire()
        try:
            self._guard.reject_reentrant_mutation()
            self._purge(now)
            if len(self._live) >= self._max_live:
                raise _handoff.policy_error("policy.evaluation_capacity_exceeded")
            self._live[invocation] = None
        finally:
            self._guard.release()
        return invocation

    def _purge(self, now: datetime | None) -> None:
        for invocation, record in tuple(self._live.items()):
            if record is not None and not record.is_live(now):
                del self._live[invocation]

    def _close_invocation(self, invocation: Invocation) -> None:
        self._guard.acquire()
        try:
            record = self._live.get(invocation)
            if record is not None and record.lifecycle == _handoff.RECEIPT_BOUND:
                # A bound receipt keeps its record until it is consumed,
                # expires or is purged; closing the call does not revoke it.
                return
            if record is not None:
                record.lifecycle = _handoff.TERMINAL
            invocation.closed = True
            self._live.pop(invocation, None)
        finally:
            self._guard.release()

    def _collect_state(
        self, context: StateContext, link: ReceiverLink,
        *, invocation: Invocation | None = None,
    ) -> dict[str, VerifiedStateSlot]:
        collected: dict[str, VerifiedStateSlot] = {}
        producers: dict[str, Any] = {_handoff.APPROVALS_SLOT: link.approvals_producer}
        self._guard.acquire()
        try:
            producers.update(self._state_producers)
        finally:
            self._guard.release()
        if any(producers.get(name) is None for name in _handoff.SLOT_NAMES):
            raise _handoff.policy_error("policy.evaluation_incomplete")
        for name in _handoff.SLOT_NAMES:
            producer = producers.get(name)
            if producer is None:
                raise _handoff.policy_error("policy.evaluation_incomplete")
            if producer is self._durability_producer and isinstance(context.decision_facts, dict):
                context.decision_facts["review"] = review_rows(collected.get("review"))
            slot = None
            try:
                slot = producer.produce(context)
            except Exception:  # noqa: BLE001 -- producer boundary, fixed diagnostic
                slot = None
            # Raise outside the handler so no producer exception is retained.
            if (
                type(slot) is _handoff.ConditionRejection
                and producer is self._condition_producer
                and slot.code in {
                    "policy.evaluation_incomplete", "policy.clock_invalid",
                    "policy.condition_verifier_missing", "policy.condition_verifier_failed",
                    "policy.condition_result_invalid", "policy.condition_unsatisfied",
                    "policy.condition_evidence_not_present", "policy.condition_evidence_invalid",
                    "policy.condition_evidence_unavailable",
                }
            ):
                if invocation is not None and slot.failed_condition is not None:
                    # Declared identities only, for the authorization error.
                    invocation.failed_condition = slot.failed_condition
                    invocation.attempted_conditions = slot.attempted_conditions
                raise _handoff.policy_error(slot.code)
            if (
                type(slot) is _handoff.ConditionRejection
                and producer is self._durability_producer
                and slot.code == _DECISION_RECORD_UNCONFIRMED
            ):
                raise _handoff.policy_error(slot.code)
            if (
                type(slot) is _handoff.ConditionRejection
                and producer is self._review_producer
                and slot.code in REVIEW_CODES
            ):
                raise _handoff.policy_error(slot.code)
            if slot is None or not _handoff.validated_slot(slot, context):
                raise _handoff.policy_error("policy.evaluation_incomplete")
            if (
                slot.slot != name
                or slot.producer_id != producer.producer_id
                or slot.schema_revision != producer.schema_revision
            ):
                raise _handoff.policy_error("policy.evaluation_incomplete")
            if name == "conditions" and not _handoff.condition_slot_matches(slot, context):
                raise _handoff.policy_error("policy.evaluation_incomplete")
            collected[name] = slot
        return collected

    def _evaluate_bound(
        self,
        invocation: Invocation,
        *,
        verified_approvals: frozenset[VerifiedApproval],
        receiver_generation: int,
    ) -> PolicyEvaluation:
        request = invocation.request
        self._admit_evaluation_input(
            request, verified_approvals, invocation.checkpoint_id
        )
        self._guard.acquire()
        try:
            snapshot = self._snapshot()
        finally:
            self._guard.release()
        outcome = self._run_engine(
            request, verified_approvals, snapshot, invocation=invocation
        )
        context = StateContext(
            request=request,
            checkpoint_id=invocation.checkpoint_id,
            verified_approvals=verified_approvals,
            receiver_generation=receiver_generation,
        )
        inventory, entries, coverage, _informational, _diagnostics = outcome["condition_data"]
        context = StateContext(
            request=request, checkpoint_id=invocation.checkpoint_id,
            verified_approvals=verified_approvals, receiver_generation=receiver_generation,
            condition_inventory=inventory, condition_entries=entries, condition_coverage=coverage,
            condition_proof=_handoff.ConditionProof(),
            decision_facts=(
                self._decision_facts(request, verified_approvals, invocation, outcome)
                if self._durability_producer is not None else None
            ),
        )
        state = self._collect_state(context, invocation.link, invocation=invocation)
        request_bytes = _handoff.canonical_request_bytes(request, verified_approvals)
        if request_bytes is None:
            raise _handoff.policy_error("policy.evaluation_input_invalid")
        selection_bytes = _handoff.canonical_selection_bytes(outcome["selection"])
        if selection_bytes is None:
            raise _handoff.policy_error("policy.evaluation_incomplete")
        evaluated_at = self._now()
        valid_until = self._deadline(evaluated_at)
        for slot in state.values():
            if slot.valid_until is not None and slot.valid_until < valid_until:
                valid_until = slot.valid_until
        verification_ref = uuid4().hex
        self._guard.acquire()
        try:
            if self._generation != snapshot[0]:
                raise _handoff.policy_error("policy.evaluation_state_changed")
            if invocation not in self._live or self._live[invocation] is not None:
                raise _handoff.policy_error("policy.evaluation_incomplete")
            evaluation = _create_evaluation(
                ruler_id=self._ruler_id,
                action_id=request.action_id,
                action_type=request.action_type,
                trace_id=request.trace_id,
                selection=outcome["selection"],
                checkpoint_id=invocation.checkpoint_id,
                generation=snapshot[0],
                evaluated_at=evaluated_at,
                valid_until=valid_until,
                decision=outcome["aggregate"],
                policy_decisions=outcome["decisions"],
                informative_decisions=outcome["informative_decisions"],
                informative_diagnostics=outcome["informative_diagnostics"],
                verification_ref=verification_ref,
            )
            record = BindingRecord(
                incarnation=self._incarnation,
                link=invocation.link,
                invocation=invocation,
                evaluation=evaluation,
                evaluation_id=evaluation.evaluation_id,
                verification_ref=verification_ref,
                request_bytes=request_bytes,
                selection_bytes=selection_bytes,
                policy_generation=snapshot[0],
                receiver_generation=receiver_generation,
                checkpoint_id=invocation.checkpoint_id,
                state=state,
                condition_inventory=context.condition_inventory,
                condition_entries=context.condition_entries,
                condition_coverage=context.condition_coverage,
                condition_proof=context.condition_proof,
                condition_informational=outcome["condition_data"][3],
                evaluated_at=evaluated_at,
                valid_until=valid_until,
            )
            record.state_signature = _handoff.state_signature(state)
            self._live[invocation] = record
        finally:
            self._guard.release()
        return evaluation

    def _current_selection(
        self, request: AuthorizationRequest
    ) -> tuple[PolicySelection, bytes | None]:
        selection = self._resolve_selection(request)
        return selection, _handoff.canonical_selection_bytes(selection)

    def _current_state(
        self,
        invocation: Invocation,
        *,
        verified_approvals: frozenset[VerifiedApproval],
        receiver_generation: int,
    ) -> dict[str, VerifiedStateSlot] | None:
        context = StateContext(
            request=invocation.request,
            checkpoint_id=invocation.checkpoint_id,
            verified_approvals=verified_approvals,
            receiver_generation=receiver_generation,
        )
        self._guard.acquire()
        try:
            record = self._live.get(invocation)
        finally:
            self._guard.release()
        if record is None:
            # Preserve an absent observation until the ordered recognition checks.
            # This is not an empty verified-state mapping or a producer default.
            return None
        context = self._condition_context(record, verified_approvals, receiver_generation, "pre_issue")
        return self._collect_state(context, invocation.link)

    def _condition_context(
        self, record: BindingRecord, verified_approvals: frozenset[VerifiedApproval],
        receiver_generation: int, stage: str,
    ) -> StateContext:
        return StateContext(
            request=record.invocation.request, checkpoint_id=record.checkpoint_id,
            verified_approvals=verified_approvals, receiver_generation=receiver_generation,
            condition_inventory=record.condition_inventory, condition_entries=record.condition_entries,
            condition_coverage=record.condition_coverage, condition_proof=record.condition_proof,
            stage=stage, retained_condition_slot=record.state.get("conditions"),
            retained_durability_slot=record.state.get("durability"),
        )

    def _current_state_for_consumption(
        self,
        receiver: object,
        receipt_id: object,
        *,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
        receiver_generation: int,
    ) -> tuple[dict[str, VerifiedStateSlot], str | None]:
        self._guard.acquire()
        try:
            record = self._find_receipt_binding(receiver, receipt_id)
        finally:
            self._guard.release()
        if record is None:
            # The critical section reports the missing binding itself.
            return {}, None
        if not self._confirm_state(record, "pre_consumption"):
            return {}, "authorization.policy_evidence_changed"
        context = StateContext(
            request=request,
            checkpoint_id=record.checkpoint_id,
            verified_approvals=verified_approvals,
            receiver_generation=receiver_generation,
        )
        context = self._condition_context(record, verified_approvals, receiver_generation, "pre_consumption")
        return self._collect_state(context, record.link), None

    def _confirm_invocation_state(self, invocation: Invocation) -> bool:
        """Confirm the retained proof outside the final guarded transition.

        Do not replace it with a freshly collected equal slot or renew its
        deadline. The caller passes this observation to the ordered claim
        checks; a failed confirmation must not mask an earlier binding fault.
        """
        self._guard.acquire()
        try:
            record = self._live.get(invocation)
        finally:
            self._guard.release()
        return record is not None and self._confirm_state(record)

    def _confirm_state(self, record: BindingRecord, stage: str = "pre_issue") -> bool:
        producers: dict[str, Any] = {
            _handoff.APPROVALS_SLOT: record.link.approvals_producer
        }
        self._guard.acquire()
        try:
            producers.update(self._state_producers)
        finally:
            self._guard.release()
        for name, slot in record.state.items():
            producer = producers.get(name)
            if producer is None:
                return False
            try:
                if not producer.confirm(slot):
                    return False
            except Exception:  # noqa: BLE001 -- producer boundary
                return False
            if name == "conditions" and producer is self._condition_producer:
                context = self._condition_context(record, frozenset(), record.receiver_generation, stage)
                if not producer.confirm_context(context):
                    return False
        return True

    def _begin_critical(self) -> _handoff.CriticalPhase:
        return self._guard.begin_critical()

    def _end_critical(self, phase: _handoff.CriticalPhase) -> None:
        self._guard.end_critical(phase)

    def _validate_and_claim(
        self,
        invocation: Invocation,
        evaluation: object,
        *,
        receiver: object,
        request_bytes: bytes | None,
        current_selection_bytes: bytes | None,
        current_state: Mapping[str, VerifiedStateSlot] | None,
        state_confirmed: bool,
        receiver_generation: int,
        now: datetime,
    ) -> tuple[BindingRecord | None, str | None]:
        """Run the fixed receiver checks and claim the record exactly once.

        The caller holds the critical phase. This returns a reason code
        instead of raising so that no evidence callback runs under the
        guard.
        """
        if type(evaluation) is not PolicyEvaluation:
            return None, "authorization.policy_evaluation_invalid"
        # Recognition precedes invocation matching. Search the existing sole
        # ledger by exact result identity, never by public fields or equality.
        record = next(
            (
                entry for entry in self._live.values()
                if entry is not None and entry.evaluation is evaluation
            ),
            None,
        )
        if record is None:
            return None, "authorization.policy_evaluation_unrecognized"
        if record.incarnation is not self._incarnation:
            return None, "authorization.policy_evaluation_unrecognized"
        if record.link.receiver is not receiver or record.invocation is not invocation:
            return None, "authorization.policy_invocation_mismatch"
        if record.lifecycle != _handoff.PENDING:
            return None, "authorization.policy_evaluation_used"
        if request_bytes is None or request_bytes != record.request_bytes:
            # The fixed order checks the full request before the selection.
            # Nothing but the request the invocation was opened for may
            # reach the store, whatever the fingerprint callback returns.
            return None, "authorization.request_mismatch"
        if current_selection_bytes is None:
            return None, "authorization.policy_evaluation_incomplete"
        if current_selection_bytes != record.selection_bytes:
            return None, "authorization.policy_selection_changed"
        if (
            record.policy_generation != self._generation
            or record.receiver_generation != receiver_generation
        ):
            return None, "authorization.policy_generation_changed"
        if (
            record.checkpoint_id != invocation.checkpoint_id
            or record.checkpoint_id != self.entry_checkpoint_id
        ):
            return None, "authorization.policy_checkpoint_mismatch"
        if current_state is None or set(current_state) != set(_handoff.SLOT_NAMES):
            return None, "authorization.policy_evaluation_incomplete"
        if _handoff.state_signature(current_state) != record.state_signature:
            return None, "authorization.policy_evidence_changed"
        if not state_confirmed:
            return None, "authorization.policy_evidence_changed"
        if now < record.evaluated_at:
            # A clock that moves backwards cannot extend or revive validity.
            return None, "authorization.clock_invalid"
        if now >= record.valid_until:
            return None, "authorization.policy_evaluation_expired"
        if record.evaluation.decision.verdict is not AuthorizationVerdict.GRANT:
            return None, "authorization.policy_evaluation_invalid"
        record.lifecycle = _handoff.CLAIMED
        record.receiver = receiver
        return record, None

    def _bind_receipt(self, record: BindingRecord, receipt: object) -> None:
        record.receipt = receipt
        record.lifecycle = _handoff.RECEIPT_BOUND

    def _find_receipt_binding(
        self, receiver: object, receipt_id: object
    ) -> BindingRecord | None:
        for record in self._live.values():
            if record is None or record.receipt is None:
                continue
            if record.receiver is not receiver:
                continue
            if getattr(record.receipt, "receipt_id", None) == receipt_id:
                return record
        return None

    def _validate_receipt_binding(
        self,
        receiver: object,
        receipt: object,
        *,
        request_bytes: bytes | None,
        current_selection_bytes: bytes | None,
        current_state: Mapping[str, VerifiedStateSlot],
        receiver_generation: int,
        now: datetime,
    ) -> tuple[BindingRecord | None, str | None]:
        """Recheck the private binding before the store consumes authority."""
        record = self._find_receipt_binding(
            receiver, getattr(receipt, "receipt_id", None)
        )
        if record is None or record.lifecycle != _handoff.RECEIPT_BOUND:
            return None, "authorization.policy_binding_unavailable"
        if record.receipt != receipt:
            return None, "authorization.receipt_mismatch"
        if record.receipt is not receipt:
            # The record found by public ID binds the original object only.
            # Equal fields do not make its authority available to this copy.
            return None, "authorization.policy_binding_unavailable"
        if request_bytes is None or request_bytes != record.request_bytes:
            return None, "authorization.request_mismatch"
        if current_selection_bytes is None:
            return None, "authorization.policy_evaluation_incomplete"
        if current_selection_bytes != record.selection_bytes:
            return None, "authorization.policy_selection_changed"
        if (
            record.policy_generation != self._generation
            or record.receiver_generation != receiver_generation
        ):
            return None, "authorization.policy_generation_changed"
        if (
            record.checkpoint_id != record.invocation.checkpoint_id
            or record.checkpoint_id != self.entry_checkpoint_id
        ):
            return None, "authorization.policy_checkpoint_mismatch"
        if set(current_state) != set(_handoff.SLOT_NAMES):
            return None, "authorization.policy_evaluation_incomplete"
        if _handoff.state_signature(current_state) != record.state_signature:
            return None, "authorization.policy_evidence_changed"
        if now < record.evaluated_at:
            # A clock that moves backwards cannot extend or revive validity.
            return None, "authorization.clock_invalid"
        if now >= record.valid_until:
            return None, "authorization.policy_evaluation_expired"
        return record, None

    def _terminate(self, record: BindingRecord) -> None:
        record.lifecycle = _handoff.TERMINAL
        self._live.pop(record.invocation, None)


def _informative_rows(
    selection: PolicySelection,
    decisions: tuple[PolicyDecision, ...],
    diagnostics: tuple[tuple[str, str], ...],
) -> list[dict[str, Any]]:
    """One row per informative entry evaluated or diagnosed, in selection order.

    A row holds the policy's identity, the verdict and reason codes it
    returned, if any, and SETT's diagnostic codes for it. An entry that does
    not apply was neither evaluated nor diagnosed, and leaves no row.
    """
    results = {decision.policy_id: decision for decision in decisions}
    rows = []
    for binding in selection.informative:
        policy_id = str.__str__(binding.policy_id)
        decision = results.get(policy_id)
        codes = [str.__str__(code) for identifier, code in diagnostics if identifier == policy_id]
        if decision is None and not codes:
            continue
        rows.append({
            "policy_id": policy_id,
            "policy_version": str.__str__(binding.policy_version),
            "verdict": None if decision is None else decision.verdict.value,
            "reason_codes": [] if decision is None else [str.__str__(code) for code in decision.reason_codes],
            "diagnostics": codes,
        })
    return rows


def _provenance(canonical: dict[str, Any]) -> dict[str, Any]:
    """A detached copy of an admitted binding's origins and profile, for its decision record row.

    The policy ID and version are already in the row, and the contract
    version is fixed, so only the declared provenance is kept.
    """
    view = json.loads(canonical_json_bytes(canonical, field="policy binding"))
    profile = None if view["profile_id"] is None else {
        "profile_id": view["profile_id"], "profile_revision": view["profile_revision"],
    }
    return {"origins": view["origins"], "profile": profile}


def _valid_identifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_identifier(value, field="identifier")
    except (SETTValidationError, UnicodeError):
        return False
    return True


def _valid_resolver_id(resolver: object) -> bool:
    return _valid_identifier(getattr(resolver, "resolver_id", None))


def _admit_controlled_checkpoints(values: object, entry: str) -> dict[str, tuple[str, ...]]:
    """Freeze the declared later checkpoints, or reject the whole value."""
    if values is None:
        return {}
    admitted = False
    frozen: dict[str, tuple[str, ...]] = {}
    try:
        if not isinstance(values, Mapping) or not 0 <= len(values) <= 256:
            raise ValueError
        for action_type, checkpoints in tuple(islice(values.items(), 257)):
            if not _valid_identifier(action_type) or not isinstance(checkpoints, Sequence):
                raise ValueError
            if isinstance(checkpoints, str | bytes | bytearray | memoryview):
                raise ValueError
            items = tuple(islice(checkpoints, 17))
            if len(items) > 16 or len(items) != len(checkpoints):
                raise ValueError
            if not all(_valid_identifier(item) for item in items):
                raise ValueError
            if entry in items or len(set(items)) != len(items):
                raise ValueError
            frozen[str.__str__(action_type)] = tuple(str.__str__(item) for item in items)
        if len(frozen) != len(values):
            raise ValueError
        admitted = True
    except Exception:  # noqa: BLE001 -- sanitize constructor admission failures
        pass
    if not admitted:
        raise _handoff.policy_error("policy.ruler_configuration_invalid")
    return frozen


def _configured_identifier(value: str, field: str) -> str:
    if not _valid_identifier(value):
        raise _handoff.policy_error("policy.ruler_configuration_invalid")
    return value


def _configured_lifetime(seconds: float) -> timedelta:
    lifetime = None
    try:
        lifetime = timedelta(seconds=float(seconds))
    except (OverflowError, ValueError, OSError):
        lifetime = None
    if lifetime is None:
        raise _handoff.policy_error("policy.ruler_configuration_invalid")
    return lifetime
