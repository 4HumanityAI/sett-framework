"""Reported condition verification values, without a verifier, producer or effect."""

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

from .conditions import ConditionValidityMode, PolicyCondition


class ConditionVerificationState(str, Enum):
    """A reported observation state, not applicability or final authorization."""

    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    EVIDENCE_NOT_PRESENT = "evidence_not_present"
    EVIDENCE_INVALID = "evidence_invalid"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


def _result_time(value: datetime | None) -> datetime | None:
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


def _result_identifiers(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    """Admit bounded unique identifiers and retain them as a sorted tuple."""
    if not isinstance(values, Sequence) or isinstance(
        values, str | bytes | bytearray | memoryview | Mapping
    ):
        raise ValueError
    size = len(values)
    if not 0 <= size <= MAX_JSON_ITEMS:
        raise ValueError
    items = tuple(islice(values, MAX_JSON_ITEMS + 1))
    if len(items) != size:
        raise ValueError
    for item in items:
        validate_identifier(item, field=field)
    if len(set(items)) != len(items):
        raise ValueError
    return tuple(sorted(items))


def _result_time_text(value: datetime | None) -> str | None:
    """Project a normalized UTC timestamp with six fractional digits and Z."""
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _verification_projection(
    condition: PolicyCondition,
    state: ConditionVerificationState,
    checked_at: datetime,
    valid_until: datetime | None,
    evidence_refs: tuple[str, ...],
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    """Build fresh full operational data, not a safe trace or authority token."""
    return {
        "condition": condition.canonical_view(),
        "state": state.value,
        "checked_at": _result_time_text(checked_at),
        "valid_until": _result_time_text(valid_until),
        "evidence_refs": list(evidence_refs),
        "reason_codes": list(reason_codes),
    }


def _validate_verification(
    verification: ConditionVerification,
) -> tuple[datetime, datetime | None, tuple[str, ...], tuple[str, ...]]:
    """Apply the scalar, nested, bounded, relational and aggregate checks in order."""
    state = verification.state
    if type(state) is not ConditionVerificationState:
        raise ValueError
    checked_at = _result_time(verification.checked_at)
    if checked_at is None:
        raise ValueError
    valid_until = _result_time(verification.valid_until)

    condition = verification.condition
    if type(condition) is not PolicyCondition:
        raise ValueError
    evidence_refs = _result_identifiers(verification.evidence_refs, field="evidence_ref")
    reason_codes = _result_identifiers(verification.reason_codes, field="reason_code")

    if state is ConditionVerificationState.SATISFIED:
        if not evidence_refs:
            raise ValueError
        if condition.validity_mode is ConditionValidityMode.RECHECK:
            if valid_until is not None:
                raise ValueError
        elif valid_until is None or not (
            checked_at < valid_until <= condition.attestation_not_after
        ):
            raise ValueError
    else:
        if not reason_codes or valid_until is not None:
            raise ValueError
        if state is ConditionVerificationState.EVIDENCE_NOT_PRESENT and evidence_refs:
            raise ValueError

    canonical_json_bytes(
        _verification_projection(
            condition, state, checked_at, valid_until, evidence_refs, reason_codes
        ),
        field="condition verification",
    )
    return checked_at, valid_until, evidence_refs, reason_codes


@dataclass(frozen=True, slots=True, kw_only=True)
class ConditionVerification:
    """Report one condition observation; this value governs no decision by itself.

    A structurally valid value is not current evidence, a receipt, a policy
    decision or an institutional conclusion, and constructing it confers no
    authority. Creating this value installs no verifier or producer and calls
    nothing. The configured condition producer admits it only against the
    inventory it retained for the invocation.
    """

    condition: PolicyCondition
    state: ConditionVerificationState
    checked_at: datetime
    valid_until: datetime | None
    evidence_refs: Sequence[str]
    reason_codes: Sequence[str]

    def __post_init__(self) -> None:
        valid = False
        try:
            checked_at, valid_until, evidence_refs, reason_codes = (
                _validate_verification(self)
            )
            valid = True
        except Exception:  # noqa: BLE001 -- result validation; no raw cause/context
            pass
        # Leave the raw validation handler before creating the public diagnostic.
        if not valid:
            error = SETTPolicyConfigurationError(
                "The condition verification result is invalid.",
                reason_code="policy.condition_verification_invalid",
            )
            try:
                raise error
            finally:
                # Python chains an exception the caller is still handling; drop it.
                error.__context__ = None
        object.__setattr__(self, "checked_at", checked_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "reason_codes", reason_codes)

    def canonical_view(self) -> dict[str, object]:
        """Return detached full observation data, without a validity assertion."""
        return _verification_projection(
            self.condition, self.state, self.checked_at, self.valid_until,
            self.evidence_refs, self.reason_codes,
        )
