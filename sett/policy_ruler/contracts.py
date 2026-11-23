"""Policy callback and selection protocols, owned by the policy domain."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sett._governance.values import AuthorizationRequest, VerifiedApproval
from sett.policy_ruler.condition_results import ConditionVerification
from sett.policy_ruler.conditions import PolicyCondition
from sett.policy_ruler.models import PolicyDecision, PolicySelection
from sett.policy_ruler.review import ReviewVerification, ReviewVerificationRequest


@runtime_checkable
class AuthorizationPolicy(Protocol):
    """A deterministic policy registered for one exact action type."""

    policy_id: str

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> PolicyDecision:
        """Evaluate one detached request and return this policy's decision."""
        ...


@runtime_checkable
class PolicySelectionResolver(Protocol):
    """An application-owned source of one exact-action policy declaration.

    The resolver receives the already detached request and returns the
    declaration for that exact action. It records application provenance;
    it does not authenticate an authority, determine applicable law or
    grant permission. Its result is validated before any policy callback
    runs and before any handler is reached.
    """

    resolver_id: str

    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        """Return the declared selection for one exact action invocation."""
        ...


@runtime_checkable
class ConditionVerifier(Protocol):
    """An application callback protocol for condition observations.

    This names an application callback, not an Expert, Agent, Adapter, Ruler
    or producer. Declaring it installs no verifier and calls nothing.
    Verifiers are registered through ``PolicyRuler(condition_verifiers=...)``,
    and the configured private condition producer calls them during
    authorization. A runtime instance check tests attribute presence only; it
    does not validate signatures, returned values or truthfulness.

    Both methods receive the complete operational AuthorizationRequest,
    including payload access. That is broad exposure, not a minimal or
    public-safe view: repr omissions, frozen values and detached views are
    not access control, redaction or isolation from hostile Python, and
    declared field trust is not identity or consent verification. Neither
    the policy selection nor the actual execution checkpoint is supplied; the
    declared checkpoint is readable from the condition, and reading it proves
    no arrival at that point.

    Missing inputs must not be recovered through rewritten requests,
    overloaded identifiers, ambient configuration or a preconfigured mutable
    selection. A real need for one of them would be a change to this
    interface, designed explicitly. Actual and declared checkpoints are
    checked separately inside the policy layer, and a fabricated matching
    declaration is not accepted.

    The policy layer's comparison of the complete request and the
    authoritative approvals does not depend on what a callback sees: no
    filtered view, digest or fingerprint replaces it. Canonical views that a
    callback generates, including views with empty approvals, do not prove
    which approvals the real invocation kept. The condition producer
    compares each result with the declaration it expected from its own
    retained inventory, not from the result, and a fresh recheck and the
    retained proof stay separate. A consumer that depends on correlation or
    execution fields owns that dependency; narrowing this interface later
    would be a public contract change with a migration cost.
    """

    verifier_id: str
    verifier_version: str

    def verify(
        self,
        request: AuthorizationRequest,
        *,
        condition: PolicyCondition,
    ) -> ConditionVerification:
        """Report one observation for the declared condition."""
        ...

    def confirm(
        self,
        verification: ConditionVerification,
        *,
        request: AuthorizationRequest,
    ) -> bool:
        """Answer whether that exact reported observation remains valid."""
        ...


class ReviewVerifier(Protocol):
    """An application callback declaration for one review obligation.

    Declaring this protocol installs no verifier, producer, notification,
    deadline calculation, scheduler, persistence or governed transition.
    Returned facts still require independent admission against the retained
    obligation and invocation context.
    """

    verifier_id: str
    verifier_version: str

    def verify(self, request: ReviewVerificationRequest, /) -> ReviewVerification:
        """Report facts for the supplied declared review obligation."""
        ...
