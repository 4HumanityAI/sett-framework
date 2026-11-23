"""Restoring a trace pairs an event that ends one boundary and starts the next.

``handler.started`` ends the ``handler.authorized`` boundary and starts its own.
A restored trace must close the first when the second starts, so an effect that
started is never reported as an open authorization boundary.
"""

import pytest
from test_governance_recovery import Durable, ProcessStopped

from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.execution_context import ExecutionContext
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream


def restored(*kinds):
    """Record one chain of handler events, each caused by the previous, and restore it."""
    stream = InMemoryEvidenceStream(backend_id="backend.demo", stream_id="evidence.main")
    recorder = TraceRecorder(
        evidence_stream=stream, namespace_id="demo.namespace", evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    context = ExecutionContext()
    cause = None
    for kind in kinds:
        event = recorder.record(
            context, kind=kind, component_type="handler", component_name="test_fixture.effect",
            status=kind.split(".")[1], cause_id=cause, effect_boundary=kind == "handler.authorized",
        )
        cause = event.event_id
    report = TraceRecorder.restore(
        evidence_stream=stream, namespace_id="demo.namespace", evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    (trace,) = report.traces
    return report, [boundary.start_kind for boundary in trace.unresolved_boundaries]


@pytest.mark.parametrize(
    ("kinds", "open_kinds"),
    [
        (("handler.authorized", "handler.started", "handler.completed"), []),
        (("handler.authorized", "handler.blocked"), []),
        (("handler.authorized", "handler.started"), ["handler.started"]),
        (("handler.authorized",), ["handler.authorized"]),
        (("handler.authorized", "handler.started", "handler.completed",
          "handler.authorized", "handler.started"), ["handler.started"]),
    ],
)
def test_a_started_handler_closes_its_authorization_boundary(kinds, open_kinds):
    report, unresolved = restored(*kinds)
    assert unresolved == open_kinds
    assert report.requires_reconciliation is bool(open_kinds)


def test_a_completed_governed_effect_needs_no_reconciliation():
    durable = Durable()
    durable.handler()
    durable.submit()
    report = durable.restored()
    assert report.traces[0].unresolved_boundaries == ()
    assert not report.requires_reconciliation


def test_a_stopped_handler_leaves_only_its_own_boundary_open():
    durable = Durable()

    def stop():
        raise ProcessStopped

    durable.handler(stop)
    with pytest.raises(ProcessStopped):
        durable.submit()
    (boundary,) = durable.restored().traces[0].unresolved_boundaries
    assert boundary.start_kind == "handler.started"
