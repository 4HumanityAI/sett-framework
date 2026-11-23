"""Mode-specific trace limits and validation of deeply nested payloads."""
import pytest

from sett import Action, EthicalFilter, SETTExecutor, SETTOrchestrator
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.execution_context import ExecutionContext, is_sensitive_key
from sett.exceptions import SETTValidationError
from sett.persistence import DurabilityLevel, InMemoryEvidenceStream


def test_reason_code_bound_applies_with_an_evidence_stream():
    # A stream in RAM still uses the bounded evidence format.
    recorder = TraceRecorder(
        evidence_stream=InMemoryEvidenceStream(backend_id="b", stream_id="s"),
        namespace_id="test", evidence_durability=DurabilityLevel.IN_MEMORY,
    )
    with pytest.raises(SETTValidationError):
        recorder.record(
            ExecutionContext.create(), kind="test.event", component_type="test",
            component_name="test", status="ok", reason_codes=("x" * 100_000,),
        )
    assert recorder.event_count == 0


def test_ephemeral_reason_vocabulary_keeps_its_existing_contract():
    recorder = TraceRecorder()
    event = recorder.record(
        ExecutionContext.create(), kind="test.event", component_type="test",
        component_name="test", status="ok", reason_codes=("x" * 100_000,),
    )
    assert event.reason_codes == ("x" * 100_000,)
    assert recorder.event_count == 1


@pytest.mark.parametrize("method", ["submit", "submit_controlled"])
def test_excessive_payload_nesting_raises_framework_validation_without_effect(method):
    orch = SETTOrchestrator(ethical_filter=EthicalFilter())
    executor, calls = SETTExecutor(), []
    executor.register_handler("test.nested", lambda p: calls.append(True))
    orch.register_executor(executor)
    payload = {}
    current = payload
    for _ in range(5000):
        current["child"] = {}
        current = current["child"]
    if method == "submit":
        with pytest.raises(SETTValidationError) as caught:
            executor.submit(Action("test.nested", payload))
        error = caught.value
    else:
        result = executor.submit_controlled(Action("test.nested", payload))
        assert result.status.value == "failed"
        assert result.reason_code == "action.payload_too_deep"
        error = result.exception
        assert isinstance(error, SETTValidationError)
    assert calls == []
    assert error.__context__ is None


@pytest.mark.parametrize("name", ["password", "pa\u0301ssword", "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44"])
def test_sensitive_key_compatibility_and_accent_forms_are_detected(name):
    assert is_sensitive_key(name)
    with pytest.raises(SETTValidationError):
        ExecutionContext.create(metadata={name: "synthetic"})
