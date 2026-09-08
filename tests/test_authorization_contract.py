"""Adversarial contract tests for SETT's Authorization Ruler."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sett import SETTValidationError
from sett.authorization_ruler import (
    ApprovalInput,
    ApprovalRequirement,
    AuthorizationConstraint,
    AuthorizationEvent,
    AuthorizationRequest,
    AuthorizationRuler,
    AuthorizationVerdict,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    PolicyDecision,
    ReceiptUse,
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    StrictJSONAuthorizationFingerprint,
    VerifiedApproval,
)


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current


class StaticPolicy:
    def __init__(self, policy_id: str, decision: PolicyDecision) -> None:
        self.policy_id = policy_id
        self._decision = decision
        self.calls: list[str] = []

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> PolicyDecision:
        self.calls.append(request.action_id)
        return self._decision


class ApprovalPolicy:
    policy_id = "policy.approval"

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> PolicyDecision:
        if any(
            item.requirement_id == "approval.operator"
            for item in verified_approvals
        ):
            return PolicyDecision.grant(
                self.policy_id,
                reason_codes=("approval.valid",),
            )
        return PolicyDecision.require_approval(
            self.policy_id,
            requirements=(
                ApprovalRequirement("approval.operator", "verifier.operator"),
            ),
            reason_codes=("approval.required",),
        )


class OperatorVerifier:
    verifier_id = "verifier.operator"

    def verify(
        self,
        approval_input: ApprovalInput,
        request: AuthorizationRequest,
    ) -> VerifiedApproval | None:
        evidence = approval_input.evidence
        if evidence == {"token": "valid"}:
            return VerifiedApproval(
                requirement_id=approval_input.requirement_id,
                verifier_id=self.verifier_id,
                authority_id="authority.operator",
            )
        return None


class ExplodingPolicy:
    policy_id = "policy.exploding"

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> PolicyDecision:
        raise RuntimeError("private provider details")


class RecordingPolicy:
    def __init__(self, policy_id: str, calls: list[str]) -> None:
        self.policy_id = policy_id
        self._calls = calls

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> PolicyDecision:
        self._calls.append(self.policy_id)
        return PolicyDecision.grant(self.policy_id)


class RawApprovalProbePolicy:
    policy_id = "policy.raw-approval-probe"

    def __init__(self) -> None:
        self.raw_evidence_seen = False

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[VerifiedApproval],
    ) -> PolicyDecision:
        reader = getattr(request, "_approval_inputs_for_verification", None)
        if callable(reader) and reader():
            self.raw_evidence_seen = True
            return PolicyDecision.grant(self.policy_id)
        return PolicyDecision.require_approval(
            self.policy_id,
            requirements=(
                ApprovalRequirement("approval.operator", "verifier.operator"),
            ),
        )


class FailingEvidence:
    def record(self, event: object) -> None:
        raise RuntimeError("sink unavailable")


def authoritative(value: Any, source_id: str) -> FieldValue:
    return FieldValue(value, source_id=source_id, trust=FieldTrust.AUTHORITATIVE)


def request(
    *,
    payload: dict[str, Any] | None = None,
    principal: str = "principal-1",
    resource: str = "resource-1",
    scope: tuple[str, ...] = ("scope.read",),
    purpose: str = "purpose.operation",
    context_value: str = "context-1",
    mode: ExecutionMode = ExecutionMode.PLAIN,
    idempotency_key: str | None = None,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        action_id="action-1",
        trace_id="trace-1",
        action_type="action.execute",
        payload=payload or {"value": 1},
        principal=authoritative(principal, "source.identity"),
        resource=authoritative(resource, "source.resource"),
        scope=tuple(authoritative(item, "source.scope") for item in scope),
        purpose=authoritative(purpose, "source.purpose"),
        data_labels=(authoritative("label.internal", "source.labels"),),
        context_facts={
            "context.fact": authoritative(context_value, "source.context"),
        },
        execution_mode=mode,
        idempotency_key=idempotency_key,
    )


def grant_policy(
    policy_id: str = "policy.grant",
    *,
    receipt_use: ReceiptUse = ReceiptUse.SINGLE_USE,
) -> StaticPolicy:
    return StaticPolicy(
        policy_id,
        PolicyDecision.grant(policy_id, receipt_use=receipt_use),
    )


def ruler(
    *,
    clock: FixedClock | None = None,
    evidence: InMemoryAuthorizationEvidence | None = None,
    store: InMemoryAuthorizationReceiptStore | None = None,
) -> AuthorizationRuler:
    return AuthorizationRuler(
        issuer_id="issuer.reference",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=store or InMemoryAuthorizationReceiptStore(),
        evidence_sink=evidence or InMemoryAuthorizationEvidence(),
        clock=clock or FixedClock(),
    )


def test_request_detaches_security_relevant_values() -> None:
    payload = {"nested": {"items": [1, 2]}}
    principal_value = {"id": "principal-1"}
    item = AuthorizationRequest(
        action_id="action-1",
        trace_id="trace-1",
        action_type="action.execute",
        payload=payload,
        principal=authoritative(principal_value, "source.identity"),
    )

    payload["nested"]["items"].append(3)
    principal_value["id"] = "attacker"

    assert item.payload_copy() == {"nested": {"items": [1, 2]}}
    assert item.principal.value == {"id": "principal-1"}


@pytest.mark.parametrize("value", [{"bad": {1, 2}}, {"bad": float("nan")}])
def test_request_rejects_values_without_a_strict_json_representation(
    value: object,
) -> None:
    with pytest.raises(SETTValidationError):
        request(payload=value)  # type: ignore[arg-type]


def test_request_rejects_non_string_json_object_keys_structurally() -> None:
    with pytest.raises(SETTValidationError):
        request(payload={1: "value"})  # type: ignore[dict-item]


@pytest.mark.parametrize("trust", [FieldTrust.PROPOSED, FieldTrust.UNKNOWN])
def test_non_authoritative_principal_cannot_reach_a_grant(
    trust: FieldTrust,
) -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = AuthorizationRequest(
        action_id="action-1",
        trace_id="trace-1",
        action_type="action.execute",
        payload={"value": 1},
        principal=FieldValue(
            "principal-1",
            source_id="source.proposal",
            trust=trust,
        ),
    )

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.authorize(item)

    assert raised.value.reason_code == "authorization.provenance_not_authoritative"


@pytest.mark.parametrize(
    ("change", "kwargs"),
    [
        ("payload", {"payload": {"value": 2}}),
        ("principal", {"principal": "principal-2"}),
        ("resource", {"resource": "resource-2"}),
        ("scope", {"scope": ("scope.write",)}),
        ("purpose", {"purpose": "purpose.other"}),
        ("context", {"context_value": "context-2"}),
        ("mode", {"mode": ExecutionMode.CONTROLLED}),
        ("operation", {"idempotency_key": "operation-2"}),
    ],
)
def test_fingerprint_binds_every_security_dimension(
    change: str,
    kwargs: dict[str, Any],
) -> None:
    policy = StrictJSONAuthorizationFingerprint()
    baseline = request(idempotency_key="operation-1")
    changed_arguments = {"idempotency_key": "operation-1", **kwargs}
    changed = request(**changed_arguments)

    assert policy.fingerprint(baseline, frozenset()) != policy.fingerprint(
        changed,
        frozenset(),
    ), change


def test_fingerprint_binds_verified_approval_authority() -> None:
    policy = StrictJSONAuthorizationFingerprint()
    item = request()
    first = frozenset(
        {
            VerifiedApproval(
                "approval.operator", "verifier.operator", "authority.first"
            )
        }
    )
    second = frozenset(
        {
            VerifiedApproval(
                "approval.operator", "verifier.operator", "authority.second"
            )
        }
    )

    assert policy.fingerprint(item, first) != policy.fingerprint(item, second)


def test_policies_run_in_stable_identifier_order() -> None:
    calls: list[str] = []
    gate = ruler()
    gate.register_policy("action.execute", RecordingPolicy("policy.z", calls))
    gate.register_policy("action.execute", RecordingPolicy("policy.a", calls))

    outcome = gate.authorize(request())

    assert outcome.decision.verdict is AuthorizationVerdict.GRANT
    assert calls == ["policy.a", "policy.z"]


def test_any_deny_is_terminal_for_the_aggregate_decision() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy("policy.a"))
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.b",
            PolicyDecision.deny("policy.b", reason_codes=("policy.denied",)),
        ),
    )
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.c",
            PolicyDecision.require_approval(
                "policy.c",
                requirements=(
                    ApprovalRequirement("approval.operator", "verifier.operator"),
                ),
            ),
        ),
    )

    outcome = gate.authorize(request())

    assert outcome.decision.verdict is AuthorizationVerdict.DENY
    assert outcome.receipt is None


def test_missing_exact_action_policy_fails_closed() -> None:
    gate = ruler()
    gate.register_policy("action.other", grant_policy())

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.authorize(request())

    assert raised.value.reason_code == "authorization.policy_missing"


def test_policy_exception_is_a_system_failure_without_provider_message() -> None:
    gate = ruler()
    gate.register_policy("action.execute", ExplodingPolicy())

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.authorize(request())

    assert raised.value.reason_code == "authorization.policy_failed"
    assert "private provider details" not in str(raised.value)


def test_requirement_without_verified_evidence_produces_no_receipt() -> None:
    gate = ruler()
    gate.register_policy("action.execute", ApprovalPolicy())

    outcome = gate.authorize(request())

    assert outcome.decision.verdict is AuthorizationVerdict.REQUIRE_APPROVAL
    assert outcome.receipt is None


def test_injected_verifier_can_satisfy_only_its_declared_requirement() -> None:
    gate = ruler()
    gate.register_policy("action.execute", ApprovalPolicy())
    gate.register_approval_verifier(OperatorVerifier())
    approval = ApprovalInput(
        requirement_id="approval.operator",
        verifier_id="verifier.operator",
        evidence={"token": "valid"},
    )

    outcome = gate.authorize(request(), approval_inputs=(approval,))

    assert outcome.decision.verdict is AuthorizationVerdict.GRANT
    assert outcome.receipt is not None


def test_self_asserted_or_invalid_approval_does_not_grant() -> None:
    gate = ruler()
    gate.register_policy("action.execute", ApprovalPolicy())
    gate.register_approval_verifier(OperatorVerifier())
    approval = ApprovalInput(
        requirement_id="approval.operator",
        verifier_id="verifier.operator",
        evidence={"token": "self-asserted"},
    )

    outcome = gate.authorize(request(), approval_inputs=(approval,))

    assert outcome.decision.verdict is AuthorizationVerdict.REQUIRE_APPROVAL
    assert outcome.receipt is None


def test_policy_cannot_read_raw_approval_inputs_from_the_request_api() -> None:
    gate = ruler()
    policy = RawApprovalProbePolicy()
    gate.register_policy("action.execute", policy)
    gate.register_approval_verifier(OperatorVerifier())
    approval = ApprovalInput(
        requirement_id="approval.operator",
        verifier_id="verifier.operator",
        evidence={"token": "self-asserted"},
    )

    outcome = gate.authorize(request(), approval_inputs=(approval,))

    assert outcome.decision.verdict is AuthorizationVerdict.REQUIRE_APPROVAL
    assert outcome.receipt is None
    assert not policy.raw_evidence_seen


def test_receipt_public_fields_are_not_authority() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = request()
    receipt = gate.authorize(item).receipt
    assert receipt is not None
    forged = replace(receipt, request_fingerprint="0" * 64)

    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(forged, item)

    assert raised.value.reason_code == "authorization.receipt_mismatch"


def test_payload_mismatch_blocks_receipt_use() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    receipt = gate.authorize(request()).receipt
    assert receipt is not None

    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(receipt, request(payload={"value": 2}))

    assert raised.value.reason_code == "authorization.request_mismatch"


def test_expired_receipt_blocks_use() -> None:
    clock = FixedClock()
    gate = ruler(clock=clock)
    gate.register_policy("action.execute", grant_policy())
    item = request()
    receipt = gate.authorize(item).receipt
    assert receipt is not None
    clock.current += timedelta(seconds=61)

    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(receipt, item)

    assert raised.value.reason_code == "authorization.receipt_expired"


def test_revoked_receipt_blocks_use() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = request()
    receipt = gate.authorize(item).receipt
    assert receipt is not None
    gate.revoke(receipt.receipt_id)

    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(receipt, item)

    assert raised.value.reason_code == "authorization.receipt_revoked"


def test_plain_receipt_is_single_use() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = request()
    receipt = gate.authorize(item).receipt
    assert receipt is not None

    gate.verify_and_consume(receipt, item)
    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(receipt, item)

    assert raised.value.reason_code == "authorization.receipt_consumed"


def test_request_cannot_choose_replay_without_policy_consent() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = request(
        mode=ExecutionMode.CONTROLLED,
        idempotency_key="operation-1",
    )
    receipt = gate.authorize(item).receipt
    assert receipt is not None
    assert receipt.receipt_use is ReceiptUse.SINGLE_USE

    gate.verify_and_consume(receipt, item)
    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(receipt, item)

    assert raised.value.reason_code == "authorization.receipt_consumed"


def test_replay_requires_unanimous_policy_consent() -> None:
    gate = ruler()
    gate.register_policy(
        "action.execute",
        grant_policy(
            "policy.allows-replay",
            receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
        ),
    )
    gate.register_policy("action.execute", grant_policy("policy.single-use"))
    item = request(
        mode=ExecutionMode.CONTROLLED,
        idempotency_key="operation-1",
    )

    receipt = gate.authorize(item).receipt

    assert receipt is not None
    assert receipt.receipt_use is ReceiptUse.SINGLE_USE


def test_controlled_receipt_allows_only_same_idempotent_operation() -> None:
    gate = ruler()
    gate.register_policy(
        "action.execute",
        grant_policy(
            receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
        ),
    )
    item = request(
        mode=ExecutionMode.CONTROLLED,
        idempotency_key="operation-1",
    )
    receipt = gate.authorize(item).receipt
    assert receipt is not None

    gate.verify_and_consume(receipt, item)
    gate.verify_and_consume(receipt, item)

    with pytest.raises(SETTAuthorizationError):
        gate.verify_and_consume(
            receipt,
            request(
                mode=ExecutionMode.CONTROLLED,
                idempotency_key="operation-2",
            ),
        )


def test_public_receipt_cannot_expand_its_use_semantics() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = request(
        mode=ExecutionMode.CONTROLLED,
        idempotency_key="operation-1",
    )
    receipt = gate.authorize(item).receipt
    assert receipt is not None
    forged = replace(
        receipt,
        receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
    )

    with pytest.raises(SETTAuthorizationError) as raised:
        gate.verify_and_consume(forged, item)

    assert raised.value.reason_code == "authorization.receipt_mismatch"


def test_receipt_use_has_no_abstention_value() -> None:
    assert set(ReceiptUse) == {
        ReceiptUse.SINGLE_USE,
        ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
    }


def test_single_use_consumption_is_atomic_under_concurrency() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy())
    item = request()
    receipt = gate.authorize(item).receipt
    assert receipt is not None

    def consume() -> bool:
        try:
            gate.verify_and_consume(receipt, item)
        except SETTAuthorizationError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: consume(), range(2_000)))

    assert results.count(True) == 1


def test_evidence_contains_shape_but_not_sensitive_values() -> None:
    evidence = InMemoryAuthorizationEvidence()
    gate = ruler(evidence=evidence)
    gate.register_policy("action.execute", grant_policy())
    sensitive = request(
        payload={"secret": "payload-secret"},
        principal="principal-secret",
        resource="resource-secret",
        scope=("scope-secret",),
    )

    outcome = gate.authorize(sensitive)
    assert outcome.receipt is not None
    gate.verify_and_consume(outcome.receipt, sensitive)
    exported = repr(
        [
            (event.kind, event.reason_codes, event.attributes)
            for event in evidence.events()
        ]
    )

    for secret in (
        "payload-secret",
        "principal-secret",
        "resource-secret",
        "scope-secret",
        outcome.receipt.request_fingerprint,
    ):
        assert secret not in exported


def test_authorization_evidence_preserves_concurrent_events() -> None:
    evidence = InMemoryAuthorizationEvidence()

    def record_batch(worker: int) -> None:
        for index in range(200):
            evidence.record(
                AuthorizationEvent(
                    "authorization.policy_checked",
                    attributes={"worker": worker, "index": index},
                )
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(record_batch, range(8)))

    events = evidence.events()
    assert len(events) == 1_600
    assert {
        (event.attributes["worker"], event.attributes["index"])
        for event in events
    } == {(worker, index) for worker in range(8) for index in range(200)}


def test_evidence_failure_prevents_receipt_issue() -> None:
    gate = AuthorizationRuler(
        issuer_id="issuer.reference",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=FailingEvidence(),
        clock=FixedClock(),
    )
    gate.register_policy("action.execute", grant_policy())

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.authorize(request())

    assert raised.value.reason_code == "authorization.evidence_failed"


@pytest.mark.parametrize("ttl", [None, 0, -1, True, float("nan"), float("inf")])
def test_receipt_lifetime_has_no_silent_or_unlimited_default(ttl: object) -> None:
    with pytest.raises((TypeError, SETTValidationError)):
        AuthorizationRuler(
            issuer_id="issuer.reference",
            receipt_ttl_seconds=ttl,  # type: ignore[arg-type]
            fingerprint_policy=StrictJSONAuthorizationFingerprint(),
            receipt_store=InMemoryAuthorizationReceiptStore(),
            evidence_sink=InMemoryAuthorizationEvidence(),
            clock=FixedClock(),
        )


def test_duplicate_policy_identifier_is_rejected() -> None:
    gate = ruler()
    gate.register_policy("action.execute", grant_policy("policy.same"))

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.register_policy("action.execute", grant_policy("policy.same"))

    assert raised.value.reason_code == "authorization.policy_duplicate"


def test_constraints_are_descriptors_and_never_executed_by_the_ruler() -> None:
    constraint = AuthorizationConstraint(
        "constraint.limit",
        metadata={"limit": 10},
    )
    gate = ruler()
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.grant",
            PolicyDecision.grant("policy.grant", constraints=(constraint,)),
        ),
    )

    outcome = gate.authorize(request())

    assert outcome.decision.constraints == (constraint,)
    assert outcome.receipt is not None
    assert outcome.receipt.constraint_ids == ("constraint.limit",)


def test_constraint_identifiers_cannot_collide_across_policies() -> None:
    constraint = AuthorizationConstraint("constraint.same")
    evidence = InMemoryAuthorizationEvidence()
    gate = ruler(evidence=evidence)
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.a",
            PolicyDecision.grant("policy.a", constraints=(constraint,)),
        ),
    )
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.b",
            PolicyDecision.grant("policy.b", constraints=(constraint,)),
        ),
    )

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.authorize(request())

    assert raised.value.reason_code == "authorization.constraint_duplicate"
    assert evidence.events()[-1].kind == "authorization.failed"
    assert evidence.events()[-1].reason_codes == (
        "authorization.constraint_duplicate",
    )


def test_requirement_identifier_cannot_name_two_verifiers() -> None:
    evidence = InMemoryAuthorizationEvidence()
    gate = ruler(evidence=evidence)
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.a",
            PolicyDecision.require_approval(
                "policy.a",
                requirements=(ApprovalRequirement("approval.same", "verifier.a"),),
            ),
        ),
    )
    gate.register_policy(
        "action.execute",
        StaticPolicy(
            "policy.b",
            PolicyDecision.require_approval(
                "policy.b",
                requirements=(ApprovalRequirement("approval.same", "verifier.b"),),
            ),
        ),
    )

    with pytest.raises(SETTAuthorizationConfigurationError) as raised:
        gate.authorize(request())

    assert (
        raised.value.reason_code
        == "authorization.approval_requirement_conflict"
    )
    assert evidence.events()[-1].kind == "authorization.failed"
    assert evidence.events()[-1].reason_codes == (
        "authorization.approval_requirement_conflict",
    )
