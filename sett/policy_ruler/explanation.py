"""Explanations of one authorization decision, as fixed views for different readers.

``explain()`` builds an ``Explanation`` from a ``DecisionRecord``, for an
evaluation that completed, or from a ``SETTAuthorizationError``, for an
attempt that was blocked and wrote no decision record. An explanation offers
two views, each a closed set of fields:

- ``operational_view()``, for the deployment's own operators;
- ``public_view()``, for anyone else.

The views carry stable codes and structured values, never sentences: what a
reader is told, in which words and language, belongs to the application. The
public view shows SETT's own reason codes and enumerations, and a value the
application or its policies supplied only when the application approves it
in the call; anything else is withheld and counted. A policy's declared
origins, from a decision record of version 3, and the informative results of
a record of version 4 follow the same rule. Next steps appear only as the
application declares them. No view carries a request payload, a
principal, a fingerprint, an evidence reference, an approval authority's
identity or an exception message.

The authorization verdict, the effect's state and the ethics verdict stay
separate fields. Nothing combines them into one judgement or a score, and
nothing states that an external requirement is satisfied or that data was
deleted. An explanation is a value to read, never authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sett._governance.validation import MAX_JSON_ITEMS, validate_identifier
from sett._governance.verdict import AuthorizationVerdict
from sett.ethics_ruler.ethic_kernel.filter import FilterVerdict
from sett.exceptions import SETTAuthorizationError, SETTValidationError
from sett.policy_ruler.condition_results import ConditionVerificationState
from sett.policy_ruler.decision_record import DecisionRecord
from sett.policy_ruler.governance_recovery import RecoveredDecision, RecoveredOutcome
from sett.policy_ruler.review import ReviewMode

# The reason codes SETT itself raises at the authorization boundary: a fixed
# vocabulary without application data, so the public view may show them.
_SETT_CODES = frozenset("authorization." + code for code in (
    "approval_conflict", "approval_required", "approval_requirement_conflict",
    "checkpoint_blocked", "checkpoint_invalid", "checkpoint_unavailable", "clock_invalid",
    "condition_evidence_invalid", "condition_evidence_not_present",
    "condition_evidence_unavailable", "condition_result_invalid", "condition_unsatisfied",
    "condition_verifier_failed", "condition_verifier_missing", "constraint_duplicate",
    "decision_record_unconfirmed", "denied", "evidence_failed", "fingerprint_failed",
    "not_configured", "policy_applicability_invalid", "policy_applicability_undetermined",
    "policy_binding_invalid", "policy_binding_mismatch", "policy_binding_required",
    "policy_binding_unavailable", "policy_checkpoint_mismatch", "policy_contract_unsupported",
    "policy_duplicate", "policy_evaluation_capacity_exceeded", "policy_evaluation_expired",
    "policy_evaluation_failed", "policy_evaluation_incomplete", "policy_evaluation_invalid",
    "policy_evaluation_unrecognized", "policy_evaluation_used", "policy_evidence_changed",
    "policy_failed", "policy_generation_changed", "policy_handoff_reentrant",
    "policy_identity_mismatch", "policy_invalid", "policy_invocation_mismatch",
    "policy_missing", "policy_origin_invalid", "policy_required_empty",
    "policy_result_invalid", "policy_ruler_configuration_mismatch", "policy_ruler_invalid",
    "policy_ruler_missing", "policy_selection_changed", "policy_selection_facts_unverified",
    "policy_selection_failed", "policy_selection_invalid", "policy_selection_not_effective",
    "policy_selection_resolver_conflict", "policy_selection_resolver_invalid",
    "policy_selection_resolver_missing", "policy_selection_result_invalid",
    "policy_selection_scope_mismatch", "policy_source_invalid", "policy_version_mismatch",
    "provenance_not_authoritative", "receipt_consumed", "receipt_duplicate",
    "receipt_expired", "receipt_invalid", "receipt_mismatch", "receipt_revoked",
    "receipt_store_failed", "receipt_unknown", "request_mismatch",
    "request_resolution_failed", "request_resolution_invalid", "resolver_missing",
    "review_restriction_active", "review_result_invalid", "review_state_unavailable",
    "review_verifier_failed", "review_verifier_missing", "system_failure",
    "verifier_duplicate", "verifier_failed", "verifier_identity_mismatch",
    "verifier_invalid", "verifier_missing", "verifier_result_invalid",
))
_VERDICTS = frozenset(item.value for item in AuthorizationVerdict)
_STATES = frozenset(item.value for item in ConditionVerificationState)
_MODES = frozenset(item.value for item in ReviewMode)
_OUTCOMES = frozenset(item.value for item in RecoveredOutcome)
_EFFECT_ENDS = frozenset({"completed", "failed", "cancelled", "timed_out", "blocked"})
_ETHICS = frozenset(item.value for item in FilterVerdict)
# Policy-layer codes that can appear as an informative policy's diagnostic.
_INFORMATIVE_CODES = frozenset({"policy.condition_inventory_invalid"})


def _identifiers(values: Iterable[str], field: str) -> tuple[str, ...]:
    """Admit a bounded sequence of identifiers, kept in order."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise SETTValidationError(f"{field} must be a collection of identifiers.")
    admitted = []
    for value in values:
        if len(admitted) == MAX_JSON_ITEMS:
            raise SETTValidationError(f"{field} accepts at most {MAX_JSON_ITEMS} identifiers.")
        admitted.append(str.__str__(validate_identifier(value, field=field)))
    return tuple(admitted)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicValues:
    """The application's values it approves for the public view, by kind.

    SETT cannot know which of an application's identifiers are fit for the
    public, so the application decides. A value it or its policies supplied
    appears in the public view only when listed here under its kind. By
    default nothing is approved, and every such value is withheld.
    """

    policy_ids: frozenset[str] = frozenset()
    reason_codes: frozenset[str] = frozenset()
    action_types: frozenset[str] = frozenset()
    condition_ids: frozenset[str] = frozenset()
    obligation_ids: frozenset[str] = frozenset()
    layer_ids: frozenset[str] = frozenset()
    requirement_ids: frozenset[str] = frozenset()
    source_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Admit each kind as a frozen set of identifiers."""
        for name in ("policy_ids", "reason_codes", "action_types", "condition_ids", "obligation_ids",
                     "layer_ids", "requirement_ids", "source_ids"):
            object.__setattr__(self, name, frozenset(_identifiers(getattr(self, name), name)))


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SETTValidationError(f"{field} is malformed.")
    return str.__str__(value)


def _optional(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _row(row: object, field: str, *keys: str) -> tuple[str, ...]:
    if not isinstance(row, dict) or any(key not in row for key in keys):
        raise SETTValidationError(f"{field} is malformed.")
    return tuple(_text(row[key], field) for key in keys)


def _codes(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise SETTValidationError(f"{field} is malformed.")
    return tuple(_text(value, field) for value in values)


def _identity(value: object, field: str) -> tuple[str, ...]:
    """A ``(policy_id, policy_version, condition_id)`` or observation row, exactly three values."""
    items = _codes(value, field)
    if len(items) != 3:
        raise SETTValidationError(f"{field} is malformed.")
    return items


def _origins(values: object) -> tuple[tuple[Any, ...], ...] | None:
    """A policy row's recorded origins; None when the record holds none (versions 1 and 2)."""
    if values is None:
        return None
    if not isinstance(values, list):
        raise SETTValidationError("origins is malformed.")
    origins = []
    for origin in values:
        layer, requirement, revision = _row(origin, "origins", "layer_id", "requirement_id", "requirement_revision")
        source = origin.get("source")
        origins.append((
            layer, requirement, revision,
            None if source is None else _row(source, "origins", "source_id", "source_version", "locator"),
            _optional(origin.get("source_absence_reason"), "origins"),
        ))
    return tuple(origins)


def _profile(value: object) -> tuple[str, ...] | None:
    return None if value is None else _row(value, "profile", "profile_id", "profile_revision")


def _requirements(values: object) -> tuple[tuple[str, ...], ...] | None:
    """A condition row's declared requirements; None when the record holds none."""
    if values is None:
        return None
    if not isinstance(values, list):
        raise SETTValidationError("requirements is malformed.")
    return tuple(_row(item, "requirements", "layer_id", "requirement_id") for item in values)


def _informative(rows: object) -> tuple[tuple[Any, ...], ...] | None:
    """A record's informative rows; None when the record holds none (versions 1 to 3)."""
    if rows is None:
        return None
    if not isinstance(rows, (list, tuple)):
        raise SETTValidationError("informative is malformed.")
    return tuple(
        (*_row(row, "informative", "policy_id", "policy_version"),
         _optional(row.get("verdict"), "informative"),
         _codes(row.get("reason_codes"), "informative"), _codes(row.get("diagnostics"), "informative"))
        for row in rows
    )


def _from_record(record: DecisionRecord, recovered: RecoveredDecision | None) -> dict[str, Any]:
    if recovered is not None:
        if type(recovered) is not RecoveredDecision:
            raise TypeError("recovered must be a RecoveredDecision or None.")
        if recovered.operation_id != record.operation_id:
            raise ValueError("recovered describes another decision record.")
    decision = record.decision
    if not isinstance(decision, dict):
        raise SETTValidationError("decision is malformed.")
    return {
        "source": "decision_record",
        "reference": _optional(record.authorization_attempt_id, "authorization_attempt_id"),
        "action_type": _text(record.action_type, "action_type"),
        "verdict": _text(decision.get("verdict"), "verdict"),
        "reason_code": None,
        "original_reason_code": None,
        "policies": tuple(
            (*_row(row, "policies", "policy_id", "policy_version", "verdict"),
             _codes(row.get("reason_codes"), "policies"),
             _origins(row.get("origins")), _profile(row.get("profile")))
            for row in record.policies
        ),
        "policy_rows_may_be_incomplete": False,
        "conditions": tuple(
            (*_row(row, "conditions", "policy_id", "policy_version", "condition_id", "state"),
             _requirements(row.get("requirements")))
            for row in record.conditions
        ),
        "failed_required_condition": None,
        "attempted_conditions": (),
        "pending_approvals": _codes(decision.get("requirement_ids", ()), "requirement_ids"),
        "review_obligations": tuple(
            _row(row, "review_obligations", "obligation_id", "policy_id", "policy_version", "mode")
            for row in record.review_obligations
        ),
        "restrictions_cleared": tuple(
            _row(row, "review", "obligation_id", "operation_id", "outcome") for row in record.review
        ),
        "informative": _informative(record.informative),
        "recovery_outcome": None if recovered is None else recovered.outcome.value,
        "effect_end": None if recovered is None else _optional(recovered.effect, "effect"),
    }


def _from_error(error: SETTAuthorizationError) -> dict[str, Any]:
    # The exception's message and arguments are never read.
    observations = error.policy_observations or ()
    codes = error.policy_observation_reason_codes or ()
    if len(observations) != len(codes):
        raise SETTValidationError("policy_observation_reason_codes is malformed.")
    failed = error.failed_condition
    return {
        "source": "authorization_error",
        "reference": _optional(error.authorization_attempt_id, "authorization_attempt_id"),
        "action_type": None,
        "verdict": None,
        "reason_code": _text(error.reason_code, "reason_code"),
        "original_reason_code": _optional(error.original_reason_code, "original_reason_code"),
        "policies": tuple(
            (*_identity(row, "policy_observations"), _codes(reasons, "policy_observation_reason_codes"),
             None, None)
            for row, reasons in zip(observations, codes, strict=True)
        ),
        "policy_rows_may_be_incomplete": True,
        "conditions": (),
        "failed_required_condition": None if failed is None else _identity(failed, "failed_condition"),
        "attempted_conditions": tuple(
            _identity(item, "attempted_conditions") for item in error.attempted_conditions or ()
        ),
        "pending_approvals": (),
        "review_obligations": (),
        "restrictions_cleared": (),
        "informative": None,
        "recovery_outcome": None,
        "effect_end": None,
    }


def _attempt_id(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


class _Disclosure:
    """Shows a value only when it is allowed, and counts what it withholds."""

    def __init__(self) -> None:
        self.withheld = 0

    def show(self, value: str | None, allowed: frozenset[str]) -> str | None:
        if value is None:
            return None
        if value in allowed:
            return value
        self.withheld += 1
        return None


def _condition(identity: tuple[str, ...]) -> dict[str, str]:
    return dict(zip(("policy_id", "policy_version", "condition_id"), identity, strict=True))


def _origin_view(origin: tuple[Any, ...]) -> dict[str, Any]:
    layer, requirement, revision, source, reason = origin
    return {
        "layer_id": layer, "requirement_id": requirement, "requirement_revision": revision,
        "source": None if source is None else dict(
            zip(("source_id", "source_version", "locator"), source, strict=True)
        ),
        "source_absence_reason": reason,
    }


class Explanation:
    """One decision, or one blocked attempt, ready to be shown; never authority.

    Build it with ``explain()``. Both views are drawn from the same facts;
    each call returns a new value.
    """

    __slots__ = ("_facts",)

    def __init__(self, facts: dict[str, Any], /) -> None:
        """Keep the facts collected by ``explain()``."""
        self._facts = facts

    @property
    def source(self) -> str:
        """``decision_record`` or ``authorization_error``."""
        return self._facts["source"]

    def operational_view(self, *, next_steps: Iterable[str] = ()) -> dict[str, Any]:
        """The view for the deployment's own operators: recorded identities, still minimized."""
        facts = self._facts
        failed = facts["failed_required_condition"]
        return {
            "view": "operational",
            "source": facts["source"],
            "reference": facts["reference"],
            "action_type": facts["action_type"],
            "verdict": facts["verdict"],
            "reason_code": facts["reason_code"],
            "original_reason_code": facts["original_reason_code"],
            "policies": [
                {"policy_id": policy_id, "policy_version": version, "verdict": verdict,
                 "reason_codes": list(codes),
                 "origins": None if origins is None else [_origin_view(item) for item in origins],
                 "profile": None if profile is None else dict(
                     zip(("profile_id", "profile_revision"), profile, strict=True)
                 )}
                for policy_id, version, verdict, codes, origins, profile in facts["policies"]
            ],
            "policy_rows_may_be_incomplete": facts["policy_rows_may_be_incomplete"],
            "conditions": [
                {"policy_id": policy_id, "policy_version": version, "condition_id": condition_id,
                 "state": state,
                 "requirements": None if requirements is None else [
                     {"layer_id": layer, "requirement_id": requirement} for layer, requirement in requirements
                 ]}
                for policy_id, version, condition_id, state, requirements in facts["conditions"]
            ],
            "failed_required_condition": None if failed is None else _condition(failed),
            "attempted_conditions": [_condition(item) for item in facts["attempted_conditions"]],
            "pending_approvals": list(facts["pending_approvals"]),
            "review_obligations": [
                {"obligation_id": obligation_id, "policy_id": policy_id, "policy_version": version,
                 "mode": mode}
                for obligation_id, policy_id, version, mode in facts["review_obligations"]
            ],
            "restrictions_cleared": [
                {"obligation_id": obligation_id, "declared_by": operation_id, "outcome": outcome}
                for obligation_id, operation_id, outcome in facts["restrictions_cleared"]
            ],
            "informative": None if facts["informative"] is None else [
                {"policy_id": policy_id, "policy_version": version, "verdict": verdict,
                 "reason_codes": list(codes), "diagnostics": list(diagnostics)}
                for policy_id, version, verdict, codes, diagnostics in facts["informative"]
            ],
            "recovery_outcome": facts["recovery_outcome"],
            "effect_end": facts["effect_end"],
            "ethics_verdict": facts["ethics_verdict"],
            "next_steps": list(_identifiers(next_steps, "next_steps")),
        }

    def public_view(
        self, approved: PublicValues | None = None, *, next_steps: Iterable[str] = (),
    ) -> dict[str, Any]:
        """The view for anyone else: SETT's own vocabulary and the approved values only."""
        if approved is None:
            approved = PublicValues()
        elif type(approved) is not PublicValues:
            raise TypeError("approved must be PublicValues or None.")
        steps = list(_identifiers(next_steps, "next_steps"))
        facts = self._facts
        out = _Disclosure()
        codes = _SETT_CODES | approved.reason_codes
        reference = facts["reference"]
        if reference is not None and not _attempt_id(reference):
            out.withheld += 1
            reference = None
        policies = []
        for policy_id, _version, verdict, reasons, origins, _held_profile in facts["policies"]:
            if out.show(policy_id, approved.policy_ids) is None:
                continue
            policies.append({
                "policy_id": policy_id, "verdict": out.show(verdict, _VERDICTS),
                "reason_codes": [code for code in reasons if out.show(code, approved.reason_codes)],
                "origins": None if origins is None else [
                    {"layer_id": out.show(layer, approved.layer_ids),
                     "requirement_id": out.show(requirement, approved.requirement_ids),
                     "source_id": None if source is None else out.show(source[0], approved.source_ids),
                     "source_declared": source is not None}
                    for layer, requirement, _revision, source, _reason in origins
                ],
            })
        conditions = [
            {"condition_id": condition_id, "state": out.show(state, _STATES)}
            for _policy_id, _version, condition_id, state, _requirements in facts["conditions"]
            if out.show(condition_id, approved.condition_ids) is not None
        ]
        failed = facts["failed_required_condition"]
        informative = None
        if facts["informative"] is not None:
            informative = []
            for policy_id, _version, verdict, reasons, diagnostics in facts["informative"]:
                if out.show(policy_id, approved.policy_ids) is None:
                    continue
                informative.append({
                    "policy_id": policy_id, "verdict": out.show(verdict, _VERDICTS),
                    "reason_codes": [code for code in reasons if out.show(code, approved.reason_codes)],
                    "diagnostics": [code for code in diagnostics
                                    if out.show(code, _SETT_CODES | _INFORMATIVE_CODES)],
                })
        obligations = [
            {"obligation_id": obligation_id, "mode": out.show(mode, _MODES)}
            for obligation_id, _policy_id, _version, mode in facts["review_obligations"]
            if out.show(obligation_id, approved.obligation_ids) is not None
        ]
        view = {
            "view": "public",
            "source": facts["source"],
            "reference": reference,
            "action_type": out.show(facts["action_type"], approved.action_types),
            "verdict": out.show(facts["verdict"], _VERDICTS),
            "reason_code": out.show(facts["reason_code"], codes),
            "original_reason_code": out.show(facts["original_reason_code"], codes),
            "policies": policies,
            "policy_rows_may_be_incomplete": facts["policy_rows_may_be_incomplete"],
            "conditions": conditions,
            "failed_required_condition": (
                None if failed is None
                else {"condition_id": out.show(failed[2], approved.condition_ids)}
            ),
            "pending_approvals": len(facts["pending_approvals"]),
            "review_obligations": obligations,
            "restrictions_cleared": len(facts["restrictions_cleared"]),
            "informative": informative,
            "recovery_outcome": out.show(facts["recovery_outcome"], _OUTCOMES),
            "effect_end": out.show(facts["effect_end"], _EFFECT_ENDS),
            "ethics_verdict": out.show(facts["ethics_verdict"], _ETHICS),
            "next_steps": steps,
        }
        view["withheld"] = out.withheld
        return view


def explain(
    source: DecisionRecord | SETTAuthorizationError,
    *,
    recovered: RecoveredDecision | None = None,
    ethics_verdict: FilterVerdict | None = None,
) -> Explanation:
    """Build the explanation of one decision record or one blocked attempt.

    ``recovered`` is the decision's item from ``read_governance_recovery()``
    and adds the effect's state; it applies only to a decision record.
    ``ethics_verdict`` is the application's ethics verdict, kept as a field
    of its own.
    """
    if ethics_verdict is not None and type(ethics_verdict) is not FilterVerdict:
        raise TypeError("ethics_verdict must be a FilterVerdict or None.")
    if type(source) is DecisionRecord:
        facts = _from_record(source, recovered)
    elif isinstance(source, SETTAuthorizationError):
        if recovered is not None:
            raise ValueError("A blocked attempt has no decision record to recover.")
        facts = _from_error(source)
    else:
        raise TypeError("source must be a DecisionRecord or a SETTAuthorizationError.")
    facts["ethics_verdict"] = None if ethics_verdict is None else ethics_verdict.value
    return Explanation(facts)
