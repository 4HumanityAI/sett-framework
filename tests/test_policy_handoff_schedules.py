"""Controlled concurrent schedules around the final guarded transition.

Each case drives one explicit, recorded interleaving with events rather
than sleeps, so the observed order is the asserted order. These are local
serialization guarantees only: they say nothing about network atomicity
and they do not interrupt a running callback.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest

from sett._governance.validation import canonical_json_bytes
from sett.authorization_ruler import (
    AuthorizationRequest,
    AuthorizationRuler,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    PolicyApplicability,
    PolicyApplicabilityState,
    PolicyBinding,
    PolicyDecision,
    PolicyOrigin,
    PolicySelection,
    SETTAuthorizationError,
    StrictJSONAuthorizationFingerprint,
)
from sett.policy_ruler import PolicyRuler
from sett.policy_ruler._handoff import PRODUCED_SLOT_NAMES, VerifiedStateSlot

ENTRY = "test_fixture.entry"
EFFECT = "test_fixture.effect"


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current


def binding(policy_id: str = "test_fixture.policy", version: str = "1") -> PolicyBinding:
    return PolicyBinding(
        policy_id=policy_id,
        policy_version=version,
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


class Resolver:
    resolver_id = "test_fixture.resolver"

    def resolve(self, request: AuthorizationRequest) -> PolicySelection:
        item = binding()
        return PolicySelection(
            selection_id="test_fixture.selection",
            selection_revision="1",
            contract_version="2.0",
            authority_ref="test_fixture.authority",
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            action_id=request.action_id,
            action_type=request.action_type,
            required=[item],
            informative=[],
            applicability=[
                PolicyApplicability(
                    policy_id=item.policy_id,
                    policy_version=item.policy_version,
                    state=PolicyApplicabilityState.APPLIES,
                    justification="Synthetic test declaration",
                    fact_names=[],
                    evidence_refs=[],
                )
            ],
            facts={},
        )


class SyntheticStateProducer:
    """A test-only producer with an optional hook outside the guard."""

    schema_revision = "1"

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self.producer_id = "test_fixture.producer." + slot
        self.on_produce: Any = None

    def produce(self, context: Any) -> VerifiedStateSlot:
        if self.on_produce is not None:
            self.on_produce()
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
        return True


class RecordingSink(InMemoryAuthorizationEvidence):
    """An evidence sink with a hook on one declared event kind."""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: dict[str, Any] = {}

    def record(self, event: Any) -> None:
        super().record(event)
        hook = self.hooks.pop(event.kind, None)
        if hook is not None:
            hook()


class HookedStore(InMemoryAuthorizationReceiptStore):
    """The reference store with a hook inside the guarded transition."""

    def __init__(self) -> None:
        super().__init__()
        self.on_issue: Any = None
        self.issued = 0

    def issue(self, receipt: Any) -> None:
        self.issued += 1
        if self.on_issue is not None:
            self.on_issue()
        super().issue(receipt)


class StaticPolicy:
    policy_id = "test_fixture.policy"

    def evaluate(self, request: Any, verified_approvals: Any) -> Any:
        return PolicyDecision.grant(self.policy_id, conditions=(), review_obligations=())


class NeutralVerifier:
    def __init__(self, verifier_id: str) -> None:
        self.verifier_id = verifier_id

    def verify(self, approval_input: Any, request: Any) -> Any:
        return None


class Harness:
    def __init__(self) -> None:
        self.clock = FixedClock()
        self.evidence = RecordingSink()
        self.store = HookedStore()
        self.owner = PolicyRuler(
            ruler_id="test_fixture.policy_ruler",
            entry_checkpoint_id=ENTRY,
            evaluation_ttl_seconds=3_600,
            max_live_evaluations=64,
            clock=self.clock,
            evidence_sink=self.evidence,
            selection_resolver=Resolver(),
        )
        self.producers = {
            name: SyntheticStateProducer(name) for name in PRODUCED_SLOT_NAMES
        }
        for producer in self.producers.values():
            self.owner._register_state_producer(producer)
        self.issuer = AuthorizationRuler(
            issuer_id="test_fixture.issuer",
            receipt_ttl_seconds=3_600,
            fingerprint_policy=StrictJSONAuthorizationFingerprint(),
            receipt_store=self.store,
            evidence_sink=self.evidence,
            clock=self.clock,
            policy_ruler=self.owner,
        )
        self.issuer.register_policy(EFFECT, StaticPolicy(), binding=binding())


def request(action_id: str = "test_fixture.action") -> AuthorizationRequest:
    return AuthorizationRequest(
        action_id=action_id,
        trace_id="test_fixture.trace",
        action_type=EFFECT,
        payload={"value": 1},
        principal=FieldValue(
            "test_fixture.subject",
            source_id="test_fixture.source",
            trust=FieldTrust.AUTHORITATIVE,
        ),
    )


def run_writer(work: Any) -> tuple[threading.Thread, list[str]]:
    """Start one writer thread and return it with its own order log."""
    order: list[str] = []

    def target() -> None:
        order.append("writer.start")
        work()
        order.append("writer.committed")

    thread = threading.Thread(target=target)
    return thread, order


# ── a mutation committed before the final guard ──────────────────────────


def test_a_commit_before_the_guard_invalidates_the_pending_binding() -> None:
    harness = Harness()
    schedule: list[str] = []

    def commit() -> None:
        harness.owner.register_policy(
            "test_fixture.other",
            StaticPolicy(),
            binding=binding(),
        )

    thread, order = run_writer(commit)

    def before_guard() -> None:
        schedule.append("receiver.state_collected")
        thread.start()
        thread.join()
        schedule.append("receiver.resumed")

    harness.producers["durability"].on_produce = before_guard

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    schedule.extend(order)
    assert raised.value.reason_code == "authorization.policy_generation_changed"
    assert harness.store.issued == 0
    assert schedule == [
        "receiver.state_collected",
        "receiver.resumed",
        "writer.start",
        "writer.committed",
    ]


# ── a mutation attempted during the guarded transition ───────────────────


def test_a_writer_during_the_transition_waits_for_one_stable_generation() -> None:
    harness = Harness()
    schedule: list[str] = []
    started = threading.Event()

    def commit() -> None:
        harness.owner.register_policy(
            "test_fixture.other",
            StaticPolicy(),
            binding=binding(),
        )

    thread, order = run_writer(commit)

    def inside_guard() -> None:
        schedule.append("receiver.inside_guard")
        thread.start()
        started.wait(timeout=5)
        schedule.append("receiver.store_write")

    def marker() -> None:
        started.set()

    harness.store.on_issue = inside_guard
    original = harness.owner.register_policy

    def guarded_registration(*args: Any, **kwargs: Any) -> None:
        marker()
        original(*args, **kwargs)

    harness.owner.register_policy = guarded_registration  # type: ignore[method-assign]

    outcome = harness.issuer.authorize(request())
    thread.join(timeout=5)
    schedule.extend(order)

    assert outcome.receipt is not None
    assert harness.store.issued == 1
    assert schedule[:2] == ["receiver.inside_guard", "receiver.store_write"]
    assert "writer.committed" in schedule
    assert schedule.index("receiver.store_write") < schedule.index(
        "writer.committed"
    )


# ── an event callback changes configuration after evaluation ─────────────


def test_an_event_callback_change_is_detected_before_issue() -> None:
    harness = Harness()

    def change() -> None:
        harness.issuer.register_approval_verifier(
            NeutralVerifier("test_fixture.late")
        )

    harness.evidence.hooks["authorization.policy_checked"] = change

    with pytest.raises(SETTAuthorizationError) as raised:
        harness.issuer.authorize(request())

    assert raised.value.reason_code == "authorization.policy_generation_changed"
    assert harness.store.issued == 0


# ── Single-use authority stays single under real contention ──────────────


def test_only_one_thread_consumes_one_single_use_authority() -> None:
    harness = Harness()
    item = request()
    receipt = harness.issuer.authorize(item).receipt
    assert receipt is not None

    def consume(_index: int) -> bool:
        try:
            harness.issuer.verify_and_consume(receipt, item)
        except SETTAuthorizationError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, range(256)))

    assert results.count(True) == 1


def test_concurrent_authorizations_keep_disjoint_bindings() -> None:
    harness = Harness()

    def authorize(index: int) -> str | None:
        outcome = harness.issuer.authorize(request(f"test_fixture.action-{index}"))
        return None if outcome.receipt is None else outcome.receipt.receipt_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(authorize, range(32)))

    assert len(set(receipts)) == 32
    assert None not in receipts
    assert harness.store.issued == 32
