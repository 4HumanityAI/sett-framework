"""Joint contract tests for Authorization and the Execution Ruler."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import pytest

from sett import (
    Action,
    ApprovalInput,
    AuthorizationRequest,
    AuthorizationRuler,
    EthicalFilter,
    EthicalRuleset,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    PolicyDecision,
    ReceiptUse,
    SETTAgent,
    SETTAuthorizationError,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTExecutor,
    SETTOrchestrator,
    SETTPersistenceError,
    SETTValidationError,
    StrictJSONAuthorizationFingerprint,
    SystemAuthorizationClock,
)
from sett._governance.validation import canonical_json_bytes
from sett.audit_ruler.closure import AuditClosureStatus
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.authorization_ruler import (
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyOrigin,
    PolicySelection,
)
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryEvidenceStream,
    InMemoryStateRecordStore,
    PersistenceBackend,
    PersistencePolicy,
    decode_trace_evidence_frame,
)
from sett.policy_ruler import PolicyRuler
from sett.policy_ruler._handoff import PRODUCED_SLOT_NAMES, VerifiedStateSlot

ENTRY_CHECKPOINT = "test_fixture.entry"


def declared_binding(policy_id: str) -> PolicyBinding:
    """Build one neutral declared binding for a test policy."""
    return PolicyBinding(
        policy_id=policy_id,
        policy_version="1",
        contract_version="2.0",
        origins=[
            PolicyOrigin(
                requirement_id="test_fixture.requirement",
                requirement_revision="1",
                layer_id="test_fixture.layer",
                source=None,
                source_absence_reason="Synthetic test fixture",
            )
        ],
        profile_id=None,
        profile_revision=None,
    )


class SingleSelectionResolver:
    """Declare one required policy for whichever action is authorized."""

    resolver_id = "test_fixture.resolver"

    def __init__(self, policy_id: str) -> None:
        self.binding = declared_binding(policy_id)

    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        """Return the declared selection for this exact action."""
        return PolicySelection(
            selection_id="test_fixture.selection",
            selection_revision="1",
            contract_version="2.0",
            authority_ref="test_fixture.authority",
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id,
            action_type=request.action_type,
            required=[self.binding],
            informative=[],
            applicability=[
                PolicyApplicability(
                    policy_id=self.binding.policy_id,
                    policy_version=self.binding.policy_version,
                    state=PolicyApplicabilityState.APPLIES,
                    justification="Synthetic test declaration",
                    fact_names=[],
                    evidence_refs=[],
                )
            ],
            facts={},
        )


class SyntheticStateProducer:
    """A test-only producer for a slot whose real adapter is not built."""

    schema_revision = "1"

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self.producer_id = "test_fixture.producer." + slot

    def produce(self, context: Any) -> VerifiedStateSlot:
        """Return one synthetic complete record for this slot."""
        value_bytes = b"{}"
        if self.slot == "conditions":
            value_bytes = canonical_json_bytes({
                "inventory": json.loads(context.condition_inventory),
                "observations": {},
                "test_fixture_value_hex": value_bytes.hex(),
            }, field="synthetic condition state")
        return VerifiedStateSlot(
            slot=self.slot,
            producer_id=self.producer_id,
            schema_revision=self.schema_revision,
            epoch=0,
            action_id=context.request.action_id,
            checkpoint_id=context.checkpoint_id,
            required_keys=(),
            observed_keys=(),
            value_bytes=value_bytes,
            evidence_refs=(),
            valid_until=None,
        )

    def confirm(self, slot: VerifiedStateSlot) -> bool:
        """Report that the synthetic record is still current."""
        return True


def configured_authorization(policy_id: str) -> AuthorizationRuler:
    """Build one authorization issuer over its own configured policy owner."""
    evidence = InMemoryAuthorizationEvidence()
    owner = PolicyRuler(
        ruler_id="test_fixture.policy_ruler",
        entry_checkpoint_id=ENTRY_CHECKPOINT,
        evaluation_ttl_seconds=3_600,
        max_live_evaluations=256,
        clock=SystemAuthorizationClock(),
        evidence_sink=evidence,
        selection_resolver=SingleSelectionResolver(policy_id),
    )
    for name in PRODUCED_SLOT_NAMES:
        owner._register_state_producer(SyntheticStateProducer(name))
    return AuthorizationRuler(
        issuer_id="issuer.local",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=evidence,
        policy_ruler=owner,
    )


class StaticPolicy:
    """Return one fixed decision for every request."""

    def __init__(self, policy_id: str, decision: PolicyDecision) -> None:
        self.policy_id = policy_id
        self._decision = decision

    def evaluate(
        self,
        request: AuthorizationRequest,
        verified_approvals: frozenset[object],
    ) -> PolicyDecision:
        """Return the configured deterministic decision."""

        return self._decision


class AuthoritativeResolver:
    """Build complete requests while preserving every SETT-owned field."""

    def __init__(self, *, replace_payload: Mapping[str, Any] | None = None) -> None:
        self.replace_payload = replace_payload
        self.modes: list[ExecutionMode] = []
        self.trace_ids: list[str] = []

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
        """Add application authority without changing execution identity."""

        self.modes.append(execution_mode)
        self.trace_ids.append(trace_id)
        return AuthorizationRequest(
            action_id=action_id,
            trace_id=trace_id,
            action_type=action_type,
            payload=(
                dict(self.replace_payload)
                if self.replace_payload is not None
                else dict(payload)
            ),
            principal=FieldValue(
                "principal.local",
                source_id="identity.session",
                trust=FieldTrust.AUTHORITATIVE,
            ),
            execution_mode=execution_mode,
            idempotency_key=idempotency_key,
        )


def configured_executor(
    decision: PolicyDecision,
    *,
    resolver: AuthoritativeResolver | None = None,
    ethical_filter: EthicalFilter | None = None,
    orchestrator: SETTOrchestrator | None = None,
) -> tuple[SETTExecutor, SETTOrchestrator, AuthoritativeResolver]:
    """Build one attached executor with deterministic authorization."""

    ruler = configured_authorization(decision.policy_id)
    ruler.register_policy(
        "record",
        StaticPolicy(decision.policy_id, decision),
        binding=declared_binding(decision.policy_id),
    )
    selected_resolver = resolver or AuthoritativeResolver()
    executor = SETTExecutor(
        authorization_ruler=ruler,
        authorization_request_resolver=selected_resolver,
    )
    orchestrator = orchestrator or SETTOrchestrator(ethical_filter=ethical_filter)
    orchestrator.register_executor(executor)
    return executor, orchestrator, selected_resolver


def test_execution_ruler_preserves_public_and_historical_imports() -> None:
    import sett
    from sett.core_ruler import executor as historical
    from sett.core_ruler.executor import SETTExecutor as HistoricalExecutor
    from sett.execution_ruler import SETTExecutor as RulerExecutor
    from sett.execution_ruler import executor as implementation

    assert sett.SETTExecutor is RulerExecutor
    assert HistoricalExecutor is RulerExecutor
    assert RulerExecutor.__module__ == "sett.execution_ruler.executor"
    assert historical.HandlerContext is implementation.HandlerContext
    assert historical.current_handler_context is implementation.current_handler_context


def test_authorization_configuration_is_all_or_nothing() -> None:
    ruler = configured_authorization("policy.allow")

    with pytest.raises(SETTValidationError):
        SETTExecutor(authorization_ruler=ruler)

    with pytest.raises(SETTValidationError):
        SETTExecutor(authorization_request_resolver=AuthoritativeResolver())


def test_grant_is_verified_before_handler_authorization() -> None:
    executor, orchestrator, resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=())
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", lambda payload: calls.append(payload) or "ok")
    result = executor.submit(
        Action(action_type="record", payload={"value": 1}),
    )

    assert result == "ok"
    assert calls == [{"value": 1}]
    trace_id = resolver.trace_ids[-1]
    events = orchestrator.get_trace(trace_id)
    kinds = [event.kind for event in events]
    verified_index = kinds.index("authorization.receipt_verified")
    authorized_index = kinds.index("handler.authorized")
    assert verified_index < authorized_index
    assert events[authorized_index].cause_id == events[verified_index].event_id
    assert orchestrator.verify_traces(trace_id)


def test_denial_never_reaches_the_handler() -> None:
    executor, orchestrator, resolver = configured_executor(
        PolicyDecision.deny("policy.deny", reason_codes=("policy.denied",), conditions=(), review_obligations=())
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", calls.append)

    with pytest.raises(SETTAuthorizationError) as raised:
        executor.submit(Action(action_type="record", payload={"value": 1}))

    assert raised.value.reason_code == "authorization.denied"
    assert calls == []
    trace_id = resolver.trace_ids[-1]
    events = orchestrator.get_trace(trace_id)
    assert any(event.kind == "authorization.receipt_rejected" for event in events)
    assert not any(event.kind == "handler.authorized" for event in events)
    assert orchestrator.verify_traces(trace_id)


def test_raw_approval_evidence_never_reaches_execution_trace() -> None:
    executor, orchestrator, resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=())
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", calls.append)
    secret = "one-time-code-884213"

    with pytest.raises(SETTAuthorizationError):
        executor.submit(
            Action(action_type="record", payload={"value": 1}),
            approval_inputs=(
                ApprovalInput(
                    requirement_id="approval.local",
                    verifier_id="verifier.missing",
                    evidence=secret,
                ),
            ),
        )

    trace_id = resolver.trace_ids[-1]
    exported = orchestrator.export_trace(trace_id)
    assert secret not in repr(exported)
    assert calls == []
    assert orchestrator.verify_traces(trace_id)


def test_ethics_rejection_happens_before_authorization_resolution() -> None:
    resolver = AuthoritativeResolver()
    strict = EthicalRuleset(
        name="strict",
        reject_threshold=0.5,
        warn_threshold=0.1,
    )
    executor, orchestrator, _resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=()),
        resolver=resolver,
        ethical_filter=EthicalFilter(ruleset=strict),
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", calls.append)

    with pytest.raises(SETTEthicalFilterRejectedError):
        executor.submit(
            Action(action_type="record", payload={"value": 1}),
            emotional_state="crisis",
        )

    assert resolver.trace_ids == []
    assert calls == []
    events = orchestrator.get_trace(orchestrator._trace_recorder._events[0].trace_id)
    assert not any(event.kind.startswith("authorization.") for event in events)
    assert orchestrator.verify_traces(events[0].trace_id)


def test_resolver_cannot_replace_the_detached_payload() -> None:
    resolver = AuthoritativeResolver(replace_payload={"value": 2})
    executor, _orchestrator, _resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=()),
        resolver=resolver,
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", calls.append)

    with pytest.raises(SETTAuthorizationError) as raised:
        executor.submit(Action(action_type="record", payload={"value": 1}))

    assert raised.value.reason_code == "authorization.request_mismatch"
    assert calls == []


def test_exporter_cannot_switch_authority_after_handler_resolution() -> None:
    executor, orchestrator, resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=())
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("restricted", calls.append)
    action = Action(action_type="restricted", payload={"value": 1})

    def mutate_caller_action(event: Mapping[str, Any]) -> None:
        if event["kind"] == "action.proposed":
            action.action_type = "record"

    orchestrator.register_trace_exporter(mutate_caller_action)
    with pytest.raises(SETTAuthorizationError) as raised:
        executor.submit(action)

    assert raised.value.reason_code == "authorization.policy_missing"
    assert calls == []
    assert action.action_type == "record"
    trace_id = resolver.trace_ids[-1]
    events = orchestrator.get_trace(trace_id)
    assert not any(event.kind == "handler.authorized" for event in events)
    assert orchestrator.verify_traces(trace_id)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (1, True),
        ({"nested": [False]}, {"nested": [0]}),
        (1.0, 1),
        (-0.0, 0.0),
    ],
)
def test_resolver_cannot_change_payload_scalar_representation(
    original: Any,
    replacement: Any,
) -> None:
    resolver = AuthoritativeResolver(replace_payload={"value": replacement})
    executor, _orchestrator, _resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=()),
        resolver=resolver,
    )
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", calls.append)

    with pytest.raises(SETTAuthorizationError) as raised:
        executor.submit(Action(action_type="record", payload={"value": original}))

    assert raised.value.reason_code == "authorization.request_mismatch"
    assert calls == []


def test_approval_inputs_fail_closed_when_authorization_is_disabled() -> None:
    executor = SETTExecutor()
    SETTOrchestrator().register_executor(executor)
    executor.register_handler("record", lambda payload: payload)
    approval = ApprovalInput(
        requirement_id="approval.local",
        verifier_id="verifier.local",
        evidence="opaque-value",
    )

    with pytest.raises(SETTConfigurationError) as raised:
        executor.submit(
            Action(action_type="record"),
            approval_inputs=(approval,),
        )

    assert raised.value.reason_code == "authorization.not_configured"


def test_controlled_submission_declares_controlled_authority() -> None:
    resolver = AuthoritativeResolver()
    executor, _orchestrator, _resolver = configured_executor(
        PolicyDecision.grant(
            "policy.replay",
            receipt_use=ReceiptUse.CONTROLLED_IDEMPOTENT_REPLAY,
            conditions=(),
            review_obligations=(),
        ),
        resolver=resolver,
    )
    executor.register_controlled_handler(
        "record",
        lambda payload, context: (payload, context.idempotency_key),
    )

    result = executor.submit_controlled(
        Action(
            action_type="record",
            payload={"value": 1},
            idempotency_key="operation.local",
        )
    )

    assert result.unwrap() == ({"value": 1}, "operation.local")
    assert resolver.modes == [ExecutionMode.CONTROLLED]


class EffectAgent(SETTAgent):
    """Submit one effect through the executor from a pipeline stage."""

    def __init__(self) -> None:
        super().__init__(name="effect_agent", domain="effect")

    def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return self.submit_action("record", input_data)


class AuthorizationBarrierStream(InMemoryEvidenceStream):
    """Observe or refuse only the confirmation immediately before effect."""

    def __init__(self, *, refuse: bool = False) -> None:
        super().__init__(backend_id="backend.local", stream_id="evidence.local")
        self.refuse = refuse
        self.confirmed_kinds: list[str] = []
        self.refused_authorization = False

    def flush(self, *, through):
        kind = decode_trace_evidence_frame(self.read()[-1].payload).payload["kind"]
        if self.refuse and kind == "handler.authorized":
            self.refused_authorization = True
            raise SETTPersistenceError(
                "Effect authorization could not be confirmed.",
                reason_code="audit.evidence_write_failed",
                backend_id="backend.local",
            )
        receipt = super().flush(through=through)
        self.confirmed_kinds.append(kind)
        return receipt


def durable_effect_executor(stream: AuthorizationBarrierStream):
    """Combine all three subsystems with explicit in-memory requirements."""

    backend = PersistenceBackend(
        namespace_id="namespace.local",
        evidence=stream,
        state=InMemoryStateRecordStore(backend_id="backend.local"),
    )
    policy = PersistencePolicy(
        snapshot_durability=DurabilityLevel.IN_MEMORY,
        evidence_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(),
        audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )
    orchestrator = SETTOrchestrator(
        persistence_backend=backend,
        persistence_policy=policy,
    )
    executor, orchestrator, _resolver = configured_executor(
        PolicyDecision.grant("policy.allow", conditions=(), review_obligations=()),
        orchestrator=orchestrator,
    )
    orchestrator.register_agent(EffectAgent())
    return executor, orchestrator, backend, policy


def test_authorized_pipeline_confirms_effect_barrier_and_restores_trace() -> None:
    stream = AuthorizationBarrierStream()
    executor, orchestrator, backend, policy = durable_effect_executor(stream)
    calls: list[dict[str, Any]] = []

    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        assert "handler.authorized" in stream.confirmed_kinds
        kinds = [
            decode_trace_evidence_frame(entry.payload).payload["kind"]
            for entry in stream.read()
        ]
        assert kinds.index("authorization.receipt_verified") < kinds.index(
            "handler.authorized"
        )
        calls.append(payload)
        return payload

    executor.register_handler("record", handle)
    result = orchestrator.run_pipeline_controlled(["effect"], {"value": 1})

    assert result.succeeded, result.exception
    assert result.audit_closure.status is AuditClosureStatus.SATISFIED
    assert calls == [{"value": 1}]
    assert orchestrator.verify_traces(result.trace_id)
    report = TraceRecorder.restore(
        evidence_stream=stream,
        namespace_id=backend.namespace_id,
        evidence_durability=policy.evidence_durability,
    )
    assert report.chain_integrity
    assert report.traces[0].trace_completeness


def test_authorized_pipeline_never_executes_without_durable_barrier() -> None:
    stream = AuthorizationBarrierStream(refuse=True)
    executor, orchestrator, _backend, _policy = durable_effect_executor(stream)
    calls: list[dict[str, Any]] = []
    executor.register_handler("record", calls.append)

    with pytest.raises(SETTPersistenceError):
        orchestrator.run_pipeline_controlled(["effect"], {"value": 1})

    assert stream.refused_authorization
    assert calls == []
    kinds = [
        decode_trace_evidence_frame(entry.payload).payload["kind"]
        for entry in stream.read()
    ]
    assert "authorization.receipt_verified" in kinds
    assert "handler.started" not in kinds
    assert "handler.completed" not in kinds
