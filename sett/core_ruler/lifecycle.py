"""Cooperative cancellation, deadlines and scoped cleanup for one run.

``ExecutionContext`` (see ``execution_context.py``) answers "which
execution does this belong to". This module answers the next question:
"may it still continue, how much time is left, and how does it clean up
after itself".

The single most important design decision here is what this module does
NOT contain: a second execution tree.

``ExecutionContext`` has been a tree since v0.11.0 - ``derive()`` keeps
``trace_id``, sets ``parent_id`` to the current ``run_id`` and mints a new
``run_id``, which is exactly what Orchestrator -> Agent -> Expert ->
PipelineStage -> Handler already do at every instrumented boundary. So an
``ExecutionControl`` carries a reference to its own ``ExecutionContext``
and nothing more: subtree cancellation is answered by walking the
``parent_id`` chain that already exists, through
``ExecutionControlRegistry``. There is no ``parent`` attribute and no
``create_child()`` on any cancellation object in this module, because a
parallel hierarchy maintained next to the real one is a pair of
structures that will eventually disagree.

What that buys, concretely: **the ``run_id`` you can read off an exported
trace event is the handle you can cancel.** Controls are built on the
same contexts the ``TraceRecorder`` records, so an application can keep
the ``run_id`` of one conversational turn and later cancel exactly that
turn - not the whole session, and not just the leaf that happens to be
running.

Three distinct ways an execution can stop, kept apart deliberately:

* cancellation - somebody asked for it (``SETTCancelledError``);
* deadline - it ran out of time (``SETTTimeoutError``);
* dependency - it never became reachable because something it needed
  failed (``SETTDependencyError``).

A cancellation tree is not a dependency graph. If A is cancelled and B
consequently never runs, B was not cancelled: nobody ever asked anything
of B. Collapsing those into one state is how a causal skip gets
misreported as a caller decision, so they never share a field or a type.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sett.audit_ruler.trace import (
    CANCEL_REQUESTED_KIND,
    CLEANUP_COMPLETED_KIND,
    CLEANUP_FAILED_KIND,
    CLEANUP_STARTED_KIND,
    DEADLINE_EXCEEDED_KIND,
)
from sett.core_ruler.execution_context import ExecutionContext
from sett.exceptions import (
    SETTCancelledError,
    SETTConfigurationError,
    SETTTimeoutError,
    SETTValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sett.audit_ruler.trace import TraceRecorder

logger = logging.getLogger(__name__)

CleanupHook = Callable[[], None]
CancellationCallback = Callable[[], None]

DEFAULT_CANCELLATION_REASON = "execution.cancelled_by_caller"
DEADLINE_EXCEEDED_REASON = "execution.deadline_exceeded"


# ── Cancellation state ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CancellationSnapshot:
    """An immutable read of one node's cancellation state."""

    cancelled: bool
    reason: str | None = None
    cause_event_id: str | None = None
    cancelled_run_id: str | None = None
    """The node the cancellation was actually requested on.

    Differs from the observing node whenever cancellation was inherited
    from an ancestor, which is what makes a cancelled leaf able to report
    *which* branch was cancelled rather than just that it was.
    """


class _ControlNode:
    """Registry-side mutable state for one ``run_id``.

    Holds no parent pointer of its own: the parent is read from
    ``context.parent_id`` every time it is needed, so this node can never
    disagree with the ``ExecutionContext`` it describes.
    """

    __slots__ = (
        "context",
        "control",
        "event",
        "reason",
        "cause_event_id",
        "cancelled_run_id",
        "callbacks",
        "released",
    )

    def __init__(
        self, context: ExecutionContext, control: "ExecutionControl"
    ) -> None:
        self.context = context
        self.control = control
        self.event = threading.Event()
        self.reason: str | None = None
        self.cause_event_id: str | None = None
        self.cancelled_run_id: str | None = None
        self.callbacks: list[CancellationCallback] = []
        self.released = False

    def snapshot(self) -> CancellationSnapshot:
        if not self.event.is_set():
            return CancellationSnapshot(cancelled=False)
        return CancellationSnapshot(
            cancelled=True,
            reason=self.reason,
            cause_event_id=self.cause_event_id,
            cancelled_run_id=self.cancelled_run_id,
        )


class ExecutionControlRegistry:
    """Index of live ``ExecutionControl`` nodes, keyed by ``run_id``.

    The parent/child relation is never stored here as data of its own: it
    is read from each node's ``ExecutionContext.parent_id``. This registry
    is an index over the causal tree that already exists, never a second
    tree.

    How cancellation actually propagates, and why it is correct:

    1. ``cancel_subtree(run_id)`` marks that node and walks **down** to
       every registered descendant, setting each one's event. Descendants
       are found through a children index built at registration time from
       ``context.parent_id`` - a lookup accelerator for a relation that
       lives in the contexts, not an independent structure.
    2. A control that registers *after* an ancestor was already cancelled
       inherits that cancellation during ``register()``, by walking
       **up** the ``parent_id`` chain once.

    Both run under the same lock, so a node is always covered by exactly
    one of the two: either it was already registered when the cancel
    happened (case 1), or it registered afterwards (case 2). There is no
    window in between. Observing cancellation afterwards is then a
    lock-free ``Event.is_set()``, which matters because checkpoints are
    called far more often than cancellations happen.

    Nodes are evicted when released, but a released node whose children
    are still registered is retained until the last of them is gone: a
    node dropped while its subtree is alive would break the chain that
    subtree is resolved against.
    """

    def __init__(self) -> None:
        """Start an empty registry; nodes appear as controls are entered."""
        self._lock = threading.RLock()
        self._nodes: dict[str, _ControlNode] = {}
        self._children: dict[str, set[str]] = {}

    # ── registration ─────────────────────────────────────────────────────

    def register(self, control: "ExecutionControl") -> None:
        """Register a control, inheriting any ancestor's cancellation.

        Raises:
            SETTConfigurationError: If this ``run_id`` is already
                registered, or still retained because its subtree has not
                finished evicting yet. Two live controls for one execution
                node would mean two answers to "is this cancelled", which
                is exactly the ambiguity this module exists to avoid - and
                a released-but-retained node (see ``_evict_if_possible``)
                is still that same node as far as any of its still-live
                children are concerned: overwriting it here would let a
                child's later eviction walk up into a control that never
                actually parented it. M-04 (severe audit): this used to
                check only ``not existing.released``, so a run_id retained
                purely because its subtree was still alive could be
                silently reused for an unrelated registration.
        """
        context = control.context
        with self._lock:
            existing = self._nodes.get(context.run_id)
            if existing is not None:
                raise SETTConfigurationError(
                    f"An ExecutionControl is already registered for run_id "
                    f"'{context.run_id}'. Every execution node owns exactly "
                    f"one control; derive a child context for nested work."
                )
            node = _ControlNode(context, control)
            self._nodes[context.run_id] = node
            if context.parent_id is not None:
                self._children.setdefault(context.parent_id, set()).add(
                    context.run_id
                )
            inherited = self._inherited_cancellation(context)
            if inherited is not None:
                node.reason = inherited.reason
                node.cause_event_id = inherited.cause_event_id
                node.cancelled_run_id = inherited.cancelled_run_id
                node.event.set()

    def release(self, run_id: str) -> None:
        """Mark a control as finished and evict it when it is safe to.

        An unknown ``run_id`` is a no-op, the same answer ``snapshot()``
        and ``cancel_subtree()`` already give: releasing means "this node
        is done", and a node the registry never held is already done as
        far as the registry is concerned. Raising would make an
        already-evicted parent - eviction being this class's own decision,
        not the caller's - fail a cleanup path that has nothing left to
        clean.
        """
        with self._lock:
            node = self._nodes.get(run_id)
            if node is None:
                return
            node.released = True
            self._evict_if_possible(run_id)

    def _evict_if_possible(self, run_id: str) -> None:
        """Drop released nodes bottom-up, never one that still has children."""
        current: str | None = run_id
        while current is not None:
            node = self._nodes.get(current)
            if node is None:
                return
            if not node.released or self._children.get(current):
                return
            parent_id = node.context.parent_id
            del self._nodes[current]
            self._children.pop(current, None)
            if parent_id is not None:
                siblings = self._children.get(parent_id)
                if siblings is not None:
                    siblings.discard(current)
                    if not siblings:
                        del self._children[parent_id]
            current = parent_id

    # ── observation ──────────────────────────────────────────────────────

    def snapshot(self, run_id: str) -> CancellationSnapshot:
        """Return the cancellation state of one node.

        An unknown ``run_id`` reports "not cancelled": the registry has no
        information about it, and inventing a cancellation for a node it
        has never seen would be worse than admitting ignorance.
        """
        with self._lock:
            node = self._nodes.get(run_id)
            if node is None:
                return CancellationSnapshot(cancelled=False)
            return node.snapshot()

    def control_for(self, run_id: str) -> "ExecutionControl | None":
        """The live control for ``run_id``, when there is one.

        Exists so a caller holding only a ``run_id`` can cancel *through
        the control* rather than straight through this registry. That
        matters: the control is what records the cancellation request and
        carries its event id down to every descendant, so a cancellation
        issued this way stays causally explained, while one issued
        directly against the registry would stop the work just as
        effectively but leave nothing in the trace saying who asked.
        """
        with self._lock:
            node = self._nodes.get(run_id)
            if node is None or node.released:
                return None
            return node.control

    def is_registered(self, run_id: str) -> bool:
        """Whether this registry holds a live, unreleased node for ``run_id``."""
        with self._lock:
            node = self._nodes.get(run_id)
            return node is not None and not node.released

    def _inherited_cancellation(
        self, context: ExecutionContext
    ) -> CancellationSnapshot | None:
        """Walk up ``parent_id`` looking for an already-cancelled ancestor.

        Caller must hold the lock. Stops at the first ancestor that is not
        registered: a chain SETT cannot see is a chain it will not guess
        about.
        """
        seen: set[str] = {context.run_id}
        current = context.parent_id
        while current is not None and current not in seen:
            seen.add(current)
            node = self._nodes.get(current)
            if node is None:
                return None
            if node.event.is_set():
                return node.snapshot()
            current = node.context.parent_id
        return None

    # ── authority ────────────────────────────────────────────────────────

    def cancel_subtree(
        self,
        run_id: str,
        *,
        reason: str = DEFAULT_CANCELLATION_REASON,
        cause_event_id: str | None = None,
    ) -> bool:
        """Cancel one node and every registered descendant of it.

        Idempotent: cancelling an already-cancelled node returns False and
        changes nothing, including the original reason - the first cause
        of a cancellation is the true one and is never overwritten by a
        later duplicate request.

        Returns:
            True if this call cancelled a node that was not cancelled
            before; False if the node was unknown or already cancelled.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise SETTValidationError(
                "Cancellation reason must be a non-empty string."
            )
        pending: list[CancellationCallback] = []
        with self._lock:
            root = self._nodes.get(run_id)
            if root is None or root.event.is_set():
                return False
            stack = [run_id]
            visited: set[str] = set()
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                node = self._nodes.get(current)
                if node is None or node.event.is_set():
                    continue
                node.reason = reason
                node.cause_event_id = cause_event_id
                node.cancelled_run_id = run_id
                node.event.set()
                # LIFO within a node: the most recently registered
                # callback unwinds first, matching cleanup-hook ordering.
                pending.extend(reversed(node.callbacks))
                stack.extend(self._children.get(current, ()))

        for callback in pending:
            try:
                callback()
            except Exception:
                # A faulty observer must not stop the cancellation from
                # reaching the rest of the subtree, and must not replace
                # the reason the execution is stopping.
                logger.warning(
                    "[Lifecycle] A cancellation callback raised; cancellation "
                    "of run '%s' continues regardless.",
                    run_id,
                    exc_info=True,
                )
        return True

    def add_callback(self, run_id: str, callback: CancellationCallback) -> None:
        """Register a callback fired when this node becomes cancelled.

        Fires immediately, in the calling thread, when the node is already
        cancelled: an observer that arrives late still learns the truth.
        """
        if not callable(callback):
            raise SETTValidationError("Cancellation callback must be callable.")
        with self._lock:
            node = self._nodes.get(run_id)
            if node is None:
                raise SETTConfigurationError(
                    f"No ExecutionControl is registered for run_id '{run_id}'."
                )
            if not node.event.is_set():
                node.callbacks.append(callback)
                return
        callback()

    def wait(self, run_id: str, seconds: float | None) -> bool:
        """Block until this node is cancelled or ``seconds`` elapse."""
        with self._lock:
            node = self._nodes.get(run_id)
            event = node.event if node is not None else None
        if event is None:
            if seconds:
                time.sleep(seconds)
            return False
        return event.wait(timeout=seconds)

    @property
    def live_run_ids(self) -> tuple[str, ...]:
        """Run identifiers currently held by the registry.

        Intended for tests and diagnostics: a registry that keeps growing
        across runs is a leak, and this is how that gets asserted.
        """
        with self._lock:
            return tuple(self._nodes)


_DEFAULT_REGISTRY = ExecutionControlRegistry()


def default_registry() -> ExecutionControlRegistry:
    """The process-wide registry used when no other one is supplied."""
    return _DEFAULT_REGISTRY


# ── Token and source ─────────────────────────────────────────────────────────


class CancellationToken:
    """Observation-only view of one execution node's cancellation state.

    Holds no cancellation state of its own and cannot cancel anything:
    this is what travels downward into experts, adapters and handlers, so
    that being able to *see* a cancellation never implies being able to
    *cause* one. Authority stays with ``CancellationSource``.
    """

    __slots__ = ("_registry", "_run_id")

    def __init__(self, registry: ExecutionControlRegistry, run_id: str) -> None:
        """Bind a read-only view to one node of ``registry``."""
        self._registry = registry
        self._run_id = run_id

    @property
    def run_id(self) -> str:
        """The execution node this token observes."""
        return self._run_id

    @property
    def is_cancelled(self) -> bool:
        """Whether that node is cancelled right now."""
        return self._registry.snapshot(self._run_id).cancelled

    def snapshot(self) -> CancellationSnapshot:
        """An immutable read of that node's cancellation state."""
        return self._registry.snapshot(self._run_id)

    def throw_if_cancelled(self) -> None:
        """Raise ``SETTCancelledError`` when this node is cancelled."""
        snapshot = self._registry.snapshot(self._run_id)
        if snapshot.cancelled:
            raise SETTCancelledError(
                snapshot.reason or DEFAULT_CANCELLATION_REASON,
                run_id=self._run_id,
                reason=snapshot.reason or DEFAULT_CANCELLATION_REASON,
                cause_event_id=snapshot.cause_event_id,
            )

    def on_cancel(self, callback: CancellationCallback) -> None:
        """Run ``callback`` when this node becomes cancelled."""
        self._registry.add_callback(self._run_id, callback)

    def wait_or_raise(self, seconds: float) -> bool:
        """Wait up to ``seconds``, raising if cancellation arrives first.

        Exists so that no caller ever has to reach into private state to
        implement a cancellable sleep.

        Returns:
            False on a plain timeout - the wait finished and nothing was
            cancelled.

        Raises:
            SETTCancelledError: If cancellation was observed during (or
                before) the wait.
        """
        if seconds is not None and seconds < 0:
            raise SETTValidationError("Wait duration must not be negative.")
        if self._registry.wait(self._run_id, seconds):
            self.throw_if_cancelled()
            return True
        return False

    def __repr__(self) -> str:
        return (
            f"CancellationToken(run_id={self._run_id!r}, "
            f"cancelled={self.is_cancelled})"
        )


class CancellationSource:
    """The authority to cancel one execution node and its subtree.

    A thin handle over the registry, deliberately holding no state: the
    split between this and ``CancellationToken`` is about *capability*,
    not about storage. Whoever starts a run keeps the source; everything
    downstream only ever receives ``.token``.
    """

    __slots__ = ("_registry", "_run_id")

    def __init__(self, registry: ExecutionControlRegistry, run_id: str) -> None:
        """Bind the cancelling side to one node of ``registry``."""
        self._registry = registry
        self._run_id = run_id

    @property
    def run_id(self) -> str:
        """The execution node this source can cancel."""
        return self._run_id

    @property
    def token(self) -> CancellationToken:
        """An observation-only view of this source, safe to hand out."""
        return CancellationToken(self._registry, self._run_id)

    def cancel(
        self,
        *,
        reason: str = DEFAULT_CANCELLATION_REASON,
        cause_event_id: str | None = None,
    ) -> bool:
        """Cancel this node and every registered node below it."""
        return self._registry.cancel_subtree(
            self._run_id, reason=reason, cause_event_id=cause_event_id
        )

    def __repr__(self) -> str:
        return f"CancellationSource(run_id={self._run_id!r})"


# ── Durations ────────────────────────────────────────────────────────────────


def validate_duration_seconds(
    value: object,
    *,
    name: str,
    allow_zero: bool = False,
) -> float:
    """Return ``value`` as a usable number of seconds, or refuse it.

    One function because there were five copies of this rule -
    ``Deadline.after``, ``LifecyclePolicy``'s two tables,
    ``RetryPolicy``, ``ExecutionState.duration_seconds`` and the reserved
    adapter ``timeout`` kwarg - and the fifth was written by copying the
    fourth, which is how the adapter kwarg ended up the one place that
    still accepted ``NaN``. Five copies of a rule is five chances for one
    of them to be the exception nobody notices.

    NaN is the value that matters. Every comparison against it is False,
    so a NaN deadline never expires, a NaN backoff never elapses, and a
    NaN duration reports a measurement that did not happen. Infinity is
    refused for a related reason: the honest spelling of "no limit" is
    ``None``, not a number that degenerates every comparison it takes
    part in.

    Args:
        value: The candidate, already known not to be ``None``.
        name: What to call it in the error message.
        allow_zero: Whether zero is meaningful here. A timeout of zero is
            not (it would mean "already expired", which ``None`` and a
            real deadline both express better), but an elapsed duration
            or a backoff of zero is perfectly ordinary.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SETTValidationError(
            f"{name} must be a number of seconds, got "
            f"{type(value).__name__}."
        )
    if not math.isfinite(value):
        raise SETTValidationError(
            f"{name} must be a finite number of seconds, got {value!r}. "
            "A non-finite duration compares False against every bound, so "
            "it would never be reached."
        )
    if allow_zero:
        if value < 0:
            raise SETTValidationError(f"{name} must not be negative.")
    elif value <= 0:
        raise SETTValidationError(f"{name} must be greater than zero.")
    return float(value)


# ── Deadlines ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Deadline:
    """A point in time after which an execution must stop.

    Measured with ``time.monotonic()``, never with wall-clock time: a
    system clock can be adjusted backwards or forwards mid-run, and a
    timeout that can be extended or triggered by an NTP correction is not
    a timeout. Wall-clock timestamps are still recorded separately for
    reporting, where they are the right tool.
    """

    expires_at_monotonic: float | None = None

    @classmethod
    def after(cls, seconds: float | None) -> "Deadline":
        """Build a deadline ``seconds`` from now; ``None`` means unbounded."""
        if seconds is None:
            return cls()
        seconds = validate_duration_seconds(seconds, name="Timeout")
        return cls(time.monotonic() + seconds)

    @property
    def is_unbounded(self) -> bool:
        """Whether this deadline sets no limit at all."""
        return self.expires_at_monotonic is None

    @property
    def expired(self) -> bool:
        """Whether the time has already run out, on the monotonic clock."""
        return (
            self.expires_at_monotonic is not None
            and time.monotonic() >= self.expires_at_monotonic
        )

    def remaining(self) -> float | None:
        """Seconds left, never negative; ``None`` when unbounded."""
        if self.expires_at_monotonic is None:
            return None
        return max(0.0, self.expires_at_monotonic - time.monotonic())

    def child(self, timeout_seconds: float | None) -> "Deadline":
        """The tighter of this deadline and a local timeout.

        A nested unit can shorten the time it is given and never extend
        it: asking for 30 seconds inside a run with 2 left yields 2.
        """
        local = Deadline.after(timeout_seconds)
        if self.expires_at_monotonic is None:
            return local
        if local.expires_at_monotonic is None:
            return self
        return Deadline(
            min(self.expires_at_monotonic, local.expires_at_monotonic)
        )

    def throw_if_expired(self, run_id: str | None = None) -> None:
        """Raise ``SETTTimeoutError`` if the time has run out; else return."""
        if self.expired:
            raise SETTTimeoutError(
                DEADLINE_EXCEEDED_REASON, run_id=run_id, deadline=self
            )


# ── Timeout policy ───────────────────────────────────────────────────────────


def _freeze_timeouts(values: Mapping[str, float] | None) -> Mapping[str, float]:
    frozen: dict[str, float] = {}
    for key, value in dict(values or {}).items():
        if not isinstance(key, str) or not key:
            raise SETTValidationError("Timeout keys must be non-empty strings.")
        frozen[key] = validate_duration_seconds(
            value, name=f"Timeout for {key!r}"
        )
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    """Per-level timeout defaults, with per-name overrides.

    Every field defaults to ``None``, which means unbounded: a framework
    that silently imposes a timeout nobody asked for would break existing
    runs the day it is introduced. Timeouts are opt-in and compose
    downward only (see ``Deadline.child``).
    """

    run_timeout_seconds: float | None = None
    default_stage_timeout_seconds: float | None = None
    default_agent_timeout_seconds: float | None = None
    default_adapter_timeout_seconds: float | None = None
    default_handler_timeout_seconds: float | None = None

    stage_timeouts: Mapping[str, float] = field(default_factory=dict)
    agent_timeouts: Mapping[str, float] = field(default_factory=dict)
    adapter_timeouts: Mapping[str, float] = field(default_factory=dict)
    handler_timeouts: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "run_timeout_seconds",
            "default_stage_timeout_seconds",
            "default_agent_timeout_seconds",
            "default_adapter_timeout_seconds",
            "default_handler_timeout_seconds",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            object.__setattr__(
                self, name, validate_duration_seconds(value, name=name)
            )
        for name in (
            "stage_timeouts",
            "agent_timeouts",
            "adapter_timeouts",
            "handler_timeouts",
        ):
            object.__setattr__(self, name, _freeze_timeouts(getattr(self, name)))

    def stage_timeout(self, domain: str) -> float | None:
        """Seconds allowed for a pipeline stage in ``domain``, if any."""
        return self.stage_timeouts.get(domain, self.default_stage_timeout_seconds)

    def agent_timeout(self, domain: str) -> float | None:
        """Seconds allowed for an agent serving ``domain``, if any."""
        return self.agent_timeouts.get(domain, self.default_agent_timeout_seconds)

    def adapter_timeout(self, name: str) -> float | None:
        """Seconds allowed for the adapter called ``name``, if any."""
        return self.adapter_timeouts.get(
            name, self.default_adapter_timeout_seconds
        )

    def handler_timeout(self, action_type: str) -> float | None:
        """Seconds allowed for the handler of ``action_type``, if any."""
        return self.handler_timeouts.get(
            action_type, self.default_handler_timeout_seconds
        )


# ── Execution control ────────────────────────────────────────────────────────


class ControlPhase(str, Enum):
    """Where one control is in its own lifecycle.

    Deliberately not the same vocabulary as ``ExecutionStatus``: this
    describes the control object, while ``ExecutionStatus`` describes how
    the work turned out. One control that finished is ``FINISHED`` here
    whether the work succeeded, was cancelled or timed out.
    """

    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"


class ExecutionControl:
    """Whether one execution node may continue, for how long, and cleanup.

    Carries the node's ``ExecutionContext`` rather than any hierarchy of
    its own. ``run_id`` and ``parent_id`` come from there, which is what
    makes a control cancellable by the same identifier that appears in
    its trace events.
    """

    __slots__ = (
        "context",
        "deadline",
        "cleanup_hooks",
        "_registry",
        "_phase",
        "_terminal_error",
        "_cleanup_errors",
        "_started_at",
        "_finished_at",
        "_started_at_monotonic",
        "_finished_at_monotonic",
        "_trace_recorder",
        "_deadline_recorded",
    )

    def __init__(
        self,
        context: ExecutionContext,
        *,
        deadline: Deadline | None = None,
        registry: ExecutionControlRegistry | None = None,
        trace_recorder: "TraceRecorder | None" = None,
    ) -> None:
        """Build a control over ``context``; enter it to make it live."""
        if not isinstance(context, ExecutionContext):
            raise SETTValidationError(
                "ExecutionControl requires an ExecutionContext."
            )
        self.context = context
        self.deadline = deadline if deadline is not None else Deadline()
        self.cleanup_hooks: list[CleanupHook] = []
        self._registry = registry if registry is not None else default_registry()
        self._phase = ControlPhase.PENDING
        self._terminal_error: BaseException | None = None
        self._cleanup_errors: tuple[str, ...] = ()
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._started_at_monotonic: float | None = None
        self._finished_at_monotonic: float | None = None
        self._trace_recorder: "TraceRecorder | None" = trace_recorder
        self._deadline_recorded = False

    def attach_trace_recorder(self, recorder: "TraceRecorder") -> None:
        """Record this control's lifecycle events on an existing recorder.

        Entirely optional: without a recorder, cancellation and cleanup
        still work and simply go unrecorded.

        Contract: the recorder must already be recording on this control's
        own context, which is what every instrumented boundary in SETT
        does - the orchestrator derives a context, records its
        ``*.started`` event on it, and builds the control on that same
        context. Attaching a recorder to a control whose context has never
        appeared in the trace would emit an event whose parent run is
        unknown, and the recorder's own causal verification would rightly
        reject it.
        """
        self._trace_recorder = recorder

    def _record(
        self,
        kind: str,
        *,
        status: str,
        reason_codes: tuple[str, ...] = (),
        attributes: Mapping[str, object] | None = None,
        cause_id: str | None = None,
    ) -> str | None:
        """Best-effort lifecycle event; returns its id when one was written.

        Tracing never decides whether an execution may stop: a recorder
        that fails must not turn a cancellation into an error, so failures
        here are logged and swallowed. The one place in SETT where a trace
        failure does block progress is the real-effect boundary in the
        executor, and that is deliberate and separate.
        """
        recorder = self._trace_recorder
        if recorder is None:
            return None
        try:
            event = recorder.record(
                self.context,
                kind=kind,
                component_type="execution",
                component_name=self.context.run_id,
                status=status,
                cause_id=(
                    cause_id or recorder.last_event_id(self.context.run_id)
                ),
                reason_codes=reason_codes,
                attributes=dict(attributes or {}),
            )
        except Exception:
            logger.warning(
                "[Lifecycle] Could not record '%s' for run '%s'; execution "
                "control is unaffected.",
                kind,
                self.context.run_id,
                exc_info=True,
            )
            return None
        return event.event_id

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def create_root(
        cls,
        context: ExecutionContext,
        *,
        timeout_seconds: float | None = None,
        registry: ExecutionControlRegistry | None = None,
        trace_recorder: "TraceRecorder | None" = None,
    ) -> "ExecutionControl":
        """Start a control tree at ``context``."""
        return cls(
            context,
            deadline=Deadline.after(timeout_seconds),
            registry=registry,
            trace_recorder=trace_recorder,
        )

    def child(
        self,
        *,
        context: ExecutionContext | None = None,
        timeout_seconds: float | None = None,
    ) -> "ExecutionControl":
        """Derive a control for nested work.

        ``context`` should be the context the caller already derived for
        this boundary, so the control and the trace events describe the
        same node under the same ``run_id``. When omitted, one is derived
        here.
        """
        child_context = context if context is not None else self.context.derive()
        if child_context.parent_id != self.context.run_id:
            raise SETTValidationError(
                "A child ExecutionControl must be built on a context derived "
                "from its parent's context; got parent_id "
                f"'{child_context.parent_id}' under run_id "
                f"'{self.context.run_id}'."
            )
        return ExecutionControl(
            child_context,
            deadline=self.deadline.child(timeout_seconds),
            registry=self._registry,
            trace_recorder=self._trace_recorder,
        )

    # ── identity and observation ─────────────────────────────────────────

    @property
    def run_id(self) -> str:
        """This control's own execution node."""
        return self.context.run_id

    @property
    def parent_id(self) -> str | None:
        """The run this one was derived from, or ``None`` at the root."""
        return self.context.parent_id

    @property
    def trace_id(self) -> str:
        """The trace every run in this tree shares."""
        return self.context.trace_id

    @property
    def registry(self) -> ExecutionControlRegistry:
        """The registry holding this control and its relatives."""
        return self._registry

    @property
    def token(self) -> CancellationToken:
        """The observation-only view handed to nested work."""
        return CancellationToken(self._registry, self.context.run_id)

    @property
    def source(self) -> CancellationSource:
        """The authority to cancel this node's subtree."""
        return CancellationSource(self._registry, self.context.run_id)

    @property
    def phase(self) -> ControlPhase:
        """Where this control is in its own lifecycle."""
        return self._phase

    @property
    def is_cancelled(self) -> bool:
        """Whether this control has been cancelled, directly or by an ancestor."""
        return self._registry.snapshot(self.context.run_id).cancelled

    @property
    def cleanup_errors(self) -> tuple[str, ...]:
        """Descriptions of cleanup hooks that raised; never re-raised."""
        return self._cleanup_errors

    @property
    def terminal_error(self) -> BaseException | None:
        """The exception that ended this control, when one did."""
        return self._terminal_error

    @property
    def started_at(self) -> datetime | None:
        """When this control was entered, or ``None`` if it never was."""
        return self._started_at

    @property
    def finished_at(self) -> datetime | None:
        """When this control finished, or ``None`` while it still runs."""
        return self._finished_at

    @property
    def elapsed_seconds(self) -> float | None:
        """Monotonic duration, so a clock adjustment cannot distort it."""
        if self._started_at_monotonic is None:
            return None
        end = self._finished_at_monotonic
        if end is None:
            end = time.monotonic()
        return end - self._started_at_monotonic

    def cancel(
        self,
        *,
        reason: str = DEFAULT_CANCELLATION_REASON,
        cause_event_id: str | None = None,
    ) -> bool:
        """Cancel this node and everything registered below it.

        When a recorder is attached, the request itself is recorded first
        and its event id is carried into the cancellation. That is what
        makes every cancelled terminal further down causally explained:
        the error raised in a leaf points back, through
        ``cause_event_id``, at the exact event where somebody asked for
        this to stop - not merely at the fact that it stopped.
        """
        recorded_cause = cause_event_id
        if not self.is_cancelled:
            # Pre-checked so that repeated cancel() calls do not each add
            # a request event. A concurrent cancellation slipping in
            # between is benign: the recorded request genuinely happened,
            # and cancel_subtree below simply reports it changed nothing.
            recorded_cause = (
                self._record(
                    CANCEL_REQUESTED_KIND,
                    status="requested",
                    reason_codes=(reason,),
                    attributes={"target_run_id": self.context.run_id},
                    cause_id=cause_event_id,
                )
                or cause_event_id
            )
        return self._registry.cancel_subtree(
            self.context.run_id, reason=reason, cause_event_id=recorded_cause
        )

    def checkpoint(self) -> None:
        """Observe cancellation and the deadline; raise if either stops us.

        Cancellation is checked first: it is an explicit instruction from
        a caller, while an expired deadline is a condition. When both hold,
        reporting the instruction is the more accurate account of why the
        work stopped.
        """
        self.token.throw_if_cancelled()
        if self.deadline.expired and not self._deadline_recorded:
            # Once per control, not once per checkpoint: a loop that keeps
            # checking after its deadline passed would otherwise bury the
            # trace under identical events.
            self._deadline_recorded = True
            self._record(
                DEADLINE_EXCEEDED_KIND,
                status="timed_out",
                reason_codes=(DEADLINE_EXCEEDED_REASON,),
            )
        self.deadline.throw_if_expired(run_id=self.context.run_id)

    def remaining_seconds(self) -> float | None:
        """Time left for this node; ``None`` when unbounded."""
        return self.deadline.remaining()

    # ── cleanup ──────────────────────────────────────────────────────────

    def register_cleanup(self, callback: CleanupHook) -> None:
        """Push a cleanup hook onto this scope's stack."""
        if not callable(callback):
            raise SETTValidationError("Cleanup hook must be callable.")
        if self._phase is ControlPhase.FINISHED:
            raise SETTConfigurationError(
                "Cannot register a cleanup hook on a finished "
                "ExecutionControl; its cleanup has already run."
            )
        self.cleanup_hooks.append(callback)

    def run_cleanup(self) -> tuple[str, ...]:
        """Run every hook LIFO, and report failures without raising.

        A failing hook never replaces the reason the execution stopped: a
        run that timed out and then failed to delete a temp file timed
        out, and reporting it as an ``OSError`` would lose the fact that
        matters. Failures are collected by type name instead.
        """
        errors: list[str] = []
        hooks = list(self.cleanup_hooks)
        self.cleanup_hooks.clear()
        if not hooks:
            # Nothing to unwind: recording a started/completed pair for an
            # empty stack would add noise to every single scope in a run.
            return ()
        started_id = self._record(
            CLEANUP_STARTED_KIND,
            status="started",
            attributes={"hook_count": len(hooks)},
        )
        for callback in reversed(hooks):
            try:
                callback()
            except Exception as error:
                errors.append(type(error).__name__)
                logger.warning(
                    "[Lifecycle] Cleanup hook failed for run '%s': %s",
                    self.context.run_id,
                    type(error).__name__,
                    exc_info=True,
                )
        self._cleanup_errors = self._cleanup_errors + tuple(errors)
        self._record(
            CLEANUP_FAILED_KIND if errors else CLEANUP_COMPLETED_KIND,
            status="failed" if errors else "completed",
            reason_codes=tuple(f"cleanup.{name}" for name in errors),
            attributes={"hook_count": len(hooks), "error_count": len(errors)},
            cause_id=started_id,
        )
        return tuple(errors)

    # ── context manager ──────────────────────────────────────────────────

    def __enter__(self) -> "ExecutionControl":
        """Register, then verify this node may start at all.

        Python does not call ``__exit__()`` when ``__enter__()`` raises, so
        anything that must happen on the way out is done here explicitly
        before the exception leaves this frame. Without that, a control
        whose entry checkpoint failed would stay registered forever and
        report a non-terminal phase for work that never ran.
        """
        if self._phase is not ControlPhase.PENDING:
            raise SETTConfigurationError(
                "An ExecutionControl cannot be entered twice; derive a child "
                "control for nested work."
            )
        self._phase = ControlPhase.RUNNING
        self._started_at = datetime.now(timezone.utc)
        self._started_at_monotonic = time.monotonic()
        try:
            self._registry.register(self)
            self.checkpoint()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._finish(exc_value)
        return False

    def _finish(self, error: BaseException | None) -> None:
        """Settle terminal state, run cleanup and leave the registry once."""
        if self._phase is ControlPhase.FINISHED:
            return
        self._phase = ControlPhase.FINISHED
        self._terminal_error = error
        self._finished_at = datetime.now(timezone.utc)
        self._finished_at_monotonic = time.monotonic()
        try:
            self.run_cleanup()
        finally:
            self._registry.release(self.context.run_id)

    def __repr__(self) -> str:
        return (
            f"ExecutionControl(run_id={self.context.run_id!r}, "
            f"phase={self._phase.value!r}, "
            f"cancelled={self.is_cancelled})"
        )


# ── Ambient propagation ──────────────────────────────────────────────────────

_ACTIVE_CONTROL: ContextVar[ExecutionControl | None] = ContextVar(
    "sett_execution_control", default=None
)


def current_execution_control() -> ExecutionControl | None:
    """The control active in this thread/task, if any.

    Returns ``None`` when nobody established one, which is what keeps
    every checkpoint in the framework a no-op for callers that never
    asked for lifecycle management.
    """
    return _ACTIVE_CONTROL.get()


@contextmanager
def lifecycle_scope(control: ExecutionControl) -> Iterator[ExecutionControl]:
    """Bind a control as the active one for the duration of one operation.

    Mirrors ``execution_scope()`` from ``execution_context.py`` on purpose:
    the two are used side by side at every instrumented boundary, one
    carrying identity and the other carrying permission to continue.
    """
    token = _ACTIVE_CONTROL.set(control)
    try:
        yield control
    finally:
        _ACTIVE_CONTROL.reset(token)


def checkpoint() -> None:
    """Observe the ambient control, if there is one.

    The convenience form of ``current_execution_control().checkpoint()``
    for application code inside long loops.
    """
    control = current_execution_control()
    if control is not None:
        control.checkpoint()


def derive_control_for(
    context: ExecutionContext, *, timeout_seconds: float | None = None
) -> ExecutionControl | None:
    """A child control for ``context``, when the ambient control is its parent.

    Used by the instrumented boundaries to give each nested node its own
    cancellable control under the same ``run_id`` the trace already uses.

    Args:
        timeout_seconds: A level-specific timeout to apply on top of the
            parent's remaining deadline (see ``Deadline.child``), typically
            resolved from a ``LifecyclePolicy`` by the caller. Omitting it
            means this node only inherits whatever time its parent has
            left, exactly as before this parameter existed.

    Returns ``None`` in the two cases where deriving one would be wrong
    rather than merely unnecessary: when no control is active at all (the
    ordinary path for callers who never asked for lifecycle management),
    and when the ambient control does not describe ``context``'s parent -
    which happens if an application drives a component outside the
    boundary pairing. Rather than raise there, the boundary falls back to
    checking the ambient control directly: cancellation still works, only
    the extra per-node handle is skipped.
    """
    control = current_execution_control()
    if control is None:
        return None
    if context.parent_id != control.context.run_id:
        return None
    return control.child(context=context, timeout_seconds=timeout_seconds)


def remaining_seconds() -> float | None:
    """Time left on the ambient control; ``None`` when unbounded or absent."""
    control = current_execution_control()
    if control is None:
        return None
    return control.remaining_seconds()


_ACTIVE_LIFECYCLE_POLICY: ContextVar["LifecyclePolicy | None"] = ContextVar(
    "sett_lifecycle_policy", default=None
)


def current_lifecycle_policy() -> "LifecyclePolicy | None":
    """The ``LifecyclePolicy`` active in this thread/task, if any.

    ``None`` when nobody established one, which keeps every policy lookup
    a no-op for callers outside an orchestrator's controlled entry points -
    the same default-absent behaviour every other ambient value in this
    module has.
    """
    return _ACTIVE_LIFECYCLE_POLICY.get()


@contextmanager
def lifecycle_policy_scope(
    policy: "LifecyclePolicy | None",
) -> Iterator["LifecyclePolicy | None"]:
    """Bind ``policy`` as the active one for the duration of one run.

    Set once at a controlled entry point's root, alongside
    ``lifecycle_scope()``: the policy does not change per nested node the
    way the control does, so nested boundaries do not need to re-bind it.
    This is what lets an Expert, which holds no reference to the
    orchestrator, still resolve a per-adapter timeout by name through
    ``lifecycle_kwargs(adapter_name=...)``.
    """
    token = _ACTIVE_LIFECYCLE_POLICY.set(policy)
    try:
        yield policy
    finally:
        _ACTIVE_LIFECYCLE_POLICY.reset(token)


RESERVED_TIMEOUT_KWARG = "timeout"
RESERVED_TOKEN_KWARG = "cancellation_token"


@dataclass(frozen=True, slots=True)
class AdapterLifecycle:
    """The time and the cancellation an adapter call was given.

    External service adapters sit outside the Agent/Expert tree and are
    never registered with the orchestrator, so an ambient control cannot
    be handed to them the way it is to a component. What SETT standardizes
    instead is two reserved keyword arguments - ``timeout`` and
    ``cancellation_token`` - which every adapter interface already accepts
    through ``**kwargs``, and which this type unpacks in one place so no
    adapter has to invent its own spelling of them.
    """

    timeout: float | None = None
    token: CancellationToken | None = None

    @property
    def is_cancelled(self) -> bool:
        """Whether the caller handed down a token that is now cancelled."""
        return self.token is not None and self.token.is_cancelled

    def throw_if_cancelled(self) -> None:
        """Raise if cancellation was asked for; a no-op with no token."""
        if self.token is not None:
            self.token.throw_if_cancelled()

    def effective_timeout(self, adapter_default: float | None) -> float | None:
        """The shorter of what the run has left and the adapter's own default.

        An adapter never gets to wait longer than the run it is part of:
        a client configured for 30 seconds inside a turn with 2 left has
        2. It may, of course, be stricter than the run on its own account.
        """
        candidates = [
            value
            for value in (self.timeout, adapter_default)
            if value is not None
        ]
        if not candidates:
            return None
        return max(0.0, min(candidates))


def lifecycle_kwargs(*, adapter_name: str | None = None) -> dict[str, object]:
    """The reserved kwargs to forward from an Expert into an adapter call.

    Usage inside an expert::

        text = self._llm.complete(prompt, **lifecycle_kwargs())

    Returns an empty dict when no control is active, so the same call
    works identically for a caller who never asked for lifecycle
    management.

    Args:
        adapter_name: When given, and an orchestrator-wide
            ``LifecyclePolicy`` is active (``lifecycle_policy_scope()``,
            set automatically by ``process_controlled()`` and
            ``run_pipeline_controlled()``), the timeout returned is
            further tightened to ``LifecyclePolicy.adapter_timeout(name)``
            when that is stricter than the time the run has left. An
            Expert calling an adapter it wants policy-aware timeouts for
            names itself here - adapters sit outside the Agent/Expert
            tree the orchestrator instruments directly, so this is the
            one place that boundary is opted into rather than automatic.
    """
    control = current_execution_control()
    if control is None:
        return {}
    timeout = control.remaining_seconds()
    if adapter_name is not None:
        policy = current_lifecycle_policy()
        if policy is not None:
            policy_timeout = policy.adapter_timeout(adapter_name)
            if policy_timeout is not None:
                timeout = (
                    policy_timeout
                    if timeout is None
                    else min(timeout, policy_timeout)
                )
    return {
        RESERVED_TIMEOUT_KWARG: timeout,
        RESERVED_TOKEN_KWARG: control.token,
    }


def pop_lifecycle_kwargs(kwargs: dict[str, Any]) -> AdapterLifecycle:
    """Remove the reserved kwargs from an adapter's ``**kwargs``.

    Removed rather than read, so the remaining kwargs can be forwarded to
    a provider SDK that would reject unknown arguments.

    Falls back to the ambient control when the caller passed neither. That
    matters more than it looks: it means an official adapter honours a
    cancellation even when the expert calling it was written before any of
    this existed and forwards nothing - the same "existing code does not
    need to know" property the rest of v0.12.0 relies on.
    """
    timeout = kwargs.pop(RESERVED_TIMEOUT_KWARG, None)
    token = kwargs.pop(RESERVED_TOKEN_KWARG, None)
    if timeout is None and token is None:
        control = current_execution_control()
        if control is not None:
            return AdapterLifecycle(
                timeout=control.remaining_seconds(), token=control.token
            )
    if timeout is not None:
        timeout = validate_duration_seconds(
            timeout,
            name=f"The reserved '{RESERVED_TIMEOUT_KWARG}' adapter argument",
            # Zero is meaningful here and elsewhere it is not: a run whose
            # deadline has already passed hands its adapters exactly 0.0,
            # and refusing that would turn "no time left" into a crash.
            allow_zero=True,
        )
    if token is not None and not isinstance(token, CancellationToken):
        raise SETTValidationError(
            f"The reserved '{RESERVED_TOKEN_KWARG}' adapter argument must be "
            "a CancellationToken or None."
        )
    return AdapterLifecycle(timeout=timeout, token=token)


def stop_reason_code(error: BaseException) -> str:
    """The trace reason code for an execution that was stopped.

    Cancellation carries the reason somebody gave when they asked for it,
    so that a barge-in and a session teardown are distinguishable in the
    trace rather than both reading "cancelled".
    """
    if isinstance(error, SETTCancelledError):
        return error.reason or DEFAULT_CANCELLATION_REASON
    return DEADLINE_EXCEEDED_REASON


def stop_cause_id(
    recorder: "TraceRecorder",
    trace_id: str,
    fallback_event_id: str | None,
    error: BaseException,
) -> str | None:
    """Link a stopped terminal back to the request that stopped it.

    Uses the cancellation request's own event when this recorder actually
    holds it in this trace, which is what makes a cancelled leaf point at
    the moment somebody asked rather than merely at its own start. Falls
    back otherwise: a ``cause_id`` naming an event the recorder never saw,
    or one from another trace, is exactly what ``verify()`` rejects, so an
    unverifiable link is worse than a local one.
    """
    cause_event_id = getattr(error, "cause_event_id", None)
    if cause_event_id and recorder.knows_event(cause_event_id, trace_id=trace_id):
        return cause_event_id
    return fallback_event_id


def cooperative_sleep(seconds: float, token: CancellationToken) -> None:
    """Sleep that ends early, and raises, when cancellation arrives.

    Used for retry backoff: a backoff that ignores cancellation turns a
    cancelled run into one that keeps a caller waiting for a result it
    already said it did not want.
    """
    token.wait_or_raise(seconds)
