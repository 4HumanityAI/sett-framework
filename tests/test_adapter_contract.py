"""Public adapter capability, selection, health, and error contracts."""
from datetime import datetime, timezone

import pytest

from sett import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterHandle,
    AdapterHealth,
    AdapterHealthStatus,
    AdapterKind,
    AdapterRequirements,
    CapabilitySupport,
    ConnectivityRequirement,
    ExecutionContext,
    PrioritySelectionPolicy,
    SETTAdapterError,
    SETTLLMAdapterError,
    SETTServiceAdapterError,
    TraceRecorder,
    select_adapter,
)
from sett.core_ruler.adapter import adapter_call_scope, normalized_adapter_error
from sett.core_ruler.lifecycle import ExecutionControl, lifecycle_scope
from sett.services_llm.base import LLMBase


class LegacyLLM(LLMBase):
    @property
    def model_name(self):
        return "legacy"

    def complete(self, prompt, system="", **kwargs):
        return "ok"

    def chat(self, messages, system="", **kwargs):
        return "ok"


class DeclaredLLM(LegacyLLM):
    def __init__(self, *, streaming=CapabilitySupport.NO):
        self._streaming = streaming

    @property
    def capabilities(self):
        return AdapterCapabilities(
            AdapterKind.LLM,
            streaming=self._streaming,
            languages=("en", "es-AR"),
            connectivity=ConnectivityRequirement.REMOTE_SERVICE,
            input_formats=("text",),
            output_formats=("text",),
            metadata={"limits": {"max_output_tokens": 1024}},
        )


def test_a_legacy_adapter_gets_additive_unknown_contracts():
    adapter = LegacyLLM()
    assert adapter.capabilities.kind is AdapterKind.LLM
    assert adapter.capabilities.streaming is CapabilitySupport.UNKNOWN
    assert adapter.health.status is AdapterHealthStatus.UNKNOWN
    assert adapter.check_health().status is AdapterHealthStatus.UNKNOWN


def test_capabilities_are_defensive_and_json_safe():
    source = {"nested": {"values": [1, 2]}}
    capabilities = AdapterCapabilities(AdapterKind.LLM, metadata=source)
    source["nested"]["values"].append(3)
    view = capabilities.safe_view()
    assert view["metadata"] == {"nested": {"values": [1, 2]}}
    view["metadata"]["nested"]["values"].append(4)
    assert capabilities.safe_view()["metadata"] == {"nested": {"values": [1, 2]}}


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_capabilities_reject_non_finite_metadata(value):
    with pytest.raises(ValueError):
        AdapterCapabilities(AdapterKind.LLM, metadata={"bad": value})


def test_error_hierarchy_is_widened_without_breaking_old_constructors():
    llm = SETTLLMAdapterError("old")
    service = SETTServiceAdapterError("old")
    assert isinstance(llm, SETTAdapterError)
    assert isinstance(service, SETTAdapterError)
    assert str(llm) == "old"


def test_error_safe_view_never_contains_message_or_cause():
    cause = RuntimeError("Authorization: Bearer secret")
    error = SETTLLMAdapterError(
        "bounded public message",
        kind=AdapterErrorKind.AUTHENTICATION,
        provider="example",
        reason_code="adapter.authentication_failed",
    )
    error.__cause__ = cause
    view = error.safe_view()
    assert view["kind"] == "authentication"
    assert "message" not in view
    assert "secret" not in repr(view)


# ── the public message carries no provider-supplied text ───────────────
#
# Found by the v0.14.0 audit. The audit
# put a synthetic bearer token in a provider exception and recovered it
# from the adapter error's public string. ``safe_view()`` was already
# clean; the leak was ``str(error)``, which ordinary application logging
# formats by default. The sentinel below is the audit's own probe.

_PROVIDER_SENTINEL = "Authorization: Bearer SECRET-SENTINEL-123456789"


def _sentinel_error(**overrides):
    cause = RuntimeError(_PROVIDER_SENTINEL)
    defaults = {
        "error_type": SETTServiceAdapterError,
        "provider": "azure_speech",
        "adapter_name": "AzureTTSAdapter",
        "operation": "synthesize",
    }
    error = normalized_adapter_error(cause, **{**defaults, **overrides})
    error.__cause__ = cause
    return error


def test_a_normalized_message_is_built_only_from_controlled_identifiers():
    error = _sentinel_error(kind=AdapterErrorKind.AUTHENTICATION)

    assert str(error) == "azure_speech synthesize failed (authentication)"


def test_a_normalized_message_excludes_provider_supplied_text():
    error = _sentinel_error(kind=AdapterErrorKind.AUTHENTICATION)

    assert "SECRET-SENTINEL-123456789" not in str(error)
    assert "SECRET-SENTINEL-123456789" not in repr(error.safe_view())


def test_the_provider_exception_is_still_reachable_as_the_cause():
    """Removing the text from the message must not remove the diagnosis."""
    error = _sentinel_error(kind=AdapterErrorKind.AUTHENTICATION)

    assert isinstance(error.__cause__, RuntimeError)
    assert "SECRET-SENTINEL-123456789" in str(error.__cause__)


def test_a_protocol_status_is_controlled_data_and_stays_in_the_message():
    """The status comes from the transport, not from provider prose."""
    error = _sentinel_error(status_code=429)

    assert str(error) == "azure_speech synthesize failed (quota, status 429)"
    assert error.status_code == 429
    assert error.retryable is True


def test_selection_rejects_unknown_instead_of_treating_it_as_no():
    selection = select_adapter(
        [AdapterHandle("legacy", LegacyLLM())],
        AdapterRequirements(AdapterKind.LLM, require_streaming=True),
    )
    assert selection.chosen is None
    assert selection.rejected["legacy"] == ("adapter.streaming_unknown",)


def test_selection_rejects_declared_no():
    selection = select_adapter(
        [AdapterHandle("sync", DeclaredLLM())],
        AdapterRequirements(AdapterKind.LLM, require_streaming=True),
    )
    assert selection.rejected["sync"] == ("adapter.streaming_unsupported",)


def test_one_compatible_candidate_needs_no_policy():
    selection = select_adapter(
        [AdapterHandle("cloud", DeclaredLLM())],
        AdapterRequirements(AdapterKind.LLM, language="es-AR"),
    )
    assert selection.chosen == "cloud"


def test_multiple_compatible_candidates_require_caller_policy():
    candidates = [
        AdapterHandle("a", DeclaredLLM()),
        AdapterHandle("b", DeclaredLLM()),
    ]
    selection = select_adapter(candidates, AdapterRequirements(AdapterKind.LLM))
    assert selection.chosen is None
    assert selection.rejected["a"] == ("adapter.selection_policy_required",)


def test_explicit_priority_is_caller_owned_and_deterministic():
    candidates = [
        AdapterHandle("cloud", DeclaredLLM()),
        AdapterHandle("local", DeclaredLLM()),
    ]
    selection = select_adapter(
        candidates,
        AdapterRequirements(AdapterKind.LLM),
        policy=PrioritySelectionPolicy(("local", "cloud")),
    )
    assert selection.chosen == "local"


def test_unavailable_health_excludes_a_candidate():
    adapter = DeclaredLLM()
    adapter._adapter_health = AdapterHealth(
        AdapterHealthStatus.UNAVAILABLE,
        datetime.now(timezone.utc),
        reason_codes=("adapter.provider_down",),
    )
    selection = select_adapter(
        [AdapterHandle("down", adapter)],
        AdapterRequirements(AdapterKind.LLM),
    )
    assert "adapter.health_unavailable" in selection.rejected["down"]


def test_adapter_call_scope_updates_passive_health():
    adapter = DeclaredLLM()
    with adapter_call_scope(adapter, "complete"):
        pass
    assert adapter.health.status is AdapterHealthStatus.HEALTHY


def test_adapter_failure_updates_health_without_raw_message():
    adapter = DeclaredLLM()
    with pytest.raises(SETTLLMAdapterError), adapter_call_scope(adapter, "complete"):
        raise SETTLLMAdapterError(
            "provider said secret",
            kind=AdapterErrorKind.UNAVAILABLE,
            reason_code="adapter.provider_unavailable",
        )
    assert adapter.health.status is AdapterHealthStatus.UNAVAILABLE
    assert "secret" not in repr(adapter.health.safe_view())


def test_adapter_events_join_the_active_verified_trace():
    recorder = TraceRecorder()
    context = ExecutionContext.create()
    started = recorder.record(
        context,
        kind="trace.started",
        component_type="orchestrator",
        component_name="test",
        status="started",
    )
    control = ExecutionControl.create_root(context, trace_recorder=recorder)
    adapter = DeclaredLLM()
    with control, lifecycle_scope(control), adapter_call_scope(adapter, "complete"):
        pass
    recorder.record(
        context,
        kind="trace.completed",
        component_type="orchestrator",
        component_name="test",
        status="completed",
        cause_id=recorder.last_event_id(context.run_id) or started.event_id,
    )
    kinds = [event.kind for event in recorder.get_trace(context.trace_id)]
    assert "adapter.call_started" in kinds
    assert "adapter.call_completed" in kinds
    assert recorder.verify(context.trace_id)
