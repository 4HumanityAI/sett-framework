"""
SETT Framework: PrivateMemory
==============================
Each agent has its own private memory.
By design, only the agent's own experts are given a reference to it
(via attach_memory(), during registration) and use it to write.
The orchestrator is never handed one.

This is a design and API-surface convention, not a runtime
access-control boundary (F24, defensive audit #2): PrivateMemory has
no capability/ACL enforcement, and any code that does obtain a
reference to an instance, e.g. by holding onto the agent object,
can call its methods directly. Nothing in this module can stop that;
what SETT provides is that the normal, documented API never gives the
orchestrator or another agent that reference in the first place. See
docs/SETT_Conventions.md for the fuller rationale.

This is one of the two core memory layers of SETT.
"""
from __future__ import annotations
from typing import Any
from copy import deepcopy
from datetime import datetime, timezone


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
    class (F24, defensive audit #2).

    Inspired by an early prototype's memory.py: simplified and made agent-specific.
    """

    def __init__(self, owner: str) -> None:
        """
        Args:
            owner: The name of the agent that owns this memory.
                   Used for logging and access control context.
        """
        self._owner = owner
        self._store: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []

    def write(self, key: str, value: Any) -> None:
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
        self._store[key] = deepcopy(value)
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

    def clear(self) -> None:
        """Clear all stored values. Use with caution.

        Recorded in get_history() like any other change: a clear that
        left no trace would mean the audit log of this memory's own
        write() calls could go silently out of sync with what is
        actually stored.
        """
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

    def __repr__(self) -> str:
        return f"PrivateMemory(owner={self._owner!r}, keys={list(self._store.keys())})"
