"""Immutable policy review declarations and application-reported fact values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from sett._governance.validation import validate_identifier
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTPolicyConfigurationError,
    SETTValidationError,
)

if TYPE_CHECKING:
    from sett.policy_ruler.models import PolicyBinding


_REVIEW_INVALID_CODE = "policy.review_obligation_invalid"
_REVIEW_INVALID_MESSAGE = "The policy review obligation declaration is invalid."


def _raise_review_invalid() -> None:
    error = SETTPolicyConfigurationError(
        _REVIEW_INVALID_MESSAGE, reason_code=_REVIEW_INVALID_CODE
    )
    try:
        raise error
    finally:
        error.__context__ = None


def _identifier(value: str, *, field_name: str) -> None:
    try:
        validate_identifier(value, field=field_name)
        return
    except (SETTValidationError, UnicodeError):
        pass
    _raise_review_invalid()


def _binding_type() -> type:
    from sett.policy_ruler.models import PolicyBinding

    return PolicyBinding


def _bounded_sequence(values: Sequence, *, name: str) -> tuple:
    from sett.policy_ruler.models import _selection_sequence

    try:
        return _selection_sequence(values, name=name, code=_REVIEW_INVALID_CODE)
    except SETTAuthorizationConfigurationError:
        _raise_review_invalid()


class ReviewMode(str, Enum):
    """A declared review obligation modality; no lifecycle is run here."""

    NOTIFY_AFTER_ACTION = "notify_after_action"
    REQUIRE_HUMAN_REVIEW_WITH_DEADLINE = "require_human_review_with_deadline"
    RESTRICT_DECLARED_FUTURE_ACTIONS = "restrict_declared_future_actions"


class ReviewOutcome(str, Enum):
    """The reported outcome of a completed review."""

    FAVORABLE = "favorable"
    UNFAVORABLE = "unfavorable"


class DeadlineStatus(str, Enum):
    """An implementation-reported deadline state, not a framework clock."""

    NOT_OVERDUE = "not_overdue"
    OVERDUE = "overdue"


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyReviewObligation:
    """One policy-owned review declaration with implementation-owned facts."""

    obligation_id: str
    binding: PolicyBinding
    mode: ReviewMode
    verifier_id: str
    verifier_version: str
    action_id: str
    action_type: str
    future_action_types: Sequence[str] = ()

    def __post_init__(self) -> None:
        for name in (
            "obligation_id",
            "verifier_id",
            "verifier_version",
            "action_id",
            "action_type",
        ):
            _identifier(getattr(self, name), field_name=name)
        if type(self.binding) is not _binding_type():
            _raise_review_invalid()
        if type(self.mode) is not ReviewMode:
            _raise_review_invalid()
        action_types = _bounded_sequence(
            self.future_action_types, name="future_action_types"
        )
        for action_type in action_types:
            _identifier(action_type, field_name="future_action_types")
        if len(set(action_types)) != len(action_types):
            _raise_review_invalid()
        restricted = self.mode is ReviewMode.RESTRICT_DECLARED_FUTURE_ACTIONS
        if restricted != bool(action_types):
            _raise_review_invalid()
        object.__setattr__(self, "future_action_types", action_types)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewVerificationRequest:
    """A verifier input naming one obligation, action and opaque evidence ref."""

    obligation: PolicyReviewObligation
    action_id: str
    action_type: str
    evidence_ref: str

    def __post_init__(self) -> None:
        if type(self.obligation) is not PolicyReviewObligation:
            _raise_review_invalid()
        for name in ("action_id", "action_type", "evidence_ref"):
            _identifier(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewCompletedFact:
    """One reported review outcome; it does not itself grant authority."""

    evidence_ref: str
    outcome: ReviewOutcome

    def __post_init__(self) -> None:
        _identifier(self.evidence_ref, field_name="evidence_ref")
        if type(self.outcome) is not ReviewOutcome:
            _raise_review_invalid()


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationDeliveredFact:
    """One reported notification delivery, distinct from review completion."""

    evidence_ref: str

    def __post_init__(self) -> None:
        _identifier(self.evidence_ref, field_name="evidence_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeadlineStatusFact:
    """An implementation-reported status with no date or time value."""

    evidence_ref: str
    status: DeadlineStatus

    def __post_init__(self) -> None:
        _identifier(self.evidence_ref, field_name="evidence_ref")
        if type(self.status) is not DeadlineStatus:
            _raise_review_invalid()


ReviewFact = ReviewCompletedFact | NotificationDeliveredFact | DeadlineStatusFact


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewVerification:
    """A reported review fact set; membership and authority remain external."""

    obligation_id: str
    binding: PolicyBinding
    action_id: str
    action_type: str
    facts: Sequence[ReviewFact]

    def __post_init__(self) -> None:
        for name in ("obligation_id", "action_id", "action_type"):
            _identifier(getattr(self, name), field_name=name)
        if type(self.binding) is not _binding_type():
            _raise_review_invalid()
        facts = _bounded_sequence(self.facts, name="facts")
        fact_types = (
            ReviewCompletedFact,
            NotificationDeliveredFact,
            DeadlineStatusFact,
        )
        if any(type(item) not in fact_types for item in facts):
            _raise_review_invalid()
        kinds = tuple(type(item) for item in facts)
        if len(set(kinds)) != len(kinds):
            _raise_review_invalid()
        object.__setattr__(self, "facts", facts)
