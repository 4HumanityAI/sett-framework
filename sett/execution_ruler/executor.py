"""
SETT Framework: Execution Ruler
==============================
The only component in a SETT system that is allowed to perform real
side effects (send a message, call an external API, contact emergency
services, move money, etc.).

Agents and experts never call the real client library directly. They
describe intent as an Action and submit it here. The Executor:

    1. Receives the Action
    2. Resolves its handler, failing closed when no route exists
    3. Runs a routable action through the EthicalFilter (Layer 1 action /
       Layer 2 user / Layer 3 environment: the same three-layer system used
       everywhere else in SETT)
    4. When configured: resolves authority, obtains a policy grant, and
       verifies and consumes its receipt
    5. If approved and authorized: invokes the handler for that action_type:
       this is the ONLY place the real side effect happens
    6. Returns the handler's result upward, so the Orchestrator (and from
       there, the application built on SETT) can incorporate
       it into the system's response

Submitting an Action through the Executor provides the structural gate:
the expert never receives a reference to the real client. The Executor
also fails closed when it is not attached to an orchestrator, when the
shared memory has no EthicalFilter, when approval is rejected, or when no
handler is registered.

``SETTAgent.propose_action()`` remains available for evaluating an action
without executing it. It now also requires orchestrator wiring; standalone
silent approval is not permitted.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sett.audit_ruler.chain import append_chained_entry, verify_chain
from sett.authorization_ruler._validation import canonical_json_bytes
from sett.authorization_ruler.contracts import AuthorizationRequestResolver
from sett.authorization_ruler.models import (
    ApprovalInput,
    AuthorizationReceipt,
    AuthorizationRequest,
    ExecutionMode,
)
from sett.authorization_ruler.ruler import AuthorizationRuler
from sett.core_ruler.action import Action
from sett.core_ruler.execution_context import (
    ExecutionContext,
    current_execution_context,
    current_trace_cause_id,
    execution_scope,
)
from sett.core_ruler.execution_result import (
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
)
from sett.core_ruler.idempotency import (
    ActionAttempt,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    action_fingerprint,
    key_digest,
    result_digest,
)
from sett.core_ruler.lifecycle import (
    CancellationToken,
    ExecutionControl,
    LifecyclePolicy,
    cooperative_sleep,
    current_execution_control,
    lifecycle_scope,
)
from sett.core_ruler.resilience import (
    ErrorClass,
    RetryPolicy,
    classify_error,
    is_retryable,
)
from sett.exceptions import (
    SETTAuthorizationConfigurationError,
    SETTAuthorizationError,
    SETTCancelledError,
    SETTConfigurationError,
    SETTError,
    SETTEthicalFilterRejectedError,
    SETTIdempotencyConflictError,
    SETTTimeoutError,
    SETTValidationError,
)

if TYPE_CHECKING:
    from sett.audit_ruler.trace import TraceRecorder
    from sett.memory_ruler.universal import UniversalMemory
    from sett.risk_ruler.risk_profile import RiskProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HandlerContext:
    """What a lifecycle-aware handler is told about its own attempt.

    The two fields that change what a correct handler does are
    ``idempotency_key`` and ``timeout_seconds``: the key is what the
    handler passes through to a provider that supports idempotent
    requests, which is the only place an "at most once" guarantee can
    actually be enforced, and the timeout is the time genuinely left in
    this run rather than a number the handler invented.

    ``attempt_number`` counts attempts against ``idempotency_key`` as the
    store itself does; it is 1 on a fresh key and keeps climbing across
    separate calls to ``submit_controlled()`` that reuse the same key,
    not just across retries inside one call. Without a key, there is no
    cross-submission identity to count against, so it falls back to this
    call's own attempt count.
    """

    action_id: str
    idempotency_key: str | None
    attempt_id: str
    attempt_number: int
    execution_context: ExecutionContext | None
    cancellation_token: CancellationToken | None
    timeout_seconds: float | None

    def throw_if_cancelled(self) -> None:
        """Convenience checkpoint for handlers that loop or poll."""
        if self.cancellation_token is not None:
            self.cancellation_token.throw_if_cancelled()


_ACTIVE_HANDLER_CONTEXT: ContextVar[HandlerContext | None] = ContextVar(
    "sett_handler_context", default=None
)


def current_handler_context() -> HandlerContext | None:
    """The handler context for the attempt running in this thread, if any."""
    return _ACTIVE_HANDLER_CONTEXT.get()


class SETTExecutor:
    """
    Receives Action proposals from agents, evaluates them through the
    EthicalFilter, and executes only the approved ones via a registered
    handler.

    Usage:
        executor = SETTExecutor()
        executor.register_handler("send_sms", lambda payload: sms_client.send(**payload))

        orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
        orchestrator.register_executor(executor)
        orchestrator.register_agent(MyAgent())

        # Inside MyAgent.process():
        #     self.submit_action("send_sms", {"to": "...", "message": "..."})
        # → evaluated by the filter; only runs sms_client.send(...) if approved.
    """

    def __init__(
        self,
        *,
        idempotency_store: IdempotencyStore | None = None,
        authorization_ruler: AuthorizationRuler | None = None,
        authorization_request_resolver: AuthorizationRequestResolver | None = None,
    ) -> None:
        """
        Args:
            idempotency_store: Where keyed operations are remembered. The
                default is an in-process store, which is enough for tests
                and single-process applications and explicitly not enough
                to claim idempotency across restarts, so inject a durable
                one for that.
            authorization_ruler: Optional concrete authorization boundary.
                Supplying it enables authorization for every registered
                handler; no per-action bypass exists.
            authorization_request_resolver: Application-owned resolver for
                authoritative principal, resource, scope, purpose, labels,
                and context facts. It must be supplied together with
                ``authorization_ruler``.
        """
        if (authorization_ruler is None) != (
            authorization_request_resolver is None
        ):
            raise SETTValidationError(
                "authorization_ruler and authorization_request_resolver must "
                "be supplied together."
            )
        if authorization_ruler is not None and not isinstance(
            authorization_ruler, AuthorizationRuler
        ):
            raise SETTValidationError(
                "authorization_ruler must be an AuthorizationRuler."
            )
        if authorization_request_resolver is not None and not isinstance(
            authorization_request_resolver, AuthorizationRequestResolver
        ):
            raise SETTValidationError(
                "authorization_request_resolver must implement "
                "AuthorizationRequestResolver."
            )
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._controlled_handlers: dict[
            str, Callable[[dict[str, Any], HandlerContext], Any]
        ] = {}
        self._universal_memory: UniversalMemory | None = None
        self._audit_log: list[dict[str, Any]] = []
        self._trace_recorder: TraceRecorder | None = None
        self._lifecycle_policy: LifecyclePolicy | None = None
        self._idempotency_store: IdempotencyStore = (
            idempotency_store or InMemoryIdempotencyStore()
        )
        self._authorization_ruler = authorization_ruler
        self._authorization_request_resolver = authorization_request_resolver

    def attach_universal_memory(self, memory: UniversalMemory) -> None:
        """
        Called by the orchestrator during register_executor().
        Gives the executor access to evaluate_action() (which forwards to
        the EthicalFilter) and to the EnvironmentalContext of a location.
        Do not call this manually.
        """
        self._universal_memory = memory

    def attach_trace_recorder(self, recorder: TraceRecorder) -> None:
        """Attach the orchestrator-owned recorder."""
        self._trace_recorder = recorder

    def attach_lifecycle_policy(self, policy: LifecyclePolicy) -> None:
        """
        Called by the orchestrator during register_executor(). Gives
        submit_controlled() a policy-derived default timeout, per
        action_type, when the caller does not pass one explicitly. Do not
        call this manually.
        """
        self._lifecycle_policy = policy

    def register_handler(
        self, action_type: str, handler: Callable[[dict[str, Any]], Any]
    ) -> None:
        """
        Register the function that actually performs a given action type.
        This is the ONLY code in the entire system allowed to run this
        side effect.

        Args:
            action_type: Must match the action_type used in Action /
                         SETTAgent.submit_action() calls (e.g. "send_sms").
                         Must be non-empty, and not already registered
                         (as either kind of handler).
            handler: A callable that receives the Action's payload dict
                     and performs the real side effect. Its return value
                     is passed back to the caller of submit().

        Raises:
            SETTValidationError: If action_type is empty/whitespace, or
                handler is not callable (F06, defensive audit #2:
                register_controlled_handler() already validated this;
                the legacy method did not).
            SETTConfigurationError: If action_type already has a handler
                registered, of either kind (F05/F07, defensive audit #2:
                a silent overwrite here used to also let the two handler
                maps drift out of sync with each other; see
                register_controlled_handler()'s docstring).
        """
        self._register_handler_guard(action_type, handler)
        self._handlers[action_type] = handler

    def _register_handler_guard(
        self, action_type: str, handler: Callable[..., Any]
    ) -> None:
        if not isinstance(action_type, str) or not action_type.strip():
            raise SETTValidationError(
                "action_type must be a non-empty string."
            )
        if not callable(handler):
            raise SETTValidationError("A handler must be callable.")
        if action_type in self._handlers or action_type in self._controlled_handlers:
            raise SETTConfigurationError(
                f"A handler is already registered for action_type "
                f"{action_type!r}. Every action type owns exactly one "
                f"handler; call executor.unregister_handler({action_type!r}) "
                f"first if you intend to replace it."
            )

    def unregister_handler(self, action_type: str) -> None:
        """Remove a previously registered handler, of either kind.

        Safe to call even if nothing was registered for this action_type.
        The counterpart that makes the "already registered" guard above
        practical rather than a one-way door.
        """
        self._handlers.pop(action_type, None)
        self._controlled_handlers.pop(action_type, None)

    def _execution_configuration_error(
        self, action: Action
    ) -> SETTConfigurationError | None:
        """Return a fail-closed error before any governance or attempt work."""
        if self._universal_memory is None:
            return SETTConfigurationError(
                "SETTExecutor is not attached to a SETTOrchestrator. "
                "Register it with orchestrator.register_executor(executor) "
                "before submitting any action.",
                reason_code="executor.not_attached",
                verdict="block",
                action_type=action.action_type,
                action_id=action.action_id,
            )
        if not self._universal_memory.has_ethical_filter:
            return SETTConfigurationError(
                "SETTExecutor cannot run because its UniversalMemory has no "
                "EthicalFilter. Real-world actions fail closed by default.",
                reason_code="ethical_filter.not_configured",
                verdict="block",
                action_type=action.action_type,
                action_id=action.action_id,
            )
        return None

    @property
    def authorization_enabled(self) -> bool:
        """Whether every handler invocation requires authorization."""

        return self._authorization_ruler is not None

    def _authorization_preflight_error(
        self,
        action: Action,
        approval_inputs: Sequence[ApprovalInput],
    ) -> SETTConfigurationError | None:
        """Reject authorization inputs that cannot be enforced honestly."""

        if self._authorization_ruler is None:
            if approval_inputs:
                return SETTConfigurationError(
                    "approval_inputs were supplied, but Authorization Ruler "
                    "is not configured on this executor.",
                    reason_code="authorization.not_configured",
                    verdict="block",
                    action_type=action.action_type,
                    action_id=action.action_id,
                )
            return None
        if self._authorization_request_resolver is None:
            return SETTConfigurationError(
                "Authorization Ruler is enabled without an authorization "
                "request resolver.",
                reason_code="authorization.resolver_missing",
                verdict="block",
                action_type=action.action_type,
                action_id=action.action_id,
            )
        return None

    def _record_authorization_rejected(
        self,
        *,
        action: Action,
        context: ExecutionContext | None,
        cause_id: str | None,
        error: BaseException,
    ) -> None:
        """Record a content-free authorization rejection when tracing exists."""

        if context is None or self._trace_recorder is None:
            return
        reason_code = getattr(
            error,
            "reason_code",
            "authorization.system_failure",
        )
        event = self._trace_recorder.record(
            context,
            kind="authorization.receipt_rejected",
            component_type="authorization",
            component_name="authorization_ruler",
            status="rejected",
            cause_id=cause_id,
            reason_codes=(reason_code,),
            attributes={
                "action_id": action.action_id,
                "error_type": type(error).__name__,
            },
        )
        try:
            error.trace_event_id = event.event_id
        except (AttributeError, TypeError):
            pass

    def _authorize_effect(
        self,
        *,
        action: Action,
        approved_payload: dict[str, Any],
        context: ExecutionContext,
        cause_id: str | None,
        approval_inputs: Sequence[ApprovalInput],
    ) -> Any:
        """Resolve, issue, and consume authority immediately before effect."""

        ruler = self._authorization_ruler
        resolver = self._authorization_request_resolver
        if ruler is None or resolver is None:
            return None
        mode = (
            ExecutionMode.CONTROLLED
            if current_handler_context() is not None
            else ExecutionMode.PLAIN
        )
        try:
            request = resolver.resolve(
                action_id=action.action_id,
                trace_id=context.trace_id,
                action_type=action.action_type,
                payload=deepcopy(approved_payload),
                execution_mode=mode,
                idempotency_key=action.idempotency_key,
            )
            if not isinstance(request, AuthorizationRequest):
                raise SETTAuthorizationConfigurationError(
                    "Authorization request resolver returned an invalid value.",
                    reason_code="authorization.request_resolution_invalid",
                )
            resolved_payload = request.payload_copy()
            if (
                request.action_id != action.action_id
                or request.trace_id != context.trace_id
                or request.action_type != action.action_type
                or request.execution_mode is not mode
                or request.idempotency_key != action.idempotency_key
                or resolved_payload != approved_payload
                # Python equality conflates booleans, integers, integral
                # floats, and signed zero. The local resolver must preserve
                # the snapshot, even where the receipt fingerprint deliberately
                # normalizes numeric equivalents for transport across processes.
                or canonical_json_bytes(resolved_payload, field="resolved payload")
                != canonical_json_bytes(approved_payload, field="approved payload")
            ):
                raise SETTAuthorizationError(
                    "Resolved authorization request does not match the SETT "
                    "execution identity or detached payload.",
                    reason_code="authorization.request_mismatch",
                )
            outcome = ruler.authorize(
                request,
                approval_inputs=approval_inputs,
            )
            receipt = outcome.receipt
            if receipt is None:
                reason_code = (
                    "authorization.denied"
                    if outcome.decision.verdict.value == "deny"
                    else "authorization.approval_required"
                )
                raise SETTAuthorizationError(
                    "Authorization did not grant effect authority.",
                    reason_code=reason_code,
                )
            if not isinstance(receipt, AuthorizationReceipt):
                raise SETTAuthorizationConfigurationError(
                    "Authorization grant did not carry a valid receipt.",
                    reason_code="authorization.receipt_invalid",
                )
            ruler.verify_and_consume(
                receipt,
                request,
                approval_inputs=approval_inputs,
            )
        except (SETTAuthorizationError, SETTValidationError) as error:
            self._record_authorization_rejected(
                action=action,
                context=context,
                cause_id=cause_id,
                error=error,
            )
            raise
        except Exception as error:
            normalized = SETTAuthorizationConfigurationError(
                "Authorization request resolution failed closed.",
                reason_code="authorization.request_resolution_failed",
            )
            self._record_authorization_rejected(
                action=action,
                context=context,
                cause_id=cause_id,
                error=normalized,
            )
            raise normalized from error

        if self._trace_recorder is None:
            return None
        return self._trace_recorder.record(
            context,
            kind="authorization.receipt_verified",
            component_type="authorization",
            component_name="authorization_ruler",
            status="verified",
            cause_id=cause_id,
            attributes={
                "action_id": action.action_id,
                "receipt_id": receipt.receipt_id,
            },
        )

    def _record_action_proposed(
        self,
        action: Action,
        context: ExecutionContext | None,
        cause_id: str | None,
    ) -> Any:
        """Record one proposal without inspecting or copying its payload."""
        if context is None or self._trace_recorder is None:
            return None
        return self._trace_recorder.record(
            context,
            kind="action.proposed",
            component_type="executor",
            component_name=action.action_type,
            status="proposed",
            cause_id=cause_id,
            attributes={
                "action_id": action.action_id,
                "action_type": action.action_type,
                "proposed_by": action.proposed_by,
            },
        )

    def _unroutable_error(
        self,
        action: Action,
        context: ExecutionContext | None,
        cause_id: str | None,
        proposed_event: Any = None,
    ) -> SETTConfigurationError:
        """Build the single normalized decision for an action with no route."""
        blocked_event = None
        if context is not None and self._trace_recorder is not None:
            blocked_event = self._trace_recorder.record(
                context,
                kind="action.blocked",
                component_type="executor",
                component_name=action.action_type,
                status="blocked",
                cause_id=(
                    proposed_event.event_id if proposed_event else cause_id
                ),
                reason_codes=("handler.not_registered",),
                attributes={
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "verdict": "block",
                    "decision_source": "routing",
                },
            )
        return SETTConfigurationError(
            f"No handler registered for action_type '{action.action_type}'. "
            f"Call executor.register_handler({action.action_type!r}, your_function) "
            f"before submitting this kind of action. "
            f"Registered types: {list(self._handlers.keys())}",
            reason_code="handler.not_registered",
            verdict="block",
            action_type=action.action_type,
            action_id=action.action_id,
            trace_event_id=(
                blocked_event.event_id if blocked_event is not None else None
            ),
        )

    def submit(
        self,
        action: Action,
        emotional_state: str = "unknown",
        risk_profile: RiskProfile | None = None,
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> Any:
        """
        Evaluate an Action through the EthicalFilter and, if approved,
        execute its registered handler.

        Args:
            action: The proposed Action.
            emotional_state: Detected emotional state of the user.
            risk_profile: Optional three-pillar RiskProfile for this user.
            location_id: Used to look up the EnvironmentalContext (Layer 3)
                         for where this action is being proposed.
            approval_inputs: Opaque additional-authority evidence. It is
                routed only to registered verifiers when authorization is
                configured; supplying it otherwise fails closed.

        Returns:
            Whatever the registered handler returns.

        Raises:
            SETTEthicalFilterRejectedError: If the EthicalFilter blocks
                the action. The handler is NEVER called in that case.
            SETTConfigurationError: If the Executor is unattached, its
                memory has no EthicalFilter, or no handler is registered for
                this action type. Every configuration error fails closed.
        """
        configuration_error = self._execution_configuration_error(action)
        if configuration_error is not None:
            raise configuration_error
        authorization_error = self._authorization_preflight_error(
            action,
            approval_inputs,
        )
        if authorization_error is not None:
            raise authorization_error

        # Resolve the route once, before policy evaluation or payload copying.
        # An action with no handler cannot reach the world, so evaluating it
        # would spend policy work on an impossible effect and record a verdict
        # for something that never had an execution route. Holding the resolved
        # callable also keeps routing consistent if registration changes later.
        action_type = action.action_type
        handler = self._handlers.get(action_type)
        if handler is not None and self.authorization_enabled:
            # Exporters and resolvers are application code. Keep the selected
            # route and its authorization identity together even if a callback
            # changes the caller's mutable Action. Payload copying still waits
            # until route preflight succeeds below.
            action = Action(
                action_type=action_type,
                payload=action.payload,
                proposed_by=action.proposed_by,
                timestamp=action.timestamp,
                action_id=action.action_id,
                idempotency_key=action.idempotency_key,
            )
        parent = execution_context or current_execution_context()
        trace_cause = cause_id or current_trace_cause_id()
        action_context = (
            parent.derive()
            if parent is not None
            else ExecutionContext.create()
            if self.authorization_enabled
            else None
        )
        proposed_event = self._record_action_proposed(
            action, action_context, trace_cause
        )

        if handler is None:
            raise self._unroutable_error(
                action,
                action_context,
                trace_cause,
                proposed_event,
            )

        # One read of the caller's Action after the route is known to exist.
        #
        # The payload used to be read from the live object twice: once to
        # build the filter's context and again to call the handler. Between
        # those two reads another thread holding the same Action could
        # change it, so policy approved one payload and the handler
        # performed a different one, and the fingerprint recorded for
        # idempotency belonged to the approved one, not the executed one.
        #
        # "The caller should not mutate it" is not an answer this framework
        # accepts anywhere else: the guarantee that an expert cannot reach
        # the real client is structural, not a rule people are asked to
        # follow, and "what policy approved is what runs" has to be the
        # same kind of guarantee. Reading once removes the second read
        # there was a race against, so no lock is needed.
        approved_payload = deepcopy(action.payload)

        environmental_context = self._universal_memory.read_environmental_context(
            location_id
        )
        # This raises SETTEthicalFilterRejectedError if rejected:
        # the handler below is only reached if it does not raise.
        try:
            verdict, policy_event = self._universal_memory._evaluate_action_with_trace(
                action=action.action_type,
                context={
                    # A further copy of the snapshot, so an analyzer that
                    # mutates what it is handed cannot reach the payload
                    # the handler is about to receive.
                    **deepcopy(approved_payload),
                    "_proposed_by": action.proposed_by,
                },
                emotional_state=emotional_state,
                risk_profile=risk_profile,
                environmental_context=environmental_context,
                execution_context=action_context,
                cause_id=proposed_event.event_id if proposed_event else trace_cause,
            )
        except Exception as error:
            if action_context is not None and self._trace_recorder is not None:
                rejected_event = self._trace_recorder.record(
                    action_context,
                    kind="action.rejected",
                    component_type="executor",
                    component_name=action.action_type,
                    status="rejected",
                    cause_id=(
                        getattr(error, "trace_event_id", None)
                        or (
                            proposed_event.event_id
                            if proposed_event else trace_cause
                        )
                    ),
                    reason_codes=(
                        "policy.reject"
                        if isinstance(error, SETTEthicalFilterRejectedError)
                        else "component.exception",
                    ),
                    attributes={
                        "action_id": action.action_id,
                        "error_type": type(error).__name__,
                    },
                )
                try:
                    error.trace_event_id = rejected_event.event_id
                except (AttributeError, TypeError):
                    pass
            raise

        approved_event = None
        authorized_event = None
        if action_context is not None and self._trace_recorder is not None:
            approved_event = self._trace_recorder.record(
                action_context,
                kind="action.approved",
                component_type="executor",
                component_name=action.action_type,
                status="approved",
                cause_id=(
                    policy_event.event_id if policy_event is not None else None
                ) or (
                    proposed_event.event_id if proposed_event else trace_cause
                ),
                reason_codes=(f"policy.{verdict.value}",),
                attributes={"action_id": action.action_id},
            )
        authorization_event = None
        if self.authorization_enabled:
            assert action_context is not None
            authorization_event = self._authorize_effect(
                action=action,
                approved_payload=approved_payload,
                context=action_context,
                cause_id=(
                    approved_event.event_id
                    if approved_event is not None
                    else policy_event.event_id
                    if policy_event is not None
                    else proposed_event.event_id
                    if proposed_event is not None
                    else trace_cause
                ),
                approval_inputs=approval_inputs,
            )
        if action_context is not None and self._trace_recorder is not None:
            try:
                authorized_event = self._trace_recorder.record(
                    action_context,
                    kind="handler.authorized",
                    component_type="handler",
                    component_name=action.action_type,
                    status="authorized",
                    cause_id=(
                        authorization_event.event_id
                        if authorization_event is not None
                        else approved_event.event_id
                    ),
                    attributes={"action_id": action.action_id},
                    effect_boundary=True,
                )
            except SETTConfigurationError as error:
                blocked_event = self._trace_recorder.record(
                    action_context,
                    kind="handler.blocked",
                    component_type="handler",
                    component_name=action.action_type,
                    status="blocked",
                    cause_id=(
                        getattr(error, "trace_event_id", None)
                        or approved_event.event_id
                    ),
                    reason_codes=("trace.exporter_failed",),
                    attributes={"action_id": action.action_id},
                )
                error.trace_event_id = blocked_event.event_id
                raise

        logger.info(
            "[Executor] Executing approved action '%s' (proposed by '%s')",
            action.action_type, action.proposed_by,
        )
        handler_event = None
        if action_context is not None and self._trace_recorder is not None:
            handler_event = self._trace_recorder.record(
                action_context,
                kind="handler.started",
                component_type="handler",
                component_name=action.action_type,
                status="started",
                cause_id=(
                    authorized_event.event_id
                    if authorized_event is not None
                    else approved_event.event_id
                ),
                attributes={"action_id": action.action_id},
            )
        try:
            if action_context is not None:
                with execution_scope(
                    action_context,
                    handler_event.event_id if handler_event else trace_cause,
                ):
                    result = handler(deepcopy(approved_payload))
            else:
                result = handler(deepcopy(approved_payload))
        except (SETTCancelledError, SETTTimeoutError) as error:
            # A handler that stopped because it was cancelled, or because
            # it ran out of time, did not fail. Both are reachable from a
            # handler today: HandlerContext.throw_if_cancelled() and the
            # adapters' own checkpoints raise exactly these, and folding
            # them into handler.failed would report the distinction this
            # version exists to draw as though it did not exist.
            if action_context is not None and self._trace_recorder is not None:
                cancelled = isinstance(error, SETTCancelledError)
                stopped_event = self._trace_recorder.record(
                    action_context,
                    kind=(
                        "handler.cancelled" if cancelled else "handler.timed_out"
                    ),
                    component_type="handler",
                    component_name=action.action_type,
                    status="cancelled" if cancelled else "timed_out",
                    cause_id=(
                        handler_event.event_id if handler_event else trace_cause
                    ),
                    reason_codes=(
                        (error.reason or "execution.cancelled")
                        if cancelled
                        else "execution.deadline_exceeded",
                    ),
                    attributes={"action_id": action.action_id},
                )
                try:
                    error.trace_event_id = stopped_event.event_id
                except (AttributeError, TypeError):
                    pass
            raise
        except Exception as error:
            if action_context is not None and self._trace_recorder is not None:
                failed_event = self._trace_recorder.record(
                    action_context,
                    kind="handler.failed",
                    component_type="handler",
                    component_name=action.action_type,
                    status="failed",
                    cause_id=(
                        handler_event.event_id if handler_event else trace_cause
                    ),
                    reason_codes=("component.exception",),
                    attributes={"error_type": type(error).__name__},
                )
                try:
                    error.trace_event_id = failed_event.event_id
                except (AttributeError, TypeError):
                    pass
            raise

        append_chained_entry(self._audit_log, {
            "action_type": action.action_type,
            "proposed_by": action.proposed_by,
            "timestamp": action.timestamp,
            "executed": True,
            "action_id": action.action_id,
        })
        if action_context is not None and self._trace_recorder is not None:
            self._trace_recorder.record(
                action_context,
                kind="handler.completed",
                component_type="handler",
                component_name=action.action_type,
                status="completed",
                cause_id=handler_event.event_id if handler_event else trace_cause,
                attributes={
                    "action_id": action.action_id,
                    "result_type": type(result).__name__,
                },
            )
        return result

    # ── Controlled submission (v0.12.0) ──────────────────────────────────

    @property
    def idempotency_store(self) -> IdempotencyStore:
        """Where this executor remembers keyed operations."""
        return self._idempotency_store

    @property
    def controlled_action_types(self) -> list[str]:
        """Action types whose handler receives a HandlerContext."""
        return list(self._controlled_handlers)

    def register_controlled_handler(
        self,
        action_type: str,
        handler: Callable[[dict[str, Any], HandlerContext], Any],
    ) -> None:
        """Register a handler that receives its attempt's HandlerContext.

        Registered alongside the legacy handlers rather than replacing
        them: a handler written for v0.11.0 keeps working untouched, and
        keeps its documented limits, with no automatic retries and no strong
        timeout guarantee, because nothing about its signature lets SETT
        hand it a deadline or a token.

        A controlled handler reached through the plain ``submit()`` fails
        closed rather than running with invented values. Degrading the
        guarantees silently is the one outcome worse than refusing: the
        handler would believe it had an idempotency key and a deadline
        that nobody actually established.

        Raises:
            SETTValidationError: If action_type is empty/whitespace, or
                handler is not callable.
            SETTConfigurationError: If action_type already has a handler
                registered, of either kind.
        """
        self._register_handler_guard(action_type, handler)

        def _controlled_shim(payload: dict[str, Any]) -> Any:
            context = current_handler_context()
            if context is None:
                raise SETTConfigurationError(
                    f"Handler for action_type '{action_type}' is a "
                    "controlled handler and needs the attempt context that "
                    "only submit_controlled() establishes. Call "
                    "executor.submit_controlled(action) instead of "
                    "submit(), or register it with register_handler() if "
                    "it does not need the context."
                )
            return handler(payload, context)

        self._controlled_handlers[action_type] = handler
        self._handlers[action_type] = _controlled_shim

    def submit_controlled(
        self,
        action: Action,
        emotional_state: str = "unknown",
        risk_profile: RiskProfile | None = None,
        location_id: str = "global",
        *,
        execution_context: ExecutionContext | None = None,
        cause_id: str | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float | None = None,
        approval_inputs: Sequence[ApprovalInput] = (),
    ) -> ExecutionResult[Any]:
        """Submit an action under lifecycle control and idempotency.

        Performs a static readiness and route preflight, then wraps
        ``submit()`` rather than reimplementing the executable path. The
        filter evaluation, handler dispatch, and every trace event at the
        effect boundary still come from one audited implementation. This
        method adds checkpoints, an idempotency reservation, attempt records,
        and an optional retry only after the preflight succeeds.

        Nothing here raises for a stopped or failed *action*: a
        rejection, a timeout, a cancellation and a component failure all
        come back as an ``ExecutionResult`` whose status distinguishes
        them, with the real exception preserved for ``unwrap()``.

        Two things still raise, and deliberately. A failure of the
        infrastructure this method depends on, such as an injected
        ``IdempotencyStore`` that cannot answer, for instance, is not an
        outcome of the action; it is the inability to determine one, and
        turning it into a FAILED result would report a decision that was
        never made. And ``KeyboardInterrupt``/``SystemExit`` propagate:
        they are the process being told to stop, not this action failing.

        Args:
            action: The proposed Action. Give it an ``idempotency_key`` to
                make it retryable and replay-protected.
            emotional_state: As in ``submit()``.
            risk_profile: As in ``submit()``.
            location_id: As in ``submit()``.
            execution_context: Optional identity for this submission.
            cause_id: Optional causal parent event.
            retry_policy: Attempts and backoff. The default is a single
                attempt.
            timeout_seconds: Optional deadline for this submission.
            approval_inputs: Opaque additional-authority evidence routed only
                to registered verifiers when authorization is configured.

        Returns:
            An ExecutionResult carrying the handler's return value on
            success.
        """
        policy = retry_policy or RetryPolicy()
        store = self._idempotency_store
        parent_control = current_execution_control()
        parent_context = execution_context or current_execution_context()
        if parent_context is None and parent_control is not None:
            # Fall back to the ambient control's own context rather than
            # starting a fresh root. A caller that established lifecycle
            # management but no execution scope still means this
            # submission to belong to that run, and deriving from anywhere
            # else would silently place it outside the tree its canceller
            # is addressing, since the effect boundary is the last place to
            # quietly lose cancellability.
            parent_context = parent_control.context
        trace_cause = cause_id or current_trace_cause_id()
        submission_context = (
            parent_context.derive()
            if parent_context is not None
            else ExecutionContext.create()
        )
        # An explicit timeout_seconds always wins. Otherwise, fall back to
        # the LifecyclePolicy's per-action_type handler timeout, resolved
        # by the orchestrator this executor is registered with, following the same
        # "explicit argument overrides the level default" rule
        # run_pipeline_controlled() already applies to stages.
        effective_timeout = timeout_seconds
        if effective_timeout is None and self._lifecycle_policy is not None:
            effective_timeout = self._lifecycle_policy.handler_timeout(
                action.action_type
            )
        if parent_control is not None and (
            submission_context.parent_id == parent_control.context.run_id
        ):
            control = parent_control.child(
                context=submission_context, timeout_seconds=effective_timeout
            )
        else:
            control = ExecutionControl.create_root(
                submission_context,
                timeout_seconds=effective_timeout,
                trace_recorder=self._trace_recorder,
            )
        trace_id = submission_context.trace_id

        # Static execution prerequisites come before retry configuration,
        # payload snapshots, fingerprints, idempotency reservations and
        # attempt events. None of those describes real progress when the
        # action has no executable route or the governance boundary itself
        # is unavailable.
        configuration_error = self._execution_configuration_error(action)
        if configuration_error is not None:
            return self._submission_failure(
                configuration_error,
                control=control,
                trace_id=trace_id,
                reason_code=configuration_error.reason_code,
            )
        authorization_error = self._authorization_preflight_error(
            action,
            approval_inputs,
        )
        if authorization_error is not None:
            return self._submission_failure(
                authorization_error,
                control=control,
                trace_id=trace_id,
                reason_code=authorization_error.reason_code,
            )
        if action.action_type not in self._handlers:
            proposed_event = self._record_action_proposed(
                action, submission_context, trace_cause
            )
            routing_error = self._unroutable_error(
                action,
                submission_context,
                trace_cause,
                proposed_event,
            )
            return self._submission_failure(
                routing_error,
                control=control,
                trace_id=trace_id,
                reason_code=routing_error.reason_code,
            )

        if policy.retries_enabled and not action.idempotency_key:
            # Retrying an action nobody can identify out in the world is
            # how one request becomes two effects.
            return self._submission_failure(
                SETTConfigurationError(
                    "A retry policy needs an idempotency_key on the action: "
                    "without one, SETT cannot tell a provider that the "
                    "second attempt is the same operation as the first, and "
                    "retrying would risk performing the effect twice."
                ),
                control=control,
                trace_id=trace_id,
                reason_code="retry.key_required",
            )

        try:
            with control, lifecycle_scope(control):
                outcome = self._submit_attempts(
                    action,
                    emotional_state,
                    risk_profile,
                    location_id,
                    control=control,
                    submission_context=submission_context,
                    trace_cause=trace_cause,
                    policy=policy,
                    store=store,
                    approval_inputs=approval_inputs,
                )
        except (SETTCancelledError, SETTTimeoutError) as error:
            # Raised by the control's own entry checkpoint, before any
            # attempt: the action never reached the filter.
            outcome = self._submission_failure(
                error, control=control, trace_id=trace_id
            )
        # M-01 fix: every state built above was constructed from inside
        # `with control`, before control.__exit__() ever ran run_cleanup().
        # control.cleanup_errors was necessarily empty at that point
        # regardless of what actually happened, so a cleanup hook that
        # failed while unwinding this submission was silently dropped from
        # the result. `with control` has now exited - cleanup has settled -
        # so the outcome is re-stamped with what really happened.
        if outcome.state.cleanup_errors != control.cleanup_errors:
            outcome = replace(
                outcome,
                state=replace(
                    outcome.state, cleanup_errors=control.cleanup_errors
                ),
            )
        return outcome

    def _submit_attempts(
        self,
        action: Action,
        emotional_state: str,
        risk_profile: RiskProfile | None,
        location_id: str,
        *,
        control: ExecutionControl,
        submission_context: ExecutionContext,
        trace_cause: str | None,
        policy: RetryPolicy,
        store: IdempotencyStore,
        approval_inputs: Sequence[ApprovalInput],
    ) -> ExecutionResult[Any]:
        """Run the attempt loop for one action inside its control."""
        trace_id = submission_context.trace_id
        key = action.idempotency_key
        # The controlled path takes its snapshot here, one level above
        # submit(), so the fingerprint and every attempt below describe
        # the same payload. Fingerprinting the caller's live Action and
        # then letting submit() read it again left the two able to
        # disagree: the key would be reserved against one payload while a
        # different one was performed under it, which is the idempotency
        # half of the same read-it-once problem.
        action = replace(action, payload=deepcopy(action.payload))
        fingerprint = action_fingerprint(action) if key else None
        last_error: BaseException | None = None
        attempts_made = 0

        for attempt_number in range(1, policy.max_attempts + 1):
            try:
                control.checkpoint()
            except (SETTCancelledError, SETTTimeoutError) as error:
                return self._submission_failure(
                    error,
                    control=control,
                    trace_id=trace_id,
                    attempt_count=max(attempts_made, 1),
                )

            if key is not None:
                try:
                    reservation = store.reserve(
                        key=key, fingerprint=fingerprint, trace_id=trace_id
                    )
                except SETTIdempotencyConflictError as error:
                    self._record_idempotency(
                        submission_context, "idempotency.conflict", key, error
                    )
                    return self._submission_failure(
                        error,
                        control=control,
                        trace_id=trace_id,
                        reason_code="idempotency.conflict",
                        attempt_count=max(attempts_made, 1),
                    )
                if reservation.already_succeeded:
                    self._record_idempotency(
                        submission_context, "idempotency.replayed", key
                    )
                    return self._replayed_result(
                        control=control,
                        trace_id=trace_id,
                        record=reservation.record,
                    )
                if not reservation.acquired:
                    kind = (
                        "idempotency.in_progress"
                        if reservation.in_progress_elsewhere
                        else "idempotency.conflict"
                    )
                    self._record_idempotency(submission_context, kind, key)
                    return self._submission_failure(
                        SETTError(
                            f"Idempotency key {key!r} is held by another "
                            f"attempt (status "
                            f"{reservation.record.status.value})."
                        ),
                        control=control,
                        trace_id=trace_id,
                        reason_code=kind,
                        attempt_count=max(attempts_made, 1),
                    )
                self._record_idempotency(
                    submission_context, "idempotency.reserved", key
                )

            attempts_made = attempt_number
            # M-02 (defensive audit #3): the loop variable above counts
            # attempts within THIS call to submit_controlled() only, and
            # resets to 1 every time a caller resubmits the same action
            # after a retryable failure, which is exactly the case an
            # idempotency_key exists to identify as the same operation.
            # A caller retried [1, 1] where the store's own record already
            # said attempt_count=2: the number handed to the log and to
            # the handler was silently wrong the moment a retry crossed a
            # submission boundary. The store already keeps the
            # authoritative count for the key (bumped by reserve() on
            # every successful reservation, including this one), so this
            # reuses that instead of inventing a second field, following the same
            # "one source of truth" shape as validate_duration_seconds()
            # and the H-02 single-read fix. Without a key there is no
            # cross-submission identity to be authoritative about, so the
            # local count is the honest answer in that case.
            operation_attempt_number = (
                reservation.record.attempt_count
                if key is not None
                else attempt_number
            )
            attempt = ActionAttempt(
                attempt_id=str(uuid4()),
                action_id=action.action_id,
                idempotency_key=key or "",
                attempt_number=operation_attempt_number,
                trace_id=trace_id,
                started_at=datetime.now(timezone.utc).isoformat(),
                fingerprint=fingerprint,
            )
            self._log_attempt(store, attempt)
            attempt_event = self._record_attempt_event(
                submission_context,
                "handler.attempt_started",
                action,
                attempt,
                status="started",
                cause_id=trace_cause,
            )
            if key is not None:
                store.mark_in_progress(key=key)

            handler_context = HandlerContext(
                action_id=action.action_id,
                idempotency_key=key,
                attempt_id=attempt.attempt_id,
                attempt_number=operation_attempt_number,
                execution_context=submission_context,
                cancellation_token=control.token,
                timeout_seconds=control.remaining_seconds(),
            )
            token = _ACTIVE_HANDLER_CONTEXT.set(handler_context)
            try:
                result = self.submit(
                    action,
                    emotional_state=emotional_state,
                    risk_profile=risk_profile,
                    location_id=location_id,
                    execution_context=submission_context,
                    cause_id=(
                        attempt_event.event_id if attempt_event else trace_cause
                    ),
                    approval_inputs=approval_inputs,
                )
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                # Not a stopped or failed attempt: the process itself is
                # being interrupted or torn down. Retrying, or reporting
                # this as a structured ExecutionResult, would misrepresent
                # what actually happened. `finally` below still runs.
                raise
            except BaseException as error:  # noqa: BLE001
                last_error = error
                classification = classify_error(error, policy)
                retryable = is_retryable(error, policy)
                self._record_attempt_event(
                    submission_context,
                    "handler.attempt_failed",
                    action,
                    attempt,
                    status="failed",
                    cause_id=(
                        attempt_event.event_id if attempt_event else trace_cause
                    ),
                    reason_codes=(
                        "action.outcome_unknown"
                        if classification is ErrorClass.AMBIGUOUS
                        else "component.exception",
                    ),
                    error_type=type(error).__name__,
                )
                self._complete_attempt(
                    store,
                    attempt,
                    status="failed",
                    error_type=type(error).__name__,
                    retryable=retryable,
                )
                if key is not None:
                    store.mark_failed(key=key, retryable=retryable)
                if not retryable or attempt_number >= policy.max_attempts:
                    return self._submission_failure(
                        error,
                        control=control,
                        trace_id=trace_id,
                        reason_code=(
                            "action.outcome_unknown"
                            if classification is ErrorClass.AMBIGUOUS
                            else None
                        ),
                        attempt_count=attempt_number,
                    )
                backoff = policy.backoff_for(attempt_number)
                if backoff:
                    remaining = control.remaining_seconds()
                    if remaining is not None and remaining <= backoff:
                        # Sleeping past the deadline just to attempt again
                        # spends the caller's remaining time on nothing.
                        return self._submission_failure(
                            error,
                            control=control,
                            trace_id=trace_id,
                            reason_code="retry.deadline_exhausted",
                            attempt_count=attempt_number,
                        )
                    try:
                        cooperative_sleep(backoff, control.token)
                    except SETTCancelledError as cancelled:
                        return self._submission_failure(
                            cancelled,
                            control=control,
                            trace_id=trace_id,
                            attempt_count=attempt_number,
                        )
                continue
            finally:
                _ACTIVE_HANDLER_CONTEXT.reset(token)

            digest = result_digest(result)
            self._record_attempt_event(
                submission_context,
                "handler.attempt_completed",
                action,
                attempt,
                status="completed",
                cause_id=(
                    attempt_event.event_id if attempt_event else trace_cause
                ),
            )
            self._complete_attempt(store, attempt, status="completed")
            if key is not None:
                store.mark_succeeded(key=key, digest=digest)
            return ExecutionResult.success(
                result,
                state=self._submission_state(
                    control,
                    ExecutionStatus.SUCCESS,
                    attempt_count=attempt_number,
                ),
                trace_id=trace_id,
                run_id=submission_context.run_id,
            )

        return self._submission_failure(
            last_error or SETTError("action.no_attempt_made"),
            control=control,
            trace_id=trace_id,
            attempt_count=max(attempts_made, 1),
        )

    # ── Controlled submission helpers ────────────────────────────────────

    def _submission_state(
        self,
        control: ExecutionControl,
        status: ExecutionStatus,
        *,
        reason_code: str | None = None,
        attempt_count: int = 1,
    ) -> ExecutionState:
        now = datetime.now(timezone.utc)
        return ExecutionState(
            status=status,
            started_at=control.started_at or now,
            finished_at=control.finished_at or now,
            reason_code=reason_code,
            completed_units=1 if status is ExecutionStatus.SUCCESS else 0,
            total_units=1,
            attempt_count=attempt_count,
            cleanup_errors=control.cleanup_errors,
            duration_seconds=control.elapsed_seconds,
        )

    def _submission_failure(
        self,
        error: BaseException,
        *,
        control: ExecutionControl,
        trace_id: str,
        reason_code: str | None = None,
        attempt_count: int = 1,
    ) -> ExecutionResult[Any]:
        """Wrap a terminating error, keeping the four stop reasons apart."""
        if isinstance(error, SETTCancelledError):
            status = ExecutionStatus.CANCELLED
            code = reason_code or (error.reason or "execution.cancelled")
        elif isinstance(error, SETTTimeoutError):
            status = ExecutionStatus.TIMED_OUT
            code = reason_code or "action.outcome_unknown"
        elif isinstance(error, SETTEthicalFilterRejectedError):
            status = ExecutionStatus.REJECTED
            code = reason_code or "policy.reject"
        else:
            status = ExecutionStatus.FAILED
            code = reason_code or "component.exception"
        return ExecutionResult.failure(
            error,
            state=self._submission_state(
                control, status, reason_code=code, attempt_count=attempt_count
            ),
            trace_id=trace_id,
            run_id=control.context.run_id,
        )

    def _replayed_result(
        self,
        *,
        control: ExecutionControl,
        trace_id: str,
        record: Any,
    ) -> ExecutionResult[Any]:
        """Report an operation that already completed under this key.

        The value is deliberately ``None``: the store keeps a digest of
        the earlier result, never the result itself, for the same reason
        the attempt log keeps no payload. Reporting success without a
        value is honest; inventing one would not be.
        """
        return ExecutionResult(
            state=self._submission_state(
                control,
                ExecutionStatus.SUCCESS,
                reason_code="idempotency.replayed",
                attempt_count=record.attempt_count,
            ),
            trace_id=trace_id,
            run_id=control.context.run_id,
            value=None,
        )

    @staticmethod
    def _log_attempt(store: IdempotencyStore, attempt: ActionAttempt) -> None:
        recorder = getattr(store, "record_attempt", None)
        if callable(recorder):
            recorder(attempt)

    @staticmethod
    def _complete_attempt(
        store: IdempotencyStore,
        attempt: ActionAttempt,
        *,
        status: str,
        error_type: str | None = None,
        retryable: bool = False,
    ) -> None:
        completer = getattr(store, "complete_attempt", None)
        if callable(completer):
            completer(
                attempt.attempt_id,
                finished_at=datetime.now(timezone.utc).isoformat(),
                status=status,
                error_type=error_type,
                retryable=retryable,
            )

    def _record_attempt_event(
        self,
        context: ExecutionContext,
        kind: str,
        action: Action,
        attempt: ActionAttempt,
        *,
        status: str,
        cause_id: str | None = None,
        reason_codes: tuple[str, ...] = (),
        error_type: str | None = None,
    ) -> Any:
        if self._trace_recorder is None:
            return None
        attributes: dict[str, Any] = {
            "action_id": action.action_id,
            "action_type": action.action_type,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt.attempt_number,
        }
        if error_type is not None:
            attributes["error_type"] = error_type
        return self._trace_recorder.record(
            context,
            kind=kind,
            component_type="handler",
            component_name=action.action_type,
            status=status,
            cause_id=(
                cause_id or self._trace_recorder.last_event_id(context.run_id)
            ),
            reason_codes=reason_codes,
            attributes=attributes,
        )

    def _record_idempotency(
        self,
        context: ExecutionContext,
        kind: str,
        key: str,
        error: BaseException | None = None,
    ) -> None:
        if self._trace_recorder is None:
            return
        attributes: dict[str, Any] = {"idempotency_key_digest": key_digest(key)}
        if error is not None:
            attributes["error_type"] = type(error).__name__
        self._trace_recorder.record(
            context,
            kind=kind,
            component_type="executor",
            component_name="idempotency",
            status=kind.split(".", 1)[-1],
            cause_id=self._trace_recorder.last_event_id(context.run_id),
            attributes=attributes,
        )

    def get_audit_log(self) -> list[dict[str, Any]]:
        """
        Log of every action that was actually executed (i.e. approved by
        the filter AND had a registered handler). Rejected or unhandled
        actions do not appear here: they never ran.
        """
        return deepcopy(self._audit_log)

    def verify_audit_log(self) -> bool:
        """Verify sequence and hash links in the internal execution log."""
        return verify_chain(self._audit_log)

    @property
    def registered_action_types(self) -> list[str]:
        """Action types that currently have a handler registered."""
        return list(self._handlers.keys())

    def __repr__(self) -> str:
        return f"SETTExecutor(handlers={self.registered_action_types})"
