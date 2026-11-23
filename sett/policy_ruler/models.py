"""Policy values: metadata, selection and the policy decision.

Metadata and selection values decide nothing by themselves: constructing
them selects no policy, verifies no evidence and authorizes no effect.
``PolicyRuler`` reads a declared selection before any policy runs.
``PolicyDecision`` is the result of a policy callback and requires explicit
condition and review declarations. Declaring conditions verifies nothing
and authorizes no effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import TYPE_CHECKING

from sett._governance.validation import (
    MAX_JSON_ITEMS,
    _validated_identifiers,
    canonical_json_bytes,
    validate_identifier,
)
from sett._governance.values import (
    ApprovalRequirement,
    AuthorizationConstraint,
    FieldValue,
    ReceiptUse,
)
from sett._governance.verdict import AuthorizationVerdict
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTPolicyConfigurationError,
    SETTValidationError,
)
from sett.policy_ruler.review import PolicyReviewObligation

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from sett.policy_ruler.conditions import PolicyCondition


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One deterministic policy result with its explicit condition declaration.

    ``conditions`` is mandatory: an empty sequence states that the decision
    carries no conditions, and omitting it is a construction error. A declared
    condition is not a constraint; declaring conditions runs no verifier,
    produces no observation and authorizes no effect. The representation shows
    each declaration's full provenance and is not a view for public exposure.
    """

    policy_id: str
    verdict: AuthorizationVerdict
    receipt_use: ReceiptUse = ReceiptUse.SINGLE_USE
    reason_codes: tuple[str, ...] = ()
    requirements: tuple[ApprovalRequirement, ...] = ()
    constraints: tuple[AuthorizationConstraint, ...] = ()
    conditions: Sequence[PolicyCondition] = field(kw_only=True)
    review_obligations: Sequence[PolicyReviewObligation] = field(kw_only=True)

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
        if any(not isinstance(item, ApprovalRequirement) for item in self.requirements):
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
        # Imported here: sett.policy_ruler.conditions imports this module at load.
        from sett.policy_ruler.conditions import PolicyCondition

        conditions: tuple[PolicyCondition, ...] = ()
        valid = False
        try:
            conditions = _policy_conditions(self.conditions, self.policy_id, PolicyCondition)
            valid = True
        except Exception:  # noqa: BLE001 -- inventory validation; no raw cause/context
            pass
        # Leave the raw validation handler before creating the public diagnostic.
        if not valid:
            error = SETTPolicyConfigurationError(
                "The policy condition inventory is invalid.",
                reason_code="policy.condition_inventory_invalid",
            )
            try:
                raise error
            finally:
                # Python chains an exception the caller is still handling; drop it.
                error.__context__ = None
        object.__setattr__(self, "conditions", conditions)
        review_obligations = _policy_review_obligations(
            self.review_obligations, self.policy_id
        )
        object.__setattr__(self, "review_obligations", review_obligations)

    @classmethod
    def grant(
        cls,
        policy_id: str,
        *,
        receipt_use: ReceiptUse = ReceiptUse.SINGLE_USE,
        reason_codes: Sequence[str] = (),
        constraints: Sequence[AuthorizationConstraint] = (),
        conditions: Sequence[PolicyCondition],
        review_obligations: Sequence[PolicyReviewObligation],
    ) -> PolicyDecision:
        """Build a policy grant with explicit condition and review declarations.

        Both declarations are forwarded unchanged; the constructor validates them.
        """
        return cls(
            policy_id,
            AuthorizationVerdict.GRANT,
            receipt_use,
            tuple(reason_codes),
            constraints=tuple(constraints),
            conditions=conditions,
            review_obligations=review_obligations,
        )

    @classmethod
    def deny(
        cls,
        policy_id: str,
        *,
        reason_codes: Sequence[str] = (),
        conditions: Sequence[PolicyCondition],
        review_obligations: Sequence[PolicyReviewObligation],
    ) -> PolicyDecision:
        """Build a terminal policy denial with explicit review declarations.

        Both declarations are forwarded unchanged; the constructor validates them.
        """
        return cls(
            policy_id,
            AuthorizationVerdict.DENY,
            ReceiptUse.SINGLE_USE,
            tuple(reason_codes),
            conditions=conditions,
            review_obligations=review_obligations,
        )

    @classmethod
    def require_approval(
        cls,
        policy_id: str,
        *,
        requirements: Sequence[ApprovalRequirement],
        reason_codes: Sequence[str] = (),
        conditions: Sequence[PolicyCondition],
        review_obligations: Sequence[PolicyReviewObligation],
    ) -> PolicyDecision:
        """Build a decision requiring predeclared additional authority.

        Both declarations are forwarded unchanged; the constructor validates them.
        """
        return cls(
            policy_id,
            AuthorizationVerdict.REQUIRE_APPROVAL,
            ReceiptUse.SINGLE_USE,
            tuple(reason_codes),
            tuple(requirements),
            conditions=conditions,
            review_obligations=review_obligations,
        )


def _policy_conditions(
    values: Sequence[PolicyCondition],
    policy_id: str,
    condition_type: type[PolicyCondition],
) -> tuple[PolicyCondition, ...]:
    """Admit one decision's bounded condition declaration; verify nothing.

    Each entry is revalidated through its canonical view and canonical bounds;
    the aggregate bound covers the collection only.
    """
    if not isinstance(values, Sequence) or isinstance(
        values, str | bytes | bytearray | memoryview | Mapping
    ):
        raise ValueError
    size = len(values)
    if size > MAX_JSON_ITEMS:
        raise ValueError
    # Bound traversal even when a custom sequence misreports its length.
    items = tuple(islice(values, MAX_JSON_ITEMS + 1))
    if len(items) != size or any(type(item) is not condition_type for item in items):
        raise ValueError
    rows = []
    for item in items:
        view = item.canonical_view()
        canonical_json_bytes(view, field="policy condition")
        binding = view["binding"]
        key = (binding["policy_id"], binding["policy_version"], view["condition_id"])
        if key[0] != policy_id:
            raise ValueError
        rows.append((key, item, view))
    if len({key for key, _, _ in rows}) != len(rows):
        raise ValueError
    rows.sort(key=lambda row: row[0])
    canonical_json_bytes([view for _, _, view in rows], field="policy conditions")
    return tuple(item for _, item, _ in rows)


def _policy_review_obligations(
    values: Sequence[PolicyReviewObligation], policy_id: str
) -> tuple[PolicyReviewObligation, ...]:
    code = "policy.review_obligation_invalid"
    try:
        items = _selection_sequence(values, name="review_obligations", code=code)
    except SETTAuthorizationConfigurationError:
        _raise_review_obligation_invalid()
    if any(type(item) is not PolicyReviewObligation for item in items):
        _raise_review_obligation_invalid()
    identifiers = tuple(item.obligation_id for item in items)
    if (
        len(set(identifiers)) != len(identifiers)
        or any(item.binding.policy_id != policy_id for item in items)
    ):
        _raise_review_obligation_invalid()
    return items


def _raise_review_obligation_invalid() -> None:
    error = SETTPolicyConfigurationError(
        "The policy review obligation declaration is invalid.",
        reason_code="policy.review_obligation_invalid",
    )
    try:
        raise error
    finally:
        error.__context__ = None


def _policy_identifier(value: str, *, field_name: str, code: str) -> None:
    try:
        validate_identifier(value, field=field_name)
        return
    except (SETTValidationError, UnicodeError):
        pass
    # Raise outside the handler so raw validation exceptions are not retained.
    raise SETTAuthorizationConfigurationError(
        f"{field_name} must be a valid bounded identifier.", reason_code=code
    )


def _policy_text(value: str, *, field_name: str, code: str) -> None:
    valid = isinstance(value, str) and bool(value) and value == value.strip()
    if valid:
        valid = not any(ord(char) < 32 or ord(char) == 127 for char in value)
    if valid:
        try:
            valid = len(value.encode("utf-8")) <= 1024
        except UnicodeError:
            valid = False
    if not valid:
        raise SETTAuthorizationConfigurationError(
            f"{field_name} must be trimmed text within its 1024-byte limit.",
            reason_code=code,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySourceReference:
    """A versioned provision reference, without retrieval or legal validation."""

    source_id: str
    source_version: str
    locator: str

    def __post_init__(self) -> None:
        code = "authorization.policy_source_invalid"
        _policy_identifier(self.source_id, field_name="source_id", code=code)
        _policy_identifier(self.source_version, field_name="source_version", code=code)
        _policy_text(self.locator, field_name="locator", code=code)

    def canonical_view(self) -> dict[str, object]:
        """Return a fresh complete operational metadata projection."""
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "locator": self.locator,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyOrigin:
    """One requirement revision and layer with explicit source provenance."""

    requirement_id: str
    requirement_revision: str
    layer_id: str
    source: PolicySourceReference | None
    source_absence_reason: str | None

    def __post_init__(self) -> None:
        code = "authorization.policy_origin_invalid"
        for name in ("requirement_id", "requirement_revision", "layer_id"):
            _policy_identifier(getattr(self, name), field_name=name, code=code)
        if self.source is not None and type(self.source) is not PolicySourceReference:
            raise SETTAuthorizationConfigurationError(
                "source must be an exact PolicySourceReference or None.",
                reason_code=code,
            )
        if self.source_absence_reason is not None:
            _policy_text(
                self.source_absence_reason,
                field_name="source_absence_reason",
                code=code,
            )
        if (self.source is None) == (self.source_absence_reason is None):
            raise SETTAuthorizationConfigurationError(
                "Declare either a source or a source_absence_reason.", reason_code=code
            )

    def canonical_view(self) -> dict[str, object]:
        """Return all origin fields, including explicit source absence."""
        return {
            "requirement_id": self.requirement_id,
            "requirement_revision": self.requirement_revision,
            "layer_id": self.layer_id,
            "source": self.source.canonical_view() if self.source is not None else None,
            "source_absence_reason": self.source_absence_reason,
        }


def _policy_origins(values: Sequence[PolicyOrigin]) -> tuple[PolicyOrigin, ...]:
    code = "authorization.policy_binding_invalid"
    if not isinstance(values, Sequence) or isinstance(
        values, str | bytes | bytearray | memoryview
    ):
        raise SETTAuthorizationConfigurationError(
            "origins must be a finite sequence of PolicyOrigin values.",
            reason_code=code,
        )
    detached: tuple[PolicyOrigin, ...] = ()
    valid = False
    try:
        size = len(values)
        if 1 <= size <= MAX_JSON_ITEMS:
            # Bound traversal even when a custom sequence misreports its length.
            detached = tuple(islice(values, MAX_JSON_ITEMS + 1))
            valid = len(detached) == size
    except Exception:  # noqa: BLE001 -- caller-owned sequence boundary, fixed diagnostic
        pass
    if not valid or any(type(item) is not PolicyOrigin for item in detached):
        raise SETTAuthorizationConfigurationError(
            "origins must contain 1 to 256 exact PolicyOrigin values.", reason_code=code
        )
    return detached


def _policy_origin_key(
    origin: PolicyOrigin,
) -> tuple[str, str, str, bool, str, str, str]:
    source = origin.source
    return (
        origin.layer_id,
        origin.requirement_id,
        origin.requirement_revision,
        source is not None,
        source.source_id if source is not None else "",
        source.source_version if source is not None else "",
        source.locator if source is not None else "",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyBinding:
    """An immutable policy identity bound to its declared provenance."""

    policy_id: str
    policy_version: str
    contract_version: str
    origins: Sequence[PolicyOrigin]
    profile_id: str | None
    profile_revision: str | None

    def __post_init__(self) -> None:
        code = "authorization.policy_binding_invalid"
        for name in ("policy_id", "policy_version", "contract_version"):
            _policy_identifier(getattr(self, name), field_name=name, code=code)
        for name in ("profile_id", "profile_revision"):
            value = getattr(self, name)
            if value is not None:
                _policy_identifier(value, field_name=name, code=code)
        origins = _policy_origins(self.origins)
        if self.contract_version != "2.0":
            raise SETTAuthorizationConfigurationError(
                "The policy contract version is unsupported.",
                reason_code="authorization.policy_contract_unsupported",
            )
        if (self.profile_id is None) != (self.profile_revision is None):
            raise SETTAuthorizationConfigurationError(
                "profile_id and profile_revision must be declared together.",
                reason_code=code,
            )
        revisions: dict[tuple[str, str], str] = {}
        links: set[tuple[str, str, str | None, str | None, str | None]] = set()
        for origin in origins:
            requirement = (origin.layer_id, origin.requirement_id)
            revision = revisions.setdefault(requirement, origin.requirement_revision)
            if revision != origin.requirement_revision:
                raise SETTAuthorizationConfigurationError(
                    "A requirement in one layer cannot have conflicting revisions.",
                    reason_code=code,
                )
            source = origin.source
            link = (
                *requirement,
                source.source_id if source is not None else None,
                source.source_version if source is not None else None,
                source.locator if source is not None else None,
            )
            if link in links:
                raise SETTAuthorizationConfigurationError(
                    "origins cannot contain duplicate provenance links.",
                    reason_code=code,
                )
            links.add(link)
        object.__setattr__(
            self, "origins", tuple(sorted(origins, key=_policy_origin_key))
        )
        try:
            canonical_json_bytes(
                PolicyBinding.canonical_view(self), field="policy binding"
            )
            return
        except (SETTValidationError, UnicodeError):
            pass
        raise SETTAuthorizationConfigurationError(
            "The policy binding exceeds canonical JSON limits.", reason_code=code
        )

    def canonical_view(self) -> dict[str, object]:
        """Return a detached full value for comparison or recording, not authority."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "contract_version": self.contract_version,
            "origins": [origin.canonical_view() for origin in self.origins],
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
        }


class PolicyApplicabilityState(str, Enum):
    """Application-declared applicability, without a legal determination."""

    APPLIES = "applies"
    DOES_NOT_APPLY = "does_not_apply"
    UNDETERMINED = "undetermined"


def _selection_sequence(values: Sequence, *, name: str, code: str) -> tuple:
    """Detach a bounded sequence without trusting a custom length or iterator."""
    detached: tuple = ()
    valid = False
    if isinstance(values, Sequence) and not isinstance(
        values, str | bytes | bytearray | memoryview | Mapping
    ):
        try:
            size = len(values)
            if 0 <= size <= MAX_JSON_ITEMS:
                detached = tuple(islice(values, MAX_JSON_ITEMS + 1))
                valid = len(detached) == size
        except Exception:  # noqa: BLE001 -- caller container, fixed diagnostic
            pass
    if not valid:
        raise SETTAuthorizationConfigurationError(
            f"{name} must be a finite sequence of at most 256 entries.",
            reason_code=code,
        )
    return detached


def _selection_json_limit(value: dict[str, object], *, code: str) -> None:
    try:
        canonical_json_bytes(value, field="policy selection metadata")
        return
    except (SETTValidationError, UnicodeError):
        pass
    raise SETTAuthorizationConfigurationError(
        "The complete policy metadata exceeds canonical JSON limits.", reason_code=code
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyApplicability:
    """A declared applicability record; it decides nothing by itself."""

    policy_id: str
    policy_version: str
    state: PolicyApplicabilityState
    justification: str
    fact_names: Sequence[str]
    evidence_refs: Sequence[str]

    def __post_init__(self) -> None:
        code = "authorization.policy_applicability_invalid"
        _policy_identifier(self.policy_id, field_name="policy_id", code=code)
        _policy_identifier(self.policy_version, field_name="policy_version", code=code)
        if type(self.state) is not PolicyApplicabilityState:
            raise SETTAuthorizationConfigurationError(
                "state must be an exact PolicyApplicabilityState member.",
                reason_code=code,
            )
        _policy_text(self.justification, field_name="justification", code=code)
        collections = {}
        for name in ("fact_names", "evidence_refs"):
            items = _selection_sequence(getattr(self, name), name=name, code=code)
            for item in items:
                _policy_identifier(item, field_name=name, code=code)
            collections[name] = items
        for name, items in collections.items():
            if len(set(items)) != len(items):
                raise SETTAuthorizationConfigurationError(
                    f"{name} cannot contain duplicate identifiers.", reason_code=code
                )
            object.__setattr__(self, name, tuple(sorted(items)))
        _selection_json_limit(PolicyApplicability.canonical_view(self), code=code)

    def canonical_view(self) -> dict[str, object]:
        """Return full detached operational data, not a public-safe projection."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "state": self.state.value,
            "justification": self.justification,
            "fact_names": list(self.fact_names),
            "evidence_refs": list(self.evidence_refs),
        }


def _selection_time(value: datetime, *, code: str) -> datetime:
    normalized = None
    if isinstance(value, datetime):
        try:
            if value.utcoffset() is not None:
                normalized = value.astimezone(timezone.utc)
        except Exception:  # noqa: BLE001 -- caller timezone, fixed diagnostic
            pass
    if normalized is None:
        raise SETTAuthorizationConfigurationError(
            "effective_at must be a valid timezone-aware datetime.", reason_code=code
        )
    return normalized


def _selection_facts(
    values: Mapping[str, FieldValue | None], *, code: str
) -> Mapping[str, FieldValue | None]:
    detached = None
    if isinstance(values, Mapping):
        try:
            size = len(values)
            if 0 <= size <= MAX_JSON_ITEMS:
                keys = tuple(islice(values, MAX_JSON_ITEMS + 1))
                if len(keys) == size:
                    # Check identifiers before sorting or invoking key hashing.
                    for key in keys:
                        _policy_identifier(key, field_name="fact name", code=code)
                    if len(set(keys)) == size:
                        detached = {key: values[key] for key in sorted(keys)}
        except Exception:  # noqa: BLE001 -- caller mapping, fixed diagnostic
            pass
    if detached is None or any(
        item is not None and type(item) is not FieldValue for item in detached.values()
    ):
        raise SETTAuthorizationConfigurationError(
            "facts must map up to 256 unique identifiers to exact FieldValue or None.",
            reason_code=code,
        )
    return MappingProxyType(detached)


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySelection:
    """An exact-action declaration of applicable policies, read by PolicyRuler before any policy runs."""

    selection_id: str
    selection_revision: str
    contract_version: str
    authority_ref: str
    effective_at: datetime
    action_id: str
    action_type: str
    required: Sequence[PolicyBinding]
    informative: Sequence[PolicyBinding]
    applicability: Sequence[PolicyApplicability]
    facts: Mapping[str, FieldValue | None]

    def __post_init__(self) -> None:
        code = "authorization.policy_selection_invalid"
        for name in (
            "selection_id",
            "selection_revision",
            "contract_version",
            "authority_ref",
        ):
            _policy_identifier(getattr(self, name), field_name=name, code=code)
        effective_at = _selection_time(self.effective_at, code=code)
        for name in ("action_id", "action_type"):
            _policy_identifier(getattr(self, name), field_name=name, code=code)
        if self.contract_version != "2.0":
            raise SETTAuthorizationConfigurationError(
                "The policy contract version is unsupported.",
                reason_code="authorization.policy_contract_unsupported",
            )
        collections = {}
        for name, model in (
            ("required", PolicyBinding),
            ("informative", PolicyBinding),
            ("applicability", PolicyApplicability),
        ):
            items = _selection_sequence(getattr(self, name), name=name, code=code)
            if any(type(item) is not model for item in items):
                raise SETTAuthorizationConfigurationError(
                    f"{name} must contain exact {model.__name__} values.",
                    reason_code=code,
                )
            collections[name] = items
        facts = _selection_facts(self.facts, code=code)
        bindings = collections["required"] + collections["informative"]
        if len(bindings) > MAX_JSON_ITEMS:
            raise SETTAuthorizationConfigurationError(
                "required and informative together cannot exceed 256 bindings.",
                reason_code=code,
            )
        ids = [item.policy_id for item in bindings]
        if len(set(ids)) != len(ids):
            raise SETTAuthorizationConfigurationError(
                "The inventories cannot repeat a policy ID, including other versions.",
                reason_code=code,
            )
        expected = {(item.policy_id, item.policy_version) for item in bindings}
        actual = [
            (item.policy_id, item.policy_version)
            for item in collections["applicability"]
        ]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise SETTAuthorizationConfigurationError(
                "Each inventory binding requires exactly one matching applicability.",
                reason_code=code,
            )
        referenced = {
            name for item in collections["applicability"] for name in item.fact_names
        }
        if referenced != set(facts):
            raise SETTAuthorizationConfigurationError(
                "facts must contain exactly the referenced names; use None for missing facts.",
                reason_code=code,
            )
        if any(item.contract_version != self.contract_version for item in bindings):
            raise SETTAuthorizationConfigurationError(
                "Every binding must use the selected policy contract version.",
                reason_code=code,
            )
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "facts", facts)
        for name, items in collections.items():
            object.__setattr__(
                self,
                name,
                tuple(
                    sorted(
                        items, key=lambda item: (item.policy_id, item.policy_version)
                    )
                ),
            )
        _selection_json_limit(PolicySelection.canonical_view(self), code=code)

    def canonical_view(self) -> dict[str, object]:
        """Return full data for comparison, not authority or a public trace."""
        return {
            "selection_id": self.selection_id,
            "selection_revision": self.selection_revision,
            "contract_version": self.contract_version,
            "authority_ref": self.authority_ref,
            "effective_at": self.effective_at.isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            "action_id": self.action_id,
            "action_type": self.action_type,
            "required": [item.canonical_view() for item in self.required],
            "informative": [item.canonical_view() for item in self.informative],
            "applicability": [item.canonical_view() for item in self.applicability],
            "facts": {
                key: value.canonical_view() if value is not None else None
                for key, value in self.facts.items()
            },
        }
