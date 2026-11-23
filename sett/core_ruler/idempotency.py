"""Whether an external effect has already been attempted or completed.

``action_id`` and ``idempotency_key`` are not the same thing and conflating
them is how a retry turns into a second charge. ``action_id`` identifies
one concrete proposal; ``idempotency_key`` identifies the business
operation out in the world. A retry keeps the key and takes a new attempt.

The key is never derived automatically from ``trace_id``: one trace may
legitimately contain two different external operations of the same type,
such as two reminders in one turn or two messages to two people. Giving them a
shared key would make the second one silently vanish as a duplicate.
Keys are chosen by the application, from its own domain:

    payment:invoice-2481
    notification:appointment-992:reminder-24h
    calendar:event-721:create

What this module can and cannot promise is stated plainly in
``IdempotencyStore``: SETT does not offer exactly-once, because nothing
talking to an external system can.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

from sett.exceptions import SETTIdempotencyConflictError, SETTValidationError


class IdempotencyStatus(str, Enum):
    """Where one keyed operation stands."""

    RESERVED = "reserved"
    """Claimed by a caller that has not started its attempt yet."""

    IN_PROGRESS = "in_progress"
    """An attempt is running right now, possibly in another thread."""

    SUCCEEDED = "succeeded"
    """The effect completed. Further attempts must replay, not repeat."""

    FAILED_RETRYABLE = "failed_retryable"
    """The attempt failed in a way that may be tried again."""

    FAILED_FINAL = "failed_final"
    """The attempt failed in a way that must not be tried again.

    Includes the case that matters most: an attempt whose outcome is
    unknown because the connection dropped after the request went out.
    Not knowing whether the effect happened is a reason to stop, not a
    reason to try again.
    """


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """What a store knows about one key."""

    key: str
    fingerprint: str
    status: IdempotencyStatus
    trace_id: str
    attempt_count: int = 0
    result_digest: str | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    """The outcome of asking a store for permission to act.

    ``acquired`` is the part the plan's original single-return signature
    could not express, and it is the part that makes the concurrent case
    correct: two threads reaching the same fresh key both get a record
    back, and exactly one of them gets ``acquired=True``. Without it, the
    loser cannot tell "I own this attempt" from "somebody else is already
    running it", which is precisely the race an idempotency store exists
    to arbitrate.
    """

    record: IdempotencyRecord
    acquired: bool

    @property
    def already_succeeded(self) -> bool:
        """Whether this key's effect already completed; replay, do not repeat."""
        return self.record.status is IdempotencyStatus.SUCCEEDED

    @property
    def in_progress_elsewhere(self) -> bool:
        """Whether somebody else holds this key and has not finished yet."""
        return not self.acquired and self.record.status in (
            IdempotencyStatus.RESERVED,
            IdempotencyStatus.IN_PROGRESS,
        )


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    """One recorded attempt at a keyed operation.

    Deliberately holds no payload. An attempt log is kept for as long as
    the operation matters, often longer, and a payload is exactly the part
    likely to carry something about a person. The fingerprint identifies
    the action just as well for every purpose this record serves.

    ``attempt_number`` mirrors ``IdempotencyRecord.attempt_count`` at the
    moment this attempt was reserved when the operation carries a key: it
    counts attempts against the key, not against one call to
    ``submit_controlled()``, so it keeps climbing across a caller
    resubmitting the same key after a retryable failure instead of
    resetting to 1.
    """

    attempt_id: str
    action_id: str
    idempotency_key: str
    attempt_number: int
    trace_id: str
    started_at: str
    finished_at: str | None = None
    status: str = "started"
    error_type: str | None = None
    retryable: bool = False
    fingerprint: str | None = None


def _canonicalize_for_fingerprint(value: Any) -> Any:
    """Normalize values so equivalent payloads fingerprint identically.

    The concrete bug this fixes: ``{"amount": 1500}`` and
    ``{"amount": 1500.0}`` describe the same action, but one has been
    through a JSON round trip in a different layer and the other has not,
    and hashing them separately turns a legitimate retry into a fingerprint
    conflict.

    It does NOT unify ``True`` with ``1``. That is a real type difference,
    not a serialization artifact, and a payload that says ``True`` where
    another says ``1`` is describing something else. Non-integer floats
    are left exactly as they are.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {
            key: _canonicalize_for_fingerprint(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize_for_fingerprint(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def action_fingerprint(action: Any) -> str:
    """A stable hash of what an action would do.

    Reusing one key for a different payload is a programming error that
    would otherwise either duplicate an effect or suppress a legitimate
    one, so the fingerprint exists to make it loud instead of silent.

    Raises:
        SETTValidationError: If the payload contains a value ``json.dumps``
            cannot serialize. This used to fall back
            to ``default=repr``, which is unstable in both directions: an
            object's default ``repr()`` embeds its memory address, so the
            *same* logical payload fingerprints differently every attempt
            (a retry would never match its own first attempt), while two
            genuinely *different* objects can share a custom ``repr()``
            and collide onto the same fingerprint (a different operation
            would be silently treated as a duplicate of the first). Both
            failure directions matter for a value used to decide whether
            an external effect happens once or twice, so this fails
            closed instead: an idempotency key requires a JSON-safe
            payload, full stop.
    """
    canonical_payload = _canonicalize_for_fingerprint(dict(action.payload))
    try:
        encoded = json.dumps(
            {
                "action_type": action.action_type,
                "payload": canonical_payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SETTValidationError(
            "An idempotent action's payload must be JSON-serializable: "
            f"{error}. An idempotency_key makes SETT hash the payload to "
            "detect a reused key describing a different operation, and a "
            "representation that is not stable and unique per value "
            "(such as a bare repr() of an arbitrary object) would make "
            "that detection either always miss on retry or occasionally "
            "collide between two different actions."
        ) from error
    return sha256(encoded).hexdigest()


def key_digest(key: str) -> str:
    """A stable, non-reversible short form of an idempotency key.

    Keys are chosen by the application from its own domain, and the
    guidance for choosing them ("payment:invoice-2481",
    "notification:appointment-992:reminder-24h") asks for exactly the
    kind of business identifier that should not travel in an exported
    trace. The trace already records shape rather than content elsewhere
    - ``result_type`` instead of the result and ``field_count`` instead of
    the fields. This keeps that habit for the one value that was
    being written out in full.

    Correlation survives: the same key always yields the same digest, so
    every event for one operation still lines up, and an operator holding
    a key can compute its digest to find them. The full key stays where
    it is genuinely needed, in the ``IdempotencyStore``.
    """
    return sha256(key.encode("utf-8")).hexdigest()[:16]


def result_digest(value: Any) -> str:
    """A short summary used to compare handler outcomes without storing them.

    This digest is not encryption or a confidentiality boundary. Low-entropy
    outcomes may be recoverable by guessing candidate values, and equal
    outcomes remain correlatable. It stays inside the idempotency record and
    is not suitable as durable state-integrity evidence.
    """
    try:
        encoded = json.dumps(
            _canonicalize_for_fingerprint(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=repr,
        )
    except (TypeError, ValueError):
        encoded = repr(value)
    return sha256(encoded.encode("utf-8")).hexdigest()


@runtime_checkable
class IdempotencyStore(Protocol):
    """Durable memory of which keyed effects were attempted or completed.

    What SETT can honestly promise, and no more: it will not repeat an
    action carrying a key it has a durable record for, when the provider
    honours that same key. That is effectively-once at best, never
    exactly-once. A store that only lives in memory says nothing at all
    about what happened before the last restart, and claiming otherwise
    would be the most dangerous sentence in this framework.

    Unknown identities are a silent no-op
    -------------------------------------
    ``mark_in_progress()``, ``mark_succeeded()`` and ``mark_failed()`` on
    a key that was never reserved do nothing and raise nothing. So does
    completing an attempt identifier the store never issued. This is the
    contract, not an omission: a transition describes something that
    happened to a reservation, and a store with no such reservation has
    nothing to describe. Raising instead would turn a store's own
    eviction or retention policy, which SETT deliberately does not
    dictate, into an error in the caller's code path, at the worst
    possible moment, after the effect already ran.

    ``SETTExecutor`` always reserves before it transitions, so this
    branch is not reached by SETT's own path; it is stated because the
    Protocol is public and an implementation has to know which of the
    two it is expected to do.
    """

    def reserve(
        self, *, key: str, fingerprint: str, trace_id: str
    ) -> IdempotencyReservation:
        """Claim a key for an attempt, or report who already holds it.

        Raises:
            SETTIdempotencyConflictError: If the key exists with a
                different fingerprint.
        """
        ...

    def mark_in_progress(self, *, key: str) -> None:
        """Note that the attempt for this key has actually started.

        A key with no reservation is a no-op; see the class docstring.
        """
        ...

    def mark_succeeded(self, *, key: str, digest: str | None) -> None:
        """Record that the effect completed; further calls must replay.

        A key with no reservation is a no-op; see the class docstring.
        """
        ...

    def mark_failed(self, *, key: str, retryable: bool) -> None:
        """Record that the attempt failed, and whether it may be retried.

        A key with no reservation is a no-op; see the class docstring.
        """
        ...

    def get(self, key: str) -> IdempotencyRecord | None:
        """Return the current record for a key, if any."""
        ...


class InMemoryIdempotencyStore:
    """Reference implementation, correct within one process only.

    Useful for tests, for single-process applications, and as the shape a
    durable store should follow. It is explicitly NOT enough to claim
    idempotency across restarts: everything it knows disappears with the
    process. An application that needs the guarantee to survive a crash
    injects a store backed by SQLite, a transactional database, or an
    external service.
    """

    def __init__(self) -> None:
        """Start an empty store; everything it learns dies with the process."""
        self._lock = threading.RLock()
        self._records: dict[str, IdempotencyRecord] = {}
        self._attempts: list[ActionAttempt] = []

    def reserve(
        self, *, key: str, fingerprint: str, trace_id: str
    ) -> IdempotencyReservation:
        """Claim a key, or report who already holds it.

        Raises:
            SETTIdempotencyConflictError: If the key exists with a
                different fingerprint.
            SETTValidationError: If the key is empty or not a string.
        """
        if not isinstance(key, str) or not key.strip():
            raise SETTValidationError(
                "An idempotency key must be a non-empty string."
            )
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                record = IdempotencyRecord(
                    key=key,
                    fingerprint=fingerprint,
                    status=IdempotencyStatus.RESERVED,
                    trace_id=trace_id,
                    attempt_count=1,
                )
                self._records[key] = record
                return IdempotencyReservation(record=record, acquired=True)

            if existing.fingerprint != fingerprint:
                raise SETTIdempotencyConflictError(
                    f"Idempotency key {key!r} was already used for a "
                    "different action. Reusing a key for a different "
                    "operation would either duplicate an effect or "
                    "suppress a legitimate one.",
                    key=key,
                    expected_fingerprint=existing.fingerprint,
                    actual_fingerprint=fingerprint,
                )

            if existing.status is IdempotencyStatus.FAILED_RETRYABLE:
                record = replace(
                    existing,
                    status=IdempotencyStatus.RESERVED,
                    attempt_count=existing.attempt_count + 1,
                )
                self._records[key] = record
                return IdempotencyReservation(record=record, acquired=True)

            # SUCCEEDED, FAILED_FINAL, or held by somebody else.
            return IdempotencyReservation(record=existing, acquired=False)

    def mark_in_progress(self, *, key: str) -> None:
        """Move a reserved key to IN_PROGRESS; unknown key is a no-op."""
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                return
            self._records[key] = replace(
                existing, status=IdempotencyStatus.IN_PROGRESS
            )

    def mark_succeeded(self, *, key: str, digest: str | None) -> None:
        """Move a reserved key to SUCCEEDED; unknown key is a no-op."""
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                return
            self._records[key] = replace(
                existing,
                status=IdempotencyStatus.SUCCEEDED,
                result_digest=digest,
            )

    def mark_failed(self, *, key: str, retryable: bool) -> None:
        """Move a reserved key to a FAILED status; unknown key is a no-op."""
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                return
            self._records[key] = replace(
                existing,
                status=(
                    IdempotencyStatus.FAILED_RETRYABLE
                    if retryable
                    else IdempotencyStatus.FAILED_FINAL
                ),
            )

    def get(self, key: str) -> IdempotencyRecord | None:
        """The current record for a key, or ``None`` if there is none."""
        with self._lock:
            return self._records.get(key)

    # ── Attempt log ──────────────────────────────────────────────────────

    def record_attempt(self, attempt: ActionAttempt) -> None:
        """Append one attempt to the log, in the order it started."""
        with self._lock:
            self._attempts.append(attempt)

    def complete_attempt(
        self,
        attempt_id: str,
        *,
        finished_at: str,
        status: str,
        error_type: str | None = None,
        retryable: bool = False,
    ) -> None:
        """Close a logged attempt; an unknown attempt_id is a no-op."""
        with self._lock:
            for index, attempt in enumerate(self._attempts):
                if attempt.attempt_id == attempt_id:
                    self._attempts[index] = replace(
                        attempt,
                        finished_at=finished_at,
                        status=status,
                        error_type=error_type,
                        retryable=retryable,
                    )
                    return

    def attempts_for(self, key: str) -> tuple[ActionAttempt, ...]:
        """Attempts recorded for one key, in the order they started."""
        with self._lock:
            return tuple(
                attempt
                for attempt in self._attempts
                if attempt.idempotency_key == key
            )

    def __repr__(self) -> str:
        with self._lock:
            return f"InMemoryIdempotencyStore(keys={len(self._records)})"
