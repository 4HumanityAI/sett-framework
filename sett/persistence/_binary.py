"""Strict binary primitives shared by SETT persistence formats."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sett.exceptions import SETTPersistenceFormatError

_IDENTIFIER = re.compile(r"[a-z0-9._-]+\Z", re.ASCII)


def _format_error(message: str, reason_code: str) -> SETTPersistenceFormatError:
    return SETTPersistenceFormatError(message, reason_code=reason_code)


def encode_uint(value: int, width: int, *, field: str, reason_code: str) -> bytes:
    """Encode one bounded unsigned integer in network byte order."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _format_error(f"{field} must be an integer.", reason_code)
    maximum = (1 << (width * 8)) - 1
    if not 0 <= value <= maximum:
        raise _format_error(
            f"{field} must be between 0 and {maximum}.", reason_code
        )
    return value.to_bytes(width, "big")


def encode_text(
    value: str,
    *,
    field: str,
    maximum: int = 65535,
    reason_code: str,
    allow_empty: bool = False,
    identifier: bool = False,
    reject_controls: bool = False,
) -> bytes:
    """Encode strict UTF-8 with a checked unsigned 16-bit length."""
    if not isinstance(value, str):
        raise _format_error(f"{field} must be a string.", reason_code)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise _format_error(f"{field} must be valid UTF-8.", reason_code) from error
    if not encoded and not allow_empty:
        raise _format_error(f"{field} cannot be empty.", reason_code)
    if len(encoded) > maximum:
        raise _format_error(
            f"{field} exceeds its {maximum}-byte limit.", reason_code
        )
    if identifier and (
        len(encoded) > 128 or not encoded.isascii() or _IDENTIFIER.fullmatch(value) is None
    ):
        raise _format_error(
            f"{field} must be a lowercase ASCII format identifier.", reason_code
        )
    if reject_controls and any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _format_error(f"{field} cannot contain control characters.", reason_code)
    return encode_uint(len(encoded), 2, field=f"{field} length", reason_code=reason_code) + encoded


def encode_blob(
    value: bytes,
    *,
    field: str,
    maximum: int,
    reason_code: str,
) -> bytes:
    """Encode opaque bytes after enforcing a caller-owned allocation limit."""
    if not isinstance(value, bytes):
        raise _format_error(f"{field} must be bytes.", reason_code)
    if len(value) > maximum:
        raise _format_error(
            f"{field} exceeds its {maximum}-byte limit.", reason_code
        )
    return encode_uint(len(value), 4, field=f"{field} length", reason_code=reason_code) + value


def encode_optional(
    value: str | bytes | None,
    *,
    encoder: Callable[[str | bytes], bytes],
) -> bytes:
    """Encode an explicit presence marker followed by a present value."""
    if value is None:
        return b"\x00"
    return b"\x01" + encoder(value)


@dataclass(slots=True)
class BinaryReader:
    """Bounds-checking reader that never allocates from an unchecked length."""

    data: bytes
    reason_code: str
    truncated_reason_code: str | None = None
    offset: int = 0

    def _failure_reason(self) -> str:
        return self.truncated_reason_code or self.reason_code

    def take(self, length: int, *, field: str) -> bytes:
        """Consume exactly ``length`` bytes or report truncation."""
        if length < 0 or self.offset + length > len(self.data):
            raise _format_error(
                f"{field} is truncated.", self._failure_reason()
            )
        start = self.offset
        self.offset += length
        return self.data[start : self.offset]

    def uint(self, width: int, *, field: str) -> int:
        """Read one unsigned big-endian integer of fixed width."""
        return int.from_bytes(self.take(width, field=field), "big")

    def text(
        self,
        *,
        field: str,
        maximum: int = 65535,
        allow_empty: bool = False,
        identifier: bool = False,
        reject_controls: bool = False,
    ) -> str:
        """Read one bounded strict UTF-8 string."""
        length = self.uint(2, field=f"{field} length")
        if length > maximum:
            raise _format_error(
                f"{field} exceeds its {maximum}-byte limit.", self.reason_code
            )
        raw = self.take(length, field=field)
        try:
            value = raw.decode("utf-8", "strict")
        except UnicodeError as error:
            raise _format_error(
                f"{field} must be valid UTF-8.", self.reason_code
            ) from error
        encode_text(
            value,
            field=field,
            maximum=maximum,
            reason_code=self.reason_code,
            allow_empty=allow_empty,
            identifier=identifier,
            reject_controls=reject_controls,
        )
        return value

    def blob(self, *, field: str, maximum: int) -> bytes:
        """Read one bounded opaque blob after checking its declared length."""
        length = self.uint(4, field=f"{field} length")
        if length > maximum:
            raise _format_error(
                f"{field} exceeds its {maximum}-byte limit.", self.reason_code
            )
        return self.take(length, field=field)

    def optional_text(
        self, *, field: str, maximum: int = 65535
    ) -> str | None:
        """Read an explicit optional UTF-8 string."""
        marker = self.uint(1, field=f"{field} presence marker")
        if marker == 0:
            return None
        if marker != 1:
            raise _format_error(
                f"{field} has an invalid presence marker.", self.reason_code
            )
        return self.text(field=field, maximum=maximum)

    def optional_blob(self, *, field: str, maximum: int) -> bytes | None:
        """Read an explicit optional opaque blob."""
        marker = self.uint(1, field=f"{field} presence marker")
        if marker == 0:
            return None
        if marker != 1:
            raise _format_error(
                f"{field} has an invalid presence marker.", self.reason_code
            )
        return self.blob(field=field, maximum=maximum)

    def finish(self, *, field: str = "frame") -> None:
        """Reject any bytes left after the declared structure."""
        if self.offset != len(self.data):
            raise _format_error(
                f"{field} contains trailing bytes.", self.reason_code
            )
