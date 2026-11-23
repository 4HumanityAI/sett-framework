"""The producer-created observation returned by PolicyRuler evaluation.

A PolicyEvaluation is an observation, never a permit. It has no public
constructor, no deserialization entry point and no field a receiver may
accept as proof. ``AuthorizationRuler.authorize()`` has no parameter that
accepts one: copying its fields, rebuilding an equal object or learning
its identifier supplies no authority. Only the private record that
PolicyRuler keeps for its own bound invocation can be recognized, and
that record is compared by object identity together with the complete
detached state described in ``docs/policy_handoff.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sett._governance.values import AuthorizationDecision
from sett.policy_ruler.models import PolicyDecision, PolicySelection

_IMMUTABLE = "PolicyEvaluation instances are immutable."


class PolicyEvaluation:
    """One immutable observation of a completed policy evaluation.

    The same class carries a public inspection result and the internally
    generated bound result. Identical property values do not make the two
    interchangeable: only the bound result is registered in the producing
    ruler's private ledger, and ``verification_ref`` is always ``None``
    for a public inspection. The observation is operational data, not a
    public-safe trace: selection facts and policy reason codes may carry
    deployment detail.
    """

    __slots__ = (
        "_action_id",
        "_action_type",
        "_checkpoint_id",
        "_decision",
        "_evaluated_at",
        "_evaluation_id",
        "_generation",
        "_informative_decisions",
        "_informative_diagnostics",
        "_policy_decisions",
        "_ruler_id",
        "_selection",
        "_trace_id",
        "_valid_until",
        "_verification_ref",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Refuse public construction; PolicyRuler is the only producer."""
        raise TypeError("PolicyEvaluation instances are created by PolicyRuler.")

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse attribute assignment on a completed observation."""
        raise AttributeError(_IMMUTABLE)

    def __delattr__(self, name: str) -> None:
        """Refuse attribute deletion on a completed observation."""
        raise AttributeError(_IMMUTABLE)

    def __repr__(self) -> str:
        """Return an opaque representation carrying no evaluated content."""
        return f"PolicyEvaluation(evaluation_id={self._evaluation_id!r})"

    @property
    def evaluation_id(self) -> str:
        """Return the fresh opaque identifier; it is not a capability."""
        return self._evaluation_id

    @property
    def ruler_id(self) -> str:
        """Return the declared policy ruler label, not an instance proof."""
        return self._ruler_id

    @property
    def action_id(self) -> str:
        """Return the exact evaluated action identifier."""
        return self._action_id

    @property
    def action_type(self) -> str:
        """Return the exact evaluated action type."""
        return self._action_type

    @property
    def trace_id(self) -> str:
        """Return the exact causal request identifier."""
        return self._trace_id

    @property
    def selection(self) -> PolicySelection:
        """Return the immutable selection snapshot that governed this run."""
        return self._selection

    @property
    def checkpoint_id(self) -> str:
        """Return the exact checkpoint this evaluation was bound to."""
        return self._checkpoint_id

    @property
    def generation(self) -> int:
        """Return the observed nonnegative policy-instance generation."""
        return self._generation

    @property
    def evaluated_at(self) -> datetime:
        """Return the aware UTC completion time of this evaluation."""
        return self._evaluated_at

    @property
    def valid_until(self) -> datetime:
        """Return the exclusive upper bound; this asserts no current validity."""
        return self._valid_until

    @property
    def decision(self) -> AuthorizationDecision:
        """Return the composed aggregate; it is not a receipt or a permit."""
        return self._decision

    @property
    def policy_decisions(self) -> tuple[PolicyDecision, ...]:
        """Return the required evaluated results, in binding order."""
        return self._policy_decisions

    @property
    def informative_decisions(self) -> tuple[PolicyDecision, ...]:
        """Return the successful informative results, which grant nothing."""
        return self._informative_decisions

    @property
    def informative_diagnostics(self) -> tuple[tuple[str, str], ...]:
        """Return informative policy identifier and reason-code pairs."""
        return self._informative_diagnostics

    @property
    def verification_ref(self) -> str | None:
        """Return the bound invocation's opaque state reference, or None."""
        return self._verification_ref


def _create_evaluation(
    *,
    ruler_id: str,
    action_id: str,
    action_type: str,
    trace_id: str,
    selection: PolicySelection,
    checkpoint_id: str,
    generation: int,
    evaluated_at: datetime,
    valid_until: datetime,
    decision: AuthorizationDecision,
    policy_decisions: tuple[PolicyDecision, ...],
    informative_decisions: tuple[PolicyDecision, ...],
    informative_diagnostics: tuple[tuple[str, str], ...],
    verification_ref: str | None,
) -> PolicyEvaluation:
    """Build one observation; private to the producing policy ruler."""
    evaluation = object.__new__(PolicyEvaluation)
    assign = object.__setattr__
    assign(evaluation, "_evaluation_id", uuid4().hex)
    assign(evaluation, "_ruler_id", ruler_id)
    assign(evaluation, "_action_id", action_id)
    assign(evaluation, "_action_type", action_type)
    assign(evaluation, "_trace_id", trace_id)
    assign(evaluation, "_selection", selection)
    assign(evaluation, "_checkpoint_id", checkpoint_id)
    assign(evaluation, "_generation", generation)
    assign(evaluation, "_evaluated_at", evaluated_at)
    assign(evaluation, "_valid_until", valid_until)
    assign(evaluation, "_decision", decision)
    assign(evaluation, "_policy_decisions", policy_decisions)
    assign(evaluation, "_informative_decisions", informative_decisions)
    assign(evaluation, "_informative_diagnostics", informative_diagnostics)
    assign(evaluation, "_verification_ref", verification_ref)
    return evaluation
