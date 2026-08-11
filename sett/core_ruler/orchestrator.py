"""
SETT Framework: SETTOrchestrator
==============================
The brain and central meeting point of all agents.

The orchestrator:
- Holds all registered agents
- Manages the UniversalMemory (the only memory it can access)
- Routes input to the appropriate agent(s)
- Applies the EthicalFilter before executing actions
- Synthesizes the final system response from agent results

The orchestrator does NOT have access to agents' private memory.
It only sees what agents choose to publish to universal memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sett.audit_ruler.trace import TraceEvent, TraceExporter, TraceRecorder
from sett.core_ruler.agent import SETTAgent
from sett.core_ruler.execution_context import (
    ExecutionContext,
    TracedResult,
    current_execution_context,
    current_trace_cause_id,
    execution_scope,
)
from sett.core_ruler.execution_result import (
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
)
from sett.core_ruler.executor import SETTExecutor
from sett.core_ruler.lifecycle import (
    ExecutionControl,
    ExecutionControlRegistry,
    LifecyclePolicy,
    derive_control_for,
    lifecycle_policy_scope,
    lifecycle_scope,
    stop_cause_id,
    stop_reason_code,
)
from sett.ethics_ruler.ethic_kernel.filter import EthicalFilter
from sett.exceptions import (
    SETTAgentNotFoundError,
    SETTCancelledError,
    SETTError,
    SETTEthicalFilterRejectedError,
    SETTTimeoutError,
    SETTValidationError,
)
from sett.memory_ruler.universal import UniversalMemory
from sett.risk_ruler.environmental_context import EnvironmentalContext
from sett.risk_ruler.risk_level import RiskLevel

if TYPE_CHECKING:
    from sett.core_ruler.pipeline import PipelineResult, PipelineStep

logger = logging.getLogger(__name__)

# Which trace terminal closes a controlled run, per structured status.
# Cancellation and deadline exhaustion get their own kinds rather than
# sharing "trace.failed": a trace that cannot tell "somebody stopped this"
# from "this broke" cannot answer the question it exists to answer.
_TRACE_TERMINAL_BY_STATUS = {
    ExecutionStatus.CANCELLED: "trace.cancelled",
    ExecutionStatus.TIMED_OUT: "trace.timed_out",
    ExecutionStatus.REJECTED: "trace.failed",
    ExecutionStatus.FAILED: "trace.failed",
}

_PIPELINE_TERMINAL_BY_STATUS = {
    ExecutionStatus.SUCCESS: "pipeline.completed",
    ExecutionStatus.PARTIAL: "pipeline.partial",
    ExecutionStatus.CANCELLED: "pipeline.cancelled",
    ExecutionStatus.TIMED_OUT: "pipeline.timed_out",
    ExecutionStatus.REJECTED: "pipeline.rejected",
    ExecutionStatus.FAILED: "pipeline.failed",
}


class SETTOrchestrator:
    """
    The SETTOrchestrator is the core of any SETT-based system.

    It coordinates all agents, manages universal memory, and ensures
    every action passes through the ethical filter before execution.

    Usage:
        orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
        orchestrator.register_agent(HealthAgent())
        orchestrator.register_agent(CommunicationsAgent())

        result = orchestrator.process(
            input_data={"heart_rate_bpm": 110},
            domain="health"
        )
    """

    def __init__(
        self,
        ethical_filter: EthicalFilter | None = None,
        *,
        lifecycle_policy: LifecyclePolicy | None = None,
    ) -> None:
        """
        Args:
            ethical_filter: The EthicalFilter to use. Defaults to a new
                            EthicalFilter with the default SETT ruleset.
            lifecycle_policy: Optional per-level timeout defaults used by
                            the controlled entry points. Omitting it - the
                            default - imposes no timeout anywhere, so an
                            orchestrator built exactly as in v0.11.0
                            behaves exactly as it did.
        """
        self._agents: dict[str, SETTAgent] = {}
        self._universal_memory = UniversalMemory()
        self._ethical_filter = ethical_filter or EthicalFilter()
        self._executor = None  # SETTExecutor | None: set via register_executor()
        self._trace_recorder = TraceRecorder()
        self._last_trace_id: str | None = None
        self._lifecycle_policy = lifecycle_policy or LifecyclePolicy()
        self._control_registry = ExecutionControlRegistry()

        # Connect the ethical filter to universal memory
        # Every write to universal memory will pass through it
        self._universal_memory.set_ethical_filter(self._ethical_filter)
        self._universal_memory.set_trace_recorder(self._trace_recorder)
        self._ethical_filter.set_trace_recorder(self._trace_recorder)

    def register_agent(self, agent: SETTAgent) -> None:
        """
        Register an agent with the orchestrator.
        Connects the agent to universal memory, and to the Executor if
        one has already been registered (order-independent: if the
        Executor is registered later via register_executor(), it is
        retroactively attached to every agent registered so far).

        Registering an agent for a domain that already has one REPLACES
        it. That is deliberate and is how a ``StubDomainAgent`` is swapped
        for the real implementation once it exists - "replacing a stub
        with the real piece is transparent (same domain, zero changes to
        the rest)", which the fail-honest scaffolding convention promises
        and the stub tests exercise. Unlike ``register_expert()`` and
        ``register_handler()``, which refuse a duplicate, this one cannot
        refuse without removing a published capability.

        The documented use is a stub giving way to a real agent. Two real
        agents landing on one domain is almost always an accident, and it
        used to be indistinguishable in the log from an ordinary
        registration, so that case is logged as a warning while the
        intended swap stays quiet.

        Args:
            agent: A SETTAgent instance. Registering over an existing
                domain replaces the agent currently serving it.
        """
        from sett.core_ruler.stub_agent import StubDomainAgent

        replaced = self._agents.get(agent.domain)
        agent.attach_universal_memory(self._universal_memory)
        agent.attach_trace_recorder(self._trace_recorder)
        if self._executor is not None:
            agent.attach_executor(self._executor)
        self._agents[agent.domain] = agent
        if replaced is not None and not isinstance(replaced, StubDomainAgent):
            logger.warning(
                "[Orchestrator] Agent '%s' replaced '%s' on domain '%s'. "
                "Replacing a StubDomainAgent is the documented way to swap "
                "in a real implementation; replacing a real agent is "
                "usually two agents sharing a domain by accident.",
                agent.name, replaced.name, agent.domain,
            )
        else:
            logger.info(
                "[Orchestrator] Agent registered: '%s' (domain: '%s')",
                agent.name, agent.domain
            )

    def register_executor(self, executor: SETTExecutor) -> None:
        """
        Register a SETTExecutor with this orchestrator. Gives it access
        to universal memory (so it can evaluate actions through the
        EthicalFilter and read EnvironmentalContext), and attaches it to
        every agent already registered: as well as to any agent
        registered afterward, automatically.

        Args:
            executor: A SETTExecutor instance.
        """
        self._executor = executor
        executor.attach_universal_memory(self._universal_memory)
        executor.attach_trace_recorder(self._trace_recorder)
        executor.attach_lifecycle_policy(self._lifecycle_policy)
        for agent in self._agents.values():
            agent.attach_executor(executor)
        logger.info("[Orchestrator] Executor registered.")

    def get_agent(self, domain: str) -> SETTAgent:
        """
        Retrieve a registered agent by domain.

        Args:
            domain: The domain key of the agent.

        Raises:
            SETTAgentNotFoundError: If no agent is registered for that domain.
        """
        if domain not in self._agents:
            raise SETTAgentNotFoundError(
                f"No agent registered for domain '{domain}'. "
                f"Registered domains: {list(self._agents.keys())}"
            )
        return self._agents[domain]

    def process(
        self,
        input_data: dict[str, Any],
        domain: str | None = None,
        emotional_state: str = "unknown",
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """
        Process an input through the system.

        If domain is specified, routes directly to that agent.
        If no domain is given, broadcasts to all agents and collects results.

        v0.1.1 fix: emotional_state and location_id are now actually
        propagated to the agent (and from there, automatically, to the
        EthicalFilter via _publish_to_universal()). Previously
        emotional_state was accepted here but silently dropped before
        reaching agent.process(): every real evaluation ran with
        emotional_state="unknown" regardless of what was passed in.

        Args:
            input_data: The data to process.
            domain: Optional domain to route to a specific agent.
            emotional_state: The detected emotional state of the user.
                             When integrated with the Sentiment Analyzer agent,
                             this is passed automatically to the EthicalFilter.
            location_id: The shared space this interaction happens in.
                         Used to look up the EnvironmentalContext (Layer 3)
                         for this location. Defaults to "global".

        Returns:
            The result from the agent (or a dict of results from all agents).
        """
        if execution_context is not None and not isinstance(
            execution_context, ExecutionContext
        ):
            raise SETTValidationError(
                "execution_context must be an ExecutionContext or None."
            )
        root = execution_context or ExecutionContext.create()
        self._last_trace_id = root.trace_id
        started = self._trace_recorder.record(
            root,
            kind="trace.started",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="started",
            reason_codes=(
                "route.explicit_domain" if domain else "route.broadcast",
            ),
            attributes={"domain": domain, "mode": "route" if domain else "broadcast"},
        )
        with execution_scope(root, started.event_id):
            try:
                if domain:
                    result, terminal_event = self._route_to_agent(
                        domain,
                        input_data,
                        emotional_state,
                        location_id,
                        execution_context=root,
                        cause_id=started.event_id,
                        _return_event=True,
                    )
                else:
                    result, terminal_event = self._broadcast(
                        input_data,
                        emotional_state,
                        location_id,
                        execution_context=root,
                        cause_id=started.event_id,
                        _return_event=True,
                    )
            except Exception as error:
                self._trace_recorder.record(
                    root,
                    kind="trace.failed",
                    component_type="orchestrator",
                    component_name=self.__class__.__name__,
                    status="failed",
                    cause_id=(
                        getattr(error, "trace_event_id", None)
                        or started.event_id
                    ),
                    reason_codes=("component.exception",),
                    attributes={"error_type": type(error).__name__},
                )
                raise
            self._trace_recorder.record(
                root,
                kind="trace.completed",
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status="completed",
                cause_id=terminal_event.event_id,
            )
            return result

    def _route_to_agent(
        self,
        domain: str,
        input_data: dict[str, Any],
        emotional_state: str,
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
        _return_event: bool = False,
    ) -> Any:
        """Route input to a specific agent."""
        parent = execution_context or current_execution_context() or ExecutionContext.create()
        route_event = self._trace_recorder.record(
            parent,
            kind="route.selected",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="completed",
            cause_id=cause_id or current_trace_cause_id(),
            reason_codes=("route.explicit_domain",),
            attributes={"domain": domain},
        )
        try:
            agent = self.get_agent(domain)
        except Exception as error:
            route_failed = self._trace_recorder.record(
                parent,
                kind="route.failed",
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status="failed",
                cause_id=route_event.event_id,
                reason_codes=("route.domain_unavailable",),
                attributes={
                    "domain": str(domain),
                    "error_type": type(error).__name__,
                },
            )
            try:
                error.trace_event_id = route_failed.event_id
            except (AttributeError, TypeError):
                pass
            raise
        agent_context = parent.derive()
        agent_started = self._trace_recorder.record(
            agent_context,
            kind="agent.started",
            component_type="agent",
            component_name=agent.name,
            status="started",
            cause_id=route_event.event_id,
            attributes={"domain": domain},
        )
        agent._current_emotional_state = emotional_state
        agent._current_location_id = location_id
        # A control for this agent's own node, when lifecycle management is
        # in use. Without an ambient control this is None and every
        # checkpoint below is skipped: routing behaves exactly as it did in
        # v0.11.0 for any caller that never asked for cancellation. When one
        # is derived, it is bounded by the policy's per-agent timeout (if
        # any) on top of whatever time the parent has left.
        agent_control = derive_control_for(
            agent_context,
            timeout_seconds=self._lifecycle_policy.agent_timeout(domain),
        )
        with execution_scope(agent_context, agent_started.event_id):
            return self._run_agent(
                agent,
                domain,
                input_data,
                parent,
                agent_context,
                agent_started,
                agent_control,
                _return_event,
            )

    def _run_agent(
        self,
        agent: SETTAgent,
        domain: str,
        input_data: dict[str, Any],
        parent: ExecutionContext,
        agent_context: ExecutionContext,
        agent_started: TraceEvent,
        agent_control: ExecutionControl | None,
        _return_event: bool,
    ) -> Any:
        """Invoke one agent, closing its trace boundary whatever happens.

        Entering the control is inside this try on purpose. A control whose
        entry checkpoint fails - which is what happens when cancellation
        arrives in the window between ``agent.started`` being recorded and
        the agent actually starting - raises before the body ever runs, and
        an ``agent.started`` with no terminal is exactly the unclosed
        boundary the recorder's verification rejects.
        """
        try:
            if agent_control is None:
                result = self._invoke_agent(agent, domain, input_data, None)
            else:
                with agent_control, lifecycle_scope(agent_control):
                    result = self._invoke_agent(
                        agent, domain, input_data, agent_control
                    )
        except (SETTCancelledError, SETTTimeoutError) as error:
            cancelled = isinstance(error, SETTCancelledError)
            agent_stopped = self._trace_recorder.record(
                agent_context,
                kind="agent.cancelled" if cancelled else "agent.timed_out",
                component_type="agent",
                component_name=agent.name,
                status="cancelled" if cancelled else "timed_out",
                cause_id=stop_cause_id(
                    self._trace_recorder,
                    agent_context.trace_id,
                    (
                        self._trace_recorder.last_event_id(
                            agent_context.run_id
                        )
                        or agent_started.event_id
                    ),
                    error,
                ),
                reason_codes=(stop_reason_code(error),),
            )
            route_stopped = self._trace_recorder.record(
                parent,
                kind="route.cancelled" if cancelled else "route.timed_out",
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status="cancelled" if cancelled else "timed_out",
                cause_id=agent_stopped.event_id,
                reason_codes=(stop_reason_code(error),),
                attributes={"domain": domain},
            )
            try:
                error.trace_event_id = route_stopped.event_id
            except (AttributeError, TypeError):
                pass
            raise
        except SETTEthicalFilterRejectedError as error:
            agent_failed = self._trace_recorder.record(
                agent_context,
                kind="agent.failed",
                component_type="agent",
                component_name=agent.name,
                status="rejected",
                cause_id=error.trace_event_id or agent_started.event_id,
                reason_codes=("policy.reject",),
            )
            route_failed = self._trace_recorder.record(
                parent,
                kind="route.failed",
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status="rejected",
                cause_id=agent_failed.event_id,
                reason_codes=("policy.reject",),
                attributes={"domain": domain},
            )
            error.trace_event_id = route_failed.event_id
            logger.warning(
                "[Orchestrator] EthicalFilter blocked action from agent '%s'.",
                agent.name
            )
            raise
        except Exception as error:
            agent_failed = self._trace_recorder.record(
                agent_context,
                kind="agent.failed",
                component_type="agent",
                component_name=agent.name,
                status="failed",
                cause_id=(
                    getattr(error, "trace_event_id", None)
                    or agent_started.event_id
                ),
                reason_codes=("component.exception",),
                attributes={"error_type": type(error).__name__},
            )
            route_failed = self._trace_recorder.record(
                parent,
                kind="route.failed",
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status="failed",
                cause_id=agent_failed.event_id,
                reason_codes=("component.exception",),
                attributes={
                    "domain": domain,
                    "error_type": type(error).__name__,
                },
            )
            try:
                error.trace_event_id = route_failed.event_id
            except (AttributeError, TypeError):
                pass
            raise
        agent_completed = self._trace_recorder.record(
            agent_context,
            kind="agent.completed",
            component_type="agent",
            component_name=agent.name,
            status="completed",
            cause_id=(
                self._trace_recorder.last_event_id(agent_context.run_id)
                or agent_started.event_id
            ),
        )
        route_completed = self._trace_recorder.record(
            parent,
            kind="route.completed",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="completed",
            cause_id=agent_completed.event_id,
            attributes={"domain": domain},
        )
        if _return_event:
            return result, route_completed
        return result

    def _invoke_agent(
        self,
        agent: SETTAgent,
        domain: str,
        input_data: dict[str, Any],
        control: ExecutionControl | None,
    ) -> dict[str, Any]:
        """Call one agent, observing the control around it when there is one."""
        if control is not None:
            control.checkpoint()
        logger.debug(
            "[Orchestrator] Routing to agent '%s' (domain: '%s')",
            agent.name, domain
        )
        result = agent.process(input_data)
        if control is not None:
            control.checkpoint()
        return result

    # ── Controlled entry points (v0.12.0) ────────────────────────────────────

    @property
    def control_registry(self) -> ExecutionControlRegistry:
        """The registry holding this orchestrator's live execution controls.

        Cancellation is addressed by ``run_id`` - the same identifier that
        appears in exported trace events. An application that keeps the
        ``run_id`` of a conversational turn can later cancel exactly that
        turn, without touching the session it belongs to or the sibling
        work running beside it.
        """
        return self._control_registry

    @property
    def lifecycle_policy(self) -> LifecyclePolicy:
        """The timeout defaults used by the controlled entry points."""
        return self._lifecycle_policy

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = "execution.cancelled_by_caller",
    ) -> bool:
        """Cancel one execution node and everything running beneath it.

        Safe to call from another thread: that is the point. Returns False
        when the node is unknown or was already cancelled, so a caller can
        tell "I stopped it" from "it was already over".

        Routed through the live control rather than straight at the
        registry, so the request is recorded and its event id travels down
        to every node it stops. Without that the work would halt just the
        same, but the trace would show things stopping with nothing saying
        who asked - which is the question a trace exists to answer.
        """
        control = self._control_registry.control_for(run_id)
        if control is not None:
            return control.cancel(reason=reason)
        return self._control_registry.cancel_subtree(run_id, reason=reason)

    def _finish_state(
        self,
        control: ExecutionControl,
        status: ExecutionStatus,
        *,
        reason_code: str | None = None,
        completed_units: int = 0,
        total_units: int | None = None,
    ) -> ExecutionState:
        """Build the terminal state of a controlled run from its control."""
        now = datetime.now(timezone.utc)
        return ExecutionState(
            status=status,
            started_at=control.started_at or now,
            finished_at=control.finished_at or now,
            reason_code=reason_code,
            completed_units=completed_units,
            total_units=total_units,
            cleanup_errors=control.cleanup_errors,
            duration_seconds=control.elapsed_seconds,
        )

    @staticmethod
    def _status_for(error: BaseException) -> tuple[ExecutionStatus, str]:
        """Map a terminating exception to its structured status.

        The four cases are kept apart deliberately: an ethical rejection is
        not a technical failure, and a deadline is not a cancellation.
        """
        if isinstance(error, SETTCancelledError):
            return ExecutionStatus.CANCELLED, stop_reason_code(error)
        if isinstance(error, SETTTimeoutError):
            return ExecutionStatus.TIMED_OUT, stop_reason_code(error)
        if isinstance(error, SETTEthicalFilterRejectedError):
            return ExecutionStatus.REJECTED, "policy.reject"
        return ExecutionStatus.FAILED, "component.exception"

    def process_controlled(
        self,
        input_data: dict[str, Any],
        domain: str | None = None,
        emotional_state: str = "unknown",
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult[dict[str, Any]]:
        """Process an input under cancellation and deadline control.

        A new method rather than a rewiring of ``process()``, the same way
        ``process_traced()`` was added in v0.11.0: the existing entry point
        keeps its exact behaviour, including which exception type reaches
        an existing caller, and applications adopt this one when they want
        the new guarantees.

        Nothing here raises for a run that was stopped. Cancellation, a
        deadline, an ethical rejection and a technical failure all come
        back as an ``ExecutionResult`` whose status distinguishes them, and
        whose ``exception`` still holds the real error for anyone who wants
        it - ``unwrap()`` re-raises it unchanged.

        To be able to cancel this run, pass an ``execution_context`` you
        built yourself and hand its ``run_id`` to whoever might need to
        stop it; ``orchestrator.cancel(run_id)`` then cancels this run and
        everything nested inside it, from any thread.

        Args:
            input_data: The data to process.
            domain: Optional domain to route to a specific agent.
            emotional_state: Propagated exactly as in ``process()``.
            location_id: Propagated exactly as in ``process()``.
            execution_context: Optional root identity for this run.
            timeout_seconds: Optional deadline for the whole run,
                overriding the lifecycle policy's ``run_timeout_seconds``.

        Returns:
            An ExecutionResult carrying the agent result on success, or the
            structured reason it stopped.
        """
        if execution_context is not None and not isinstance(
            execution_context, ExecutionContext
        ):
            raise SETTValidationError(
                "execution_context must be an ExecutionContext or None."
            )
        root = execution_context or ExecutionContext.create()
        self._last_trace_id = root.trace_id
        run_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._lifecycle_policy.run_timeout_seconds
        )
        started = self._trace_recorder.record(
            root,
            kind="trace.started",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="started",
            reason_codes=(
                "route.explicit_domain" if domain else "route.broadcast",
            ),
            attributes={
                "domain": domain,
                "mode": "route" if domain else "broadcast",
                "controlled": True,
            },
        )
        control = ExecutionControl.create_root(
            root,
            timeout_seconds=run_timeout,
            registry=self._control_registry,
            trace_recorder=self._trace_recorder,
        )
        try:
            with control, execution_scope(root, started.event_id), lifecycle_scope(
                control
            ), lifecycle_policy_scope(self._lifecycle_policy):
                if domain:
                    result, terminal_event = self._route_to_agent(
                        domain,
                        input_data,
                        emotional_state,
                        location_id,
                        execution_context=root,
                        cause_id=started.event_id,
                        _return_event=True,
                    )
                else:
                    result, terminal_event = self._broadcast(
                        input_data,
                        emotional_state,
                        location_id,
                        execution_context=root,
                        cause_id=started.event_id,
                        _return_event=True,
                    )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            # Not a stopped run: these mean the process itself is being
            # interrupted or torn down, which is not this framework's to
            # report as a structured ExecutionResult. Cleanup already ran
            # via the `with control` block above unwinding; propagating
            # here just lets the interpreter's own shutdown continue.
            raise
        except BaseException as error:
            status, reason_code = self._status_for(error)
            self._trace_recorder.record(
                root,
                kind=_TRACE_TERMINAL_BY_STATUS[status],
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status=status.value,
                cause_id=stop_cause_id(
                    self._trace_recorder,
                    root.trace_id,
                    (
                        getattr(error, "trace_event_id", None)
                        or started.event_id
                    ),
                    error,
                ),
                reason_codes=(reason_code,),
                attributes={"error_type": type(error).__name__},
            )
            return ExecutionResult.failure(
                error,
                state=self._finish_state(
                    control, status, reason_code=reason_code
                ),
                trace_id=root.trace_id,
                run_id=root.run_id,
            )
        self._trace_recorder.record(
            root,
            kind="trace.completed",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="completed",
            cause_id=terminal_event.event_id,
        )
        return ExecutionResult.success(
            result,
            state=self._finish_state(
                control, ExecutionStatus.SUCCESS, completed_units=1,
                total_units=1,
            ),
            trace_id=root.trace_id,
            run_id=root.run_id,
        )

    def process_traced(
        self,
        input_data: dict[str, Any],
        domain: str | None = None,
        emotional_state: str = "unknown",
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
    ) -> TracedResult[dict[str, Any]]:
        """Process input and return the result with explicit trace identity."""
        root = execution_context or ExecutionContext.create()
        result = self.process(
            input_data,
            domain=domain,
            emotional_state=emotional_state,
            location_id=location_id,
            execution_context=root,
        )
        return TracedResult(result=result, trace_id=root.trace_id, context=root)

    def _broadcast(
        self,
        input_data: dict[str, Any],
        emotional_state: str,
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
        _return_event: bool = False,
    ) -> Any:
        """
        Broadcast input to all registered agents and collect results.
        Agents that are blocked by the EthicalFilter have their error recorded.
        """
        parent = execution_context or current_execution_context() or ExecutionContext.create()
        broadcast_event = self._trace_recorder.record(
            parent,
            kind="broadcast.started",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="started",
            cause_id=cause_id or current_trace_cause_id(),
            reason_codes=("route.broadcast",),
            attributes={"domain_count": len(self._agents)},
        )
        last_cause_id = broadcast_event.event_id
        results: dict[str, Any] = {}
        for domain in self._agents:
            try:
                results[domain], route_terminal = self._route_to_agent(
                    domain,
                    input_data,
                    emotional_state,
                    location_id,
                    execution_context=parent,
                    cause_id=broadcast_event.event_id,
                    _return_event=True,
                )
                last_cause_id = route_terminal.event_id
            except SETTEthicalFilterRejectedError as e:
                results[domain] = {"blocked": True, "reason": str(e)}
                last_cause_id = e.trace_event_id or last_cause_id
                logger.warning(
                    "[Orchestrator] Agent '%s' blocked during broadcast.",
                    self._agents[domain].name,
                )
            except (SETTCancelledError, SETTTimeoutError) as error:
                # A broadcast that was stopped was not a broadcast that
                # broke. Recording it as "failed" would put the one
                # distinction this version exists to make back where it
                # was, at the level where an application actually reads it.
                cancelled = isinstance(error, SETTCancelledError)
                broadcast_stopped = self._trace_recorder.record(
                    parent,
                    kind=(
                        "broadcast.cancelled"
                        if cancelled
                        else "broadcast.timed_out"
                    ),
                    component_type="orchestrator",
                    component_name=self.__class__.__name__,
                    status="cancelled" if cancelled else "timed_out",
                    cause_id=stop_cause_id(
                        self._trace_recorder,
                        parent.trace_id,
                        (
                            getattr(error, "trace_event_id", None)
                            or last_cause_id
                        ),
                        error,
                    ),
                    reason_codes=(stop_reason_code(error),),
                    attributes={"domain_count": len(self._agents)},
                )
                try:
                    error.trace_event_id = broadcast_stopped.event_id
                except (AttributeError, TypeError):
                    pass
                raise
            except Exception as error:
                broadcast_failed = self._trace_recorder.record(
                    parent,
                    kind="broadcast.failed",
                    component_type="orchestrator",
                    component_name=self.__class__.__name__,
                    status="failed",
                    cause_id=(
                        getattr(error, "trace_event_id", None)
                        or last_cause_id
                    ),
                    reason_codes=("component.exception",),
                    attributes={"error_type": type(error).__name__},
                )
                try:
                    error.trace_event_id = broadcast_failed.event_id
                except (AttributeError, TypeError):
                    pass
                raise
        broadcast_completed = self._trace_recorder.record(
            parent,
            kind="broadcast.completed",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="completed",
            cause_id=last_cause_id,
            attributes={"domain_count": len(self._agents)},
        )
        if _return_event:
            return results, broadcast_completed
        return results

    def read_universal_memory(self) -> dict[str, Any]:
        """
        Read the current state of universal memory.
        Returns a snapshot of everything all agents have published.
        """
        return self._universal_memory.read_all()

    # ── Environmental context (multi-instance coordination) ──────────────────

    def publish_environmental_context(
        self,
        risk_level: RiskLevel,
        location_id: str = "global",
        source_domain: str = "orchestrator",
        message: str = "",
    ) -> EnvironmentalContext:
        """
        Publish an environmental risk level to a shared location slot.

        Called when an agent detects that the environment around the user
        has reached a notable risk level. Other SETT instances in the same
        location will read this and adjust their behavior.

        No personal data is published: only the RiskLevel, location,
        and the source domain that triggered it.

        Args:
            risk_level: The RiskLevel to publish for this location.
            location_id: Identifier of the shared space (e.g. "store_42").
            source_domain: Which agent domain triggered this (e.g. "health").
            message: Optional description. Must NOT contain personal data.

        Returns:
            The EnvironmentalContext that was published.
        """
        ctx = EnvironmentalContext(
            risk_level=risk_level,
            location_id=location_id,
            source_domain=source_domain,
            message=message,
        )
        self._universal_memory.publish_environmental_context(ctx)
        logger.info(
            "[Orchestrator] Environmental context published: %s @ %s",
            risk_level, location_id,
        )
        return ctx

    def read_environmental_context(
        self, location_id: str = "global"
    ) -> EnvironmentalContext | None:
        """
        Read the current EnvironmentalContext for a location.

        Returns None if no context has been published for this location.
        """
        return self._universal_memory.read_environmental_context(location_id)

    def read_all_environmental_contexts(self) -> dict[str, EnvironmentalContext]:
        """Return all published EnvironmentalContexts, keyed by location_id."""
        return self._universal_memory.read_all_environmental_contexts()

    # ── Native pipelines ─────────────────────────────────────────────────────

    def run_pipeline(
        self,
        steps: list[PipelineStep | str],
        input_data: dict[str, Any],
        emotional_state: str = "unknown",
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
    ) -> PipelineResult:
        """
        Run an ordered sequence of stages, each handled by a different
        registered agent, with explicit data flow between stages.

        This is a NEW, additive capability: process() (route-to-one /
        broadcast-to-all) is unchanged. Each stage executes through the
        exact same path as routed processing: same propagation of
        emotional_state and location_id, same EthicalFilter evaluation
        on publish, same audit log entries.

        Three guarantees define the mechanism:

        1. **Memory isolation between stages.** Each stage's input is
           passed explicitly: the previous stage's output, optionally
           reshaped by the step's ``transform``: never read from
           universal memory. Agents keep their own PrivateMemory and
           never see another stage's intermediate reasoning.

        2. **Fail-closed configuration.** All stage domains are
           validated before the first stage runs; an empty pipeline or
           an unknown domain raises before any side effect. A transform
           that returns a non-dict raises SETTConfigurationError at that
           stage.

        3. **Rejection handling as part of the mechanism.** If the
           EthicalFilter rejects a stage, that agent publishes nothing
           (the filter raises before the write), the remaining stages
           are skipped, and the rejection is returned EXPLICITLY in
           ``PipelineResult.rejection``: with the structured fields
           (action, score, threshold, principle, reasoning) taken from
           the exception's attributes. It is never written to, nor
           meant to be read from, universal memory.

        Args:
            steps: Ordered stages. Each element is a PipelineStep, or a
                   plain domain string (shorthand for
                   ``PipelineStep(domain)``).
            input_data: The pipeline's original input. The first stage
                        receives it as-is unless its transform says
                        otherwise; transforms of later stages also
                        receive it as their first argument.
            emotional_state: Propagated to every stage, same as process().
            location_id: Propagated to every stage, same as process().

        Returns:
            A PipelineResult with one StageOutcome per stage, the final
            output when completed, and the RejectionOutcome when halted.

        Raises:
            SETTConfigurationError: Empty pipeline, or a transform
                returned a non-dict.
            SETTAgentNotFoundError: A stage's domain has no registered
                agent (raised before any stage runs).
        """
        from sett.core_ruler.execution_result import ExecutionStatus
        from sett.core_ruler.pipeline import (
            PipelineResult,
            PipelineStep,
            RejectionOutcome,
            SkipCause,
            StageOutcome,
        )
        from sett.exceptions import SETTConfigurationError

        if execution_context is not None and not isinstance(
            execution_context, ExecutionContext
        ):
            raise SETTValidationError(
                "execution_context must be an ExecutionContext or None."
            )
        root = execution_context or ExecutionContext.create()
        self._last_trace_id = root.trace_id
        trace_started = self._trace_recorder.record(
            root,
            kind="trace.started",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="started",
            reason_codes=("route.pipeline",),
            attributes={"mode": "pipeline", "stage_count": len(steps)},
        )
        pipeline_context = root.derive()
        pipeline_started = self._trace_recorder.record(
            pipeline_context,
            kind="pipeline.started",
            component_type="pipeline",
            component_name="native_pipeline",
            status="started",
            cause_id=trace_started.event_id,
            attributes={"stage_count": len(steps)},
        )

        last_pipeline_event_id = pipeline_started.event_id
        with execution_scope(root, trace_started.event_id):
            try:
                if not steps:
                    raise SETTConfigurationError(
                        "run_pipeline() requires at least one step. "
                        "An empty pipeline is a configuration error, not a no-op."
                    )

                # Normalize and validate the WHOLE pipeline before executing
                # any stage: configuration failures cannot produce effects.
                normalized: list[PipelineStep] = []
                for raw_step in steps:
                    if isinstance(raw_step, PipelineStep):
                        step = raw_step
                    elif isinstance(raw_step, str):
                        step = PipelineStep(domain=raw_step)
                    else:
                        raise SETTConfigurationError(
                            "Pipeline steps must be PipelineStep instances "
                            "or domain strings."
                        )
                    if not isinstance(step.domain, str) or not step.domain:
                        raise SETTConfigurationError(
                            "Pipeline stage domains must be non-empty strings."
                        )
                    normalized.append(step)
                for step in normalized:
                    self.get_agent(step.domain)

                outcomes: list[StageOutcome] = []
                prev_output: dict[str, Any] | None = None
                rejection: RejectionOutcome | None = None
                # v0.12.0: which stage halted the pipeline, so a skipped
                # stage can name the dependency that made it unreachable
                # instead of only reporting that it never ran.
                skip_cause: SkipCause | None = None

                for index, step in enumerate(normalized):
                    stage_context = pipeline_context.derive()
                    stage_started = self._trace_recorder.record(
                        stage_context,
                        kind="pipeline.stage_started",
                        component_type="pipeline_stage",
                        component_name=step.domain,
                        status="started",
                        cause_id=last_pipeline_event_id,
                        attributes={"index": index, "domain": step.domain},
                    )
                    if rejection is not None:
                        outcomes.append(
                            StageOutcome(
                                domain=step.domain,
                                status="skipped",
                                reason_code=(
                                    "pipeline.previous_stage_rejected"
                                ),
                                skip_cause=skip_cause,
                            )
                        )
                        stage_skipped = self._trace_recorder.record(
                            stage_context,
                            kind="pipeline.stage_skipped",
                            component_type="pipeline_stage",
                            component_name=step.domain,
                            status="skipped",
                            cause_id=stage_started.event_id,
                            reason_codes=(
                                "pipeline.previous_stage_rejected",
                            ),
                            attributes={"index": index},
                        )
                        last_pipeline_event_id = stage_skipped.event_id
                        continue

                    try:
                        # Explicit stage data flow: never through shared memory.
                        if step.transform is not None:
                            stage_input = step.transform(input_data, prev_output)
                            if not isinstance(stage_input, dict):
                                raise SETTConfigurationError(
                                    f"Pipeline stage '{step.domain}': transform "
                                    "must return a dict, got "
                                    f"{type(stage_input).__name__}."
                                )
                        else:
                            stage_input = input_data if index == 0 else prev_output

                        logger.debug(
                            "[Orchestrator] Pipeline stage %d -> '%s'",
                            index,
                            step.domain,
                        )
                        result, route_terminal = self._route_to_agent(
                            step.domain,
                            stage_input,
                            emotional_state,
                            location_id,
                            execution_context=stage_context,
                            cause_id=stage_started.event_id,
                            _return_event=True,
                        )
                    except SETTEthicalFilterRejectedError as error:
                        rejection = RejectionOutcome.from_error(
                            step.domain, error
                        )
                        skip_cause = SkipCause(
                            domain=step.domain,
                            run_id=stage_context.run_id,
                            reason_code="policy.reject",
                        )
                        outcomes.append(
                            StageOutcome(
                                domain=step.domain,
                                status="rejected",
                                rejection=rejection,
                                execution_status=ExecutionStatus.REJECTED,
                                reason_code="policy.reject",
                            )
                        )
                        stage_rejected = self._trace_recorder.record(
                            stage_context,
                            kind="pipeline.stage_rejected",
                            component_type="pipeline_stage",
                            component_name=step.domain,
                            status="rejected",
                            cause_id=(
                                error.trace_event_id
                                or stage_started.event_id
                            ),
                            reason_codes=("policy.reject",),
                            attributes={"index": index},
                        )
                        last_pipeline_event_id = stage_rejected.event_id
                        logger.warning(
                            "[Orchestrator] Pipeline halted at stage %d "
                            "('%s'): EthicalFilter rejected.",
                            index,
                            step.domain,
                        )
                        continue
                    except Exception as error:
                        stage_failed = self._trace_recorder.record(
                            stage_context,
                            kind="pipeline.stage_failed",
                            component_type="pipeline_stage",
                            component_name=step.domain,
                            status="failed",
                            cause_id=(
                                getattr(error, "trace_event_id", None)
                                or stage_started.event_id
                            ),
                            reason_codes=("component.exception",),
                            attributes={
                                "index": index,
                                "error_type": type(error).__name__,
                            },
                        )
                        try:
                            error.trace_event_id = stage_failed.event_id
                        except (AttributeError, TypeError):
                            pass
                        raise

                    outcomes.append(
                        StageOutcome(
                            domain=step.domain,
                            status="completed",
                            output=result,
                            execution_status=ExecutionStatus.SUCCESS,
                        )
                    )
                    stage_completed = self._trace_recorder.record(
                        stage_context,
                        kind="pipeline.stage_completed",
                        component_type="pipeline_stage",
                        component_name=step.domain,
                        status="completed",
                        cause_id=route_terminal.event_id,
                        attributes={"index": index},
                    )
                    last_pipeline_event_id = stage_completed.event_id
                    prev_output = result

                completed = rejection is None
                result = PipelineResult(
                    completed=completed,
                    steps=tuple(outcomes),
                    output=prev_output if completed else None,
                    rejection=rejection,
                )
                pipeline_terminal = self._trace_recorder.record(
                    pipeline_context,
                    kind=(
                        "pipeline.completed"
                        if completed else "pipeline.rejected"
                    ),
                    component_type="pipeline",
                    component_name="native_pipeline",
                    status="completed" if completed else "rejected",
                    cause_id=last_pipeline_event_id,
                    reason_codes=() if completed else ("policy.reject",),
                )
                self._trace_recorder.record(
                    root,
                    kind="trace.completed",
                    component_type="orchestrator",
                    component_name=self.__class__.__name__,
                    status="completed" if completed else "rejected",
                    cause_id=pipeline_terminal.event_id,
                )
                return result
            except Exception as error:
                pipeline_failed = self._trace_recorder.record(
                    pipeline_context,
                    kind="pipeline.failed",
                    component_type="pipeline",
                    component_name="native_pipeline",
                    status="failed",
                    cause_id=(
                        getattr(error, "trace_event_id", None)
                        or last_pipeline_event_id
                    ),
                    reason_codes=("component.exception",),
                    attributes={"error_type": type(error).__name__},
                )
                trace_failed = self._trace_recorder.record(
                    root,
                    kind="trace.failed",
                    component_type="orchestrator",
                    component_name=self.__class__.__name__,
                    status="failed",
                    cause_id=pipeline_failed.event_id,
                    reason_codes=("component.exception",),
                    attributes={"error_type": type(error).__name__},
                )
                try:
                    error.trace_event_id = trace_failed.event_id
                except (AttributeError, TypeError):
                    pass
                raise

    def run_pipeline_controlled(
        self,
        steps: list[PipelineStep | str],
        input_data: dict[str, Any],
        emotional_state: str = "unknown",
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult[PipelineResult]:
        """Run a pipeline under cancellation and per-stage deadline control.

        Added alongside ``run_pipeline()``, which is untouched.

        Partial work is reported as partial. If two stages of four
        completed and the third was cancelled, the result is ``PARTIAL``
        with ``completed_units=2`` - not ``CANCELLED``, which would throw
        away the two that really did finish, and not ``SUCCESS``, which
        would hide the two that did not. ``result.value`` still carries the
        full ``PipelineResult``, so the completed stages' outputs remain
        available without unwrapping.

        The stages after the one that stopped are reported the way a
        dependency failure is reported anywhere else in SETT: ``skipped``,
        with a ``skip_cause`` naming the stage that made them unreachable,
        and with no ``execution_status`` at all. They were not cancelled.
        Nobody asked anything of them.

        Args:
            steps: Ordered stages, as in ``run_pipeline()``. A
                ``PipelineStep`` may carry its own ``timeout_seconds``.
            input_data: The pipeline's original input.
            emotional_state: Propagated to every stage.
            location_id: Propagated to every stage.
            execution_context: Optional root identity; pass one to keep the
                ``run_id`` you can later cancel.
            timeout_seconds: Optional deadline for the whole pipeline.

        Returns:
            An ExecutionResult wrapping the PipelineResult.
        """
        from sett.core_ruler.pipeline import (
            PipelineResult,
            PipelineStep,
            RejectionOutcome,
            SkipCause,
            StageOutcome,
        )
        from sett.exceptions import SETTConfigurationError

        if execution_context is not None and not isinstance(
            execution_context, ExecutionContext
        ):
            raise SETTValidationError(
                "execution_context must be an ExecutionContext or None."
            )
        root = execution_context or ExecutionContext.create()
        self._last_trace_id = root.trace_id
        run_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._lifecycle_policy.run_timeout_seconds
        )
        trace_started = self._trace_recorder.record(
            root,
            kind="trace.started",
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status="started",
            reason_codes=("route.pipeline",),
            attributes={
                "mode": "pipeline",
                "stage_count": len(steps),
                "controlled": True,
            },
        )
        root_control = ExecutionControl.create_root(
            root,
            timeout_seconds=run_timeout,
            registry=self._control_registry,
            trace_recorder=self._trace_recorder,
        )

        outcomes: list[StageOutcome] = []
        completed_units = 0
        rejection: RejectionOutcome | None = None
        skip_cause: SkipCause | None = None
        stop_error: BaseException | None = None
        prev_output: dict[str, Any] | None = None
        pipeline_context = root.derive()

        try:
            with root_control, execution_scope(
                root, trace_started.event_id
            ), lifecycle_scope(root_control), lifecycle_policy_scope(
                self._lifecycle_policy
            ):
                pipeline_started = self._trace_recorder.record(
                    pipeline_context,
                    kind="pipeline.started",
                    component_type="pipeline",
                    component_name="native_pipeline",
                    status="started",
                    cause_id=trace_started.event_id,
                    attributes={"stage_count": len(steps)},
                )
                last_pipeline_event_id = pipeline_started.event_id
                pipeline_control = root_control.child(context=pipeline_context)

                with pipeline_control, execution_scope(
                    pipeline_context, pipeline_started.event_id
                ), lifecycle_scope(pipeline_control):
                    if not steps:
                        raise SETTConfigurationError(
                            "run_pipeline_controlled() requires at least one "
                            "step. An empty pipeline is a configuration "
                            "error, not a no-op."
                        )

                    # Whole pipeline validated before any stage runs, so a
                    # misconfiguration cannot produce partial side effects.
                    normalized: list[PipelineStep] = []
                    for raw_step in steps:
                        if isinstance(raw_step, PipelineStep):
                            step = raw_step
                        elif isinstance(raw_step, str):
                            step = PipelineStep(domain=raw_step)
                        else:
                            raise SETTConfigurationError(
                                "Pipeline steps must be PipelineStep "
                                "instances or domain strings."
                            )
                        if not isinstance(step.domain, str) or not step.domain:
                            raise SETTConfigurationError(
                                "Pipeline stage domains must be non-empty "
                                "strings."
                            )
                        normalized.append(step)
                    for step in normalized:
                        self.get_agent(step.domain)

                    for index, step in enumerate(normalized):
                        stage_context = pipeline_context.derive()
                        stage_started = self._trace_recorder.record(
                            stage_context,
                            kind="pipeline.stage_started",
                            component_type="pipeline_stage",
                            component_name=step.domain,
                            status="started",
                            cause_id=last_pipeline_event_id,
                            attributes={
                                "index": index,
                                "domain": step.domain,
                            },
                        )
                        if skip_cause is not None:
                            outcomes.append(
                                StageOutcome(
                                    domain=step.domain,
                                    status="skipped",
                                    reason_code="pipeline.not_executed",
                                    skip_cause=skip_cause,
                                )
                            )
                            stage_skipped = self._trace_recorder.record(
                                stage_context,
                                kind="pipeline.stage_skipped",
                                component_type="pipeline_stage",
                                component_name=step.domain,
                                status="skipped",
                                cause_id=stage_started.event_id,
                                reason_codes=("pipeline.not_executed",),
                                attributes={"index": index},
                            )
                            last_pipeline_event_id = stage_skipped.event_id
                            continue

                        stage_timeout = (
                            step.timeout_seconds
                            if step.timeout_seconds is not None
                            else self._lifecycle_policy.stage_timeout(
                                step.domain
                            )
                        )
                        stage_control = pipeline_control.child(
                            context=stage_context,
                            timeout_seconds=stage_timeout,
                        )
                        try:
                            with stage_control, lifecycle_scope(stage_control):
                                stage_control.checkpoint()
                                if step.transform is not None:
                                    stage_input = step.transform(
                                        input_data, prev_output
                                    )
                                    if not isinstance(stage_input, dict):
                                        raise SETTConfigurationError(
                                            f"Pipeline stage '{step.domain}': "
                                            "transform must return a dict, "
                                            f"got {type(stage_input).__name__}."
                                        )
                                else:
                                    stage_input = (
                                        input_data if index == 0 else prev_output
                                    )
                                stage_control.checkpoint()
                                result, route_terminal = self._route_to_agent(
                                    step.domain,
                                    stage_input,
                                    emotional_state,
                                    location_id,
                                    execution_context=stage_context,
                                    cause_id=stage_started.event_id,
                                    _return_event=True,
                                )
                                stage_control.checkpoint()
                        except (SETTCancelledError, SETTTimeoutError) as error:
                            cancelled = isinstance(error, SETTCancelledError)
                            stop_error = error
                            status = (
                                ExecutionStatus.CANCELLED
                                if cancelled
                                else ExecutionStatus.TIMED_OUT
                            )
                            reason_code = stop_reason_code(error)
                            outcomes.append(
                                StageOutcome(
                                    domain=step.domain,
                                    status=(
                                        "cancelled" if cancelled else "timed_out"
                                    ),
                                    execution_status=status,
                                    reason_code=reason_code,
                                )
                            )
                            # The stage that stopped is the dependency the
                            # later ones will name. It was cancelled or it
                            # timed out; they simply never became reachable.
                            skip_cause = SkipCause(
                                domain=step.domain,
                                run_id=stage_context.run_id,
                                reason_code=reason_code,
                            )
                            stage_stopped = self._trace_recorder.record(
                                stage_context,
                                kind=(
                                    "pipeline.stage_cancelled"
                                    if cancelled
                                    else "pipeline.stage_timed_out"
                                ),
                                component_type="pipeline_stage",
                                component_name=step.domain,
                                status=(
                                    "cancelled" if cancelled else "timed_out"
                                ),
                                cause_id=stop_cause_id(
                                    self._trace_recorder,
                                    stage_context.trace_id,
                                    (
                                        getattr(error, "trace_event_id", None)
                                        or stage_started.event_id
                                    ),
                                    error,
                                ),
                                reason_codes=(reason_code,),
                                attributes={"index": index},
                            )
                            last_pipeline_event_id = stage_stopped.event_id
                            continue
                        except SETTEthicalFilterRejectedError as error:
                            stop_error = error
                            rejection = RejectionOutcome.from_error(
                                step.domain, error
                            )
                            skip_cause = SkipCause(
                                domain=step.domain,
                                run_id=stage_context.run_id,
                                reason_code="policy.reject",
                            )
                            outcomes.append(
                                StageOutcome(
                                    domain=step.domain,
                                    status="rejected",
                                    rejection=rejection,
                                    execution_status=ExecutionStatus.REJECTED,
                                    reason_code="policy.reject",
                                )
                            )
                            stage_rejected = self._trace_recorder.record(
                                stage_context,
                                kind="pipeline.stage_rejected",
                                component_type="pipeline_stage",
                                component_name=step.domain,
                                status="rejected",
                                cause_id=(
                                    error.trace_event_id
                                    or stage_started.event_id
                                ),
                                reason_codes=("policy.reject",),
                                attributes={"index": index},
                            )
                            last_pipeline_event_id = stage_rejected.event_id
                            logger.warning(
                                "[Orchestrator] Pipeline halted at stage %d "
                                "('%s'): EthicalFilter rejected.",
                                index,
                                step.domain,
                            )
                            continue
                        except Exception as error:
                            stage_failed = self._trace_recorder.record(
                                stage_context,
                                kind="pipeline.stage_failed",
                                component_type="pipeline_stage",
                                component_name=step.domain,
                                status="failed",
                                cause_id=(
                                    getattr(error, "trace_event_id", None)
                                    or stage_started.event_id
                                ),
                                reason_codes=("component.exception",),
                                attributes={
                                    "index": index,
                                    "error_type": type(error).__name__,
                                },
                            )
                            try:
                                error.trace_event_id = stage_failed.event_id
                            except (AttributeError, TypeError):
                                pass
                            raise

                        completed_units += 1
                        outcomes.append(
                            StageOutcome(
                                domain=step.domain,
                                status="completed",
                                output=result,
                                execution_status=ExecutionStatus.SUCCESS,
                            )
                        )
                        stage_completed = self._trace_recorder.record(
                            stage_context,
                            kind="pipeline.stage_completed",
                            component_type="pipeline_stage",
                            component_name=step.domain,
                            status="completed",
                            cause_id=route_terminal.event_id,
                            attributes={"index": index},
                        )
                        last_pipeline_event_id = stage_completed.event_id
                        prev_output = result

                    status, reason_code = self._pipeline_status(
                        stop_error, completed_units
                    )
                    pipeline_terminal = self._trace_recorder.record(
                        pipeline_context,
                        kind=_PIPELINE_TERMINAL_BY_STATUS[status],
                        component_type="pipeline",
                        component_name="native_pipeline",
                        status=status.value,
                        cause_id=last_pipeline_event_id,
                        reason_codes=(
                            () if reason_code is None else (reason_code,)
                        ),
                        attributes={
                            "completed_units": completed_units,
                            "total_units": len(normalized),
                        },
                    )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            # As in process_controlled(): not a stopped run, propagate.
            raise
        except BaseException as error:
            # Reached only for configuration errors and component
            # exceptions: the stage-level stops above are recorded as
            # outcomes, not raised.
            failure_status, failure_reason = self._status_for(error)
            pipeline_failed = self._trace_recorder.record(
                pipeline_context,
                kind="pipeline.failed",
                component_type="pipeline",
                component_name="native_pipeline",
                status="failed",
                cause_id=(
                    getattr(error, "trace_event_id", None)
                    or trace_started.event_id
                ),
                reason_codes=("component.exception",),
                attributes={"error_type": type(error).__name__},
            )
            trace_failed = self._trace_recorder.record(
                root,
                kind=_TRACE_TERMINAL_BY_STATUS[failure_status],
                component_type="orchestrator",
                component_name=self.__class__.__name__,
                status=failure_status.value,
                cause_id=pipeline_failed.event_id,
                reason_codes=(failure_reason,),
                attributes={"error_type": type(error).__name__},
            )
            try:
                error.trace_event_id = trace_failed.event_id
            except (AttributeError, TypeError):
                pass
            # A stage that raises unwinds through this handler instead of
            # through the normal PARTIAL path below, but the stages before
            # it completed just as really as they would have under a
            # cancellation - the outcome of a genuine failure is not
            # informative enough to justify discarding them. Same shape as
            # the PipelineResult built below, so a caller does not need to
            # know which of the two paths produced it.
            partial_result = (
                PipelineResult(
                    completed=False,
                    steps=tuple(outcomes),
                    output=None,
                    rejection=rejection,
                )
                if outcomes
                else None
            )
            return ExecutionResult.failure(
                error,
                state=self._finish_state(
                    root_control,
                    failure_status,
                    reason_code=failure_reason,
                    completed_units=completed_units,
                    total_units=len(steps) or None,
                ),
                trace_id=root.trace_id,
                run_id=root.run_id,
                value=partial_result,
            )

        completed = status is ExecutionStatus.SUCCESS
        pipeline_result = PipelineResult(
            completed=completed,
            steps=tuple(outcomes),
            output=prev_output if completed else None,
            rejection=rejection,
        )
        self._trace_recorder.record(
            root,
            kind=(
                "trace.completed"
                if status in (ExecutionStatus.SUCCESS, ExecutionStatus.PARTIAL)
                else _TRACE_TERMINAL_BY_STATUS.get(status, "trace.failed")
            ),
            component_type="orchestrator",
            component_name=self.__class__.__name__,
            status=status.value,
            cause_id=pipeline_terminal.event_id,
        )
        state = self._finish_state(
            root_control,
            status,
            reason_code=reason_code,
            completed_units=completed_units,
            total_units=len(normalized),
        )
        if completed:
            return ExecutionResult.success(
                pipeline_result,
                state=state,
                trace_id=root.trace_id,
                run_id=root.run_id,
            )
        return ExecutionResult.failure(
            stop_error
            or SETTError(reason_code or "pipeline.did_not_complete"),
            state=state,
            trace_id=root.trace_id,
            run_id=root.run_id,
            value=pipeline_result,
        )

    @staticmethod
    def _pipeline_status(
        stop_error: BaseException | None, completed_units: int
    ) -> tuple[ExecutionStatus, str | None]:
        """Decide a pipeline's terminal status from what stopped it.

        Work that really finished is never thrown away: a run whose third
        stage was cancelled after two completed is PARTIAL, not CANCELLED.
        Only a run that produced nothing at all reports the bare stop.
        """
        if stop_error is None:
            return ExecutionStatus.SUCCESS, None
        reason_code = (
            "policy.reject"
            if isinstance(stop_error, SETTEthicalFilterRejectedError)
            else stop_reason_code(stop_error)
        )
        if isinstance(stop_error, SETTEthicalFilterRejectedError):
            return ExecutionStatus.REJECTED, reason_code
        if completed_units > 0:
            return ExecutionStatus.PARTIAL, reason_code
        if isinstance(stop_error, SETTCancelledError):
            return ExecutionStatus.CANCELLED, reason_code
        return ExecutionStatus.TIMED_OUT, reason_code

    # ── Audit and introspection ──────────────────────────────────────────────

    def get_ethical_audit_log(self) -> list[dict[str, Any]]:
        """
        Return the full audit log of all ethical decisions.
        Useful for compliance, debugging, and reporting.
        """
        return self._ethical_filter.get_audit_log()

    def verify_ethical_audit_log(self) -> bool:
        """
        Verify the sequence and hash chain of the ethical audit log
        returned by get_ethical_audit_log().

        Delegates to EthicalFilter.verify_audit_log(): the same
        tamper-evidence guarantee added in v0.8.0, made reachable from
        the orchestrator, the entry point every example in the docs
        already uses, instead of requiring access to the private
        `_ethical_filter` attribute.
        """
        return self._ethical_filter.verify_audit_log()

    def get_trace(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """Return immutable events for one execution trace."""
        return self._trace_recorder.get_trace(trace_id)

    def export_trace(
        self,
        trace_id: str,
        *,
        view: str = "sanitized",
    ) -> tuple[dict[str, Any], ...]:
        """Export a defensive privacy-safe trace view."""
        return self._trace_recorder.export_trace(trace_id, view=view)

    def register_trace_exporter(self, exporter: TraceExporter) -> None:
        """Register a callable that receives each sanitized event."""
        self._trace_recorder.register_exporter(exporter)

    def verify_traces(self, trace_id: str | None = None) -> bool:
        """Verify recorder hash integrity and causal references.

        Delegates to TraceRecorder.verify(): the integrity check is
        global across every trace this recorder has recorded, not
        scoped to `trace_id` (F28, defensive audit #2). See
        TraceRecorder.verify()'s docstring for the full semantics.
        """
        return self._trace_recorder.verify(trace_id)

    @property
    def last_trace_id(self) -> str | None:
        """Most recently started trace; intended for sequential debugging."""
        return self._last_trace_id

    @property
    def registered_domains(self) -> list[str]:
        """List of all registered agent domains."""
        return list(self._agents.keys())

    def __repr__(self) -> str:
        return f"SETTOrchestrator(domains={self.registered_domains})"
