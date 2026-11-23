"""Private dependency leaf for existing authorization verdict semantics."""

from __future__ import annotations

from enum import Enum

from sett.exceptions import SETTValidationError


class AuthorizationVerdict(str, Enum):
    """A deterministic authority outcome with no implicit escalation path."""

    GRANT = "grant"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


def permits_effect(verdict: object) -> bool:
    """Return whether an exact, recognized verdict permits an effect.

    Unknown values, strings that merely resemble a verdict, and incomplete
    approval requirements all fail closed. Callers must pass the enum member
    produced by the authorization mechanism.
    """

    return verdict is AuthorizationVerdict.GRANT


def validate_verdict_semantics(
    verdict: AuthorizationVerdict,
    *,
    additional_authority_declared: bool,
) -> None:
    """Validate the settled relationship between a verdict and approval.

    This function intentionally accepts only a boolean declaration. How
    additional authority is represented does not belong in this check.
    """

    if not isinstance(verdict, AuthorizationVerdict):
        raise SETTValidationError(
            "Authorization verdict must be an AuthorizationVerdict member."
        )
    if verdict is AuthorizationVerdict.REQUIRE_APPROVAL:
        if not additional_authority_declared:
            raise SETTValidationError(
                "REQUIRE_APPROVAL needs a specifically declared additional authority."
            )
        return
    if additional_authority_declared:
        raise SETTValidationError(
            "Additional authority belongs only to REQUIRE_APPROVAL. A GRANT "
            "or DENY cannot carry a latent approval path."
        )


def additional_authority_may_be_evaluated(
    verdict: object,
    *,
    additional_authority_declared: bool,
) -> bool:
    """Return whether reevaluation with additional authority is legitimate.

    A DENY is terminal. Human or secondary approval is therefore considered
    only after REQUIRE_APPROVAL declared the missing authority in advance.
    """

    return (
        verdict is AuthorizationVerdict.REQUIRE_APPROVAL
        and additional_authority_declared
    )
