"""Private dependency leaf for existing authorization evidence contracts."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from sett._governance.validation import (
    FrozenMap,
    freeze_json,
    thaw_json,
    validate_identifier,
)
from sett.exceptions import SETTValidationError

# The identifier of the authorization attempt running in this context. The
# authorization ruler sets it for the duration of one authorize() or
# verify_and_consume() call, so every event written during that call, by the
# ruler or by the policy layer, carries it without extra parameters.
_ATTEMPT_ID: ContextVar[str | None] = ContextVar("sett_authorization_attempt_id", default=None)


def _with_attempt_id(attributes: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return event attributes with the current attempt identifier, if any."""
    attempt_id = _ATTEMPT_ID.get()
    if attempt_id is None:
        return attributes
    merged = dict(attributes or {})
    merged["authorization_attempt_id"] = attempt_id
    return merged


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
