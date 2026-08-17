"""
SETT Framework: UniversalMemory
==============================
Shared memory accessible by the orchestrator and all agents.

Agents publish ONLY their final results here: not internal reasoning.
Every write passes through the EthicalFilter if one is configured.

Also handles EnvironmentalContext: the shared risk state that
multiple SETT instances in the same location can read and publish.
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sett.audit_ruler.chain import append_chained_entry, verify_chain
from sett.core_ruler.execution_context import (
    ExecutionContext,
    current_execution_context,
    current_trace_cause_id,
)
from sett.exceptions import (
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTMemoryAccessDeniedError,
    SETTValidationError,
)

if TYPE_CHECKING:
    from sett.audit_ruler.trace import TraceRecorder
    from sett.ethics_ruler.ethic_kernel.filter import EthicalFilter
    from sett.risk_ruler.environmental_context import EnvironmentalContext
    from sett.risk_ruler.risk_profile import RiskProfile

# Reserved key prefix for environmental context entries.
#
# One store holds two kinds of entry: agent results under their domain
# and environmental contexts under this prefix. Telling them apart
# is what read_all() and read_environmental_context() rely on. A domain
# that happens to start with the prefix therefore lands in the store as
# an agent result and is read back as an environmental context: it
# disappears from read_all() and makes read_environmental_context()
# search for fields a normal result does not have. The prefix is
# reserved below rather than merely undocumented.
ENV_CONTEXT_PREFIX = "__env_ctx__"

# Retained as an alias: existing call sites in this module use this name.
_ENV_CONTEXT_PREFIX = ENV_CONTEXT_PREFIX

# RiskProfile's own three pillar field names (see risk_ruler/risk_profile.py).
# RiskProfile.to_dict() is documented to live exclusively in PrivateMemory:
# these names, or a raw RiskProfile instance, are rejected anywhere inside
# a published result rather than merely being undocumented to publish.
_RISK_PROFILE_RESERVED_KEYS = frozenset(
    {
        "emotional_instability",
        "influence_vulnerability",
        "collateral_damage_potential",
    }
)


def _reject_reserved_agent_name(agent: str) -> None:
    """Refuse a domain that would land inside the environmental namespace.

    Checked here rather than in ``SETTAgent.__init__`` because the
    namespace belongs to this class: the prefix is defined here, both
    kinds of entry live in this store, and every write, whether from an
    agent or from a direct caller, passes through ``update()``. Validating in the
    agent constructor as well would put this module's constant in a
    second place to keep in step.
    """
    if not isinstance(agent, str) or not agent.strip():
        raise SETTValidationError(
            "UniversalMemory agent domain must be a non-empty string, "
            f"got {agent!r}."
        )
    if agent.startswith(ENV_CONTEXT_PREFIX):
        raise SETTValidationError(
            f"Agent domain {agent!r} is reserved: the prefix "
            f"{ENV_CONTEXT_PREFIX!r} identifies environmental context "
            "entries in this store, so a domain starting with it would be "
            "written as a result and read back as an environmental "
            "context. Publish environmental state with "
            "publish_environmental_context() instead."
        )


def _reject_risk_profile_leak(value: Any, *, agent: str) -> None:
    """Recursively refuse a RiskProfile, or its reserved fields, in ``value``.

    A cooperative Expert never does this; it has no reason to reach for
    these names. What this guards against is an adversarial or simply
    careless one that embeds a raw ``RiskProfile`` instance, or its
    serialized pillar keys, inside the dict it hands to ``update()``. The
    existing privacy test only exercised the cooperative case; this makes
    the documented "RiskProfile never leaves PrivateMemory" boundary a
    structural check at the one place everything published to
    UniversalMemory must pass through, rather than a promise every future
    Expert author has to remember unprompted.
    """
    from sett.risk_ruler.risk_profile import RiskProfile

    if isinstance(value, RiskProfile):
        raise SETTMemoryAccessDeniedError(
            f"Agent '{agent}' tried to publish a RiskProfile instance to "
            "UniversalMemory. RiskProfile values are private to the "
            "assessing agent and must stay in PrivateMemory; only the "
            "derived RiskLevel may be shared, via "
            "publish_environmental_context()."
        )
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key in _RISK_PROFILE_RESERVED_KEYS:
                raise SETTMemoryAccessDeniedError(
                    f"Agent '{agent}' tried to publish reserved RiskProfile "
                    f"field '{key}' to UniversalMemory. RiskProfile pillar "
                    "values are private to the assessing agent and must "
                    "stay in PrivateMemory."
                )
            _reject_risk_profile_leak(nested, agent=agent)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_risk_profile_leak(item, agent=agent)


class UniversalMemory:
    """
    Universal memory shared across all agents and the orchestrator.

    Two types of data live here:

    1. Agent results: published via update(), read via read()/read_all().
       Each agent publishes its final result under its domain name.

    2. Environmental context: published via publish_environmental_context(),
       read via read_environmental_context().
       Used for multi-instance coordination (the "warehouse scenario"):
       one SETT instance publishes a RiskLevel for a location,
       others in the same location read it and adjust their behavior.

    Thread-safe by default.
    """

    def __init__(self) -> None:
        """Start an empty shared memory, with no filter or recorder attached."""
        self._store: dict[str, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []
        self._ethical_filter: EthicalFilter | None = None
        self._trace_recorder: TraceRecorder | None = None
        self._lock = threading.Lock()

    def set_ethical_filter(self, ethical_filter: EthicalFilter) -> None:
        """Attach an EthicalFilter to intercept all writes."""
        self._ethical_filter = ethical_filter

    def set_trace_recorder(self, recorder: TraceRecorder) -> None:
        """Attach the orchestrator-owned trace recorder."""
        self._trace_recorder = recorder

    # ── Agent results ────────────────────────────────────────────────────────

    def update(
        self,
        agent: str,
        result: dict[str, Any],
        emotional_state: str = "unknown",
        risk_profile: RiskProfile | None = None,
        environmental_context: EnvironmentalContext | None = None,
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
    ) -> None:
        """
        Called by an agent to publish its final result.
        Passes through the EthicalFilter before being committed.

        v0.1.1 fix: previously this only passed action="memory_write" and
        a context wrapped as {"agent": agent, "result": result}: so (a)
        emotional_state/risk_profile/environmental_context never reached
        the filter in the real flow (they silently defaulted every time),
        and (b) detectors that read biometric data expected keys directly
        in `context` but received them nested one level deeper under
        "result" instead, so they never matched.

        Now the published result's keys are spread directly into the
        context passed to the filter (so both flat and "health"-nested
        detectors can find them), and all three risk layers are forwarded.
        The publishing agent's domain is still available, namespaced as
        "_source_agent" to avoid colliding with the agent's own data.
        """
        # One read of the caller's object, at the door. Everything below,
        # including the leak check, the filter's context and the stored
        # value, is derived from this single copy and never from `result` again.
        #
        # Reading it twice (once to evaluate, once to store) left a window
        # in which another thread holding the same dict could change it in
        # between, so the filter approved one value and a different one was
        # stored. That is not a caller discipline problem: SETT's guarantee
        # is that what policy approved is what happens, and a guarantee
        # that depends on nobody touching an object they still hold is not
        # structural. Copying once closes the window without a lock,
        # because there is no longer a second read to race against.
        _reject_reserved_agent_name(agent)
        published = deepcopy(result)
        _reject_risk_profile_leak(published, agent=agent)

        trace_context = execution_context or current_execution_context()
        trace_cause = cause_id or current_trace_cause_id()
        proposed_event = None
        if trace_context is not None and self._trace_recorder is not None:
            proposed_event = self._trace_recorder.record(
                trace_context,
                kind="memory.publication_proposed",
                component_type="universal_memory",
                component_name=agent,
                status="proposed",
                cause_id=trace_cause,
                reason_codes=("memory.publication",),
                attributes={"agent": agent, "field_count": len(published)},
            )

        policy_event = None
        if self._ethical_filter is not None:
            # A further copy of the snapshot, not the snapshot itself: an
            # analyzer that mutates what it was handed during evaluation
            # would otherwise taint the very value this method goes on to
            # store, with no signal that it happened.
            context = deepcopy(published)
            context["_source_agent"] = agent
            try:
                _, policy_event = self._ethical_filter._evaluate_with_trace(
                    action="memory_write",
                    context=context,
                    emotional_state=emotional_state,
                    risk_profile=risk_profile,
                    environmental_context=environmental_context,
                    execution_context=trace_context,
                    cause_id=(
                        proposed_event.event_id if proposed_event else trace_cause
                    ),
                )
            except SETTEthicalFilterRejectedError as error:
                if trace_context is not None and self._trace_recorder is not None:
                    rejected_event = self._trace_recorder.record(
                        trace_context,
                        kind="memory.publication_rejected",
                        component_type="universal_memory",
                        component_name=agent,
                        status="rejected",
                        cause_id=(
                            error.trace_event_id
                            or (
                                proposed_event.event_id
                                if proposed_event else trace_cause
                            )
                        ),
                        reason_codes=("policy.reject",),
                    )
                    error.trace_event_id = rejected_event.event_id
                raise

        with self._lock:
            self._store[agent] = published
            append_chained_entry(self._history, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": agent,
                "action": "update",
            })
        if trace_context is not None and self._trace_recorder is not None:
            self._trace_recorder.record(
                trace_context,
                kind="memory.publication_committed",
                component_type="universal_memory",
                component_name=agent,
                status="completed",
                cause_id=(
                    policy_event.event_id
                    if self._ethical_filter is not None
                    and policy_event is not None
                    else (
                        proposed_event.event_id if proposed_event else trace_cause
                    )
                ),
                reason_codes=("memory.publication",),
                attributes={"agent": agent, "field_count": len(published)},
            )

    def evaluate_action(
        self,
        action: str,
        context: dict[str, Any],
        emotional_state: str = "unknown",
        risk_profile: RiskProfile | None = None,
        environmental_context: EnvironmentalContext | None = None,
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
    ) -> Any:
        """
        Evaluate a real-world side effect through the EthicalFilter BEFORE
        it is executed: used by SETTAgent.propose_action() and by
        SETTExecutor.submit(). Unlike update(), this does not write
        anything to universal memory; it only runs the action through the
        filter and lets a rejection propagate as
        SETTEthicalFilterRejectedError.

        Action evaluation is fail-closed: unlike plain memory storage, a
        proposed side effect cannot be approved without an EthicalFilter.
        """
        if self._ethical_filter is None:
            raise SETTConfigurationError(
                "Cannot evaluate a real-world action without an EthicalFilter. "
                "Attach this UniversalMemory to a SETTOrchestrator or call "
                "set_ethical_filter() before proposing actions."
            )
        verdict, _ = self._evaluate_action_with_trace(
            action=action,
            context=context,
            emotional_state=emotional_state,
            risk_profile=risk_profile,
            environmental_context=environmental_context,
            execution_context=execution_context,
            cause_id=cause_id,
        )
        return verdict

    def _evaluate_action_with_trace(
        self,
        action: str,
        context: dict[str, Any],
        emotional_state: str = "unknown",
        risk_profile: RiskProfile | None = None,
        environmental_context: EnvironmentalContext | None = None,
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
    ) -> tuple[Any, Any]:
        """Internal action evaluation returning its exact policy event."""
        if self._ethical_filter is None:
            raise SETTConfigurationError(
                "Cannot evaluate a real-world action without an EthicalFilter. "
                "Attach this UniversalMemory to a SETTOrchestrator or call "
                "set_ethical_filter() before proposing actions."
            )
        return self._ethical_filter._evaluate_with_trace(
            action=action,
            context=deepcopy(context),
            emotional_state=emotional_state,
            risk_profile=risk_profile,
            environmental_context=environmental_context,
            execution_context=execution_context or current_execution_context(),
            cause_id=cause_id or current_trace_cause_id(),
        )

    def read(self, agent: str, default: Any = None) -> Any:
        """Read a defensive copy of one agent's latest published result."""
        with self._lock:
            return deepcopy(self._store.get(agent, default))

    def read_all(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all agent results. Used by the orchestrator."""
        with self._lock:
            return deepcopy({
                k: v for k, v in self._store.items()
                if not k.startswith(_ENV_CONTEXT_PREFIX)
            })

    # ── Environmental context (multi-instance coordination) ──────────────────

    def publish_environmental_context(
        self, context: EnvironmentalContext
    ) -> None:
        """
        Publish an EnvironmentalContext to a shared location slot.

        Any SETT instance that reads this location key will receive
        the current risk level: without any personal data attached.

        Args:
            context: The EnvironmentalContext to publish.
        """
        key = f"{_ENV_CONTEXT_PREFIX}{context.location_id}"
        with self._lock:
            self._store[key] = deepcopy(context.to_dict())
            append_chained_entry(self._history, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "env_context_update",
                "location_id": context.location_id,
                "risk_level": context.risk_level.value,
                "source_domain": context.source_domain,
            })

    def read_environmental_context(
        self, location_id: str = "global"
    ) -> EnvironmentalContext | None:
        """
        Read the current EnvironmentalContext for a location.

        Returns None if no context has been published for this location.

        Args:
            location_id: The location to read. Defaults to "global".
        """
        from sett.risk_ruler.environmental_context import EnvironmentalContext
        key = f"{_ENV_CONTEXT_PREFIX}{location_id}"
        with self._lock:
            data = deepcopy(self._store.get(key))
        if data is None:
            return None
        return EnvironmentalContext.from_dict(data)

    def read_all_environmental_contexts(
        self,
    ) -> dict[str, EnvironmentalContext]:
        """Return all published EnvironmentalContexts, keyed by location_id."""
        from sett.risk_ruler.environmental_context import EnvironmentalContext
        result = {}
        with self._lock:
            for key, data in self._store.items():
                if key.startswith(_ENV_CONTEXT_PREFIX):
                    location_id = key[len(_ENV_CONTEXT_PREFIX):]
                    result[location_id] = EnvironmentalContext.from_dict(deepcopy(data))
        return result

    def get_history(self) -> list[dict[str, Any]]:
        """Defensive snapshot of the tamper-evident write history."""
        with self._lock:
            return deepcopy(self._history)

    def verify_history(self) -> bool:
        """Verify sequence and hash links in the internal write history."""
        with self._lock:
            return verify_chain(self._history)

    @property
    def has_ethical_filter(self) -> bool:
        """Whether real-world action evaluation can be governed safely."""
        return self._ethical_filter is not None

    def __repr__(self) -> str:
        agents = [k for k in self._store if not k.startswith(_ENV_CONTEXT_PREFIX)]
        env_keys = [k for k in self._store if k.startswith(_ENV_CONTEXT_PREFIX)]
        return (
            f"UniversalMemory("
            f"agents={agents}, "
            f"env_locations={[k[len(_ENV_CONTEXT_PREFIX):] for k in env_keys]})"
        )
