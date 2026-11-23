"""Bounded condition declarations, without condition verification or enforcement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import islice

from sett._governance.validation import (
    MAX_JSON_ITEMS,
    canonical_json_bytes,
    validate_identifier,
)
from sett.exceptions import SETTPolicyConfigurationError

from .models import PolicyBinding, PolicyOrigin


class ConditionValidityMode(str, Enum):
    """An explicit declaration mode, not proof of current condition validity."""

    RECHECK = "recheck"
    ATTESTATION = "attestation"


def _condition_time(value: datetime | None) -> datetime | None:
    """Normalize an optional aware timestamp without consulting a clock."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError
    utc = value.astimezone(timezone.utc)
    return datetime(
        utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second,
        utc.microsecond, tzinfo=timezone.utc,
    )


def _condition_origins(values: Sequence[PolicyOrigin]) -> tuple[PolicyOrigin, ...]:
    """Admit exact values after bounded traversal, before projecting any origin."""
    if not isinstance(values, Sequence) or isinstance(
        values, str | bytes | bytearray | memoryview | Mapping
    ):
        raise ValueError
    size = len(values)
    if not 1 <= size <= MAX_JSON_ITEMS:
        raise ValueError
    items = tuple(islice(values, MAX_JSON_ITEMS + 1))
    if len(items) != size or any(type(item) is not PolicyOrigin for item in items):
        raise ValueError
    return items


def _condition_subset(
    binding: PolicyBinding, origins: tuple[PolicyOrigin, ...]
) -> tuple[PolicyOrigin, ...]:
    """Check complete provenance and retain the explicit subset in binding order."""
    keys = tuple(
        canonical_json_bytes(origin.canonical_view(), field="condition origin")
        for origin in origins
    )
    selected = set(keys)
    if len(selected) != len(keys):
        raise ValueError
    bound = tuple(
        (canonical_json_bytes(origin.canonical_view(), field="binding origin"), origin)
        for origin in binding.origins
    )
    if not selected.issubset(key for key, _ in bound):
        raise ValueError
    return tuple(origin for key, origin in bound if key in selected)


def _condition_projection(
    condition: PolicyCondition,
    origins: tuple[PolicyOrigin, ...],
    timestamp: datetime | None,
) -> dict[str, object]:
    """Build fresh full operational data, not a safe trace or authority token."""
    return {
        "condition_id": condition.condition_id,
        "binding": condition.binding.canonical_view(),
        "origins": [origin.canonical_view() for origin in origins],
        "verifier_id": condition.verifier_id,
        "verifier_version": condition.verifier_version,
        "required": condition.required,
        "action_id": condition.action_id,
        "action_type": condition.action_type,
        "checkpoint_id": condition.checkpoint_id,
        "validity_mode": condition.validity_mode.value,
        "attestation_not_after": (
            timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
            if timestamp is not None else None
        ),
    }


def _validate_condition(
    condition: PolicyCondition,
) -> tuple[tuple[PolicyOrigin, ...], datetime | None]:
    """Apply the scalar, nested, relational and aggregate checks in order."""
    for name in ("condition_id", "verifier_id", "verifier_version"):
        validate_identifier(getattr(condition, name), field=name)
    if type(condition.required) is not bool:
        raise ValueError
    for name in ("action_id", "action_type", "checkpoint_id"):
        validate_identifier(getattr(condition, name), field=name)
    if type(condition.validity_mode) is not ConditionValidityMode:
        raise ValueError
    timestamp = _condition_time(condition.attestation_not_after)

    if type(condition.binding) is not PolicyBinding:
        raise ValueError
    origins = _condition_origins(condition.origins)
    origins = _condition_subset(condition.binding, origins)
    if (condition.validity_mode is ConditionValidityMode.RECHECK) != (timestamp is None):
        raise ValueError
    canonical_json_bytes(
        _condition_projection(condition, origins, timestamp), field="policy condition"
    )
    return origins, timestamp


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyCondition:
    """Declare a check; this value verifies nothing by itself.

    A declaration is not a verifier, verification result, producer,
    attestation, registered checkpoint or permission for an effect. Named
    dependencies are not resolved or executed. During evaluation the policy
    layer checks each declaration against its policy's binding, the request
    and the entry checkpoint or a declared controlled checkpoint, and the
    required declarations of required
    policies form the inventory that configured verifiers must verify.
    """

    condition_id: str
    binding: PolicyBinding
    origins: Sequence[PolicyOrigin]
    verifier_id: str
    verifier_version: str
    required: bool
    action_id: str
    action_type: str
    checkpoint_id: str
    validity_mode: ConditionValidityMode
    attestation_not_after: datetime | None

    def __post_init__(self) -> None:
        valid = False
        try:
            origins, timestamp = _validate_condition(self)
            valid = True
        except Exception:  # noqa: BLE001 -- constructor validation; no raw cause/context
            pass
        # Leave the raw validation handler before creating the public diagnostic.
        if not valid:
            raise SETTPolicyConfigurationError(
                "The policy condition declaration is invalid.",
                reason_code="policy.condition_invalid",
            )
        object.__setattr__(self, "origins", origins)
        object.__setattr__(self, "attestation_not_after", timestamp)

    def canonical_view(self) -> dict[str, object]:
        """Return detached full declaration data, without a validity assertion."""
        return _condition_projection(self, self.origins, self.attestation_not_after)
