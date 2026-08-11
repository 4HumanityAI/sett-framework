"""Structured, tamper-evident, privacy-preserving execution traces."""
from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from sett.core_ruler.execution_context import ExecutionContext, is_sensitive_key
from sett.exceptions import SETTConfigurationError, SETTValidationError

_GENESIS_HASH = "0" * 64
TraceExporter = Callable[[dict[str, Any]], None]

# Which terminal kinds may close each instrumented start kind.
#
# Hoisted to module level in v0.12.0 for one specific reason: the set of
# terminals that may close "trace.started" used to be written twice - once
# here and once inline in the single-terminal check further down - and two
# copies of a list that must agree is a list that eventually will not. Both
# readers now derive from this one mapping, so adding a terminal cannot be
# done for one check and forgotten for the other.
#
# v0.12.0 adds the cancelled/timed-out terminals at every level, the
# partial terminal for pipelines, and the per-attempt pair used by the
# controlled executor. Cancellation and deadline exhaustion get distinct
# kinds everywhere rather than sharing one: a trace that cannot tell "the
# user stopped this" from "this ran out of time" cannot answer the only
# question anybody asks it afterwards.
_TERMINAL_BY_START: dict[str, frozenset[str]] = {
    "trace.started": frozenset(
        {
            "trace.completed",
            "trace.failed",
            "trace.cancelled",
            "trace.timed_out",
        }
    ),
    "pipeline.started": frozenset(
        {
            "pipeline.completed",
            "pipeline.rejected",
            "pipeline.failed",
            "pipeline.cancelled",
            "pipeline.timed_out",
            "pipeline.partial",
        }
    ),
    "pipeline.stage_started": frozenset(
        {
            "pipeline.stage_completed",
            "pipeline.stage_rejected",
            "pipeline.stage_skipped",
            "pipeline.stage_failed",
            "pipeline.stage_cancelled",
            "pipeline.stage_timed_out",
        }
    ),
    "broadcast.started": frozenset(
        {
            "broadcast.completed",
            "broadcast.failed",
            "broadcast.cancelled",
            "broadcast.timed_out",
        }
    ),
    "route.selected": frozenset(
        {
            "route.completed",
            "route.failed",
            "route.cancelled",
            "route.timed_out",
        }
    ),
    "agent.started": frozenset(
        {
            "agent.completed",
            "agent.failed",
            "agent.cancelled",
            "agent.timed_out",
        }
    ),
    "expert.started": frozenset(
        {
            "expert.completed",
            "expert.failed",
            "expert.cancelled",
            "expert.timed_out",
        }
    ),
    "handler.authorized": frozenset({"handler.started", "handler.blocked"}),
    "handler.started": frozenset(
        {
            "handler.completed",
            "handler.failed",
            "handler.cancelled",
            "handler.timed_out",
        }
    ),
    "handler.attempt_started": frozenset(
        {"handler.attempt_completed", "handler.attempt_failed"}
    ),
}

TRACE_TERMINAL_KINDS: frozenset[str] = _TERMINAL_BY_START["trace.started"]
"""Kinds that close a whole trace. Derived, never written out twice."""

# Lifecycle events that are not start/terminal pairs: they annotate a run
# rather than opening or closing a boundary, so they are listed here for
# discoverability and to keep the vocabulary in one place instead of being
# spelled out as string literals at each emitting site.
CANCEL_REQUESTED_KIND = "execution.cancel_requested"
DEADLINE_EXCEEDED_KIND = "execution.deadline_exceeded"
CLEANUP_STARTED_KIND = "execution.cleanup_started"
CLEANUP_COMPLETED_KIND = "execution.cleanup_completed"
CLEANUP_FAILED_KIND = "execution.cleanup_failed"
_MAX_ATTRIBUTE_KEYS = 32
_MAX_ATTRIBUTE_DEPTH = 8
_MAX_ATTRIBUTE_BYTES = 16 * 1024
# F17 fix (defensive audit #2): this used to be a second, independently
# maintained copy of execution_context.py's own sensitive-key list, and
# the two had already diverged (this one had "medical"/"health_record"/
# "biometric", the other did not). Sensitivity now has exactly one
# definition, imported from there.


def _freeze_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise SETTValidationError(
            f"Trace attributes exceed maximum depth {_MAX_ATTRIBUTE_DEPTH}."
        )
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SETTValidationError(
                "Trace attribute numbers must be finite JSON values."
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise SETTValidationError(
                    "Trace attribute keys must be non-empty strings."
                )
            if is_sensitive_key(key):
                raise SETTValidationError(
                    f"Trace attribute key {key!r} is sensitive and cannot "
                    "be recorded."
                )
            result[key] = _freeze_value(item, depth + 1)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item, depth + 1) for item in value)
    raise SETTValidationError(
        f"Trace attributes must be JSON-safe; got {type(value).__name__}."
    )


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _freeze_attributes(
    attributes: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    raw = dict(attributes or {})
    if len(raw) > _MAX_ATTRIBUTE_KEYS:
        raise SETTValidationError(
            f"Trace attributes accept at most {_MAX_ATTRIBUTE_KEYS} keys."
        )
    frozen = _freeze_value(raw)
    assert isinstance(frozen, Mapping)
    encoded = json.dumps(
        _thaw_value(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_ATTRIBUTE_BYTES:
        raise SETTValidationError(
            f"Trace attributes exceed {_MAX_ATTRIBUTE_BYTES} bytes."
        )
    return frozen


def _canonical_bytes(entry: Mapping[str, Any]) -> bytes:
    payload = {
        key: _thaw_value(value)
        for key, value in entry.items()
        if key != "entry_hash"
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One immutable operation in a causal execution trace."""

    event_id: str
    trace_id: str
    run_id: str
    parent_id: str | None
    cause_id: str | None
    timestamp: datetime
    sequence: int
    kind: str
    component_type: str
    component_name: str
    status: str
    reason_codes: tuple[str, ...]
    attributes: Mapping[str, Any]
    previous_hash: str
    entry_hash: str

    def safe_view(self) -> dict[str, Any]:
        """This event as a dict, with attribute values reduced to shape."""
        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "cause_id": self.cause_id,
            "timestamp": self.timestamp.isoformat(),
            "sequence": self.sequence,
            "kind": self.kind,
            "component_type": self.component_type,
            "component_name": self.component_name,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "attributes": _thaw_value(self.attributes),
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }


class TraceRecorder:
    """Thread-safe in-memory recorder with hash and causal verification."""

    def __init__(self) -> None:
        """Start an empty recorder with no exporters attached."""
        self._events: list[TraceEvent] = []
        self._exporters: list[TraceExporter] = []
        self._lock = threading.RLock()

    def register_exporter(self, exporter: TraceExporter) -> None:
        """Add a sink called once per recorded event, best effort."""
        if not callable(exporter):
            raise SETTValidationError("Trace exporter must be callable.")
        with self._lock:
            self._exporters.append(exporter)

    def record(
        self,
        context: ExecutionContext,
        *,
        kind: str,
        component_type: str,
        component_name: str,
        status: str,
        cause_id: str | None = None,
        reason_codes: tuple[str, ...] | list[str] = (),
        attributes: Mapping[str, Any] | None = None,
        effect_boundary: bool = False,
    ) -> TraceEvent:
        """Commit an event before notifying best-effort exporters."""
        safe_attributes = _freeze_attributes(attributes)
        safe_reasons = tuple(str(code) for code in reason_codes)
        with self._lock:
            timestamp = datetime.now(timezone.utc)
            base = {
                "event_id": str(uuid4()),
                "trace_id": context.trace_id,
                "run_id": context.run_id,
                "parent_id": context.parent_id,
                "cause_id": cause_id,
                "timestamp": timestamp.isoformat(),
                "sequence": len(self._events) + 1,
                "kind": kind,
                "component_type": component_type,
                "component_name": component_name,
                "status": status,
                "reason_codes": list(safe_reasons),
                "attributes": _thaw_value(safe_attributes),
                "previous_hash": (
                    self._events[-1].entry_hash if self._events else _GENESIS_HASH
                ),
            }
            base["entry_hash"] = sha256(_canonical_bytes(base)).hexdigest()
            event = TraceEvent(
                event_id=base["event_id"],
                trace_id=base["trace_id"],
                run_id=base["run_id"],
                parent_id=base["parent_id"],
                cause_id=base["cause_id"],
                timestamp=timestamp,
                sequence=base["sequence"],
                kind=base["kind"],
                component_type=base["component_type"],
                component_name=base["component_name"],
                status=base["status"],
                reason_codes=safe_reasons,
                attributes=safe_attributes,
                previous_hash=base["previous_hash"],
                entry_hash=base["entry_hash"],
            )
            self._events.append(event)
            exporters = tuple(self._exporters)

        for exporter in exporters:
            try:
                exporter(event.safe_view())
            except Exception as error:
                if effect_boundary:
                    boundary_error = SETTConfigurationError(
                        "A trace exporter failed at a real-world effect boundary; "
                        "the effect was not executed."
                    )
                    boundary_error.trace_event_id = event.event_id
                    raise boundary_error from error
        return event

    def get_trace(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """Every event recorded under one trace, in recorded order."""
        with self._lock:
            return tuple(event for event in self._events if event.trace_id == trace_id)

    def knows_event(self, event_id: str, *, trace_id: str | None = None) -> bool:
        """Whether this recorder holds ``event_id``, optionally in one trace.

        Exists so a caller can safely use an event id that reached it from
        outside - a cancellation request carried on an exception, for
        instance - as a ``cause_id``. Referencing an event this recorder
        never saw, or one belonging to a different trace, is precisely what
        ``verify()`` rejects, so the honest move is to check first and fall
        back to a local cause rather than to record an unverifiable link.
        """
        if not event_id:
            return False
        with self._lock:
            for event in reversed(self._events):
                if event.event_id != event_id:
                    continue
                return trace_id is None or event.trace_id == trace_id
        return False

    def last_event_id(self, run_id: str) -> str | None:
        """Return the latest event for one execution node."""
        with self._lock:
            for event in reversed(self._events):
                if event.run_id == run_id:
                    return event.event_id
        return None

    def export_trace(
        self,
        trace_id: str,
        *,
        view: str = "sanitized",
    ) -> tuple[dict[str, Any], ...]:
        """One trace as dicts, in the 'summary' or 'sanitized' view."""
        if view not in {"summary", "sanitized"}:
            raise SETTValidationError("Trace view must be 'summary' or 'sanitized'.")
        events = self.get_trace(trace_id)
        if view == "sanitized":
            return tuple(event.safe_view() for event in events)
        return tuple(
            {
                "event_id": event.event_id,
                "trace_id": event.trace_id,
                "run_id": event.run_id,
                "parent_id": event.parent_id,
                "cause_id": event.cause_id,
                "timestamp": event.timestamp.isoformat(),
                "sequence": event.sequence,
                "kind": event.kind,
                "component_type": event.component_type,
                "component_name": event.component_name,
                "status": event.status,
                "reason_codes": list(event.reason_codes),
            }
            for event in events
        )

    def verify(self, trace_id: str | None = None) -> bool:
        """
        Verify this recorder's hash chain and structural integrity.

        The chain is global, over every event this recorder has ever
        recorded - there is no separate chain per trace_id (F28,
        defensive audit #2). Passing ``trace_id`` only adds a
        precondition, "at least one event with this trace_id exists";
        it does not scope verification to that trace alone. Corruption
        in any event, in any trace, makes ``verify(trace_id=...)``
        return False for every trace_id, not only the corrupted one.

        Args:
            trace_id: If given, additionally require that at least one
                recorded event carries this trace_id. Does not narrow
                what gets verified.

        Returns:
            True if the whole recorder's history is internally
            consistent (and, when trace_id is given, that trace_id was
            seen at all). False otherwise.
        """
        with self._lock:
            events = tuple(self._events)
        if trace_id is not None and not any(
            event.trace_id == trace_id for event in events
        ):
            return False

        expected_previous = _GENESIS_HASH
        known_events: dict[str, TraceEvent] = {}
        seen_runs: dict[str, set[str]] = {}
        root_runs: dict[str, set[str]] = {}
        run_owners: dict[str, str] = {}
        run_parents: dict[tuple[str, str], str | None] = {}
        for index, event in enumerate(events, start=1):
            view = event.safe_view()
            if event.sequence != index or event.previous_hash != expected_previous:
                return False
            if sha256(_canonical_bytes(view)).hexdigest() != event.entry_hash:
                return False
            if event.event_id in known_events:
                return False
            if event.cause_id is not None:
                cause = known_events.get(event.cause_id)
                if cause is None or cause.trace_id != event.trace_id:
                    return False

            owner = run_owners.setdefault(event.run_id, event.trace_id)
            if owner != event.trace_id:
                return False
            run_key = (event.trace_id, event.run_id)
            if run_key in run_parents:
                if run_parents[run_key] != event.parent_id:
                    return False
            else:
                run_parents[run_key] = event.parent_id

            trace_runs = seen_runs.setdefault(event.trace_id, set())
            roots = root_runs.setdefault(event.trace_id, set())
            if event.parent_id is None:
                roots.add(event.run_id)
                if len(roots) > 1:
                    return False
            elif (
                event.parent_id == event.run_id
                or event.parent_id not in trace_runs
            ):
                return False

            trace_runs.add(event.run_id)
            known_events[event.event_id] = event
            expected_previous = event.entry_hash

        traces = {event.trace_id for event in events}
        for current_trace_id in traces:
            trace_events = [
                event for event in events if event.trace_id == current_trace_id
            ]
            if not self._verify_terminals(trace_events):
                return False
        return True

    @staticmethod
    def _verify_terminals(events: list[TraceEvent]) -> bool:
        """Validate start/terminal pairs for instrumented execution kinds."""
        terminal_by_start = _TERMINAL_BY_START
        events_by_run: dict[str, list[TraceEvent]] = {}
        for event in events:
            events_by_run.setdefault(event.run_id, []).append(event)

        for run_events in events_by_run.values():
            for start_kind, terminal_kinds in terminal_by_start.items():
                open_boundaries = 0
                for event in run_events:
                    if event.kind == start_kind:
                        open_boundaries += 1
                    elif event.kind in terminal_kinds:
                        if open_boundaries == 0:
                            return False
                        open_boundaries -= 1
                if open_boundaries:
                    return False

        trace_starts = [event for event in events if event.kind == "trace.started"]
        if trace_starts:
            if len(trace_starts) != 1:
                return False
            trace_terminals = [
                event for event in events if event.kind in TRACE_TERMINAL_KINDS
            ]
            if len(trace_terminals) != 1:
                return False
            if events[-1].event_id != trace_terminals[0].event_id:
                return False
        return True

    @property
    def event_count(self) -> int:
        """How many events this recorder currently holds."""
        with self._lock:
            return len(self._events)
