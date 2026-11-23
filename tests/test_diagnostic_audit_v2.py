"""Privacy and classification at cancellation and adapter boundaries."""
from types import SimpleNamespace

import pytest

from sett import Action, EthicalFilter, SETTExecutor, SETTOrchestrator
from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import ExecutionControl, ExecutionControlRegistry
from sett.exceptions import (
    SETTAuthorizationError,
    SETTCancelledError,
    SETTServiceAdapterError,
)
from sett.services_tts_stt import assemblyai


def test_cancel_message_is_not_exported_as_reason_code():
    orch = SETTOrchestrator(ethical_filter=EthicalFilter())
    executor, events = SETTExecutor(), []

    def handler(payload):
        raise SETTCancelledError("private message", reason="PRIVATE SENTINEL person details")

    executor.register_handler("test.cancel", handler)
    orch.register_executor(executor)
    orch.register_trace_exporter(events.append)
    with pytest.raises(SETTCancelledError) as caught:
        executor.submit(Action("test.cancel", {}), execution_context=ExecutionContext.create())
    assert caught.value.reason == "execution.cancelled_by_caller"
    assert events
    assert "PRIVATE SENTINEL" not in repr(events)
    assert orch.verify_traces()


@pytest.mark.parametrize("reason", ["PRIVATE SENTINEL", "execution.user_cancelled"])
def test_cancel_request_normalizes_before_recording(reason):
    registry = ExecutionControlRegistry()
    control = ExecutionControl(ExecutionContext.create(), registry=registry)
    with control:
        control.cancel(reason=reason)
        expected = reason if reason.startswith("execution.") else "execution.cancelled_by_caller"
        assert control.token.snapshot().reason == expected


@pytest.mark.parametrize("verdict,code,status", [
    ("deny", "authorization.denied", "rejected"),
    ("block", "authorization.policy_missing", "failed"),
    ("block", "authorization.request_resolution_failed", "failed"),
])
def test_controlled_submission_keeps_authorization_diagnostic(verdict, code, status):
    orch = SETTOrchestrator(ethical_filter=EthicalFilter())
    executor = SETTExecutor()
    error = SETTAuthorizationError("synthetic boundary refusal", reason_code=code, verdict=verdict)

    def handler(payload, control):
        raise error

    executor.register_controlled_handler("test.refusal", handler)
    orch.register_executor(executor)
    result = executor.submit_controlled(Action("test.refusal", {}))
    assert result.status.value == status
    assert result.reason_code == code
    assert result.exception is error


@pytest.mark.parametrize("identifier", ["../../admin/delete?all=1#", "job/other", "", 123, None])
def test_assemblyai_rejects_non_identifier_before_polling(monkeypatch, identifier):
    calls = []
    responses = iter([{"upload_url": "https://upload.invalid/audio"}, {"id": identifier}])

    def request(method, url, **kwargs):
        calls.append((method, url))
        payload = next(responses)
        return SimpleNamespace(json=lambda: payload)

    monkeypatch.setattr(assemblyai, "checked_request", request)
    adapter = assemblyai.AssemblyAISTTAdapter("test-key")
    with pytest.raises(SETTServiceAdapterError):
        adapter.transcribe(b"synthetic audio")
    assert [method for method, _ in calls] == ["POST", "POST"]
