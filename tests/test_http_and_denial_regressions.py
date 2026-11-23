"""HTTP response ownership and real authorization verdict propagation."""
import io
import urllib.error

import pytest

from examples import governed_action as example
from sett import Action, ApprovalRequirement, SETTAuthorizationError
from sett.exceptions import SETTLLMAdapterError
from sett.policy_ruler import PolicyDecision
from sett.services_llm import ollama


@pytest.mark.parametrize("operation", ["complete", "chat"])
@pytest.mark.parametrize("status,kind,retryable", [
    (301, "unknown", None), (302, "unknown", None),
    (303, "unknown", None), (307, "unknown", None), (308, "unknown", None),
    (401, "authentication", False), (403, "authentication", False),
    (429, "quota", True), (500, "unavailable", True),
    (502, "unavailable", True), (503, "unavailable", True),
    (408, "provider_timeout", None), (504, "provider_timeout", None),
    (404, "unknown", None),
])
def test_http_error_is_closed_and_classified(monkeypatch, operation, status, kind, retryable):
    body = io.BytesIO(b"PRIVATE_RESPONSE")
    error = urllib.error.HTTPError(
        "http://localhost/private", status, "PRIVATE_REASON",
        {"Retry-After": "3"}, body,
    )
    calls = []

    def fail(request, *, timeout):
        calls.append(request)
        raise error

    monkeypatch.setattr(ollama, "_open_request", fail)
    adapter = ollama.OllamaAdapter(model="test-model")
    argument = "hello" if operation == "complete" else [{"role": "user", "content": "hello"}]
    try:
        with pytest.raises(SETTLLMAdapterError) as caught:
            getattr(adapter, operation)(argument)
        assert body.closed
        assert error.closed
        assert caught.value.__cause__ is error
        assert caught.value.kind.value == kind
        assert caught.value.reason_code == "adapter." + kind
        assert caught.value.status_code == status
        assert caught.value.retryable is retryable
        assert caught.value.retry_after_seconds == 3
        assert "PRIVATE" not in str(caught.value)
        assert "PRIVATE" not in repr(caught.value.safe_view())
        assert len(calls) == 1
    finally:
        # The in-memory fixture owns this resource even when checking old code.
        error.close()


@pytest.mark.parametrize("body", [b'{"response":"ok"}', b"not-json", b"\xff"])
def test_regular_http_response_is_closed(monkeypatch, body):
    response = io.BytesIO(body)
    monkeypatch.setattr(ollama, "_open_request", lambda *args, **kwargs: response)
    adapter = ollama.OllamaAdapter(model="test-model")
    if body.startswith(b"{"):
        assert adapter.complete("hello") == "ok"
    else:
        with pytest.raises(SETTLLMAdapterError):
            adapter.complete("hello")
    assert response.closed


@pytest.mark.parametrize("controlled_handler", [False, True])
@pytest.mark.parametrize("case,expected_status,expected_reason", [
    ("deny", "rejected", "authorization.denied"),
    ("grant", "success", None),
    ("condition", "failed", "authorization.condition_unsatisfied"),
    ("approval", "failed", "authorization.approval_required"),
    ("resolver", "failed", "authorization.request_resolution_failed"),
])
def test_real_policy_result_reaches_controlled_status(
    monkeypatch, controlled_handler, case, expected_status, expected_reason,
):
    if case == "approval":
        # Configure a real policy callback before constructing the governed system.
        def approval(self, request, verified_approvals):
            return PolicyDecision.require_approval(
                self.policy_id,
                requirements=(ApprovalRequirement("test.approval", "test.approver"),),
                conditions=(), review_obligations=(),
            )

        monkeypatch.setattr(example.RefundLimitPolicy, "evaluate", approval)
    if case == "resolver":
        def broken(self, **kwargs):
            raise RuntimeError("PRIVATE_RESOLVER_FAILURE")

        monkeypatch.setattr(example.SessionResolver, "resolve", broken)
    executor, _, effects = example.build()
    if controlled_handler:
        executor.unregister_handler(example.REFUND)
        executor.register_controlled_handler(
            example.REFUND, lambda payload, control: effects.append(payload) or "refunded",
        )
    result = executor.submit_controlled(Action(example.REFUND, {
        "amount": 250 if case == "deny" else 40,
        "customer_verified": case != "condition",
    }))
    assert result.status.value == expected_status
    if case == "grant":
        assert len(effects) == 1
        assert result.exception is None
    else:
        assert effects == []
        assert result.reason_code == expected_reason
        assert isinstance(result.exception, SETTAuthorizationError)
        assert result.exception.verdict == ("deny" if case == "deny" else "block")


def test_real_denial_retains_verdict_in_plain_submission():
    executor, _, effects = example.build()
    with pytest.raises(SETTAuthorizationError) as caught:
        executor.submit(Action(example.REFUND, {"amount": 250, "customer_verified": True}))
    assert caught.value.reason_code == "authorization.denied"
    assert caught.value.verdict == "deny"
    assert effects == []
