"""Regression coverage for action identity and public adapter diagnostics."""
import sys
import threading
import urllib.error
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from sett import Action, EthicalFilter, SETTExecutor, SETTOrchestrator
from sett._adapters.google_auth import (
    build_google_client,
    google_credentials_from_path,
)
from sett.core_ruler.execution_context import ExecutionContext
from sett.ethics_ruler.ethic_kernel.context_analyzer import (
    ContextAnalysis,
    ContextAnalyzer,
)
from sett.exceptions import (
    SETTEthicalFilterRejectedError,
    SETTLLMAdapterError,
    SETTServiceAdapterError,
)
from sett.services_llm.ollama import OllamaAdapter


class FixedRisk(ContextAnalyzer):
    """Record the evaluated identity with a deterministic score."""

    def __init__(self, score, seen):
        super().__init__()
        self.score, self.seen = score, seen

    def analyze(self, action, context, emotional_state="unknown", **kwargs):
        self.seen.append(action)
        return ContextAnalysis(
            action=action, risk_score=self.score, emotional_state=emotional_state,
            reasoning="Fixed score", consequences=[],
        )


@pytest.mark.parametrize("mutation", ["copy", "thread"])
@pytest.mark.parametrize("score", [0.0, 9.5])
def test_action_identity_survives_caller_mutation(mutation, score):
    seen, ran, events = [], [], []
    filt = EthicalFilter()
    filt.register_analyzer("audit.original", FixedRisk(score, seen))
    filt.register_analyzer("audit.replacement", FixedRisk(0.0, seen))
    orch = SETTOrchestrator(ethical_filter=filt)
    executor = SETTExecutor()
    executor.register_handler("audit.original", lambda p: ran.append("original"))
    executor.register_handler("audit.replacement", lambda p: ran.append("replacement"))
    orch.register_executor(executor)
    orch.register_trace_exporter(events.append)
    action = Action("audit.original", {})
    mutated = []

    def change():
        action.action_type = "audit.replacement"
        mutated.append(True)

    class Payload:
        def __deepcopy__(self, memo):
            change()
            return "copied"

    def exporter(event):
        if event["kind"] == "action.proposed":
            worker = threading.Thread(target=change)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()

    if mutation == "copy":
        action.payload = {"object": Payload()}
    else:
        orch.register_trace_exporter(exporter)
    if score > 9:
        with pytest.raises(SETTEthicalFilterRejectedError):
            executor.submit(action, execution_context=ExecutionContext.create())
        assert ran == []
    else:
        executor.submit(action, execution_context=ExecutionContext.create())
        assert ran == ["original"]
    assert mutated
    assert seen == ["audit.original"]
    assert all(e["component_name"] != "audit.replacement" for e in events)
    assert all(a["action_type"] == "audit.original" for a in executor.get_audit_log())


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("http://localhost", 500, "PRIVATE_SENTINEL", {}, None),
    urllib.error.URLError("PRIVATE_SENTINEL"),
    RuntimeError("PRIVATE_SENTINEL"),
])
def test_ollama_public_error_hides_provider_text(error):
    with (
        patch("sett.services_llm.ollama._open_request", side_effect=error),
        pytest.raises(SETTLLMAdapterError) as caught,
    ):
        OllamaAdapter(model="test-model").complete("hello")
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert caught.value.__cause__ is error
    if isinstance(error, urllib.error.HTTPError):
        assert caught.value.safe_view()["status_code"] == 500


@pytest.mark.parametrize("url", [
    "http://user:PRIVATE_SENTINEL@localhost:9",
    "http://user:PRIVATE_SENTINEL@localhost:bad",
    "http://localhost/?key=PRIVATE_SENTINEL",
    "http://localhost/#PRIVATE_SENTINEL",
])
def test_ollama_rejects_secret_bearing_urls_without_echo(url):
    with pytest.raises(SETTLLMAdapterError) as caught:
        OllamaAdapter(base_url=url, model="test-model")
    assert "PRIVATE_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("missing", [False, True])
def test_google_client_public_error_preserves_only_explicit_cause(missing):
    class DefaultCredentialsError(Exception):
        pass

    error = DefaultCredentialsError("PRIVATE_SENTINEL") if missing else RuntimeError("PRIVATE_SENTINEL")

    def factory(**kwargs):
        raise error

    with pytest.raises(SETTServiceAdapterError) as caught:
        build_google_client(factory, credentials=None, adapter_name="PRIVATE_SENTINEL")
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert caught.value.__cause__ is error


def test_google_credential_path_is_not_in_public_error(monkeypatch):
    error = ValueError("PRIVATE_SENTINEL")

    def load(path):
        raise error

    google = ModuleType("google")
    oauth = ModuleType("google.oauth2")
    oauth.service_account = SimpleNamespace(
        Credentials=SimpleNamespace(from_service_account_file=load),
    )
    google.oauth2 = oauth
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth)
    with pytest.raises(SETTServiceAdapterError) as caught:
        google_credentials_from_path("PRIVATE_SENTINEL/credentials.json")
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert caught.value.__cause__ is error
