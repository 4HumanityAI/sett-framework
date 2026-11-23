"""A submission never borrows the handler context of another attempt."""
from __future__ import annotations

import pytest

from sett import EthicalFilter, SETTExecutor, SETTOrchestrator
from sett.core_ruler.action import Action
from sett.core_ruler.execution_result import ExecutionStatus
from sett.exceptions import SETTConfigurationError
from sett.execution_ruler.executor import current_handler_context


def _executor() -> SETTExecutor:
    orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
    executor = SETTExecutor()
    orchestrator.register_executor(executor)
    return executor


def _identity(context):
    return (context.action_id, context.idempotency_key, context.attempt_id)


def test_plain_submission_of_a_controlled_handler_fails_at_top_level():
    executor = _executor()
    runs: list = []
    executor.register_controlled_handler("inner", lambda payload, ctx: runs.append(ctx))

    with pytest.raises(SETTConfigurationError):
        executor.submit(Action("inner", {}))
    assert runs == []


def test_plain_submission_of_a_controlled_handler_fails_when_nested():
    executor = _executor()
    seen: dict = {}

    def inner(payload, ctx):
        seen["inner"] = _identity(ctx)
        return "inner-ran"

    def outer(payload, ctx):
        seen["outer_before"] = _identity(ctx)
        try:
            executor.submit(Action("inner", {}))
        except SETTConfigurationError as error:
            seen["nested_error"] = type(error).__name__
        seen["outer_after"] = _identity(current_handler_context())
        assert current_handler_context() is ctx
        return "outer-ran"

    executor.register_controlled_handler("outer", outer)
    executor.register_controlled_handler("inner", inner)

    result = executor.submit_controlled(Action("outer", {}, idempotency_key="outer-key"))

    assert result.status is ExecutionStatus.SUCCESS
    assert "inner" not in seen
    assert seen["nested_error"] == "SETTConfigurationError"
    assert seen["outer_after"] == seen["outer_before"]


def test_nested_plain_handler_runs_without_any_handler_context():
    executor = _executor()
    seen: dict = {}

    def plain(payload):
        seen["context_inside_plain"] = current_handler_context()
        return "plain-ran"

    def outer(payload, ctx):
        value = executor.submit(Action("plain", {}))
        seen["restored"] = current_handler_context() is ctx
        return value

    executor.register_handler("plain", plain)
    executor.register_controlled_handler("outer", outer)

    result = executor.submit_controlled(Action("outer", {}, idempotency_key="outer-key"))

    assert result.value == "plain-ran"
    assert seen["context_inside_plain"] is None
    assert seen["restored"] is True


def test_nested_controlled_submission_gets_its_own_identity():
    executor = _executor()
    seen: dict = {}

    def inner(payload, ctx):
        seen["inner"] = ctx
        return "inner-ran"

    def outer(payload, ctx):
        seen["outer"] = ctx
        inner_action = Action("inner", {}, idempotency_key="inner-key")
        seen["inner_action_id"] = inner_action.action_id
        nested = executor.submit_controlled(inner_action)
        seen["restored"] = current_handler_context() is ctx
        return nested.value

    executor.register_controlled_handler("outer", outer)
    executor.register_controlled_handler("inner", inner)
    outer_action = Action("outer", {}, idempotency_key="outer-key")

    result = executor.submit_controlled(outer_action)

    assert result.value == "inner-ran"
    inner_ctx, outer_ctx = seen["inner"], seen["outer"]
    assert inner_ctx.action_id == seen["inner_action_id"]
    assert inner_ctx.idempotency_key == "inner-key"
    assert outer_ctx.action_id == outer_action.action_id
    assert outer_ctx.idempotency_key == "outer-key"
    assert inner_ctx.attempt_id != outer_ctx.attempt_id
    assert seen["restored"] is True
    # The nested run stays inside the outer run's trace.
    assert inner_ctx.execution_context.trace_id == outer_ctx.execution_context.trace_id
    assert inner_ctx.execution_context.run_id != outer_ctx.execution_context.run_id


def test_outer_context_is_restored_when_the_nested_submission_raises():
    executor = _executor()
    seen: dict = {}

    def failing(payload):
        raise RuntimeError("nested failure")

    def outer(payload, ctx):
        with pytest.raises(RuntimeError):
            executor.submit(Action("failing", {}))
        seen["restored"] = current_handler_context() is ctx
        return "outer-ran"

    executor.register_handler("failing", failing)
    executor.register_controlled_handler("outer", outer)

    result = executor.submit_controlled(Action("outer", {}, idempotency_key="outer-key"))

    assert result.status is ExecutionStatus.SUCCESS
    assert seen["restored"] is True
    assert current_handler_context() is None
