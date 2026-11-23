"""Controlled checkpoints: conditions verified while a controlled handler runs.

The authorization has the four real state producers and a real condition
verifier test double; the handler counts the segments it performs. No
synthetic producer is registered.
"""

from datetime import timedelta

import pytest
import test_policy_handoff as foundation
from test_authorization_execution_integration import AuthoritativeResolver
from test_governed_execution import journal

from sett import (
    Action,
    AuthorizationRuler,
    PolicyDecision,
    SETTAuthorizationError,
    SETTConfigurationError,
    SETTExecutor,
    SETTOrchestrator,
    StrictJSONAuthorizationFingerprint,
)
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.audit_ruler.trace import _DURABLE_ANCHOR_KINDS
from sett.core_ruler.execution_result import ExecutionStatus
from sett.core_ruler.resilience import RetryPolicy
from sett.exceptions import SETTPersistenceError, SETTPolicyConfigurationError
from sett.execution_ruler.executor import HandlerContext
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    InMemoryEvidenceStream,
    InMemoryStateRecordStore,
    PersistenceBackend,
    PersistencePolicy,
    decode_trace_evidence_frame,
)
from sett.policy_ruler import (
    ConditionValidityMode,
    ConditionVerification,
    ConditionVerificationState,
    PolicyCondition,
    PolicyRuler,
)

EFFECT = foundation.EFFECT
ACTION_ID = "test_fixture.action"
FIRST = "test_fixture.checkpoint_first"
SECOND = "test_fixture.checkpoint_second"
SATISFIED = ConditionVerificationState.SATISFIED


def condition(identifier, checkpoint, mode=ConditionValidityMode.RECHECK, limit=None):
    binding = foundation.binding()
    return PolicyCondition(
        condition_id=identifier, binding=binding, origins=list(binding.origins),
        verifier_id="test_fixture.verifier", verifier_version="1", required=True,
        action_id=ACTION_ID, action_type=EFFECT, checkpoint_id=checkpoint,
        validity_mode=mode, attestation_not_after=limit,
    )


class Verifier:
    """The application's condition verifier, as a test double."""

    verifier_id = "test_fixture.verifier"
    verifier_version = "1"

    def __init__(self, clock):
        self.clock = clock
        self.states = {}
        self.calls = []

    def verify(self, request, *, condition):
        self.calls.append(condition.condition_id)
        state = self.states.get(condition.condition_id, SATISFIED)
        if state is not SATISFIED:
            return ConditionVerification(
                condition=condition, state=state, checked_at=self.clock.current, valid_until=None,
                evidence_refs=["test_fixture.observation"], reason_codes=["test_fixture.reason"],
            )
        attested = condition.validity_mode is ConditionValidityMode.ATTESTATION
        return ConditionVerification(
            condition=condition, state=state, checked_at=self.clock.current,
            valid_until=self.clock.current + timedelta(minutes=10) if attested else None,
            evidence_refs=["test_fixture.observation"], reason_codes=[],
        )

    def confirm(self, verification, *, request):
        return True


class Checkpointed:
    """An executor whose action type declares two controlled checkpoints."""

    def __init__(self, *conditions, orchestrator=None):
        self.clock = foundation.FixedClock()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.verifier = Verifier(self.clock)
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=60, max_live_evaluations=16, clock=self.clock,
            evidence_sink=self.evidence, selection_resolver=foundation.Resolver(),
            condition_verifiers=[self.verifier], implementation_owned_consent=True,
            governance_journal=journal(), review_verifiers=[],
            controlled_checkpoints={EFFECT: (FIRST, SECOND)},
        )
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=30,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=foundation.CountingStore(),
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        decision = PolicyDecision.grant("test_fixture.policy", conditions=conditions, review_obligations=())
        self.issuer.register_policy(EFFECT, foundation.StaticPolicy(decision=decision), binding=foundation.binding())
        self.resolver = AuthoritativeResolver()
        self.executor = SETTExecutor(
            authorization_ruler=self.issuer, authorization_request_resolver=self.resolver,
        )
        self.orchestrator = orchestrator or SETTOrchestrator()
        self.orchestrator.register_executor(self.executor)
        self.segments = []

    def register(self, between=None, checkpoints=(FIRST, SECOND)):
        """Register a handler with one segment before and after each checkpoint."""

        def handle(payload, context):
            self.segments.append("segment 1")
            for number, checkpoint in enumerate(checkpoints, start=2):
                if between is not None:
                    between(number)
                context.check_policy_conditions(checkpoint)
                self.segments.append(f"segment {number}")
            return "done"

        self.executor.register_controlled_handler(EFFECT, handle)

    def submit(self, **options):
        return self.executor.submit_controlled(
            Action(action_type=EFFECT, payload={"value": 1}, action_id=ACTION_ID, **options.pop("action", {})),
            **options,
        )

    def kinds(self):
        return [event.kind for event in self.orchestrator.get_trace(self.resolver.trace_ids[-1])]

    def events(self, kind):
        return [event for event in self.orchestrator.get_trace(self.resolver.trace_ids[-1]) if event.kind == kind]


def ruler(**options):
    return PolicyRuler(
        ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
        evaluation_ttl_seconds=60, max_live_evaluations=16, clock=foundation.FixedClock(),
        evidence_sink=foundation.InMemoryAuthorizationEvidence(), selection_resolver=foundation.Resolver(),
        **options,
    )


# ── configuration ────────────────────────────────────────────────────────


def test_controlled_checkpoints_are_declared_per_action_type_in_order():
    owner = ruler(controlled_checkpoints={EFFECT: [FIRST, SECOND]})
    assert owner._controlled_checkpoints_for(EFFECT) == (FIRST, SECOND)
    assert owner._controlled_checkpoints_for("test_fixture.other") == ()


@pytest.mark.parametrize(
    "value",
    [
        "test_fixture.checkpoint",
        {EFFECT: "test_fixture.checkpoint"},
        {EFFECT: [FIRST, FIRST]},
        {EFFECT: [foundation.ENTRY]},
        {EFFECT: [" not an identifier "]},
        {EFFECT: [f"test_fixture.checkpoint_{index}" for index in range(17)]},
        {" ": [FIRST]},
    ],
)
def test_invalid_controlled_checkpoints_are_rejected(value):
    with pytest.raises(SETTPolicyConfigurationError) as caught:
        ruler(controlled_checkpoints=value)
    assert caught.value.reason_code == "policy.ruler_configuration_invalid"


def test_a_plain_handler_is_refused_for_an_action_type_with_checkpoints():
    checkpointed = Checkpointed()
    with pytest.raises(SETTConfigurationError):
        checkpointed.executor.register_handler(EFFECT, lambda payload: None)
    checkpointed.executor.register_handler("test_fixture.other", lambda payload: None)


def test_a_condition_of_an_undeclared_checkpoint_still_blocks_before_any_effect():
    checkpointed = Checkpointed(condition("test_fixture.elsewhere", "test_fixture.undeclared"))
    checkpointed.register()
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.FAILED
    assert result.exception.reason_code == "authorization.policy_result_invalid"
    assert checkpointed.segments == []


def test_a_missing_verifier_of_a_later_checkpoint_blocks_at_entry():
    binding = foundation.binding()
    later = PolicyCondition(
        condition_id="test_fixture.later", binding=binding, origins=list(binding.origins),
        verifier_id="test_fixture.absent", verifier_version="1", required=True,
        action_id=ACTION_ID, action_type=EFFECT, checkpoint_id=FIRST,
        validity_mode=ConditionValidityMode.RECHECK, attestation_not_after=None,
    )
    checkpointed = Checkpointed(later)
    checkpointed.register()
    result = checkpointed.submit()
    assert result.exception.reason_code == "authorization.condition_verifier_missing"
    assert checkpointed.segments == []


# ── passing checkpoints ──────────────────────────────────────────────────


def test_every_checkpoint_passed_in_order_runs_every_segment():
    checkpointed = Checkpointed(
        condition("test_fixture.entry_check", foundation.ENTRY),
        condition("test_fixture.first_check", FIRST),
        condition("test_fixture.second_check", SECOND),
    )
    checkpointed.register()
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.SUCCESS
    assert checkpointed.segments == ["segment 1", "segment 2", "segment 3"]
    calls = checkpointed.verifier.calls
    # Each checkpoint's condition is verified there, never earlier; the
    # entry RECHECK condition is verified again at each later checkpoint.
    assert calls.index("test_fixture.first_check") > calls.index("test_fixture.entry_check")
    assert calls.count("test_fixture.first_check") == 2
    assert calls.count("test_fixture.second_check") == 1
    passed = checkpointed.events("handler.checkpoint_passed")
    assert [event.attributes["checkpoint_id"] for event in passed] == [FIRST, SECOND]
    assert "handler.checkpoint_missed" not in checkpointed.kinds()


def test_a_valid_attestation_is_not_verified_again_at_a_checkpoint():
    attested = condition(
        "test_fixture.attested", foundation.ENTRY, ConditionValidityMode.ATTESTATION,
        limit=foundation.FixedClock().current + timedelta(hours=1),
    )
    checkpointed = Checkpointed(attested)
    checkpointed.register()
    assert checkpointed.submit().status is ExecutionStatus.SUCCESS
    assert checkpointed.verifier.calls == ["test_fixture.attested"]


# ── blocked checkpoints ──────────────────────────────────────────────────


def test_a_false_condition_at_a_checkpoint_stops_the_next_segment():
    checkpointed = Checkpointed(condition("test_fixture.first_check", FIRST))
    checkpointed.verifier.states["test_fixture.first_check"] = ConditionVerificationState.UNSATISFIED
    checkpointed.register()
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.PARTIAL
    assert result.reason_code == "authorization.condition_unsatisfied"
    assert type(result.exception) is SETTAuthorizationError
    assert result.exception.failed_condition == ("test_fixture.policy", "1", "test_fixture.first_check")
    assert checkpointed.segments == ["segment 1"]
    (blocked,) = checkpointed.events("handler.checkpoint_blocked")
    assert blocked.reason_codes == ("authorization.condition_unsatisfied",)
    assert blocked.attributes["checkpoint_id"] == FIRST
    assert "handler.failed" in checkpointed.kinds()


def test_a_recheck_condition_that_turns_false_stops_the_next_segment():
    checkpointed = Checkpointed(condition("test_fixture.entry_check", foundation.ENTRY))

    def turn_false(number):
        checkpointed.verifier.states["test_fixture.entry_check"] = ConditionVerificationState.EVIDENCE_UNAVAILABLE

    checkpointed.register(between=turn_false)
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.PARTIAL
    assert result.reason_code == "authorization.condition_evidence_unavailable"
    assert checkpointed.segments == ["segment 1"]


def test_an_attestation_outside_its_window_stops_the_next_segment():
    attested = condition(
        "test_fixture.attested", foundation.ENTRY, ConditionValidityMode.ATTESTATION,
        limit=foundation.FixedClock().current + timedelta(hours=1),
    )
    checkpointed = Checkpointed(attested)

    def pass_time(number):
        checkpointed.clock.current += timedelta(minutes=11)

    checkpointed.register(between=pass_time)
    result = checkpointed.submit()
    assert result.reason_code == "authorization.condition_result_invalid"
    assert checkpointed.segments == ["segment 1"]


def test_a_registration_during_a_segment_blocks_the_next_checkpoint():
    checkpointed = Checkpointed()

    def register_other(number):
        checkpointed.issuer.register_policy(
            "test_fixture.other", foundation.StaticPolicy("test_fixture.other"),
            binding=foundation.binding("test_fixture.other"),
        )

    checkpointed.register(between=register_other)
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.PARTIAL
    assert result.reason_code == "authorization.policy_generation_changed"
    assert checkpointed.segments == ["segment 1"]


@pytest.mark.parametrize(
    ("order", "segments"),
    [((SECOND, FIRST), 1), ((FIRST, FIRST), 2), ((FIRST, SECOND, "test_fixture.extra"), 3)],
)
def test_checkpoints_out_of_order_repeated_or_undeclared_are_invalid(order, segments):
    checkpointed = Checkpointed()
    checkpointed.register(checkpoints=order)
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.PARTIAL
    assert result.reason_code == "authorization.checkpoint_invalid"
    assert len(checkpointed.segments) == segments


def test_every_call_after_a_failure_is_blocked():
    checkpointed = Checkpointed(condition("test_fixture.first_check", FIRST))
    checkpointed.verifier.states["test_fixture.first_check"] = ConditionVerificationState.UNSATISFIED
    seen = []

    def handle(payload, context):
        for checkpoint in (FIRST, SECOND):
            try:
                context.check_policy_conditions(checkpoint)
            except SETTAuthorizationError as error:
                seen.append(error.reason_code)
        return "kept going"

    checkpointed.executor.register_controlled_handler(EFFECT, handle)
    result = checkpointed.submit()
    assert seen == ["authorization.condition_unsatisfied", "authorization.checkpoint_blocked"]
    assert result.status is ExecutionStatus.SUCCESS
    assert [event.reason_codes for event in checkpointed.events("handler.checkpoint_blocked")] == [
        ("authorization.condition_unsatisfied",), ("authorization.checkpoint_blocked",),
    ]
    assert "handler.checkpoint_missed" not in checkpointed.kinds()


def test_a_blocked_segment_is_never_retried():
    checkpointed = Checkpointed(condition("test_fixture.first_check", FIRST))
    checkpointed.verifier.states["test_fixture.first_check"] = ConditionVerificationState.UNSATISFIED
    checkpointed.register()
    result = checkpointed.submit(
        action={"idempotency_key": "test_fixture.key"},
        retry_policy=RetryPolicy(max_attempts=3, retry_on=(Exception,)),
    )
    assert result.status is ExecutionStatus.PARTIAL
    assert result.state.attempt_count == 1
    assert checkpointed.segments == ["segment 1"]


# ── missed and unavailable checkpoints ───────────────────────────────────


def test_a_handler_that_skips_its_checkpoints_keeps_its_result_and_is_recorded():
    checkpointed = Checkpointed()
    checkpointed.register(checkpoints=(FIRST,))
    result = checkpointed.submit()
    assert result.status is ExecutionStatus.SUCCESS
    (missed,) = checkpointed.events("handler.checkpoint_missed")
    assert tuple(missed.attributes["missing_checkpoints"]) == (SECOND,)


def test_a_checkpoint_is_unavailable_outside_a_governed_controlled_handler():
    executor = SETTExecutor()
    orchestrator = SETTOrchestrator()
    orchestrator.register_executor(executor)
    seen = []

    def handle(payload, context):
        try:
            context.check_policy_conditions(FIRST)
        except SETTAuthorizationError as error:
            seen.append(error.reason_code)

    executor.register_controlled_handler(EFFECT, handle)
    executor.submit_controlled(Action(action_type=EFFECT, payload={}))
    stray = HandlerContext(
        action_id=ACTION_ID, idempotency_key=None, attempt_id="test_fixture.attempt", attempt_number=1,
        execution_context=None, cancellation_token=None, timeout_seconds=None,
    )
    with pytest.raises(SETTAuthorizationError) as caught:
        stray.check_policy_conditions(FIRST)
    assert seen == ["authorization.checkpoint_unavailable"]
    assert caught.value.reason_code == "authorization.checkpoint_unavailable"


# ── durable records ──────────────────────────────────────────────────────


class RefusingStream(InMemoryEvidenceStream):
    """Refuse to confirm one trace kind."""

    def __init__(self, refused):
        super().__init__(backend_id="backend.local", stream_id="evidence.local")
        self.refused = refused

    def flush(self, *, through):
        kind = decode_trace_evidence_frame(self.read()[-1].payload).payload["kind"]
        if kind == self.refused:
            raise SETTPersistenceError(
                "Checkpoint evidence could not be confirmed.",
                reason_code="audit.evidence_write_failed", backend_id="backend.local",
            )
        return super().flush(through=through)


def durable_orchestrator(stream):
    backend = PersistenceBackend(
        namespace_id="namespace.local", evidence=stream,
        state=InMemoryStateRecordStore(backend_id="backend.local"),
    )
    policy = PersistencePolicy(
        snapshot_durability=DurabilityLevel.IN_MEMORY, evidence_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(), audit_gap_policy=AuditGapPolicy.RETURN_KNOWN_OUTCOME,
    )
    return SETTOrchestrator(persistence_backend=backend, persistence_policy=policy)


def test_checkpoint_records_are_durable_before_the_call_returns():
    assert {"handler.checkpoint_passed", "handler.checkpoint_blocked", "handler.checkpoint_missed"} <= (
        _DURABLE_ANCHOR_KINDS
    )
    stream = RefusingStream(refused="test_fixture.never")
    checkpointed = Checkpointed(orchestrator=durable_orchestrator(stream))
    checkpointed.register()
    assert checkpointed.submit().status is ExecutionStatus.SUCCESS
    kinds = [decode_trace_evidence_frame(entry.payload).payload["kind"] for entry in stream.read()]
    assert kinds.count("handler.checkpoint_passed") == 2


def test_a_checkpoint_whose_record_cannot_be_confirmed_stops_the_next_segment():
    stream = RefusingStream(refused="handler.checkpoint_passed")
    checkpointed = Checkpointed(orchestrator=durable_orchestrator(stream))
    checkpointed.register()
    # An evidence store that cannot confirm is infrastructure failing, not an
    # outcome of the action: it propagates, as at the effect barrier.
    with pytest.raises(SETTPersistenceError):
        checkpointed.submit()
    assert checkpointed.segments == ["segment 1"]
