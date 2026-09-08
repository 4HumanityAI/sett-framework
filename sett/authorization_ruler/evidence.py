"""Content-safe evidence contract for authorization decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from sett.authorization_ruler._validation import (
    FrozenMap,
    freeze_json,
    thaw_json,
    validate_identifier,
)
from sett.exceptions import SETTValidationError


@dataclass(frozen=True, slots=True, init=False)
class AuthorizationEvent:
    """One sanitized authorization event, independent of TraceRecorder."""

    kind: str
    reason_codes: tuple[str, ...]
    _attributes: FrozenMap = field(repr=False)

    def __init__(
        self,
        kind: str,
        *,
        reason_codes: tuple[str, ...] = (),
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Validate and detach one sanitized authorization event."""
        validate_identifier(kind, field="event kind")
        for reason_code in reason_codes:
            validate_identifier(reason_code, field="reason_code")
        frozen = freeze_json(attributes or {}, field="event attributes")
        if not isinstance(frozen, FrozenMap):
            raise SETTValidationError("event attributes must be an object.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "reason_codes", tuple(reason_codes))
        object.__setattr__(self, "_attributes", frozen)

    @property
    def attributes(self) -> dict[str, Any]:
        """Return a detached copy of the event attributes."""
        return thaw_json(self._attributes)


@runtime_checkable
class AuthorizationEvidenceSink(Protocol):
    """A sink for sanitized authorization evidence."""

    def record(self, event: AuthorizationEvent) -> None:
        """Record one already sanitized authorization event."""
        ...


class InMemoryAuthorizationEvidence:
    """Thread-safe in-process authorization evidence."""

    def __init__(self) -> None:
        """Initialize an empty synchronized event sequence."""
        self._events: list[AuthorizationEvent] = []
        self._lock = RLock()

    def record(self, event: AuthorizationEvent) -> None:
        """Append one validated event atomically."""
        if not isinstance(event, AuthorizationEvent):
            raise SETTValidationError("event must be an AuthorizationEvent.")
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[AuthorizationEvent, ...]:
        """Return an immutable snapshot of all recorded events."""
        with self._lock:
            return tuple(self._events)
