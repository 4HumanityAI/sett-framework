"""
SETT Framework: PrivateMemory
==============================
Each agent has its own private memory.
By design, only the agent's own experts are given a reference to it
(via attach_memory(), during registration) and use it to write.
The orchestrator is never handed one.

This is a design and API-surface convention, not a runtime
access-control boundary: PrivateMemory has
no capability/ACL enforcement, and any code that does obtain a
reference to an instance, e.g. by holding onto the agent object,
can call its methods directly. Nothing in this module can stop that;
what SETT provides is that the normal, documented API never gives the
orchestrator or another agent that reference in the first place. See
docs/SETT_Conventions.md for the fuller rationale.

The word "private" describes this position in the architecture. It is not
a storage property. When a memory_id is configured, mutations are
persisted as strict JSON into the same backend as every other durable
record, with no separate file, no separate permissions, and no encryption.
Protecting those
bytes on disk is the deploying application's responsibility; see
docs/security_model.md.

This is one of the two core memory layers of SETT.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sett.audit_ruler.integrity import IntegrityPolicy
from sett.audit_ruler.trace import TraceRecorder
from sett.core_ruler.execution_context import (
    ExecutionContext,
    current_execution_context,
    current_trace_cause_id,
)
from sett.exceptions import SETTConfigurationError, SETTValidationError
from sett.persistence.contracts import (
    PersistenceBackend,
    validate_backend_declarations,
)
from sett.persistence.formats import PrivateMemoryOperation
from sett.persistence.identity import PRIVATE_MEMORY_WRITE_ID_VERSION
from sett.persistence.policy import PersistencePolicy
from sett.persistence.private_memory import (
    PrivateMemoryMutationDescriptor,
    persist_private_memory_mutation,
    restore_private_memory_state,
)

_MISSING = object()


class PrivateMemory:
    """
    Private memory exclusive to a single agent.

    Key rules:
    - Only the experts that belong to the owner agent are given a
      reference to this instance, and use it to write.
    - The orchestrator is never given a reference to this memory.
    - Other agents are never given a reference to this memory.
    - Used for intermediate reasoning, working data, and internal state.
    - The agent uses this memory to compose its final result before
      publishing to UniversalMemory.

    These rules describe how the documented API is shaped, not a
    runtime capability check: there is no ACL here, so code that
    obtains a reference by some other means is not stopped by this
    class.
    """

    def __init__(self, owner: str, *, memory_id: str | None = None) -> None:
        """
        Args:
            owner: The name of the agent that owns this memory.
                   Used for logging and access control context.
            memory_id: Optional stable, opaque identity. Supplying it lets an
                owning agent opt in when a persistence backend is attached.
        """
        if memory_id is not None and (
            not isinstance(memory_id, str)
            or not memory_id
            or len(memory_id.encode("utf-8", "strict")) > 256
        ):
            raise SETTValidationError(
                "memory_id must be a non-empty opaque identifier of at most "
                "256 UTF-8 bytes, or None."
            )
        self._owner = owner
        self._memory_id = memory_id
        self._store: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []
        self._persistence_backend: PersistenceBackend | None = None
        self._persistence_policy: PersistencePolicy | None = None
        self._trace_recorder: TraceRecorder | None = None

    @classmethod
    def restore(
        cls,
        *,
        owner: str,
        memory_id: str,
        report,
        backend: PersistenceBackend,
        policy: PersistencePolicy,
        historical_policies: Mapping[str, IntegrityPolicy] | None = None,
    ) -> PrivateMemory:
        """Restore confirmed mutations and resume persistence at the stream head."""
        policies = dict(historical_policies or {})
        policies.setdefault(policy.integrity_policy.policy_id, policy.integrity_policy)
        restored = restore_private_memory_state(
            report,
            memory_id=memory_id,
            store=backend.state,
            namespace_id=backend.namespace_id,
            policies=policies,
        )
        recorder = TraceRecorder.resume(
            report,
            evidence_stream=backend.evidence,
            namespace_id=backend.namespace_id,
            evidence_durability=policy.evidence_durability,
        )
        memory = cls(owner=owner, memory_id=memory_id)
        memory.configure_persistence(
            backend=backend,
            policy=policy,
            trace_recorder=recorder,
        )
        memory._store = deepcopy(restored.values)
        memory._history = deepcopy(list(restored.history))
        return memory

    def configure_persistence(
        self,
        *,
        backend: PersistenceBackend,
        policy: PersistencePolicy,
        trace_recorder: TraceRecorder,
    ) -> None:
        """Attach opt-in persistence before the memory has been mutated."""
        if self._memory_id is None:
            raise SETTConfigurationError(
                "Persistent PrivateMemory requires an explicit memory_id."
            )
        if self._persistence_backend is not None:
            if (
                self._persistence_backend is backend
                and self._persistence_policy is policy
                and self._trace_recorder is trace_recorder
            ):
                return
            raise SETTConfigurationError(
                "PrivateMemory persistence is already configured."
            )
        if self._history or self._store:
            raise SETTConfigurationError(
                "PrivateMemory persistence must be configured before its "
                "first mutation."
            )
        if not isinstance(backend, PersistenceBackend):
            raise SETTValidationError("backend must be a PersistenceBackend.")
        if not isinstance(policy, PersistencePolicy):
            raise SETTValidationError("policy must be a PersistencePolicy.")
        if not isinstance(trace_recorder, TraceRecorder):
            raise SETTValidationError("trace_recorder must be a TraceRecorder.")
        validate_backend_declarations(
            backend,
            snapshot_durability=policy.snapshot_durability,
            evidence_durability=policy.evidence_durability,
        )
        self._persistence_backend = backend
        self._persistence_policy = policy
        self._trace_recorder = trace_recorder

    def _mutation_context(
        self, explicit: ExecutionContext | None
    ) -> ExecutionContext:
        if explicit is not None and not isinstance(explicit, ExecutionContext):
            raise SETTValidationError("context must be an ExecutionContext.")
        ambient = current_execution_context()
        if explicit is not None and ambient is not None and explicit != ambient:
            raise SETTConfigurationError(
                "Explicit and ambient execution contexts disagree."
            )
        context = explicit or ambient
        if context is None:
            raise SETTConfigurationError(
                "Persistent PrivateMemory mutations require an ambient "
                "ExecutionContext or an explicit context argument."
            )
        return context

    def _persist_mutation(
        self,
        *,
        operation: PrivateMemoryOperation,
        key: str | None,
        value: Any,
        context: ExecutionContext,
    ) -> tuple[str, PrivateMemoryMutationDescriptor]:
        backend = self._persistence_backend
        policy = self._persistence_policy
        recorder = self._trace_recorder
        memory_id = self._memory_id
        assert backend is not None
        assert policy is not None
        assert recorder is not None
        assert memory_id is not None
        started = recorder.record(
            context,
            kind="private_memory.mutation_started",
            component_type="private_memory",
            component_name="private_memory",
            status="started",
            cause_id=current_trace_cause_id(),
            reason_codes=("private_memory.mutation_started",),
            attributes={
                "memory_id": memory_id,
                "operation": operation.name.lower(),
                "write_id_version": PRIVATE_MEMORY_WRITE_ID_VERSION,
            },
        )
        descriptor = persist_private_memory_mutation(
            operation=operation,
            key=key,
            value=value,
            store=backend.state,
            namespace_id=backend.namespace_id,
            memory_id=memory_id,
            trace_id=context.trace_id,
            run_id=context.run_id,
            mutation_event_id=started.event_id,
            integrity_policy=policy.integrity_policy,
            required_durability=policy.snapshot_durability,
        )
        return started.event_id, descriptor

    def _confirm_mutation(
        self,
        *,
        context: ExecutionContext,
        started_event_id: str,
        descriptor: PrivateMemoryMutationDescriptor,
    ) -> None:
        recorder = self._trace_recorder
        assert recorder is not None
        recorder.record(
            context,
            kind="private_memory.mutation_persisted",
            component_type="private_memory",
            component_name="private_memory",
            status="persisted",
            cause_id=started_event_id,
            reason_codes=("private_memory.mutation_persisted",),
            attributes=descriptor.trace_attributes(),
        )

    def write(
        self,
        key: str,
        value: Any,
        *,
        context: ExecutionContext | None = None,
    ) -> None:
        """
        Write a value to private memory.
        Should only be called from within an expert that belongs to this agent.

        Args:
            key: The key to store the value under.
            value: The value to store. Can be any Python object.

        A defensive deep copy is stored, not the caller's own reference:
        without it, a caller that mutates a list or dict after write()
        returns would silently change what is stored under `key`, with no
        second write() call and nothing in get_history() to show it
        happened, the same class of gap read()/get_all() were already
        closed against.
        """
        stored_value = deepcopy(value)
        if self._persistence_backend is not None:
            mutation_context = self._mutation_context(context)
            started_event_id, descriptor = self._persist_mutation(
                operation=PrivateMemoryOperation.WRITE,
                key=key,
                value=stored_value,
                context=mutation_context,
            )
            previous = self._store.get(key, _MISSING)
            history_length = len(self._history)
            self._store[key] = stored_value
            self._history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "write",
                "key": key,
            })
            try:
                self._confirm_mutation(
                    context=mutation_context,
                    started_event_id=started_event_id,
                    descriptor=descriptor,
                )
            except Exception:
                if previous is _MISSING:
                    self._store.pop(key, None)
                else:
                    self._store[key] = previous
                del self._history[history_length:]
                raise
            return
        self._store[key] = stored_value
        self._history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "write",
            "key": key,
        })

    def read(self, key: str, default: Any = None) -> Any:
        """
        Read a defensive deep copy of a value from private memory.

        Mutating the returned value never affects internal state: the
        only way to change what is stored under `key` is a subsequent
        call to write(), which is what get_history() tracks. Before
        this method deep-copied the value, a caller could mutate a
        nested structure (a list or dict inside the stored value) in
        place and silently corrupt this memory without going through
        write() or leaving any trace in get_history().

        Args:
            key: The key to look up.
            default: Value to return if the key does not exist.

        Returns:
            A deep copy of the stored value, or default if not found.
        """
        return deepcopy(self._store.get(key, default))

    def get_all(self) -> dict[str, Any]:
        """
        Return a defensive deep copy of all private memory contents.
        Used by the agent to compose its final output.

        A shallow `dict(self._store)` copies only the top level: nested
        values (lists, dicts) inside it remain the same objects as the
        ones actually stored, so mutating them would still corrupt
        internal state. deepcopy() closes that gap, matching the
        treatment UniversalMemory already receives.
        """
        return deepcopy(self._store)

    def clear(self, *, context: ExecutionContext | None = None) -> None:
        """Clear all stored values. Use with caution.

        Recorded in get_history() like any other change: a clear that
        left no trace would mean the audit log of this memory's own
        write() calls could go silently out of sync with what is
        actually stored.
        """
        if self._persistence_backend is not None:
            mutation_context = self._mutation_context(context)
            started_event_id, descriptor = self._persist_mutation(
                operation=PrivateMemoryOperation.CLEAR,
                key=None,
                value=None,
                context=mutation_context,
            )
            previous = self._store
            history_length = len(self._history)
            self._store = {}
            self._history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "clear",
                "key": None,
            })
            try:
                self._confirm_mutation(
                    context=mutation_context,
                    started_event_id=started_event_id,
                    descriptor=descriptor,
                )
            except Exception:
                self._store = previous
                del self._history[history_length:]
                raise
            return
        self._store.clear()
        self._history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "clear",
            "key": None,
        })

    def get_history(self) -> list[dict[str, Any]]:
        """Return a defensive copy of the full write history.

        ``list(self._history)`` was a new list of the same dicts, so a
        caller could edit an entry in place and change what this memory
        reports about a write that already happened, retroactively and
        without any record that it was edited. Deep-copied for the same
        reason ``read()`` and ``get_all()`` already are: a record of what
        occurred is not something a reader gets to revise.

        This history is a debugging aid, not a tamper-evident log. The
        hash-chained record with that property lives in ``audit_ruler``;
        adding a second chain here would duplicate a mechanism the
        framework already has.
        """
        return deepcopy(self._history)

    @property
    def owner(self) -> str:
        """The name of the agent that owns this memory."""
        return self._owner

    @property
    def memory_id(self) -> str | None:
        """Stable opaque identity, or None for an ephemeral memory."""
        return self._memory_id

    def __repr__(self) -> str:
        return f"PrivateMemory(owner={self._owner!r}, keys={list(self._store.keys())})"
