"""Normative vectors for the strict JSON authorization fingerprint policy."""

from __future__ import annotations

import pytest

from sett import SETTValidationError
from sett.authorization_ruler import (
    AuthorizationRequest,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    StrictJSONAuthorizationFingerprint,
    VerifiedApproval,
)
from sett.authorization_ruler.fingerprint import canonical_authorization_bytes


def _request(payload: dict[str, object]) -> AuthorizationRequest:
    return AuthorizationRequest(
        action_id="action-vector",
        trace_id="trace-vector",
        action_type="action.vector",
        payload=payload,
        principal=FieldValue(
            "principal",
            source_id="source.identity",
            trust=FieldTrust.AUTHORITATIVE,
        ),
        resource=FieldValue(
            "resource",
            source_id="source.resource",
            trust=FieldTrust.AUTHORITATIVE,
        ),
        scope=(
            FieldValue(
                "scope.read",
                source_id="source.scope",
                trust=FieldTrust.AUTHORITATIVE,
            ),
        ),
        execution_mode=ExecutionMode.CONTROLLED,
        idempotency_key="operation-vector",
    )


def test_strict_json_fingerprint_vector_v2() -> None:
    request = _request(
        {
            "text": "caf\u00e9",
            "number": -0.0,
            "items": [1, True, None],
        }
    )
    approvals = frozenset(
        {
            VerifiedApproval(
                "approval.vector",
                "verifier.vector",
                "authority.vector",
            )
        }
    )

    assert StrictJSONAuthorizationFingerprint().fingerprint(request, approvals) == (
        "0fd84204a0e8d219da2ae8bc4fe1901aca3e3d560d7eaf5381d6fbce012b1520"
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"number": 0}, {"number": -0.0}),
        ({"number": 1}, {"number": 1.0}),
        ({"text": "caf\u00e9"}, {"text": "cafe\u0301"}),
        ({"caf\u00e9": 1}, {"cafe\u0301": 1.0}),
    ],
)
def test_semantically_equivalent_json_values_have_one_fingerprint(
    first: dict[str, object],
    second: dict[str, object],
) -> None:
    policy = StrictJSONAuthorizationFingerprint()

    assert policy.fingerprint(_request(first), frozenset()) == policy.fingerprint(
        _request(second),
        frozenset(),
    )


def test_non_integral_floats_use_exact_ieee_754_hexadecimal() -> None:
    assert canonical_authorization_bytes(
        {"values": [0.1, 5e-324, 1 / 3]}
    ) == (
        b'["object",[["values",["array",[["number","0x1.999999999999ap-4"],'
        b'["number","0x0.0000000000001p-1022"],'
        b'["number","0x1.5555555555555p-2"]]]]]]'
    )


def test_keys_that_collide_after_nfc_normalization_are_rejected() -> None:
    request = _request({"caf\u00e9": 1, "cafe\u0301": 2})

    with pytest.raises(SETTValidationError, match="collide"):
        StrictJSONAuthorizationFingerprint().fingerprint(request, frozenset())
