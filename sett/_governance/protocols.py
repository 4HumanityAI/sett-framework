"""Private dependency leaf for the existing authorization clock protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthorizationClock(Protocol):
    """An injectable timezone-aware clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware authorization time."""
        ...
