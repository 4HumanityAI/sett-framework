"""Immutable public models for the Authorization Ruler."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from sett.authorization_ruler._validation import (
    FrozenMap,
    canonical_json_bytes,
    freeze_json,
    thaw_json,
    validate_identifier,
)
from sett.authorization_ruler.verdict import AuthorizationVerdict
from sett.exceptions import SETTValidationError


class FieldTrust(str, Enum):
    """Declared authority of one security-relevant value."""

    PROPOSED = "proposed"
    AUTHORITATIVE = "authoritative"
    UNKNOWN = "unknown"


class ExecutionMode(str, Enum):
    """Execution path associated with an authorization request."""

    PLAIN = "plain"
    CONTROLLED = "controlled"


class ReceiptUse(str, Enum):
    """Policy-owned authority for using an issued receipt."""

    SINGLE_USE = "single_use"
    CONTROLLED_IDEMPOTENT_REPLAY = "controlled_idempotent_replay"


@dataclass(frozen=True, slots=True, init=False)
class FieldValue:
    """A detached value plus an attributable provenance declaration."""

    _value: Any = field(repr=False)
    source_id: str
    trust: FieldTrust

    def __init__(self, value: Any, *, source_id: str, trust: FieldTrust) -> None:
        """Detach a value and bind its declared source and trust level."""
        if not isinstance(trust, FieldTrust):
            raise SETTValidationError("trust must be a FieldTrust member.")
        frozen = freeze_json(value, field="field value")
        canonical_json_bytes(thaw_json(frozen), field="field value")
        object.__setattr__(self, "_value", frozen)
        object.__setattr__(
            self,
            "source_id",
            validate_identifier(source_id, field="source_id"),
        )
        object.__setattr__(self, "trust", trust)

    @property
    def value(self) -> Any:
        """Return a detached view of the value."""

        return thaw_json(self._value)

    def canonical_view(self) -> dict[str, Any]:
        """Return the complete provenance-bearing fingerprint view."""

        return {
            "source_id": self.source_id,
            "trust": self.trust.value,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True, init=False)
class ApprovalInput:
    """Opaque proposed evidence routed to one deterministic verifier."""

    requirement_id: str
    verifier_id: str
    _evidence: Any = field(repr=False)

    def __init__(
        self,
        *,
        requirement_id: str,
        verifier_id: str,
        evidence: Any,
    ) -> None:
        """Detach opaque evidence for one declared verifier."""
        frozen = freeze_json(evidence, field="approval evidence")
        canonical_json_bytes(thaw_json(frozen), field="approval evidence")
        object.__setattr__(
            self,
            "requirement_id",
            validate_identifier(requirement_id, field="requirement_id"),
        )
        object.__setattr__(
            self,
            "verifier_id",
            validate_identifier(verifier_id, field="verifier_id"),
        )
        object.__setattr__(self, "_evidence", frozen)

    @property
    def evidence(self) -> Any:
        """Return detached evidence for the selected verifier."""

        return thaw_json(self._evidence)


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    """A specifically declared additional authority requirement."""

    requirement_id: str
    verifier_id: str

    def __post_init__(self) -> None:
        validate_identifier(self.requirement_id, field="requirement_id")
        validate_identifier(self.verifier_id, field="verifier_id")


@dataclass(frozen=True, slots=True)
class VerifiedApproval:
    """The attributable output of one registered approval verifier."""

    requirement_id: str
    verifier_id: str
    authority_id: str

    def __post_init__(self) -> None:
        validate_identifier(self.requirement_id, field="requirement_id")
        validate_identifier(self.verifier_id, field="verifier_id")
        validate_identifier(self.authority_id, field="authority_id")

    def canonical_view(self) -> dict[str, str]:
        """Return the complete verified-approval fingerprint view."""
        return {
            "authority_id": self.authority_id,
            "requirement_id": self.requirement_id,
            "verifier_id": self.verifier_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class AuthorizationConstraint:
    """An immutable descriptor for a restriction already verified by policy."""

    constraint_id: str
    _metadata: FrozenMap = field(repr=False)

    def __init__(
        self,
        constraint_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Detach metadata for one policy-verified restriction."""
        frozen = freeze_json(metadata or {}, field="constraint metadata")
        if not isinstance(frozen, FrozenMap):
            raise SETTValidationError("constraint metadata must be an object.")
        canonical_json_bytes(thaw_json(frozen), field="constraint metadata")
        object.__setattr__(
            self,
            "constraint_id",
            validate_identifier(constraint_id, field="constraint_id"),
        )
        object.__setattr__(self, "_metadata", frozen)

    @property
    def metadata(self) -> dict[str, Any]:
        """Return a detached copy of constraint metadata."""
        return thaw_json(self._metadata)

    def canonical_view(self) -> dict[str, Any]:
        """Return the complete constraint fingerprint view."""
        return {"constraint_id": self.constraint_id, "metadata": self.metadata}


@dataclass(frozen=True, slots=True, init=False)
class AuthorizationRequest:
    """One detached request for authority over an exact action invocation."""

    action_id: str
    trace_id: str
    action_type: str
    _payload: FrozenMap = field(repr=False)
    principal: FieldValue
    resource: FieldValue | None
    scope: tuple[FieldValue, ...]
    purpose: FieldValue | None
    data_labels: tuple[FieldValue, ...]
    context_facts: tuple[tuple[str, FieldValue], ...]
    execution_mode: ExecutionMode
    idempotency_key: str | None

    def __init__(
        self,
        *,
        action_id: str,
        trace_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        principal: FieldValue,
        resource: FieldValue | None = None,
        scope: Sequence[FieldValue] = (),
        purpose: FieldValue | None = None,
        data_labels: Sequence[FieldValue] = (),
        context_facts: Mapping[str, FieldValue] | None = None,
        execution_mode: ExecutionMode = ExecutionMode.PLAIN,
        idempotency_key: str | None = None,
    ) -> None:
        """Validate and detach one complete authorization request."""
        if not isinstance(principal, FieldValue):
            raise SETTValidationError("principal must be a FieldValue.")
        if resource is not None and not isinstance(resource, FieldValue):
            raise SETTValidationError("resource must be a FieldValue or None.")
        if purpose is not None and not isinstance(purpose, FieldValue):
            raise SETTValidationError("purpose must be a FieldValue or None.")
        if not isinstance(execution_mode, ExecutionMode):
            raise SETTValidationError("execution_mode must be an ExecutionMode.")
        frozen_payload = freeze_json(payload, field="payload")
        if not isinstance(frozen_payload, FrozenMap):
            raise SETTValidationError("payload must be a JSON object.")
        canonical_json_bytes(thaw_json(frozen_payload), field="payload")
        normalized_scope = tuple(scope)
        normalized_labels = tuple(data_labels)
        if any(not isinstance(item, FieldValue) for item in normalized_scope):
            raise SETTValidationError("scope entries must be FieldValue instances.")
        if any(not isinstance(item, FieldValue) for item in normalized_labels):
            raise SETTValidationError(
                "data_labels entries must be FieldValue instances."
            )
        facts = context_facts or {}
        normalized_facts: list[tuple[str, FieldValue]] = []
        for key in sorted(facts):
            validate_identifier(key, field="context fact name")
            if not isinstance(facts[key], FieldValue):
                raise SETTValidationError(
                    "context fact values must be FieldValue instances."
                )
            normalized_facts.append((key, facts[key]))
        if idempotency_key is not None:
            validate_identifier(idempotency_key, field="idempotency_key")
        object.__setattr__(
            self,
            "action_id",
            validate_identifier(action_id, field="action_id"),
        )
        object.__setattr__(
            self,
            "trace_id",
            validate_identifier(trace_id, field="trace_id"),
        )
        object.__setattr__(
            self,
            "action_type",
            validate_identifier(action_type, field="action_type"),
        )
        object.__setattr__(self, "_payload", frozen_payload)
        object.__setattr__(self, "principal", principal)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "scope", normalized_scope)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "data_labels", normalized_labels)
        object.__setattr__(self, "context_facts", tuple(normalized_facts))
        object.__setattr__(self, "execution_mode", execution_mode)
        object.__setattr__(self, "idempotency_key", idempotency_key)

    def payload_copy(self) -> dict[str, Any]:
        """Return a detached copy of the authorized payload snapshot."""

        return thaw_json(self._payload)

    def canonical_view(
        self,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> dict[str, Any]:
        """Return the complete request representation used for fingerprinting."""

        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "approval_authorities": [
                item.canonical_view()
                for item in sorted(
                    verified_approvals,
                    key=lambda item: (
                        item.requirement_id,
                        item.verifier_id,
                        item.authority_id,
                    ),
                )
            ],
            "context_facts": {
                key: item.canonical_view() for key, item in self.context_facts
            },
            "data_labels": [item.canonical_view() for item in self.data_labels],
            "execution_mode": self.execution_mode.value,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload_copy(),
            "principal": self.principal.canonical_view(),
            "purpose": self.purpose.canonical_view() if self.purpose else None,
            "resource": self.resource.canonical_view() if self.resource else None,
            "scope": [item.canonical_view() for item in self.scope],
            "trace_id": self.trace_id,
        }

    def non_authoritative_fields(self) -> tuple[str, ...]:
        """Return populated authority fields that lack authoritative provenance."""

        fields: list[tuple[str, FieldValue]] = [("principal", self.principal)]
        if self.resource is not None:
            fields.append(("resource", self.resource))
        fields.extend(("scope", item) for item in self.scope)
        if self.purpose is not None:
            fields.append(("purpose", self.purpose))
        fields.extend(("data_labels", item) for item in self.data_labels)
        fields.extend(
            (f"context_facts.{key}", item) for key, item in self.context_facts
        )
        return tuple(
            name for name, item in fields if item.trust is not FieldTrust.AUTHORITATIVE
        )


def _validated_identifiers(
    values: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(values)
    for value in normalized:
        validate_identifier(value, field=field_name)
    return normalized


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One deterministic policy result."""

    policy_id: str
    verdict: AuthorizationVerdict
    receipt_use: ReceiptUse = ReceiptUse.SINGLE_USE
    reason_codes: tuple[str, ...] = ()
    requirements: tuple[ApprovalRequirement, ...] = ()
    constraints: tuple[AuthorizationConstraint, ...] = ()

    def __post_init__(self) -> None:
        validate_identifier(self.policy_id, field="policy_id")
        if not isinstance(self.verdict, AuthorizationVerdict):
            raise SETTValidationError("verdict must be an AuthorizationVerdict.")
        if not isinstance(self.receipt_use, ReceiptUse):
            raise SETTValidationError("receipt_use must be a ReceiptUse member.")
        object.__setattr__(
            self,
            "reason_codes",
            _validated_identifiers(self.reason_codes, field_name="reason_code"),
        )
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(self, "constraints", tuple(self.constraints))
        if any(
            not isinstance(item, ApprovalRequirement) for item in self.requirements
        ):
            raise SETTValidationError(
                "requirements must contain ApprovalRequirement instances."
            )
        if any(
            not isinstance(item, AuthorizationConstraint) for item in self.constraints
        ):
            raise SETTValidationError(
                "constraints must contain AuthorizationConstraint instances."
            )
        if self.verdict is AuthorizationVerdict.REQUIRE_APPROVAL:
            if self.receipt_use is not ReceiptUse.SINGLE_USE:
                raise SETTValidationError(
                    "REQUIRE_APPROVAL cannot authorize receipt replay."
                )
            if not self.requirements:
                raise SETTValidationError(
                    "REQUIRE_APPROVAL needs at least one declared requirement."
                )
            if self.constraints:
                raise SETTValidationError(
                    "REQUIRE_APPROVAL cannot carry grant constraints."
                )
        elif self.requirements:
            raise SETTValidationError(
                "Only REQUIRE_APPROVAL can carry approval requirements."
            )
        if self.verdict is AuthorizationVerdict.DENY:
            if self.constraints:
                raise SETTValidationError("DENY cannot carry grant constraints.")
            if self.receipt_use is not ReceiptUse.SINGLE_USE:
                raise SETTValidationError("DENY cannot authorize receipt replay.")

    @classmethod
    def grant(
        cls,
        policy_id: str,
        *,
        receipt_use: ReceiptUse = ReceiptUse.SINGLE_USE,
        reason_codes: Sequence[str] = (),
        constraints: Sequence[AuthorizationConstraint] = (),
    ) -> PolicyDecision:
        """Build a policy grant with verified constraints."""
        return cls(
            policy_id,
            AuthorizationVerdict.GRANT,
            receipt_use,
            tuple(reason_codes),
            constraints=tuple(constraints),
        )

    @classmethod
    def deny(
        cls,
        policy_id: str,
        *,
        reason_codes: Sequence[str] = (),
    ) -> PolicyDecision:
        """Build a terminal policy denial."""
        return cls(
            policy_id,
            AuthorizationVerdict.DENY,
            ReceiptUse.SINGLE_USE,
            tuple(reason_codes),
        )

    @classmethod
    def require_approval(
        cls,
        policy_id: str,
        *,
        requirements: Sequence[ApprovalRequirement],
        reason_codes: Sequence[str] = (),
    ) -> PolicyDecision:
        """Build a decision requiring predeclared additional authority."""
        return cls(
            policy_id,
            AuthorizationVerdict.REQUIRE_APPROVAL,
            ReceiptUse.SINGLE_USE,
            tuple(reason_codes),
            tuple(requirements),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """The deterministic aggregate of every exact registered policy."""

    verdict: AuthorizationVerdict
    policy_ids: tuple[str, ...]
    receipt_use: ReceiptUse = ReceiptUse.SINGLE_USE
    reason_codes: tuple[str, ...] = ()
    requirements: tuple[ApprovalRequirement, ...] = ()
    constraints: tuple[AuthorizationConstraint, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, AuthorizationVerdict):
            raise SETTValidationError("verdict must be an AuthorizationVerdict.")
        if not isinstance(self.receipt_use, ReceiptUse):
            raise SETTValidationError("receipt_use must be a ReceiptUse member.")
        object.__setattr__(
            self,
            "policy_ids",
            _validated_identifiers(self.policy_ids, field_name="policy_id"),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _validated_identifiers(self.reason_codes, field_name="reason_code"),
        )
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(self, "constraints", tuple(self.constraints))
        if not self.policy_ids:
            raise SETTValidationError("policy_ids cannot be empty.")
        if len(set(self.policy_ids)) != len(self.policy_ids):
            raise SETTValidationError("policy_ids cannot contain duplicates.")
        if any(
            not isinstance(item, ApprovalRequirement) for item in self.requirements
        ):
            raise SETTValidationError(
                "requirements must contain ApprovalRequirement instances."
            )
        if any(
            not isinstance(item, AuthorizationConstraint) for item in self.constraints
        ):
            raise SETTValidationError(
                "constraints must contain AuthorizationConstraint instances."
            )
        if self.verdict is AuthorizationVerdict.REQUIRE_APPROVAL:
            if self.receipt_use is not ReceiptUse.SINGLE_USE:
                raise SETTValidationError(
                    "REQUIRE_APPROVAL cannot authorize receipt replay."
                )
            if not self.requirements:
                raise SETTValidationError(
                    "REQUIRE_APPROVAL needs at least one declared requirement."
                )
            if self.constraints:
                raise SETTValidationError(
                    "REQUIRE_APPROVAL cannot carry grant constraints."
                )
        elif self.requirements:
            raise SETTValidationError(
                "Only REQUIRE_APPROVAL can carry approval requirements."
            )
        if self.verdict is AuthorizationVerdict.DENY:
            if self.constraints:
                raise SETTValidationError("DENY cannot carry grant constraints.")
            if self.receipt_use is not ReceiptUse.SINGLE_USE:
                raise SETTValidationError("DENY cannot authorize receipt replay.")


@dataclass(frozen=True, slots=True)
class AuthorizationReceipt:
    """A public receipt whose authority remains in issuer-owned store state."""

    receipt_id: str
    issuer_id: str
    action_id: str
    fingerprint_policy_id: str
    request_fingerprint: str
    policy_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    execution_mode: ExecutionMode
    receipt_use: ReceiptUse

    def __post_init__(self) -> None:
        for field_name in (
            "receipt_id",
            "issuer_id",
            "action_id",
            "fingerprint_policy_id",
            "request_fingerprint",
        ):
            validate_identifier(getattr(self, field_name), field=field_name)
        object.__setattr__(
            self,
            "policy_ids",
            _validated_identifiers(self.policy_ids, field_name="policy_id"),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _validated_identifiers(self.reason_codes, field_name="reason_code"),
        )
        object.__setattr__(
            self,
            "constraint_ids",
            _validated_identifiers(self.constraint_ids, field_name="constraint_id"),
        )
        if self.issued_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise SETTValidationError("receipt times must be timezone-aware.")
        if self.expires_at <= self.issued_at:
            raise SETTValidationError("expires_at must be later than issued_at.")
        if not isinstance(self.execution_mode, ExecutionMode):
            raise SETTValidationError("execution_mode must be an ExecutionMode.")
        if not isinstance(self.receipt_use, ReceiptUse):
            raise SETTValidationError("receipt_use must be a ReceiptUse member.")


@dataclass(frozen=True, slots=True)
class AuthorizationOutcome:
    """A decision plus the receipt issued only for a complete grant."""

    decision: AuthorizationDecision
    receipt: AuthorizationReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AuthorizationDecision):
            raise SETTValidationError("decision must be an AuthorizationDecision.")
        if self.receipt is not None and not isinstance(
            self.receipt,
            AuthorizationReceipt,
        ):
            raise SETTValidationError(
                "receipt must be an AuthorizationReceipt or None."
            )
        if self.decision.verdict is AuthorizationVerdict.GRANT:
            if self.receipt is None:
                raise SETTValidationError("A GRANT outcome must carry a receipt.")
        elif self.receipt is not None:
            raise SETTValidationError(
                "Only a GRANT outcome can carry an authorization receipt."
            )
