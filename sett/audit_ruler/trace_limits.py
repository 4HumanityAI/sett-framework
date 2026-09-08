"""One structural validator for every public trace event field."""
from __future__ import annotations

import json
from collections.abc import Iterable

from sett.exceptions import SETTValidationError

MAX_TRACE_KIND_BYTES = 128
MAX_TRACE_COMPONENT_TYPE_BYTES = 128
MAX_TRACE_COMPONENT_NAME_BYTES = 1024
MAX_TRACE_STATUS_BYTES = 64
MAX_TRACE_REASON_CODES = 32
MAX_TRACE_REASON_CODE_BYTES = 256
MAX_TRACE_REASON_CODES_BYTES = 4096


def _canonical_string_size(value: str) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _validate_string(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise SETTValidationError(f"Trace {field} must be a non-empty string.")
    if _canonical_string_size(value) > maximum:
        raise SETTValidationError(
            f"Trace {field} exceeds its {maximum}-byte canonical limit."
        )
    return value


def validate_trace_event_fields(
    *,
    kind: object,
    component_type: object,
    component_name: object,
    status: object,
    reason_codes: Iterable[object],
) -> tuple[str, ...]:
    """Validate bounded trace vocabulary before a boundary can be opened."""
    _validate_string(kind, field="kind", maximum=MAX_TRACE_KIND_BYTES)
    _validate_string(
        component_type,
        field="component_type",
        maximum=MAX_TRACE_COMPONENT_TYPE_BYTES,
    )
    _validate_string(
        component_name,
        field="component_name",
        maximum=MAX_TRACE_COMPONENT_NAME_BYTES,
    )
    _validate_string(status, field="status", maximum=MAX_TRACE_STATUS_BYTES)
    try:
        reasons = tuple(reason_codes)
    except TypeError as error:
        raise SETTValidationError("Trace reason_codes must be iterable.") from error
    if len(reasons) > MAX_TRACE_REASON_CODES:
        raise SETTValidationError(
            f"Trace reason_codes accept at most {MAX_TRACE_REASON_CODES} entries."
        )
    validated = tuple(
        _validate_string(
            reason,
            field="reason code",
            maximum=MAX_TRACE_REASON_CODE_BYTES,
        )
        for reason in reasons
    )
    aggregate = json.dumps(
        validated,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(aggregate) > MAX_TRACE_REASON_CODES_BYTES:
        raise SETTValidationError(
            "Trace reason_codes exceed their 4096-byte canonical limit."
        )
    return validated
