"""Private delivery records, verified-state slots and the local guard.

Nothing in this module is a public API. It holds the issuer-owned records
that make a policy evaluation usable by exactly one authorization
invocation: the ruler incarnation, the per-receiver link, the per-call
invocation witness and the binding that retains the complete detached
state. Object identity, not a declared identifier, is what these records
compare.

This is structural validation inside cooperating Python. It is not a
cryptographic token, not protection against code that deliberately
rewrites private memory, and not a guarantee of network atomicity or of
forced interruption of a running Python call.

The five closed verified-state slot names are fixed here. ``approvals``
is produced by the attached authorization receiver, which owns approval
verification. A configured private condition producer can supply
condition state. An implementation that owns consent can declare it, and
an internal producer then records that declaration in the consent slot.
A configured governance journal installs the durability producer, which
writes a confirmed decision record, and review verifiers with that journal
install the review producer. A slot without a producer leaves the state
incomplete. A missing producer is never treated as an empty requirement set.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock, get_ident
from typing import Any, Protocol, runtime_checkable

from sett._governance.validation import (
    MAX_IDENTIFIER_BYTES,
    MAX_JSON_ITEMS,
    canonical_json_bytes,
    validate_identifier,
)
from sett._governance.values import AuthorizationRequest, VerifiedApproval
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    SETTPolicyConfigurationError,
    SETTValidationError,
)
from sett.policy_ruler.models import PolicySelection

APPROVALS_SLOT = "approvals"

PRODUCED_SLOT_NAMES = ("conditions", "consent", "review", "durability")

SLOT_NAMES = (APPROVALS_SLOT, *PRODUCED_SLOT_NAMES)

POLICY_FAILURE_MESSAGE = "Policy evaluation could not establish a valid binding."

AUTHORIZATION_FAILURE_MESSAGE = "Authorization could not validate the policy binding."

PENDING = "pending"
CLAIMED = "claimed"
RECEIPT_BOUND = "receipt_bound"
TERMINAL = "terminal"


def policy_error(reason_code: str) -> SETTPolicyConfigurationError:
    """Build the fixed policy-boundary failure for one reason code."""
    return SETTPolicyConfigurationError(
        POLICY_FAILURE_MESSAGE, reason_code=reason_code
    )


def authorization_error(reason_code: str) -> SETTAuthorizationError:
    """Build the fixed blocking authorization failure for one reason code."""
    return SETTAuthorizationError(
        AUTHORIZATION_FAILURE_MESSAGE, reason_code=reason_code
    )


def authorization_configuration_error(
    reason_code: str,
) -> SETTAuthorizationConfigurationError:
    """Build the fixed authorization configuration failure for one code."""
    return SETTAuthorizationConfigurationError(
        AUTHORIZATION_FAILURE_MESSAGE, reason_code=reason_code
    )


# ── Verified state ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerifiedStateSlot:
    """One producer-created record for one closed verified-state slot.

    ``required_keys`` comes from the governing declaration, never from the
    evidence that happened to arrive; ``observed_keys`` is what the
    producer actually verified. The receiver compares them for equality,
    so a producer cannot report a partial result as complete.
    """

    slot: str
    producer_id: str
    schema_revision: str
    epoch: int
    action_id: str
    checkpoint_id: str
    required_keys: tuple[str, ...]
    observed_keys: tuple[str, ...]
    value_bytes: bytes
    evidence_refs: tuple[str, ...]
    valid_until: datetime | None

    def signature(self) -> tuple[Any, ...]:
        """Return the complete comparable form of this slot record."""
        return (
            self.slot,
            self.producer_id,
            self.schema_revision,
            self.epoch,
            self.action_id,
            self.checkpoint_id,
            self.required_keys,
            self.observed_keys,
            self.value_bytes,
            self.evidence_refs,
            self.valid_until,
        )


@dataclass(frozen=True, slots=True)
class ConditionRejection:
    """One sanitized private producer failure, never an authority value.

    ``failed_condition`` and ``attempted_conditions`` are set only when
    verification stopped on one specific condition; they carry declared
    ``(policy_id, policy_version, condition_id)`` identities, never evidence
    or verifier output.
    """

    code: str
    failed_condition: tuple[str, str, str] | None = None
    attempted_conditions: tuple[tuple[str, str, str], ...] | None = None


@dataclass(slots=True)
class ConditionProof:
    """Per-invocation producer output and fresh history, never a shared cache."""

    slot: VerifiedStateSlot | None = None
    observations: tuple[Any, ...] = ()
    history: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class StateContext:
    """The scope a producer must answer for, supplied by the invocation."""

    request: AuthorizationRequest
    checkpoint_id: str
    verified_approvals: frozenset[VerifiedApproval]
    receiver_generation: int
    condition_inventory: bytes | None = None
    condition_entries: tuple[Any, ...] = ()
    condition_coverage: tuple[bytes, ...] | None = None
    condition_proof: ConditionProof | None = None
    stage: str = "initial"
    retained_condition_slot: VerifiedStateSlot | None = None
    decision_facts: Mapping[str, Any] | None = None
    retained_durability_slot: VerifiedStateSlot | None = None


def condition_slot_matches(slot: VerifiedStateSlot, context: StateContext) -> bool:
    """Compare independently retained inventory, not two producer-supplied sets."""
    if context.condition_inventory is None or type(slot) is not VerifiedStateSlot:
        return False
    # Conditions of a controlled checkpoint are observed later, not in this slot.
    expected = tuple(
        key for key, condition, _coverage in context.condition_entries
        if getattr(condition, "checkpoint_id", None) == context.checkpoint_id
    )
    if slot.required_keys != expected:
        return False
    try:
        value = json.loads(slot.value_bytes)
        if type(value) is not dict or "inventory" not in value:
            return False
        if canonical_json_bytes(value, field="condition slot") != slot.value_bytes:
            return False
        inventory = canonical_json_bytes(value["inventory"], field="condition inventory")
    except (ValueError, TypeError, SETTValidationError, UnicodeError, RecursionError):
        return False
    return inventory == context.condition_inventory


@runtime_checkable
class VerifiedStateProducer(Protocol):
    """A registered internal path that may install one closed slot.

    This protocol is private on purpose. Condition production is explicitly
    configured, consent can be declared as owned by the implementation, a
    governance journal installs the durability producer, and review
    verifiers with that journal install the review producer. A
    synthetic producer registered by a test exercises the real
    validation; it does not establish that a production verifier exists.
    """

    slot: str
    producer_id: str
    schema_revision: str

    def produce(self, context: StateContext) -> VerifiedStateSlot | None:
        """Return the verified record for this slot, or None if unavailable."""
        ...

    def confirm(self, slot: VerifiedStateSlot) -> bool:
        """Report whether an already verified record is still current."""
        ...


def _identifier_keys(values: tuple[str, ...]) -> tuple[str, ...] | None:
    if len(values) > MAX_JSON_ITEMS:
        return None
    for value in values:
        if not isinstance(value, str):
            return None
        try:
            validate_identifier(value, field="state key")
        except (SETTValidationError, UnicodeError):
            return None
    if len(set(values)) != len(values):
        return None
    return tuple(sorted(values))


def validated_slot(slot: VerifiedStateSlot, context: StateContext) -> bool:
    """Check one produced slot against its registration and exact scope."""
    if type(slot) is not VerifiedStateSlot:
        return False
    if slot.action_id != context.request.action_id:
        return False
    if slot.checkpoint_id != context.checkpoint_id:
        return False
    if isinstance(slot.epoch, bool) or not isinstance(slot.epoch, int):
        return False
    if slot.epoch < 0:
        return False
    if not isinstance(slot.value_bytes, bytes):
        return False
    if len(slot.value_bytes) > 65_536:
        return False
    required = _identifier_keys(slot.required_keys)
    observed = _identifier_keys(slot.observed_keys)
    if required is None or observed is None or required != observed:
        return False
    if _identifier_keys(slot.evidence_refs) is None:
        return False
    if slot.valid_until is not None:
        if not isinstance(slot.valid_until, datetime):
            return False
        if slot.valid_until.utcoffset() is None:
            return False
    for name in (slot.slot, slot.producer_id, slot.schema_revision):
        if not isinstance(name, str):
            return False
        if len(name.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            return False
    return True


class ApprovalStateProducer:
    """The receiver-owned record of the authority actually verified.

    Its keys are the requirement identifiers whose authority one or more
    registered verifiers confirmed for this exact request, and its value
    bytes carry the complete verified triples together with the
    receiver's verifier generation. It binds what was verified so that a
    later change is detected; it is not a claim that every declared
    requirement was satisfied. That is still the aggregate
    REQUIRE_APPROVAL semantics.
    """

    slot = APPROVALS_SLOT
    producer_id = "sett.authorization_ruler.verified_approvals"
    schema_revision = "1"

    def produce(self, context: StateContext) -> VerifiedStateSlot | None:
        """Build the verified-approval record for one bound invocation."""
        ordered = sorted(
            context.verified_approvals,
            key=lambda item: (
                item.requirement_id,
                item.verifier_id,
                item.authority_id,
            ),
        )
        triples = [item.canonical_view() for item in ordered]
        keys = tuple({item.requirement_id for item in context.verified_approvals})
        try:
            value_bytes = canonical_json_bytes(
                {
                    "approvals": triples,
                    "verifier_generation": context.receiver_generation,
                },
                field="verified approvals",
            )
        except (SETTValidationError, UnicodeError):
            return None
        normalized = _identifier_keys(keys)
        if normalized is None:
            return None
        return VerifiedStateSlot(
            slot=self.slot,
            producer_id=self.producer_id,
            schema_revision=self.schema_revision,
            epoch=context.receiver_generation,
            action_id=context.request.action_id,
            checkpoint_id=context.checkpoint_id,
            required_keys=normalized,
            observed_keys=normalized,
            value_bytes=value_bytes,
            evidence_refs=(),
            valid_until=None,
        )

    def confirm(self, slot: VerifiedStateSlot) -> bool:
        """Report that a retained verified-approval record stays current."""
        return type(slot) is VerifiedStateSlot and slot.slot == self.slot


IMPLEMENTATION_OWNED_CONSENT_VALUE = b'{"consent":"owned_by_implementation"}'


class ImplementationOwnedConsentProducer:
    """The consent slot record of an implementation that owns consent.

    Installed only by ``PolicyRuler(implementation_owned_consent=True)``.
    The record states the implementation's declaration and nothing more:
    no keys were required or observed and no evidence was read, because
    SETT verified no consent. It is scoped to the exact action and
    checkpoint like every slot and compared like every slot; it is not an
    empty-state fallback.
    """

    slot = "consent"
    producer_id = "sett.policy_ruler.consent_owned_by_implementation"
    schema_revision = "1"

    def produce(self, context: StateContext) -> VerifiedStateSlot:
        """Record the declaration for one bound invocation."""
        return VerifiedStateSlot(
            slot=self.slot,
            producer_id=self.producer_id,
            schema_revision=self.schema_revision,
            epoch=0,
            action_id=context.request.action_id,
            checkpoint_id=context.checkpoint_id,
            required_keys=(),
            observed_keys=(),
            value_bytes=IMPLEMENTATION_OWNED_CONSENT_VALUE,
            evidence_refs=(),
            valid_until=None,
        )

    def confirm(self, slot: VerifiedStateSlot) -> bool:
        """Accept only a record with this producer's identity and value."""
        return (
            type(slot) is VerifiedStateSlot
            and slot.slot == self.slot
            and slot.producer_id == self.producer_id
            and slot.schema_revision == self.schema_revision
            and slot.epoch == 0
            and slot.value_bytes == IMPLEMENTATION_OWNED_CONSENT_VALUE
        )


# ── Local guard ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class CriticalPhase:
    """One in-progress guarded transition owned by exactly one thread."""

    thread: int
    poisoned: bool = False


class PolicyGuard:
    """One local serialization guard per PolicyRuler.

    Registry writes, producer registration, attached receivers' authority
    configuration changes and the final receipt transition all serialize
    here. Reentering a forbidden mutation from inside a critical
    transition, on the same thread, is rejected before any mutation and
    poisons that outer transition: catching the rejection cannot restore
    the outer call's authority. Other threads simply wait.

    A Python lock does not make direct mutation of callback internals or
    of external state observable, and it does not interrupt a running
    callback.
    """

    def __init__(self) -> None:
        """Create an unheld guard with no critical transition in progress."""
        self._lock = RLock()
        self._phase: CriticalPhase | None = None

    def acquire(self) -> None:
        """Acquire the guard for one short, callback-free section."""
        self._lock.acquire()

    def release(self) -> None:
        """Release the guard."""
        self._lock.release()

    def reject_reentrant_mutation(self) -> None:
        """Poison an in-progress transition that tries to mutate state."""
        phase = self._phase
        if phase is not None and phase.thread == get_ident():
            phase.poisoned = True
            raise policy_error("policy.transition_reentrant")

    def begin_critical(self) -> CriticalPhase:
        """Enter the guarded transition, rejecting same-thread reentry."""
        self._lock.acquire()
        existing = self._phase
        if existing is not None and existing.thread == get_ident():
            existing.poisoned = True
            self._lock.release()
            raise policy_error("policy.transition_reentrant")
        phase = CriticalPhase(get_ident())
        self._phase = phase
        return phase

    def end_critical(self, phase: CriticalPhase) -> None:
        """Leave the guarded transition and release the guard."""
        if self._phase is phase:
            self._phase = None
        self._lock.release()


# ── Identities and binding records ───────────────────────────────────────


class RulerIncarnation:
    """The private identity of one constructed PolicyRuler instance."""

    __slots__ = ()


class ReceiverLink:
    """The private link between one PolicyRuler and one attached receiver."""

    __slots__ = ("incarnation", "receiver", "approvals_producer")

    def __init__(self, incarnation: RulerIncarnation, receiver: object) -> None:
        """Bind one exact receiver object to one exact ruler incarnation."""
        self.incarnation = incarnation
        self.receiver = receiver
        self.approvals_producer = ApprovalStateProducer()


class Invocation:
    """One authorize call's private witness, opened and closed by the ruler."""

    __slots__ = (
        "link", "request", "checkpoint_id", "closed", "policy_observations",
        "policy_observation_reason_codes", "policy_observation_provenance", "failed_condition",
        "attempted_conditions", "request_fingerprint",
    )

    def __init__(
        self,
        link: ReceiverLink,
        request: AuthorizationRequest,
        checkpoint_id: str,
    ) -> None:
        """Open one fresh witness for an exact receiver, request and checkpoint."""
        self.link = link
        self.request = request
        self.checkpoint_id = checkpoint_id
        self.closed = False
        self.policy_observations: tuple[tuple[str, str, str], ...] = ()
        self.policy_observation_reason_codes: tuple[tuple[str, ...], ...] = ()
        self.policy_observation_provenance: tuple[dict[str, Any], ...] = ()
        self.failed_condition: tuple[str, str, str] | None = None
        self.attempted_conditions: tuple[tuple[str, str, str], ...] | None = None
        self.request_fingerprint: tuple[str, str] | None = None


@dataclass(slots=True)
class BindingRecord:
    """The complete detached state that makes one evaluation usable once."""

    incarnation: RulerIncarnation
    link: ReceiverLink
    invocation: Invocation
    evaluation: object
    evaluation_id: str
    verification_ref: str
    request_bytes: bytes
    selection_bytes: bytes
    policy_generation: int
    receiver_generation: int
    checkpoint_id: str
    state: Mapping[str, VerifiedStateSlot]
    evaluated_at: datetime
    valid_until: datetime
    lifecycle: str = PENDING
    receipt: object | None = None
    receiver: object | None = None
    state_signature: tuple[Any, ...] = field(default=())
    condition_inventory: bytes | None = None
    condition_entries: tuple[Any, ...] = ()
    condition_coverage: tuple[bytes, ...] | None = None
    condition_proof: ConditionProof | None = None
    condition_informational: bytes | None = None

    def is_live(self, now: datetime | None) -> bool:
        """Report whether this record still occupies a live capacity slot."""
        if self.lifecycle == TERMINAL:
            return False
        return not (now is not None and now >= self.valid_until)


def state_signature(state: Mapping[str, VerifiedStateSlot]) -> tuple[Any, ...]:
    """Return the complete comparable form of one verified-state record."""
    return tuple(
        (name, state[name].signature()) for name in SLOT_NAMES if name in state
    )


def canonical_request_bytes(
    request: AuthorizationRequest,
    verified_approvals: frozenset[VerifiedApproval],
) -> bytes | None:
    """Encode the full request view with SETT's fixed local JSON rules."""
    try:
        return canonical_json_bytes(
            request.canonical_view(verified_approvals),
            field="policy handoff request",
        )
    except (SETTValidationError, UnicodeError):
        return None


def canonical_selection_bytes(selection: PolicySelection) -> bytes | None:
    """Encode the full selection view with SETT's fixed local JSON rules."""
    try:
        return canonical_json_bytes(
            selection.canonical_view(), field="policy handoff selection"
        )
    except (SETTValidationError, UnicodeError):
        return None


def aware_utc(value: object) -> datetime | None:
    """Return one aware UTC datetime, or None for an unusable clock read."""
    if not isinstance(value, datetime):
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 -- caller timezone, fixed diagnostic
        return None
