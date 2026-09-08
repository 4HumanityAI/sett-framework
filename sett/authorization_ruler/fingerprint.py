"""Reference fingerprint policy for authorization requests."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from sett.authorization_ruler._validation import MAX_JSON_BYTES
from sett.authorization_ruler.models import AuthorizationRequest, VerifiedApproval
from sett.exceptions import SETTValidationError


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _normalized_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise SETTValidationError(
            "Authorization fingerprint numbers must be finite."
        )
    if value == 0.0:
        return "0"
    if value.is_integer():
        return str(int(value))
    return value.hex()


def _canonical_node(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, int | float):
        return ["number", _normalized_number(value)]
    if isinstance(value, str):
        return ["string", _normalized_text(value)]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SETTValidationError(
                    "Authorization fingerprint object keys must be strings."
                )
            normalized_key = _normalized_text(key)
            if normalized_key in normalized:
                raise SETTValidationError(
                    "Authorization fingerprint keys collide after NFC normalization."
                )
            normalized[normalized_key] = item
        return [
            "object",
            [
                [key, _canonical_node(normalized[key])]
                for key in sorted(normalized)
            ],
        ]
    if isinstance(value, Sequence) and not isinstance(
        value,
        str | bytes | bytearray | memoryview,
    ):
        return ["array", [_canonical_node(item) for item in value]]
    raise SETTValidationError(
        "Authorization fingerprints accept strict JSON values only."
    )


def canonical_authorization_bytes(value: Any) -> bytes:
    """Encode one normalized, typed authorization value deterministically."""

    encoded = json.dumps(
        _canonical_node(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise SETTValidationError(
            "Authorization fingerprint preimage exceeds its byte limit."
        )
    return encoded


class StrictJSONAuthorizationFingerprint:
    """SHA-256 over the normalized typed strict JSON request representation."""

    policy_id = "authorization.strict-json-sha256.v2"

    def fingerprint(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> str:
        """Return the versioned digest of a complete normalized request."""
        preimage = canonical_authorization_bytes(
            {
                "fingerprint_policy_id": self.policy_id,
                "request": request.canonical_view(verified_approvals),
            },
        )
        return hashlib.sha256(preimage).hexdigest()
