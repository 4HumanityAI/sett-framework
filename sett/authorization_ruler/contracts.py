"""Authorization protocols and same-object compatibility imports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from sett._governance.protocols import AuthorizationClock as AuthorizationClock
from sett._governance.values import (
    ApprovalInput,
    AuthorizationRequest,
    ExecutionMode,
    VerifiedApproval,
)
from sett.policy_ruler.contracts import AuthorizationPolicy as AuthorizationPolicy
from sett.policy_ruler.contracts import (
    PolicySelectionResolver as PolicySelectionResolver,
)


@runtime_checkable
class ApprovalVerifier(Protocol):
    """A deterministic verifier for one declared additional authority."""

    verifier_id: str

    def verify(
        self,
        approval_input: ApprovalInput,
        request: AuthorizationRequest,
    ) -> VerifiedApproval | None:
        """Return verified authority or ``None`` for invalid evidence."""
        ...


@runtime_checkable
class AuthorizationFingerprintPolicy(Protocol):
    """A caller-selectable fingerprint over the complete resolved request."""

    policy_id: str

    def fingerprint(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> str:
        """Bind a request and verified authority to a stable identifier."""
        ...


@runtime_checkable
class AuthorizationRequestResolver(Protocol):
    """Resolve application-owned authority fields for one SETT action."""

    def resolve(
        self,
        *,
        action_id: str,
        trace_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        execution_mode: ExecutionMode,
        idempotency_key: str | None,
    ) -> AuthorizationRequest:
        """Build a complete request from SETT-owned identity and payload."""
        ...
