"""Neutral adapter capabilities, health, selection and trace helpers.

Adapters remain external dependencies called by Experts; this module does
not turn them into Agents, register them with the orchestrator, or choose a
provider on an application's behalf.  It describes what an adapter declares,
what a caller requires, and why a candidate was or was not selected.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from enum import Enum
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from sett.exceptions import (
    SETTAdapterError,
    SETTCancelledError,
    SETTTimeoutError,
    SETTValidationError,
)

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

_MAX_METADATA_DEPTH = 8
_MAX_METADATA_ITEMS = 256


class AdapterKind(str, Enum):
    """The interchangeable external-service interface an adapter implements."""

    LLM = "llm"
    TTS = "tts"
    STT = "stt"
    SENTIMENT = "sentiment"


class CapabilitySupport(str, Enum):
    """A tri-state declaration: missing evidence is not evidence of absence."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ConnectivityRequirement(str, Enum):
    """Where the service must be reachable from the running application."""

    NONE = "none"
    LOCAL_SERVICE = "local_service"
    REMOTE_SERVICE = "remote_service"
    UNKNOWN = "unknown"


class AdapterHealthStatus(str, Enum):
    """The most recent honest observation of an adapter instance."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class AdapterErrorKind(str, Enum):
    """Provider-neutral adapter failure categories."""

    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    PROVIDER_TIMEOUT = "provider_timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


def _enum(value: object, enum_type: type[Enum], *, name: str) -> Enum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as error:
        raise SETTValidationError(f"{name} must be a valid {enum_type.__name__}.") from error


def _freeze_json(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> JsonValue:
    if counter is None:
        counter = [0]
    if depth > _MAX_METADATA_DEPTH:
        raise SETTValidationError("Adapter metadata exceeds the maximum nesting depth.")
    counter[0] += 1
    if counter[0] > _MAX_METADATA_ITEMS:
        raise SETTValidationError("Adapter metadata contains too many values.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SETTValidationError("Adapter metadata numbers must be finite.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise SETTValidationError("Adapter metadata keys must be non-empty strings.")
            frozen[key] = _freeze_json(item, depth=depth + 1, counter=counter)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, depth=depth + 1, counter=counter) for item in value)
    raise SETTValidationError(
        "Adapter metadata must contain only JSON-compatible values."
    )


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _clean_strings(values: Sequence[str] | None, *, name: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SETTValidationError(f"{name} must contain non-empty strings.")
        item = value.strip()
        folded = item.casefold()
        if folded not in seen:
            cleaned.append(item)
            seen.add(folded)
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """An immutable declaration of what one adapter implementation can do."""

    kind: AdapterKind
    streaming: CapabilitySupport = CapabilitySupport.UNKNOWN
    voice_cloning: CapabilitySupport = CapabilitySupport.UNKNOWN
    partial_results: CapabilitySupport = CapabilitySupport.UNKNOWN
    languages: tuple[str, ...] | None = None
    connectivity: ConnectivityRequirement = ConnectivityRequirement.UNKNOWN
    input_formats: tuple[str, ...] | None = None
    output_formats: tuple[str, ...] | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(self.kind, AdapterKind, name="kind"))
        for field_name in ("streaming", "voice_cloning", "partial_results"):
            object.__setattr__(
                self,
                field_name,
                _enum(getattr(self, field_name), CapabilitySupport, name=field_name),
            )
        object.__setattr__(
            self,
            "connectivity",
            _enum(self.connectivity, ConnectivityRequirement, name="connectivity"),
        )
        object.__setattr__(self, "languages", _clean_strings(self.languages, name="languages"))
        object.__setattr__(
            self, "input_formats", _clean_strings(self.input_formats, name="input_formats")
        )
        object.__setattr__(
            self, "output_formats", _clean_strings(self.output_formats, name="output_formats")
        )
        frozen = _freeze_json(dict(self.metadata))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "metadata", frozen)

    def safe_view(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""
        return {
            "kind": self.kind.value,
            "streaming": self.streaming.value,
            "voice_cloning": self.voice_cloning.value,
            "partial_results": self.partial_results.value,
            "languages": None if self.languages is None else list(self.languages),
            "connectivity": self.connectivity.value,
            "input_formats": None if self.input_formats is None else list(self.input_formats),
            "output_formats": None if self.output_formats is None else list(self.output_formats),
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AdapterRequirements:
    """Capabilities a caller requires before it may use an adapter."""

    kind: AdapterKind
    require_streaming: bool = False
    require_voice_cloning: bool = False
    require_partial_results: bool = False
    language: str | None = None
    require_connectivity: ConnectivityRequirement | None = None
    input_format: str | None = None
    output_format: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(self.kind, AdapterKind, name="kind"))
        if self.require_connectivity is not None:
            object.__setattr__(
                self,
                "require_connectivity",
                _enum(
                    self.require_connectivity,
                    ConnectivityRequirement,
                    name="require_connectivity",
                ),
            )
        for name in ("language", "input_format", "output_format"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise SETTValidationError(f"{name} must be a non-empty string or None.")
                object.__setattr__(self, name, value.strip())


@dataclass(frozen=True, slots=True)
class AdapterErrorView:
    """Sanitized adapter failure facts safe for health and trace exports."""

    kind: str | None = None
    provider: str | None = None
    adapter_name: str | None = None
    operation: str | None = None
    status_code: int | None = None
    retryable: bool | None = None
    retry_after_seconds: float | None = None
    capability: str | None = None
    reason_code: str | None = None

    @classmethod
    def from_error(cls, error: SETTAdapterError) -> AdapterErrorView:
        """Build a view without copying ``str(error)`` or its cause."""
        values = error.safe_view()
        return cls(**values)  # type: ignore[arg-type]

    def safe_view(self) -> dict[str, object]:
        """Return a detached mapping for logs and traces."""
        return {
            "kind": self.kind,
            "provider": self.provider,
            "adapter_name": self.adapter_name,
            "operation": self.operation,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "capability": self.capability,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """A timestamped observation, never an implied background probe."""

    status: AdapterHealthStatus
    checked_at: datetime
    latency_ms: float | None = None
    reason_codes: tuple[str, ...] = ()
    error: AdapterErrorView | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _enum(self.status, AdapterHealthStatus, name="status")
        )
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise SETTValidationError("AdapterHealth.checked_at must be timezone-aware.")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, (int, float))
            or not math.isfinite(float(self.latency_ms))
            or self.latency_ms < 0
        ):
            raise SETTValidationError("AdapterHealth.latency_ms must be finite and non-negative.")
        object.__setattr__(
            self,
            "reason_codes",
            _clean_strings(self.reason_codes, name="reason_codes") or (),
        )

    @classmethod
    def unknown(cls, *, reason_code: str = "adapter.health_not_checked") -> AdapterHealth:
        """The honest initial state for an adapter with no observation."""
        return cls(
            AdapterHealthStatus.UNKNOWN,
            datetime.now(timezone.utc),
            reason_codes=(reason_code,),
        )

    def safe_view(self) -> dict[str, object]:
        """Return a detached, provider-message-free representation."""
        return {
            "status": self.status.value,
            "checked_at": self.checked_at.isoformat(),
            "latency_ms": self.latency_ms,
            "reason_codes": list(self.reason_codes),
            "error": None if self.error is None else self.error.safe_view(),
        }


def adapter_health(adapter: object) -> AdapterHealth:
    """Read the passive snapshot even from a legacy adapter without ``super()``."""
    health = getattr(adapter, "_adapter_health", None)
    return health if isinstance(health, AdapterHealth) else AdapterHealth.unknown()


def adapter_capabilities(adapter: object, *, kind: AdapterKind) -> AdapterCapabilities:
    """Read a declaration, falling back to an honest legacy snapshot."""
    declared = getattr(adapter, "capabilities", None)
    return declared if isinstance(declared, AdapterCapabilities) else AdapterCapabilities(kind)


def _set_adapter_health(adapter: object, health: AdapterHealth) -> None:
    try:
        adapter._adapter_health = health
    except (AttributeError, TypeError):
        # A frozen/slots-only third-party adapter can still satisfy the base
        # interface; passive health simply remains UNKNOWN for that instance.
        return


def _status_for_error(error: SETTAdapterError) -> AdapterHealthStatus:
    if error.kind in (AdapterErrorKind.AUTHENTICATION, AdapterErrorKind.CONFIGURATION):
        return AdapterHealthStatus.UNAVAILABLE
    if error.kind in (AdapterErrorKind.QUOTA, AdapterErrorKind.PROVIDER_TIMEOUT):
        return AdapterHealthStatus.DEGRADED
    if error.kind is AdapterErrorKind.UNAVAILABLE:
        return AdapterHealthStatus.UNAVAILABLE
    return AdapterHealthStatus.UNKNOWN


def provider_status_code(error: BaseException) -> int | None:
    """Read a structured HTTP status without inspecting provider text."""
    for owner in (error, getattr(error, "response", None)):
        if owner is None:
            continue
        for name in ("status_code", "status"):
            value = getattr(owner, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def provider_retry_after(error: BaseException) -> float | None:
    """Read a numeric Retry-After value from structured response headers.

    Two header containers are understood: a mapping, which is what the
    ``requests`` and ``httpx`` transports expose, and the standard
    library's ``email.message.Message``, which is what ``urllib`` attaches
    to an ``HTTPError`` (as ``http.client.HTTPMessage``). Anything else is
    not read, so an unrelated object can never be mistaken for a header.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not isinstance(headers, (Mapping, Message)):
        headers = getattr(error, "headers", None)
    if not isinstance(headers, (Mapping, Message)):
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def adapter_error_kind_for_status(status_code: int | None) -> AdapterErrorKind:
    """Map protocol status, never message wording, to a neutral category."""
    if status_code in (401, 403):
        return AdapterErrorKind.AUTHENTICATION
    if status_code == 429:
        return AdapterErrorKind.QUOTA
    if status_code in (408, 504):
        return AdapterErrorKind.PROVIDER_TIMEOUT
    if status_code in (500, 502, 503):
        return AdapterErrorKind.UNAVAILABLE
    return AdapterErrorKind.UNKNOWN


def normalized_adapter_error(
    error: BaseException,
    *,
    error_type: type[SETTAdapterError],
    provider: str,
    adapter_name: str,
    operation: str,
    kind: AdapterErrorKind | None = None,
    status_code: int | None = None,
    reason_code: str | None = None,
) -> SETTAdapterError:
    """Create one structured adapter error while retaining the real cause.

    The public message is built only from values SETT controls: the
    provider label, the operation, the neutral error category, and the
    protocol status. The provider's own exception text is never part of
    it (found by the v0.14.0 audit). Truncating
    that text, which is what this function used to do, bounded its length
    and nothing else, so ``logger.error("%s", error)`` in ordinary
    application code still exported whatever the provider had put in
    ``str(e)``: an audit probe recovered a bearer token that way.

    Nothing is lost for debugging. The original exception is chained, so
    a local traceback still shows the provider's message in full, and
    ``safe_view()`` continues to expose the structured facts. What
    changed is that reaching the provider's text now requires the
    traceback, which is a deliberate diagnostic act, rather than
    happening by default in any log line that formats the exception.
    """
    status = provider_status_code(error) if status_code is None else status_code
    resolved_kind = kind or adapter_error_kind_for_status(status)
    retryable: bool | None
    if resolved_kind in (
        AdapterErrorKind.AUTHENTICATION,
        AdapterErrorKind.CONFIGURATION,
        AdapterErrorKind.INVALID_RESPONSE,
        AdapterErrorKind.UNSUPPORTED_CAPABILITY,
    ):
        retryable = False
    elif resolved_kind in (AdapterErrorKind.QUOTA, AdapterErrorKind.UNAVAILABLE):
        retryable = True
    else:
        # Provider timeouts are intentionally ambiguous and UNKNOWN stays
        # unknown.  Neither becomes a retry permission by itself.
        retryable = None
    code = reason_code or f"adapter.{resolved_kind.value}"
    status_suffix = "" if status is None else f", status {status}"
    return error_type(
        f"{provider} {operation} failed ({resolved_kind.value}{status_suffix})",
        kind=resolved_kind,
        provider=provider,
        adapter_name=adapter_name,
        operation=operation,
        status_code=status,
        retryable=retryable,
        retry_after_seconds=provider_retry_after(error),
        reason_code=code,
    )


def required_model(
    value: object,
    *,
    error_type: type[SETTAdapterError],
    provider: str,
    adapter_name: str,
    parameter: str = "model",
) -> str:
    """Return the model identifier the application chose, or refuse.

    SETT ships no default model for any provider. Which model an
    application uses is its own decision, with its own cost, quality and
    availability consequences, and a default written here would make that
    decision for it and go stale as providers retire models. An adapter
    built without one fails at construction with a configuration error
    instead of on the first call.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise error_type(
        f"{adapter_name} needs {parameter}=: the identifier of the model "
        "your application chose. SETT does not select a model for you; "
        "see the provider's documentation for the identifiers available "
        "to your account.",
        kind=AdapterErrorKind.CONFIGURATION,
        provider=provider,
        adapter_name=adapter_name,
        reason_code="adapter.model_missing",
    )


def _adapter_name(adapter: object) -> str:
    return type(adapter).__name__


def record_adapter_event(
    adapter: object,
    *,
    kind: str,
    operation: str,
    status: str,
    reason_codes: tuple[str, ...] = (),
    attributes: Mapping[str, object] | None = None,
) -> str | None:
    """Best-effort sanitized trace event on the active execution control."""
    try:
        from sett.core_ruler.execution_context import current_trace_cause_id
        from sett.core_ruler.lifecycle import current_execution_control

        control = current_execution_control()
        if control is None or control.trace_recorder is None:
            return None
        capabilities = getattr(adapter, "capabilities", None)
        modality = (
            capabilities.kind.value
            if isinstance(capabilities, AdapterCapabilities)
            else AdapterKind.LLM.value
            if adapter.__class__.__module__.startswith("sett.services_llm")
            else "unknown"
        )
        safe_attributes = {
            "operation": operation,
            "modality": modality,
            **dict(attributes or {}),
        }
        recorder = control.trace_recorder
        event = recorder.record(
            control.context,
            kind=kind,
            component_type="adapter",
            component_name=_adapter_name(adapter),
            status=status,
            cause_id=current_trace_cause_id() or recorder.last_event_id(control.run_id),
            reason_codes=reason_codes,
            attributes=safe_attributes,
        )
        return event.event_id
    except Exception:  # noqa: BLE001 - optional telemetry must be fail-open
        # Adapter tracing is observational.  The Executor remains the only
        # boundary where inability to audit blocks a real-world effect.
        return None


@contextmanager
def adapter_call_scope(adapter: object, operation: str) -> Iterator[None]:
    """Update passive health and record sanitized events around one call."""
    started = monotonic()
    start_event_id = record_adapter_event(
        adapter,
        kind="adapter.call_started",
        operation=operation,
        status="started",
    )
    try:
        yield
    except SETTAdapterError as error:
        latency = max(0.0, (monotonic() - started) * 1000.0)
        view = AdapterErrorView.from_error(error)
        reason = error.reason_code or "adapter.call_failed"
        _set_adapter_health(
            adapter,
            AdapterHealth(
                _status_for_error(error),
                datetime.now(timezone.utc),
                latency_ms=latency,
                reason_codes=(reason,),
                error=view,
            ),
        )
        record_adapter_event(
            adapter,
            kind="adapter.call_failed",
            operation=operation,
            status="failed",
            reason_codes=(reason,),
            attributes={
                "duration_ms": latency,
                "error_kind": view.kind,
                "start_event_id": start_event_id,
            },
        )
        raise
    except SETTCancelledError:
        record_adapter_event(
            adapter,
            kind="adapter.call_failed",
            operation=operation,
            status="cancelled",
            reason_codes=("execution.cancelled",),
            attributes={"start_event_id": start_event_id},
        )
        raise
    except SETTTimeoutError:
        record_adapter_event(
            adapter,
            kind="adapter.call_failed",
            operation=operation,
            status="timed_out",
            reason_codes=("execution.deadline_exceeded",),
            attributes={"start_event_id": start_event_id},
        )
        raise
    else:
        latency = max(0.0, (monotonic() - started) * 1000.0)
        _set_adapter_health(
            adapter,
            AdapterHealth(
                AdapterHealthStatus.HEALTHY,
                datetime.now(timezone.utc),
                latency_ms=latency,
                reason_codes=("adapter.call_succeeded",),
            ),
        )
        record_adapter_event(
            adapter,
            kind="adapter.call_completed",
            operation=operation,
            status="completed",
            attributes={"duration_ms": latency, "start_event_id": start_event_id},
        )


@dataclass(frozen=True, slots=True)
class AdapterHandle:
    """A caller-supplied name and adapter instance considered for selection."""

    name: str
    adapter: object
    circuit_breaker: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SETTValidationError("AdapterHandle.name must be a non-empty string.")
        object.__setattr__(self, "name", self.name.strip())


@runtime_checkable
class SelectionPolicy(Protocol):
    """An application-owned tie-breaker among already-compatible adapters."""

    def choose(
        self,
        candidates: tuple[AdapterHandle, ...],
        requirements: AdapterRequirements,
    ) -> AdapterHandle | None:
        """Choose one candidate or return None without performing any call."""


@dataclass(frozen=True, slots=True)
class PrioritySelectionPolicy:
    """Choose by the explicit adapter-name order supplied by the caller."""

    order: tuple[str, ...]

    def __post_init__(self) -> None:
        cleaned = _clean_strings(self.order, name="order") or ()
        if not cleaned:
            raise SETTValidationError("PrioritySelectionPolicy.order cannot be empty.")
        object.__setattr__(self, "order", cleaned)

    def choose(
        self,
        candidates: tuple[AdapterHandle, ...],
        requirements: AdapterRequirements,
    ) -> AdapterHandle | None:
        """Return the first eligible handle named in the caller's order."""
        del requirements
        by_name = {candidate.name: candidate for candidate in candidates}
        for name in self.order:
            if name in by_name:
                return by_name[name]
        return None


@dataclass(frozen=True, slots=True)
class AdapterSelection:
    """A deterministic selection result with rejection reason codes."""

    chosen: str | None
    candidates_considered: tuple[str, ...]
    rejected: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        candidates = _clean_strings(self.candidates_considered, name="candidates_considered") or ()
        object.__setattr__(self, "candidates_considered", candidates)
        frozen: dict[str, tuple[str, ...]] = {}
        for name, reasons in self.rejected.items():
            if not isinstance(name, str) or not name.strip():
                raise SETTValidationError("Rejected adapter names must be non-empty strings.")
            frozen[name.strip()] = _clean_strings(reasons, name="rejection reasons") or ()
        if self.chosen is None and candidates and not any(frozen.values()):
            raise SETTValidationError(
                "A selection with no chosen adapter must explain at least one rejection."
            )
        object.__setattr__(self, "rejected", MappingProxyType(frozen))

    def safe_view(self) -> dict[str, object]:
        """Return a detached representation for diagnostics and traces."""
        return {
            "chosen": self.chosen,
            "candidates_considered": list(self.candidates_considered),
            "rejected": {name: list(reasons) for name, reasons in self.rejected.items()},
        }


def _required_support(
    support: CapabilitySupport,
    *,
    missing: str,
    unknown: str,
) -> str | None:
    if support is CapabilitySupport.YES:
        return None
    return missing if support is CapabilitySupport.NO else unknown


def _contains(values: tuple[str, ...] | None, required: str, *, prefix: str) -> str | None:
    if values is None:
        return f"adapter.{prefix}_unknown"
    if required.casefold() not in {value.casefold() for value in values}:
        return f"adapter.{prefix}_unsupported"
    return None


def _rejection_reasons(handle: AdapterHandle, requirements: AdapterRequirements) -> tuple[str, ...]:
    capabilities = getattr(handle.adapter, "capabilities", None)
    if not isinstance(capabilities, AdapterCapabilities):
        return ("adapter.capabilities_unknown",)
    reasons: list[str] = []
    if capabilities.kind is not requirements.kind:
        reasons.append("adapter.kind_mismatch")
    if requirements.require_streaming:
        reason = _required_support(
            capabilities.streaming,
            missing="adapter.streaming_unsupported",
            unknown="adapter.streaming_unknown",
        )
        if reason:
            reasons.append(reason)
    if requirements.require_voice_cloning:
        reason = _required_support(
            capabilities.voice_cloning,
            missing="adapter.voice_cloning_unsupported",
            unknown="adapter.voice_cloning_unknown",
        )
        if reason:
            reasons.append(reason)
    if requirements.require_partial_results:
        reason = _required_support(
            capabilities.partial_results,
            missing="adapter.partial_results_unsupported",
            unknown="adapter.partial_results_unknown",
        )
        if reason:
            reasons.append(reason)
    if requirements.language:
        reason = _contains(capabilities.languages, requirements.language, prefix="language")
        if reason:
            reasons.append(reason)
    if requirements.input_format:
        reason = _contains(capabilities.input_formats, requirements.input_format, prefix="input_format")
        if reason:
            reasons.append(reason)
    if requirements.output_format:
        reason = _contains(capabilities.output_formats, requirements.output_format, prefix="output_format")
        if reason:
            reasons.append(reason)
    if (
        requirements.require_connectivity is not None
        and capabilities.connectivity is not requirements.require_connectivity
    ):
        reasons.append(
            "adapter.connectivity_unknown"
            if capabilities.connectivity is ConnectivityRequirement.UNKNOWN
            else "adapter.connectivity_mismatch"
        )
    health = getattr(handle.adapter, "health", None)
    if isinstance(health, AdapterHealth) and health.status is AdapterHealthStatus.UNAVAILABLE:
        reasons.append("adapter.health_unavailable")
    breaker = handle.circuit_breaker
    state = getattr(breaker, "state", None)
    if getattr(state, "value", state) == "open":
        reasons.append("adapter.circuit_open")
    return tuple(reasons)


def select_adapter(
    candidates: Sequence[AdapterHandle],
    requirements: AdapterRequirements,
    *,
    policy: SelectionPolicy | Callable[[tuple[AdapterHandle, ...], AdapterRequirements], AdapterHandle | None] | None = None,
) -> AdapterSelection:
    """Filter and explain candidates; never construct, call, or recommend one."""
    handles = tuple(candidates)
    names = [handle.name for handle in handles]
    if len(names) != len(set(names)):
        raise SETTValidationError("AdapterHandle names must be unique within a selection.")
    rejected: dict[str, tuple[str, ...]] = {}
    eligible: list[AdapterHandle] = []
    for handle in handles:
        reasons = _rejection_reasons(handle, requirements)
        if reasons:
            rejected[handle.name] = reasons
        else:
            eligible.append(handle)

    chosen: AdapterHandle | None = None
    if len(eligible) == 1:
        chosen = eligible[0]
    elif len(eligible) > 1:
        if policy is None:
            for handle in eligible:
                rejected[handle.name] = ("adapter.selection_policy_required",)
        else:
            chooser = policy.choose if isinstance(policy, SelectionPolicy) else policy
            chosen = chooser(tuple(eligible), requirements)
            if chosen is not None and chosen not in eligible:
                raise SETTValidationError("Selection policy returned an ineligible adapter.")
            if chosen is None:
                for handle in eligible:
                    rejected[handle.name] = ("adapter.selection_policy_declined",)

    selection = AdapterSelection(
        chosen=None if chosen is None else chosen.name,
        candidates_considered=tuple(names),
        rejected=rejected,
    )
    if chosen is not None:
        record_adapter_event(
            chosen.adapter,
            kind="adapter.fallback_selected",
            operation="select_adapter",
            status="selected",
            attributes={"selection": selection.safe_view()},
        )
    return selection


__all__ = [
    "AdapterCapabilities",
    "AdapterErrorKind",
    "AdapterErrorView",
    "AdapterHandle",
    "AdapterHealth",
    "AdapterHealthStatus",
    "AdapterKind",
    "AdapterRequirements",
    "AdapterSelection",
    "CapabilitySupport",
    "ConnectivityRequirement",
    "PrioritySelectionPolicy",
    "SelectionPolicy",
    "adapter_call_scope",
    "adapter_capabilities",
    "adapter_error_kind_for_status",
    "adapter_health",
    "normalized_adapter_error",
    "provider_retry_after",
    "provider_status_code",
    "record_adapter_event",
    "select_adapter",
]
