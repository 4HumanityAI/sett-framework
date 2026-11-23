"""Governed execution through the executor with the four real state producers.

Condition verifiers (none needed here), the implementation's consent
declaration, a real governance journal and review verifiers are configured;
no synthetic producer is registered. Each handler counts its own effects.
"""

from datetime import timedelta

import pytest
import test_policy_handoff as foundation
from test_authorization_execution_integration import AuthoritativeResolver

from sett import (
    Action,
    AuthorizationRuler,
    PolicyDecision,
    SETTAuthorizationError,
    SETTExecutor,
    SETTOrchestrator,
    StrictJSONAuthorizationFingerprint,
)
from sett.audit_ruler.integrity import Sha256IntegrityPolicy
from sett.core_ruler.execution_result import ExecutionStatus
from sett.core_ruler.resilience import RetryPolicy, is_retryable
from sett.exceptions import SETTAuthorizationConfigurationError, SETTCancelledError
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream
from sett.policy_ruler import GovernanceJournal, PolicyRuler
from sett.policy_ruler.decision_record import read_decision_records

EFFECT = foundation.EFFECT


def journal():
    stream = InMemoryEvidenceStream(backend_id="backend.decisions", stream_id="decisions")
    return GovernanceJournal(
        namespace_id="namespace.test", journal_id="journal.decisions", stream=stream,
        origin=stream.head(), required_durability=DurabilityLevel.IN_MEMORY,
        integrity_policy=Sha256IntegrityPolicy(), historical_integrity_policies=(),
        max_frame_bytes=65536, max_records=64, max_total_bytes=4_194_304,
    )


class Governed:
    """An executor whose authorization has every state producer configured."""

    def __init__(self, *, decision=None, evaluation_ttl=60, receipt_ttl=30):
        self.clock = foundation.FixedClock()
        self.journal = journal()
        self.evidence = foundation.InMemoryAuthorizationEvidence()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.ruler", entry_checkpoint_id=foundation.ENTRY,
            evaluation_ttl_seconds=evaluation_ttl, max_live_evaluations=16, clock=self.clock,
            evidence_sink=self.evidence, selection_resolver=foundation.Resolver(),
            condition_verifiers=[], implementation_owned_consent=True,
            governance_journal=self.journal, review_verifiers=[],
        )
        self.store = foundation.CountingStore()
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer", receipt_ttl_seconds=receipt_ttl,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(), receipt_store=self.store,
            evidence_sink=self.evidence, clock=self.clock, policy_ruler=self.owner,
        )
        self.policy = foundation.StaticPolicy(decision=decision)
        self.issuer.register_policy(EFFECT, self.policy, binding=foundation.binding())
        self.resolver = AuthoritativeResolver()
        self.executor = SETTExecutor(
            authorization_ruler=self.issuer, authorization_request_resolver=self.resolver,
        )
        self.orchestrator = SETTOrchestrator()
        self.orchestrator.register_executor(self.executor)
        self.effects = []

    def handle(self, payload):
        self.effects.append(payload)
        return "done"

    def when_authorized(self, change):
        """Run ``change`` from a trace exporter once the effect barrier is recorded."""

        def exporter(view):
            if view["kind"] == "handler.authorized":
                change()

        self.orchestrator.register_trace_exporter(exporter)

    def events(self):
        return self.orchestrator.get_trace(self.resolver.trace_ids[-1])

    def rejections(self):
        return [event for event in self.evidence.events() if event.kind == "authorization.receipt_rejected"]


def action():
    return Action(action_type=EFFECT, payload={"value": 1})


def deny():
    return PolicyDecision.deny(
        "test_fixture.policy", reason_codes=("test_fixture.denied",), conditions=(), review_obligations=(),
    )


# ── the plain allow and block pair ────────────────────────────────────────


def test_a_grant_runs_the_handler_once_and_links_its_decision_record():
    governed = Governed()
    governed.executor.register_handler(EFFECT, governed.handle)
    assert governed.executor.submit(action()) == "done"
    assert governed.effects == [{"value": 1}]
    assert governed.store.consumed == 1
    (record,) = read_decision_records(governed.journal).decisions
    (verified,) = [event for event in governed.events() if event.kind == "authorization.receipt_verified"]
    assert verified.attributes["decision_ref"] == record.authorization_attempt_id
    assert record.operation_id == "decision." + verified.attributes["decision_ref"]
    assert record.decision["verdict"] == "grant"


def test_a_denial_runs_nothing_and_keeps_its_decision_record():
    governed = Governed(decision=deny())
    governed.executor.register_handler(EFFECT, governed.handle)
    with pytest.raises(SETTAuthorizationError) as caught:
        governed.executor.submit(action())
    assert caught.value.reason_code == "authorization.denied"
    assert governed.effects == []
    (record,) = read_decision_records(governed.journal).decisions
    assert record.decision["verdict"] == "deny"


# ── changes between consumption and the handler ───────────────────────────


def test_a_registration_made_by_an_exporter_after_consumption_blocks_the_effect():
    governed = Governed()
    governed.executor.register_handler(EFFECT, governed.handle)
    governed.when_authorized(lambda: governed.issuer.register_policy(
        "test_fixture.other", foundation.StaticPolicy("test_fixture.other"),
        binding=foundation.binding("test_fixture.other"),
    ))
    with pytest.raises(SETTAuthorizationError) as caught:
        governed.executor.submit(action())
    assert caught.value.reason_code == "authorization.policy_generation_changed"
    assert governed.effects == []
    assert governed.store.consumed == 1
    blocked = [event for event in governed.events() if event.kind == "handler.blocked"]
    assert [event.reason_codes for event in blocked] == [("authorization.policy_generation_changed",)]
    assert caught.value.trace_event_id == blocked[0].event_id
    assert not any(event.kind == "handler.started" for event in governed.events())
    assert governed.rejections()[-1].reason_codes == ("authorization.policy_generation_changed",)
    assert governed.orchestrator.verify_traces(governed.resolver.trace_ids[-1])


@pytest.mark.parametrize(
    ("ttls", "shift", "code"),
    [
        ((10, 30), timedelta(seconds=15), "authorization.policy_evaluation_expired"),
        ((60, 10), timedelta(seconds=15), "authorization.receipt_expired"),
        ((60, 30), timedelta(seconds=-1), "authorization.clock_invalid"),
    ],
)
def test_time_that_passes_while_the_trace_is_written_blocks_the_effect(ttls, shift, code):
    governed = Governed(evaluation_ttl=ttls[0], receipt_ttl=ttls[1])
    governed.executor.register_handler(EFFECT, governed.handle)

    def move_clock():
        governed.clock.current += shift

    governed.when_authorized(move_clock)
    with pytest.raises(SETTAuthorizationError) as caught:
        governed.executor.submit(action())
    assert caught.value.reason_code == code
    assert governed.effects == []
    assert governed.store.consumed == 1


def test_an_unchanged_grant_passes_the_final_gate_with_exporters_present():
    governed = Governed()
    governed.executor.register_handler(EFFECT, governed.handle)
    governed.when_authorized(lambda: None)
    assert governed.executor.submit(action()) == "done"
    assert governed.effects == [{"value": 1}]


# ── controlled handlers ───────────────────────────────────────────────────


def test_a_cancelled_controlled_handler_keeps_what_it_already_did():
    governed = Governed()

    def handle(payload, context):
        governed.effects.append("first part")
        raise SETTCancelledError("test_fixture.stop")

    governed.executor.register_controlled_handler(EFFECT, handle)
    result = governed.executor.submit_controlled(action())
    assert result.status is ExecutionStatus.CANCELLED
    assert governed.effects == ["first part"]


def test_an_authorization_block_is_never_retried_automatically():
    governed = Governed(decision=deny())
    governed.executor.register_controlled_handler(EFFECT, lambda payload, context: governed.effects.append(payload))
    retry = RetryPolicy(max_attempts=3, retry_on=(SETTAuthorizationError,))
    result = governed.executor.submit_controlled(
        Action(action_type=EFFECT, payload={"value": 1}, idempotency_key="test_fixture.key"),
        retry_policy=retry,
    )
    assert result.status is ExecutionStatus.REJECTED
    assert result.exception.reason_code == "authorization.denied"
    assert governed.policy.calls == 1
    assert governed.effects == []
    assert len(read_decision_records(governed.journal).decisions) == 1


@pytest.mark.parametrize(
    "error",
    [
        SETTAuthorizationError("test_fixture.blocked", reason_code="authorization.denied"),
        SETTAuthorizationConfigurationError("test_fixture.broken", reason_code="authorization.evidence_failed"),
    ],
)
def test_a_retry_policy_cannot_make_authorization_errors_retryable(error):
    assert is_retryable(error, RetryPolicy(max_attempts=5, retry_on=(type(error),))) is False
