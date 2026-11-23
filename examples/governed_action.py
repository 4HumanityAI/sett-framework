"""Govern one action end to end with the policy layer.

A refund runs only when the application's policy grants it and the
condition the policy declares is verified. Every state producer that
authorization needs is configured explicitly:

- conditions: the application's own verifier, `IdentityVerifier` below;
- consent: the implementation declares that it owns consent, so SETT
  records that declaration and verifies no consent of its own;
- durability: a governance journal keeps one decision record per
  completed evaluation;
- review: review verifiers are configured, none is needed here.

Three attempts follow. The first is granted and its handler runs once.
The second is blocked because the declared condition is not met, and the
third because the policy denies it; neither runs the handler. The journal
then holds the decision records of the two completed evaluations, and
each decision, or the error of the blocked attempt, can be explained.

The journal here is in memory. A deployment chooses a stream with the
durability it requires; see `docs/governance_journal.md`.

Run:
    python -B examples/governed_action.py
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sett import (
    Action,
    AuthorizationRequest,
    AuthorizationRuler,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    SETTAuthorizationError,
    SETTExecutor,
    SETTOrchestrator,
    StrictJSONAuthorizationFingerprint,
    SystemAuthorizationClock,
    __version__,
)
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream
from sett.policy_ruler import (
    ConditionValidityMode,
    ConditionVerification,
    ConditionVerificationState,
    GovernanceJournal,
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyCondition,
    PolicyDecision,
    PolicyOrigin,
    PolicyRuler,
    PolicySelection,
)
from sett.policy_ruler.decision_record import read_decision_records
from sett.policy_ruler.explanation import explain

REFUND = "example.refund"
ENTRY = "example.authorization_entry"

BINDING = PolicyBinding(
    policy_id="example.refund_limit",
    policy_version="1",
    contract_version="2.0",
    origins=[
        PolicyOrigin(
            requirement_id="example.refund_rules",
            requirement_revision="1",
            layer_id="example.organization",
            source=None,
            source_absence_reason="Internal rule defined for this example",
        )
    ],
    profile_id=None,
    profile_revision=None,
)


class RefundLimitPolicy:
    """Grant refunds up to a limit, on condition that the customer is verified."""

    policy_id = BINDING.policy_id
    LIMIT = 100

    def evaluate(self, request: AuthorizationRequest, verified_approvals: frozenset) -> PolicyDecision:
        if request.payload_copy().get("amount", 0) > self.LIMIT:
            return PolicyDecision.deny(
                self.policy_id, reason_codes=("example.amount_above_limit",),
                conditions=(), review_obligations=(),
            )
        verified_customer = PolicyCondition(
            condition_id="example.customer_verified",
            binding=BINDING,
            origins=BINDING.origins,
            verifier_id=IdentityVerifier.verifier_id,
            verifier_version=IdentityVerifier.verifier_version,
            required=True,
            action_id=request.action_id,
            action_type=request.action_type,
            checkpoint_id=ENTRY,
            validity_mode=ConditionValidityMode.RECHECK,
            attestation_not_after=None,
        )
        return PolicyDecision.grant(
            self.policy_id, reason_codes=("example.amount_within_limit",),
            conditions=(verified_customer,), review_obligations=(),
        )


class IdentityVerifier:
    """The application's check of the declared condition.

    A real verifier would consult the application's identity records; this
    one reads a flag from the request so the example runs on its own.
    """

    verifier_id = "example.identity_verifier"
    verifier_version = "1"

    def __init__(self, clock: SystemAuthorizationClock) -> None:
        self.clock = clock

    def verify(self, request: AuthorizationRequest, *, condition: PolicyCondition) -> ConditionVerification:
        if request.payload_copy().get("customer_verified") is True:
            return ConditionVerification(
                condition=condition, state=ConditionVerificationState.SATISFIED,
                checked_at=self.clock.now(), valid_until=None,
                evidence_refs=("example.identity_record",), reason_codes=(),
            )
        return ConditionVerification(
            condition=condition, state=ConditionVerificationState.UNSATISFIED,
            checked_at=self.clock.now(), valid_until=None,
            evidence_refs=(), reason_codes=("example.customer_not_verified",),
        )

    def confirm(self, verification: ConditionVerification, *, request: AuthorizationRequest) -> bool:
        return request.payload_copy().get("customer_verified") is True


class RefundSelection:
    """Declare that the refund limit policy applies to every refund."""

    resolver_id = "example.selection"

    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        return PolicySelection(
            selection_id="example.refund_selection",
            selection_revision="1",
            contract_version="2.0",
            authority_ref="example.deployment",
            effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id,
            action_type=request.action_type,
            required=[BINDING],
            informative=[],
            applicability=[
                PolicyApplicability(
                    policy_id=BINDING.policy_id,
                    policy_version=BINDING.policy_version,
                    state=PolicyApplicabilityState.APPLIES,
                    justification="Declared by this deployment for every refund",
                    fact_names=[],
                    evidence_refs=[],
                )
            ],
            facts={},
        )


class SessionResolver:
    """Turn one execution identity into an authorization request."""

    def resolve(
        self,
        *,
        action_id: str,
        trace_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        execution_mode: ExecutionMode,
        idempotency_key: str | None,
    ) -> AuthorizationRequest:
        return AuthorizationRequest(
            action_id=action_id,
            trace_id=trace_id,
            action_type=action_type,
            payload=dict(payload),
            principal=FieldValue("example.operator", source_id="example.session", trust=FieldTrust.AUTHORITATIVE),
            execution_mode=execution_mode,
            idempotency_key=idempotency_key,
        )


def build() -> tuple[SETTExecutor, GovernanceJournal, list[dict[str, Any]]]:
    """Wire the policy layer, authorization and execution."""
    clock = SystemAuthorizationClock()
    evidence = InMemoryAuthorizationEvidence()
    stream = InMemoryEvidenceStream(backend_id="example.backend", stream_id="example.decisions")
    journal = GovernanceJournal(
        namespace_id="example.namespace", journal_id="example.journal", stream=stream,
        origin=stream.head(), required_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(), historical_integrity_policies=(),
        max_frame_bytes=65536, max_records=64, max_total_bytes=4_194_304,
    )
    policy_ruler = PolicyRuler(
        ruler_id="example.policy_ruler",
        entry_checkpoint_id=ENTRY,
        evaluation_ttl_seconds=60,
        max_live_evaluations=16,
        clock=clock,
        evidence_sink=evidence,
        selection_resolver=RefundSelection(),
        condition_verifiers=[IdentityVerifier(clock)],
        implementation_owned_consent=True,
        governance_journal=journal,
        review_verifiers=[],
    )
    authorization = AuthorizationRuler(
        issuer_id="example.issuer",
        receipt_ttl_seconds=30,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=evidence,
        clock=clock,
        policy_ruler=policy_ruler,
    )
    authorization.register_policy(REFUND, RefundLimitPolicy(), binding=BINDING)
    executor = SETTExecutor(authorization_ruler=authorization, authorization_request_resolver=SessionResolver())
    refunds: list[dict[str, Any]] = []
    executor.register_handler(REFUND, lambda payload: refunds.append(payload) or "refunded")
    SETTOrchestrator().register_executor(executor)
    return executor, journal, refunds


def main() -> None:
    executor, journal, refunds = build()
    print(f"SETT {__version__}: one governed action, every state producer configured")
    attempts = [
        ("refund of 40, customer verified", {"amount": 40, "customer_verified": True}),
        ("refund of 40, customer not verified", {"amount": 40, "customer_verified": False}),
        ("refund of 250, customer verified", {"amount": 250, "customer_verified": True}),
    ]
    blocked = None
    for label, payload in attempts:
        try:
            outcome = executor.submit(Action(action_type=REFUND, payload=payload))
        except SETTAuthorizationError as error:
            blocked = blocked or error
            outcome = f"blocked: {error.reason_code}"
        print(f"  {label:36} {outcome}")
    print(f"Handler runs: {len(refunds)}")

    records = read_decision_records(journal).decisions
    print("Decision records:", ", ".join(record.decision["verdict"] for record in records))
    (condition,) = explain(records[0]).operational_view()["conditions"]
    print(f"Grant, for operators: condition {condition['condition_id']} {condition['state']}")
    public = explain(blocked).public_view()
    print(f"Blocked attempt, for the public: {public['reason_code']}, {public['withheld']} values withheld")


if __name__ == "__main__":
    main()
