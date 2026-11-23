"""Public import identities after the implementation module moves."""

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("package", "previous", "current", "name"),
    [
        (
            "sett.authorization_ruler",
            "sett.authorization_ruler.ruler",
            "sett.authorization_ruler.authorizer",
            "AuthorizationRuler",
        ),
        (
            "sett.authorization_ruler",
            "sett.authorization_ruler.ruler",
            "sett.authorization_ruler.authorizer",
            "SystemAuthorizationClock",
        ),
        (
            "sett.policy_ruler",
            "sett.policy_ruler.ruler",
            "sett.policy_ruler.evaluator",
            "PolicyRuler",
        ),
        (
            "sett.execution_ruler",
            "sett.core_ruler.executor",
            "sett.execution_ruler.executor",
            "SETTExecutor",
        ),
    ],
)
def test_implementation_module_migration_preserves_public_identity(
    package, previous, current, name
):
    """Compatibility imports resolve to the one defining implementation."""
    value = getattr(import_module(current), name)
    assert getattr(import_module(previous), name) is value
    assert getattr(import_module(package), name) is value
    assert value.__module__ == current
    if name != "PolicyRuler":
        assert getattr(import_module("sett"), name) is value
