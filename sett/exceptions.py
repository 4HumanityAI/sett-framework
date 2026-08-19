"""
SETT Framework: Exceptions
==============================
All custom exceptions raised by the framework.
When something goes wrong in SETT, the developer receives a specific,
descriptive error rather than a generic Python exception.

This module is a leaf of the framework's import tree: it imports nothing
from the rest of SETT at runtime. Every other module may import from here
without any risk of a cycle, which is what lets the whole hierarchy live
in one place and keeps ``from sett.exceptions import SETTError`` a single,
complete entry point for catching anything the framework raises.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from sett.core_ruler.adapter import AdapterErrorKind
    from sett.core_ruler.lifecycle import Deadline


DEFAULT_PROVIDER_ERROR_MAX_LENGTH = 200


def bound_provider_error_message(
    error: BaseException, *, max_length: int = DEFAULT_PROVIDER_ERROR_MAX_LENGTH
) -> str:
    """
    Bound an external provider's raw exception message before it is
    embedded in a SETT exception's public message string.

    Named for what it does. It was called ``sanitize_...`` and that name
    promised redaction it never performed: a message carrying
    ``Authorization: Bearer sk-live-...`` came out carrying it still. The
    One honest option was to write a redactor for arbitrary third-party
    text. That is unbounded work that produces confidence rather than safety,
    because there is always one more format it does not know. The other was to
    say plainly that this caps length and nothing else. This is the second.

    If a provider's text must not appear in a public message at all, the
    stronger move is not to embed it: raise a SETT error describing the
    failure and keep the original as ``__cause__``, where a local
    traceback still shows it and an exported message does not.

    LLM/service adapters (OpenAI, Anthropic, Gemini, ElevenLabs, ...)
    historically embedded ``str(e)`` from the provider SDK directly
    into the adapter's own error message with no limit (F29, defensive
    audit #2). No concrete secret leak was reproduced against the real
    SDKs, but an HTTP error body or SDK exception can be arbitrarily
    long and is not something SETT controls the shape of; bounding it
    here caps what an adapter error message can carry across that
    boundary, regardless of what a given provider decides to put in
    ``str(e)`` in the future.

    The original exception is not discarded: callers should still
    chain it with ``raise SETTLLMAdapterError(...) from error`` so it
    remains available as ``__cause__`` for real debugging.

    Args:
        error: The provider SDK exception that was caught.
        max_length: Maximum length of the returned message before
            truncation. Defaults to 200 characters.

    Returns:
        ``str(error)``, truncated with a trailing marker if it exceeds
        ``max_length``.
    """
    message = str(error)
    if len(message) <= max_length:
        return message
    return message[:max_length] + "... [truncated]"


class SETTError(Exception):
    """Base exception for all SETT framework errors."""


class SETTEthicalFilterRejectedError(SETTError):
    """
    Raised when the EthicalFilter blocks an action or a memory write.

    ``str(e)`` returns the same human-readable message it always has.
    In addition, the structured data behind that message is available
    as instance attributes, so downstream code never needs to parse
    the message string:

    Attributes:
        action (str | None): The action type that was blocked.
        score (float | None): The computed harm score.
        threshold (float | None): The effective reject threshold the
            score was compared against (environmental modifiers already
            applied).
        principle (str | None): The ruleset principle in effect.
        reasoning (str | None): The analyzer's reasoning for the score.
        trace_event_id (str | None): The latest causal trace event associated
            with the rejection, when tracing is active.

    All attributes default to ``None``, so code that raises this
    exception with only a message keeps working unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str | None = None,
        score: float | None = None,
        threshold: float | None = None,
        principle: str | None = None,
        reasoning: str | None = None,
        trace_event_id: str | None = None,
    ) -> None:
        """Carry the verdict that blocked the action, not just its message."""
        super().__init__(message)
        self.action = action
        self.score = score
        self.threshold = threshold
        self.principle = principle
        self.reasoning = reasoning
        self.trace_event_id = trace_event_id


class SETTEthicalFilterWarningError(SETTError):
    """
    Reserved for a future EthicalFilter warning path.

    Not currently raised anywhere in this framework (F23, defensive
    audit #2): a WARN verdict today is allowed through and logged via
    the standard `logging` module, not raised as an exception. This
    class stays public and importable so that code written against a
    future warning-as-exception path does not need to change its
    imports, but nothing in this version emits it. Do not write a
    ``try/except SETTEthicalFilterWarningError`` expecting it to ever
    trigger against this version of SETT.
    """


class SETTMemoryAccessDeniedError(SETTError):
    """
    Raised when an entity tries to access memory it has no permission to,
    in either direction: reading memory it should not see, or publishing
    data into a shared tier it is not permitted to reach.

    Examples: the orchestrator trying to read an agent's PrivateMemory;
    an agent's ``update()`` call trying to publish a ``RiskProfile`` (or
    its reserved pillar fields) into ``UniversalMemory``, which
    ``RiskProfile``'s own documentation states never happens; this is
    what makes that a structural guarantee rather than a convention
    every caller has to remember to honour.
    """


class SETTAgentNotFoundError(SETTError):
    """
    Raised when the orchestrator cannot find a registered agent
    for the requested domain.
    """


class SETTExpertNotFoundError(SETTError):
    """
    Raised when an agent cannot find a registered expert by name.
    """


class SETTAdapterError(SETTError):
    """Common structured failure raised by every external-service adapter.

    ``kind`` deliberately remains optional.  An adapter written for an older
    SETT release can keep raising ``SETTLLMAdapterError("message")`` or
    ``SETTServiceAdapterError("message")`` unchanged; callers that need the
    v0.13 contract can distinguish an undeclared classification from a
    provider failure that was positively classified.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: AdapterErrorKind | None = None,
        provider: str | None = None,
        adapter_name: str | None = None,
        operation: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        capability: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        """Store machine-readable failure facts without parsing ``message``."""
        super().__init__(message)
        self.kind = kind
        self.provider = provider
        self.adapter_name = adapter_name
        self.operation = operation
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.capability = capability
        self.reason_code = reason_code

    def safe_view(self) -> dict[str, object]:
        """Return structured fields only, never the raw provider message."""
        kind = self.kind
        return {
            "kind": getattr(kind, "value", kind),
            "provider": self.provider,
            "adapter_name": self.adapter_name,
            "operation": self.operation,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "capability": self.capability,
            "reason_code": self.reason_code,
        }


class SETTLLMAdapterError(SETTAdapterError):
    """
    Raised when an LLM adapter fails to respond or is misconfigured.
    """


class SETTServiceAdapterError(SETTAdapterError):
    """
    Raised when a TTS, STT, sentiment, or generative AI adapter fails
    or is misconfigured.
    """


class SETTConfigurationError(SETTError):
    """
    Raised when the framework or any of its components is incorrectly configured
    before the system starts running.

    Configuration failures may also carry a normalized fail-closed decision.
    An unroutable action uses ``verdict="block"`` and a stable ``reason_code``
    without pretending that the EthicalFilter evaluated or rejected it. All
    structured attributes default to ``None`` for existing call sites.

    Attributes:
        reason_code (str | None): Stable machine-readable failure reason.
        verdict (str | None): Normalized execution decision, when applicable.
        action_type (str | None): Action type that could not be configured.
        action_id (str | None): Identifier of the affected Action.
        trace_event_id (str | None): Causal event for this failure, if traced.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        verdict: str | None = None,
        action_type: str | None = None,
        action_id: str | None = None,
        trace_event_id: str | None = None,
    ) -> None:
        """Preserve optional structured context without requiring it."""
        super().__init__(message)
        self.reason_code = reason_code
        self.verdict = verdict
        self.action_type = action_type
        self.action_id = action_id
        self.trace_event_id = trace_event_id


class SETTCancelledError(SETTError):
    """
    Raised when a caller observes cancellation on an ExecutionControl it
    owns or depends on.

    Cancellation is always an explicit request made by someone: a caller
    calling ``cancel()``, or an ancestor of this execution being
    cancelled. It is deliberately NOT the same thing as running out of
    time (``SETTTimeoutError``) and NOT the same thing as being skipped
    because something else failed (``SETTDependencyError``): three
    distinct events, three distinct types, so code that needs to tell
    them apart never has to inspect a message string.

    Attributes:
        run_id (str | None): The execution node that observed the
            cancellation. This is the same ``run_id`` that appears in
            trace events for this node.
        reason (str | None): The reason code explaining the
            cancellation, drawn from the same vocabulary as trace
            ``reason_codes`` (e.g. "execution.cancelled_by_caller").
        cause_event_id (str | None): The trace event that requested the
            cancellation, when tracing was active. Lets a cancelled
            terminal be linked back to the request that caused it.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
        cause_event_id: str | None = None,
    ) -> None:
        """Name which run was cancelled, why, and the event that asked."""
        super().__init__(message)
        self.run_id = run_id
        self.reason = reason
        self.cause_event_id = cause_event_id


class SETTTimeoutError(SETTError):
    """
    Raised when an ExecutionControl exceeds its Deadline.

    Kept separate from SETTCancelledError on purpose: both stop the
    execution, but they mean different things to whoever handles them.
    Nobody asked for a timeout; it means the work did not fit in the
    time it was given, which is frequently retryable, while a
    cancellation is an explicit instruction not to continue.

    Attributes:
        run_id (str | None): The execution node whose deadline expired.
        deadline (Deadline | None): The deadline that was exceeded. Typed
            as a forward reference only: this module never imports the
            lifecycle module at runtime, so it stays a leaf of the import
            tree (see this module's own docstring).
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        deadline: Deadline | None = None,
    ) -> None:
        """Name which run ran out of time, and against which deadline."""
        super().__init__(message)
        self.run_id = run_id
        self.deadline = deadline


class SETTDependencyError(SETTError):
    """
    Raised when an execution unit is reported as skipped because a unit
    it depended on failed or was cancelled: never because it was itself
    cancelled.

    This type exists to keep two mechanisms apart that are easy to
    conflate, and whose conflation produces a specific, real bug: a
    cancellation tree is not a dependency graph. If A is cancelled and B
    never runs as a consequence, B was NOT cancelled; nobody asked for
    B to stop, B simply never became reachable. Recording that as a
    cancellation would misattribute a causal skip to a caller decision
    that never happened.

    Attributes:
        run_id (str | None): The execution node that never ran.
        failed_dependency_run_id (str | None): The node whose failure or
            cancellation made this one unreachable.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        failed_dependency_run_id: str | None = None,
    ) -> None:
        """Name the run that never became reachable, and what it needed."""
        super().__init__(message)
        self.run_id = run_id
        self.failed_dependency_run_id = failed_dependency_run_id


class SETTIdempotencyConflictError(SETTError):
    """
    Raised when an idempotency key is reused with a different action
    payload than the one it was originally reserved for.

    The same key with the same fingerprint is a replay or a retry, and
    is allowed. The same key describing a *different* operation is a
    programming error that would otherwise silently either duplicate an
    external effect or suppress a legitimate one, so it fails closed.

    Attributes:
        key (str | None): The idempotency key involved.
        expected_fingerprint (str | None): The fingerprint recorded when
            the key was first reserved.
        actual_fingerprint (str | None): The fingerprint of the action
            that tried to reuse it.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        expected_fingerprint: str | None = None,
        actual_fingerprint: str | None = None,
    ) -> None:
        """Name the reused key and the two fingerprints that disagreed."""
        super().__init__(message)
        self.key = key
        self.expected_fingerprint = expected_fingerprint
        self.actual_fingerprint = actual_fingerprint


class SETTValidationError(SETTError, ValueError):
    """
    Raised when a data-carrying value fails its own validation (e.g. a
    RiskProfile pillar outside its documented [0.0, 1.0] range).

    Inherits from both SETTError and ValueError on purpose, the same
    pattern the standard library's own json.JSONDecodeError uses for
    ValueError. Code written against this framework's own convention
    (`except SETTError`) and code written against the standard Python
    idiom for constructor validation (`except ValueError`) both catch
    the same exception: neither has to know about the other.
    """
