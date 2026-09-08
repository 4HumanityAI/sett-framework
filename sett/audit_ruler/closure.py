"""Audit closure as an outcome axis independent from execution status."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from sett.exceptions import SETTValidationError


class AuditClosureStatus(str, Enum):
    """Whether requested durable audit closure was established."""

    NOT_REQUESTED = "not_requested"
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNCONFIRMED = "unconfirmed"


_GAP_TOKEN_ISSUER = object()


class AuditGapToken:
    """An in-process capability proving SETT observed a known audit gap."""

    __slots__ = ("_boundary_event_id", "_sealed", "_token_id")

    def __init__(
        self,
        token_id: str,
        boundary_event_id: str,
        *,
        _issuer: object | None = None,
    ) -> None:
        """Reject construction outside the framework's issuance path."""
        if _issuer is not _GAP_TOKEN_ISSUER:
            raise SETTValidationError(
                "AuditGapToken instances can only be issued by SETT."
            )
        object.__setattr__(self, "_token_id", token_id)
        object.__setattr__(self, "_boundary_event_id", boundary_event_id)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("AuditGapToken is immutable.")
        object.__setattr__(self, name, value)

    @property
    def token_id(self) -> str:
        """Return the opaque identity of this process observation."""
        return self._token_id

    @property
    def boundary_event_id(self) -> str:
        """Return the boundary SETT observed without durable closure."""
        return self._boundary_event_id


def _issue_gap_token(boundary_event_id: str) -> AuditGapToken:
    if not isinstance(boundary_event_id, str) or not boundary_event_id:
        raise SETTValidationError(
            "An audit gap requires a non-empty boundary_event_id."
        )
    return AuditGapToken(
        str(uuid4()),
        boundary_event_id,
        _issuer=_GAP_TOKEN_ISSUER,
    )


@dataclass(frozen=True, slots=True)
class AuditClosure:
    """Audit state kept separate from the truth of the execution outcome."""

    status: AuditClosureStatus
    reason_code: str | None = None
    boundary_event_id: str | None = None
    gap_token: AuditGapToken | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AuditClosureStatus):
            raise SETTValidationError(
                "AuditClosure.status must be an AuditClosureStatus member."
            )
        if self.status in {
            AuditClosureStatus.NOT_REQUESTED,
            AuditClosureStatus.SATISFIED,
        } and (
            self.reason_code is not None
            or self.boundary_event_id is not None
            or self.gap_token is not None
        ):
            raise SETTValidationError(
                "A closed audit state cannot carry gap information."
            )
        if self.status in {
            AuditClosureStatus.UNSATISFIED,
            AuditClosureStatus.UNCONFIRMED,
        }:
            if not self.reason_code or not self.boundary_event_id:
                raise SETTValidationError(
                    "An audit closure gap requires a reason and boundary."
                )
            if (
                not isinstance(self.gap_token, AuditGapToken)
                or self.gap_token.boundary_event_id
                != self.boundary_event_id
            ):
                raise SETTValidationError(
                    "An audit closure gap requires its SETT-issued token."
                )

    @classmethod
    def not_requested(cls) -> AuditClosure:
        """No durable closure was configured for this execution."""
        return cls(AuditClosureStatus.NOT_REQUESTED)

    @classmethod
    def satisfied(cls) -> AuditClosure:
        """Every requested terminal assertion reached required durability."""
        return cls(AuditClosureStatus.SATISFIED)

    @classmethod
    def unsatisfied(
        cls,
        *,
        reason_code: str = "audit.terminal_not_durable",
        boundary_event_id: str,
    ) -> AuditClosure:
        """The terminal assertion is known not to satisfy durability."""
        return cls(
            AuditClosureStatus.UNSATISFIED,
            reason_code=reason_code,
            boundary_event_id=boundary_event_id,
            gap_token=_issue_gap_token(boundary_event_id),
        )

    @classmethod
    def unconfirmed(
        cls,
        *,
        reason_code: str = "audit.terminal_durability_unconfirmed",
        boundary_event_id: str,
    ) -> AuditClosure:
        """SETT could not determine whether the terminal became durable."""
        return cls(
            AuditClosureStatus.UNCONFIRMED,
            reason_code=reason_code,
            boundary_event_id=boundary_event_id,
            gap_token=_issue_gap_token(boundary_event_id),
        )
