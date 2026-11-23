"""Private bounded validation shared by governance domains."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sett.exceptions import SETTValidationError

MAX_IDENTIFIER_BYTES = 128


MAX_JSON_DEPTH = 12


MAX_JSON_ITEMS = 256


MAX_JSON_BYTES = 65_536


def validate_identifier(value: str, *, field: str) -> str:
    """Validate a bounded, non-empty identifier without assigning meaning."""

    if not isinstance(value, str):
        raise SETTValidationError(f"{field} must be a string.")
    if not value or value != value.strip():
        raise SETTValidationError(f"{field} must be non-empty and trimmed.")
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise SETTValidationError(
            f"{field} exceeds its {MAX_IDENTIFIER_BYTES}-byte limit."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SETTValidationError(f"{field} cannot contain control characters.")
    return value


@dataclass(frozen=True, slots=True)
class FrozenMap:
    """An immutable JSON object with deterministic key order."""

    items: tuple[tuple[str, Any], ...]


def freeze_json(value: Any, *, field: str, depth: int = 0) -> Any:
    """Detach and freeze one strict JSON value."""

    if depth > MAX_JSON_DEPTH:
        raise SETTValidationError(
            f"{field} exceeds the maximum JSON depth of {MAX_JSON_DEPTH}."
        )
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SETTValidationError(f"{field} cannot contain NaN or infinity.")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_ITEMS:
            raise SETTValidationError(
                f"{field} exceeds the maximum object size of {MAX_JSON_ITEMS}."
            )
        if any(not isinstance(key, str) for key in value):
            raise SETTValidationError(f"{field} object keys must be strings.")
        frozen_items: list[tuple[str, Any]] = []
        for key in sorted(value):
            frozen_items.append(
                (
                    key,
                    freeze_json(value[key], field=f"{field}.{key}", depth=depth + 1),
                )
            )
        return FrozenMap(tuple(frozen_items))
    if isinstance(value, Sequence) and not isinstance(
        value,
        str | bytes | bytearray | memoryview,
    ):
        if len(value) > MAX_JSON_ITEMS:
            raise SETTValidationError(
                f"{field} exceeds the maximum array size of {MAX_JSON_ITEMS}."
            )
        return tuple(
            freeze_json(item, field=f"{field}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise SETTValidationError(
        f"{field} must contain only strict JSON-safe values; got "
        f"{type(value).__name__}."
    )


def thaw_json(value: Any) -> Any:
    """Return a detached mutable view of one frozen JSON value."""

    if isinstance(value, FrozenMap):
        return {key: thaw_json(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: Any, *, field: str) -> bytes:
    """Encode a strict JSON value with one deterministic Python reference."""

    plain = thaw_json(freeze_json(value, field=field))
    encoded = json.dumps(
        plain,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise SETTValidationError(
            f"{field} exceeds the {MAX_JSON_BYTES}-byte canonical limit."
        )
    return encoded


def _validated_identifiers(
    values: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(values)
    for value in normalized:
        validate_identifier(value, field=field_name)
    return normalized
