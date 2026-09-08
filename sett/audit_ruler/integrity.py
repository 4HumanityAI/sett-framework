"""Integrity policies that bind durable state bytes to audit evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, runtime_checkable

from sett.exceptions import SETTValidationError

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_LOWER_HEX = re.compile(r"[0-9a-f]+\Z")


def _validate_identifier(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8", "strict")) > 256
        or _IDENTIFIER.fullmatch(value) is None
    ):
        raise SETTValidationError(
            f"{field} must be a lowercase, non-sensitive identifier of at "
            "most 256 UTF-8 bytes."
        )
    return value


@runtime_checkable
class IntegrityPolicy(Protocol):
    """Compute and verify an optional digest over exact durable bytes.

    Policies receive bytes only. They do not import execution concepts, own
    secret storage, encrypt payloads, or choose whether integrity is required.
    """

    @property
    def policy_id(self) -> str:
        """Opaque, stable identity used to locate the historical policy."""
        ...

    @property
    def algorithm(self) -> str:
        """Stable algorithm identifier recorded beside the digest."""
        ...

    def digest(self, payload: bytes) -> bytes | None:
        """Digest exact bytes, or return None for an explicit no-digest policy."""
        ...

    def verify(self, payload: bytes, expected: bytes | None) -> bool:
        """Verify exact bytes against a digest produced by this policy."""
        ...


@dataclass(frozen=True, slots=True)
class Sha256IntegrityPolicy:
    """Reference unkeyed policy; detects changes but provides no secrecy.

    SHA-256 permits equality correlation and dictionary attacks against
    low-entropy values. It is not authentication against an actor who can
    rewrite both state and evidence.
    """

    policy_id: str = "sett.sha256.v1"
    algorithm: str = "sha256"

    def __post_init__(self) -> None:
        _validate_identifier(self.policy_id, field="policy_id")
        _validate_identifier(self.algorithm, field="algorithm")

    def digest(self, payload: bytes) -> bytes:
        """Return the SHA-256 digest of exact durable bytes."""
        if not isinstance(payload, bytes):
            raise SETTValidationError("Integrity payload must be bytes.")
        return sha256(payload).digest()

    def verify(self, payload: bytes, expected: bytes | None) -> bool:
        """Compare exact durable bytes with an expected SHA-256 digest."""
        return isinstance(expected, bytes) and self.digest(payload) == expected


@dataclass(frozen=True, slots=True)
class NoDigestPolicy:
    """Explicitly decline state digests without pretending they were omitted."""

    policy_id: str = "sett.no-digest.v1"
    algorithm: str = "none"

    def __post_init__(self) -> None:
        _validate_identifier(self.policy_id, field="policy_id")
        _validate_identifier(self.algorithm, field="algorithm")

    def digest(self, payload: bytes) -> None:
        """Validate the payload type and explicitly produce no digest."""
        if not isinstance(payload, bytes):
            raise SETTValidationError("Integrity payload must be bytes.")

    def verify(self, payload: bytes, expected: bytes | None) -> bool:
        """Accept only the explicit absence produced by this policy."""
        if not isinstance(payload, bytes):
            return False
        return expected is None


def digest_to_trace_value(digest: bytes | None) -> str | None:
    """Encode an in-memory digest for JSON-safe trace evidence."""
    if digest is None:
        return None
    if not isinstance(digest, bytes) or not digest:
        raise SETTValidationError("State digest must be non-empty bytes or None.")
    return digest.hex()


def digest_from_trace_value(value: str | None) -> bytes | None:
    """Decode the required lowercase hexadecimal trace representation."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) % 2
        or _LOWER_HEX.fullmatch(value) is None
    ):
        raise SETTValidationError(
            "State digest must be lowercase hexadecimal with even length."
        )
    return bytes.fromhex(value)
