"""Narrow protocols used by the Authorization Ruler."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sett.authorization_ruler.models import (
    ApprovalInput,
    AuthorizationRequest,
    ExecutionMode,
    PolicyDecision,
    VerifiedApproval,
)


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
class AuthorizationClock(Protocol):
    """An injectable timezone-aware clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware authorization time."""
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
