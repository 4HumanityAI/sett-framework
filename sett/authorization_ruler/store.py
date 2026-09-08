"""Issuer-owned receipt state and atomic in-memory reference storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol, runtime_checkable

from sett.authorization_ruler.errors import SETTAuthorizationError
from sett.authorization_ruler.models import AuthorizationReceipt, ReceiptUse


@runtime_checkable
class AuthorizationReceiptStore(Protocol):
    """Authoritative receipt state with atomic issue, consume, and revoke."""

    def issue(self, receipt: AuthorizationReceipt) -> None:
        """Persist issuer-owned state for a newly issued receipt."""
        ...

    def verify_and_consume(
        self,
        receipt: AuthorizationReceipt,
        *,
        request_fingerprint: str,
        now: datetime,
    ) -> None:
        """Verify public fields and atomically consume effect authority."""
        ...

    def revoke(self, receipt_id: str) -> None:
        """Revoke a known receipt before later verification."""
        ...


@dataclass(slots=True)
class _ReceiptState:
    receipt: AuthorizationReceipt
    consumed: bool = False
    revoked: bool = False


class InMemoryAuthorizationReceiptStore:
    """Thread-safe receipt storage with no guarantee across restarts."""

    def __init__(self) -> None:
        """Initialize an empty synchronized receipt store."""
        self._records: dict[str, _ReceiptState] = {}
        self._lock = RLock()

    def issue(self, receipt: AuthorizationReceipt) -> None:
        """Record a receipt unless its opaque identifier already exists."""
        with self._lock:
            if receipt.receipt_id in self._records:
                raise SETTAuthorizationError(
                    "Receipt identity is already in use.",
                    reason_code="authorization.receipt_duplicate",
                    receipt_id=receipt.receipt_id,
                )
            self._records[receipt.receipt_id] = _ReceiptState(receipt)

    def verify_and_consume(
        self,
        receipt: AuthorizationReceipt,
        *,
        request_fingerprint: str,
        now: datetime,
    ) -> None:
        """Validate issuer state and atomically consume the receipt."""
        with self._lock:
            state = self._records.get(receipt.receipt_id)
            if state is None:
                raise SETTAuthorizationError(
                    "Receipt is not known to its issuer.",
                    reason_code="authorization.receipt_unknown",
                    receipt_id=receipt.receipt_id,
                )
            if state.receipt != receipt:
                raise SETTAuthorizationError(
                    "Receipt fields do not match issuer-owned state.",
                    reason_code="authorization.receipt_mismatch",
                    receipt_id=receipt.receipt_id,
                )
            if state.revoked:
                raise SETTAuthorizationError(
                    "Receipt has been revoked.",
                    reason_code="authorization.receipt_revoked",
                    receipt_id=receipt.receipt_id,
                )
            if now >= receipt.expires_at:
                raise SETTAuthorizationError(
                    "Receipt has expired.",
                    reason_code="authorization.receipt_expired",
                    receipt_id=receipt.receipt_id,
                )
            if request_fingerprint != receipt.request_fingerprint:
                raise SETTAuthorizationError(
                    "Receipt does not match the requested action.",
                    reason_code="authorization.request_mismatch",
                    receipt_id=receipt.receipt_id,
                )
            if state.consumed and receipt.receipt_use is ReceiptUse.SINGLE_USE:
                raise SETTAuthorizationError(
                    "Receipt has already been consumed.",
                    reason_code="authorization.receipt_consumed",
                    receipt_id=receipt.receipt_id,
                )
            state.consumed = True

    def revoke(self, receipt_id: str) -> None:
        """Mark one known receipt as revoked atomically."""
        with self._lock:
            state = self._records.get(receipt_id)
            if state is None:
                raise SETTAuthorizationError(
                    "Receipt is not known to its issuer.",
                    reason_code="authorization.receipt_unknown",
                    receipt_id=receipt_id,
                )
            state.revoked = True
