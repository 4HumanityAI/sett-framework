"""Authority is still current when the application handler is entered."""
from datetime import timedelta

import pytest
from test_governance_recovery import Durable, only
from test_policy_handoff import StaticPolicy, binding

from sett import SETTAuthorizationError
from sett.policy_ruler.governance_recovery import RecoveredOutcome


@pytest.mark.parametrize(("change", "reason"), [
    ("registration", "authorization.policy_generation_changed"),
    ("expiry", "authorization.receipt_expired"),
])
def test_start_exporter_can_invalidate_authority_before_handler_entry(change, reason):
    system = Durable()
    system.handler()

    def exporter(event):
        if event["kind"] == "handler.started":
            if change == "expiry":
                system.clock.current += timedelta(seconds=30)
            else:
                name = "test_fixture.other"
                system.issuer.register_policy(name, StaticPolicy(name), binding=binding(name))

    system.orchestrator.register_trace_exporter(exporter)
    with pytest.raises(SETTAuthorizationError) as caught:
        system.submit()
    assert caught.value.reason_code == reason
    assert system.effects == [] and system.store.consumed == 1
    restored = system.restored()
    started, blocked = [e for e in restored.events if e.kind in {"handler.started", "handler.entry_blocked"}]
    assert blocked.cause_id == started.event_id
    assert blocked.reason_codes == (reason,)
    assert restored.chain_integrity and not restored.requires_reconciliation
    assert not any(e.kind in {"handler.failed", "handler.completed"} for e in restored.events)
    decision = only(system.recovery())
    assert (decision.outcome, decision.effect) == (RecoveredOutcome.EFFECT_KNOWN, "blocked")
