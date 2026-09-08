"""Adversarial checks for the settled authorization verdict semantics."""

from __future__ import annotations

import pytest

from sett import SETTValidationError
from sett.authorization_ruler import (
    AuthorizationVerdict,
    additional_authority_may_be_evaluated,
    permits_effect,
    validate_verdict_semantics,
)


def test_only_the_exact_grant_member_permits_an_effect() -> None:
    assert permits_effect(AuthorizationVerdict.GRANT)
    assert not permits_effect(AuthorizationVerdict.REQUIRE_APPROVAL)
    assert not permits_effect(AuthorizationVerdict.DENY)
    assert not permits_effect("grant")
    assert not permits_effect(None)


def test_require_approval_needs_predeclared_additional_authority() -> None:
    validate_verdict_semantics(
        AuthorizationVerdict.REQUIRE_APPROVAL,
        additional_authority_declared=True,
    )

    with pytest.raises(SETTValidationError):
        validate_verdict_semantics(
            AuthorizationVerdict.REQUIRE_APPROVAL,
            additional_authority_declared=False,
        )


@pytest.mark.parametrize(
    "verdict",
    [AuthorizationVerdict.GRANT, AuthorizationVerdict.DENY],
)
def test_grant_and_deny_cannot_hide_a_later_approval_path(
    verdict: AuthorizationVerdict,
) -> None:
    with pytest.raises(SETTValidationError):
        validate_verdict_semantics(
            verdict,
            additional_authority_declared=True,
        )


def test_deny_is_terminal_even_when_someone_offers_approval() -> None:
    assert not additional_authority_may_be_evaluated(
        AuthorizationVerdict.DENY,
        additional_authority_declared=True,
    )


def test_approval_is_considered_only_for_the_declared_state() -> None:
    assert additional_authority_may_be_evaluated(
        AuthorizationVerdict.REQUIRE_APPROVAL,
        additional_authority_declared=True,
    )
    assert not additional_authority_may_be_evaluated(
        AuthorizationVerdict.REQUIRE_APPROVAL,
        additional_authority_declared=False,
    )


def test_unknown_verdict_cannot_enter_the_semantic_contract() -> None:
    with pytest.raises(SETTValidationError):
        validate_verdict_semantics(
            "grant",  # type: ignore[arg-type]
            additional_authority_declared=False,
        )


def test_verdict_wire_values_are_stable_and_unambiguous() -> None:
    assert [verdict.value for verdict in AuthorizationVerdict] == [
        "grant",
        "require_approval",
        "deny",
    ]
