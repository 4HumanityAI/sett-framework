"""Compatibility imports for the Authorization Ruler implementation.

The implementation belongs to :mod:`sett.authorization_ruler.authorizer`.
This module preserves historical imports of the same public classes.
"""

from sett.authorization_ruler.authorizer import (
    AuthorizationRuler,
    SystemAuthorizationClock,
)

__all__ = [
    "AuthorizationRuler",
    "SystemAuthorizationClock",
]
