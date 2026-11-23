"""
templates/consent_model.py
==========================
A conceptual consent model. Copy it, rename it and adapt it, or build your
own: modifying it or replacing it is the recommended way to use it.

SETT is a toolbox for building applications; consent belongs to the
application built on it. Who the subject is, which purposes and scopes
exist, what legal basis applies and where consent records live are the
application's decisions, not the framework's. This model shows one way to
check, before an action, that the application holds a consent record for
it, using SETT's existing condition verification:

1. A policy declares the check as a required ``PolicyCondition`` whose
   verifier is ``ConsentVerifier`` (``consent_condition()`` builds one).
2. ``ConsentVerifier`` reads the subject from the request's ``principal``,
   the purpose from its ``purpose`` and the scopes from its ``scope``, and
   looks for a matching record in a ``ConsentRegistry``.
3. A current record that covers every requested scope is ``SATISFIED``,
   with the record identifier as evidence. A matching record that is
   revoked, expired or not yet valid is ``UNSATISFIED``. No matching record
   is ``EVIDENCE_NOT_PRESENT``. A request without a usable subject or
   purpose is ``EVIDENCE_INVALID``.

What this model is not:

- A record says what the application wrote down. SETT does not verify that a
  person actually agreed, and nothing here is legal advice or a compliance
  claim.
- ``ConsentRegistry`` keeps records in memory and loses them on restart.
  Replace it with your own storage.
- The verifier's clock must agree with the ``PolicyRuler`` clock; SETT
  rejects an observation outside the call window but cannot check the
  verifier's clock.
- It does not supply the consent state that authorization requires; an
  implementation that owns consent declares it with
  ``PolicyRuler(implementation_owned_consent=True)``. While the review
  producer is missing, a governed attempt does not reach condition
  verification. See ``docs/consent_model.md``.

The parts most applications will change are marked TODO.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from threading import Lock
from typing import Any

from sett.authorization_ruler import AuthorizationRequest, PolicyBinding
from sett.policy_ruler import (
    ConditionValidityMode,
    ConditionVerification,
    ConditionVerificationState,
    PolicyCondition,
)


@dataclass(frozen=True)
class ConsentRecord:
    """One consent the application recorded.

    TODO: add the fields your legal basis and audit need, such as how the
    consent was collected, its wording version or a reference to the signed
    document.
    """

    record_id: str
    subject: str
    purpose: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_current(self, now: datetime) -> bool:
        """Report whether the record is granted, not revoked and not expired at ``now``."""
        if self.granted_at is not None and now < self.granted_at:
            return False
        if self.revoked_at is not None and self.revoked_at <= now:
            return False
        return self.expires_at is None or now < self.expires_at


class ConsentRegistry:
    """An in-memory store of consent records.

    TODO: replace it with your own storage. This one keeps nothing across
    restarts and has no access control.
    """

    def __init__(self) -> None:
        """Start with no records."""
        self._records: dict[str, ConsentRecord] = {}
        self._lock = Lock()

    def record(self, record: ConsentRecord) -> None:
        """Store a new record; an identifier is used once."""
        with self._lock:
            if record.record_id in self._records:
                raise ValueError(f"consent record {record.record_id!r} already exists")
            self._records[record.record_id] = record

    def revoke(self, record_id: str, at: datetime) -> None:
        """Mark a stored record as revoked from ``at`` on."""
        with self._lock:
            self._records[record_id] = replace(self._records[record_id], revoked_at=at)

    def get(self, record_id: str) -> ConsentRecord | None:
        """Return one record, or None."""
        with self._lock:
            return self._records.get(record_id)

    def find(self, subject: str, purpose: str, scopes: frozenset[str]) -> list[ConsentRecord]:
        """Return the records for this subject and purpose that cover every scope."""
        with self._lock:
            return sorted(
                (
                    record for record in self._records.values()
                    if record.subject == subject and record.purpose == purpose
                    and scopes <= record.scopes
                ),
                key=lambda record: record.record_id,
            )


def consent_key(request: AuthorizationRequest) -> tuple[str, str, frozenset[str]] | None:
    """Read subject, purpose and scopes from the request, or None if unusable.

    TODO: map your own request fields here if consent is keyed differently.
    """
    subject = request.principal.value
    purpose = request.purpose.value if request.purpose is not None else None
    scopes = [item.value for item in request.scope]
    if not isinstance(subject, str) or not isinstance(purpose, str):
        return None
    if not all(isinstance(scope, str) for scope in scopes):
        return None
    return subject, purpose, frozenset(scopes)


class ConsentVerifier:
    """A ``ConditionVerifier`` that checks for a current consent record.

    Register it with ``PolicyRuler(condition_verifiers=[...])``. It only
    reads the registry; it never creates, extends or revokes a record.
    """

    verifier_id = "template.consent"
    verifier_version = "1"

    def __init__(self, registry: ConsentRegistry, clock: Any) -> None:
        """Bind the registry and a clock that agrees with the ``PolicyRuler`` clock."""
        self._registry = registry
        self._clock = clock

    def verify(self, request: AuthorizationRequest, *, condition: PolicyCondition) -> ConditionVerification:
        """Observe whether a current record covers this request."""
        now = self._clock.now()
        key = consent_key(request)
        if key is None:
            return self._negative(condition, now, ConditionVerificationState.EVIDENCE_INVALID,
                                  "consent.request_incomplete")
        matches = self._registry.find(*key)
        current = [record for record in matches if record.is_current(now)]
        if not matches:
            return self._negative(condition, now, ConditionVerificationState.EVIDENCE_NOT_PRESENT,
                                  "consent.not_found")
        if not current:
            return self._negative(condition, now, ConditionVerificationState.UNSATISFIED,
                                  "consent.not_current", tuple(record.record_id for record in matches))
        valid_until = None
        if condition.validity_mode is ConditionValidityMode.ATTESTATION:
            # An attestation may be reused until the record or the declaration ends.
            limits = [condition.attestation_not_after]
            limits += [record.expires_at for record in current if record.expires_at is not None]
            valid_until = min(limits)
        return ConditionVerification(
            condition=condition,
            state=ConditionVerificationState.SATISFIED,
            checked_at=now,
            valid_until=valid_until,
            evidence_refs=tuple(record.record_id for record in current),
            reason_codes=(),
        )

    def confirm(self, verification: ConditionVerification, *, request: AuthorizationRequest) -> bool:
        """Confirm a retained attestation: its records are still current."""
        now = self._clock.now()
        records = [self._registry.get(ref) for ref in verification.evidence_refs]
        return bool(records) and all(record is not None and record.is_current(now) for record in records)

    @staticmethod
    def _negative(
        condition: PolicyCondition,
        now: datetime,
        state: ConditionVerificationState,
        reason: str,
        evidence: tuple[str, ...] = (),
    ) -> ConditionVerification:
        return ConditionVerification(
            condition=condition, state=state, checked_at=now, valid_until=None,
            evidence_refs=evidence, reason_codes=(reason,),
        )


def consent_condition(
    binding: PolicyBinding,
    request: AuthorizationRequest,
    *,
    checkpoint_id: str,
    condition_id: str = "consent.required",
) -> PolicyCondition:
    """Declare a required consent check for one request, verified on every use.

    Call it from a policy's ``evaluate()`` and return the condition in the
    decision's ``conditions``. TODO: choose ATTESTATION with an
    ``attestation_not_after`` if a verified consent may be reused for a while.
    """
    return PolicyCondition(
        condition_id=condition_id,
        binding=binding,
        origins=binding.origins,
        verifier_id=ConsentVerifier.verifier_id,
        verifier_version=ConsentVerifier.verifier_version,
        required=True,
        action_id=request.action_id,
        action_type=request.action_type,
        checkpoint_id=checkpoint_id,
        validity_mode=ConditionValidityMode.RECHECK,
        attestation_not_after=None,
    )
