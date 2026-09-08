"""Pin the guarantees a foreign caller is told it can rely on.

`docs/foreign_callers.md` makes five promises to code SETT does not own.
Each one is checked here against the public boundary, so a future change
that quietly weakens one fails the suite instead of the integrator.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from sett import (
    Action,
    EthicalFilter,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTExecutor,
    SETTOrchestrator,
)
from sett.ethics_ruler.ethic_kernel.rules import default_ruleset

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT_ROOT / "examples" / "foreign_tool_loop.py"

_REFUSED_ACTION = "delete all the user's savings"


def _governed() -> tuple[SETTExecutor, list[dict[str, Any]]]:
    ruleset = default_ruleset()
    ruleset.reject_threshold = 2.0
    ruleset.warn_threshold = 1.0
    effects: list[dict[str, Any]] = []
    executor = SETTExecutor()
    executor.register_handler("summarize the report", lambda payload: effects.append(payload) or "ok")
    executor.register_handler(_REFUSED_ACTION, lambda payload: effects.append(payload) or "done")
    SETTOrchestrator(ethical_filter=EthicalFilter(ruleset=ruleset)).register_executor(executor)
    return executor, effects


def test_the_boundary_accepts_plain_types_only() -> None:
    executor, effects = _governed()
    result = executor.submit(Action(action_type="summarize the report", payload={"length": "short"}))
    assert result == "ok"
    assert effects == [{"length": "short"}]


def test_a_refusal_is_an_exception_not_a_return_value() -> None:
    executor, effects = _governed()
    with pytest.raises(SETTEthicalFilterRejectedError):
        executor.submit(Action(action_type=_REFUSED_ACTION, payload={"confirm": True}))
    assert effects == []


def test_a_caller_that_ignores_the_result_still_cannot_proceed() -> None:
    executor, effects = _governed()
    attempted = 0
    for action_type in ("summarize the report", _REFUSED_ACTION, "summarize the report"):
        try:
            executor.submit(Action(action_type=action_type, payload={}))
        except SETTEthicalFilterRejectedError:
            pass
        attempted += 1
    assert attempted == 3
    assert len(effects) == 2


def test_an_unregistered_tool_fails_closed() -> None:
    executor, effects = _governed()
    with pytest.raises(SETTConfigurationError) as failure:
        executor.submit(Action(action_type="a tool nobody registered", payload={}))
    assert failure.value.reason_code == "handler.not_registered"
    assert effects == []


def test_an_unattached_executor_fails_closed() -> None:
    executor = SETTExecutor()
    executor.register_handler("anything", lambda payload: "done")
    with pytest.raises(SETTConfigurationError) as failure:
        executor.submit(Action(action_type="anything", payload={}))
    assert failure.value.reason_code == "executor.not_attached"


def test_the_example_imports_nothing_outside_the_standard_library_and_sett() -> None:
    module = ast.parse(EXAMPLE.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "collections", "typing", "sett"}


def test_sett_never_imports_a_foreign_agent_framework() -> None:
    foreign = {"langchain", "langgraph", "crewai", "autogen", "llama_index", "haystack", "semantic_kernel"}
    found: list[str] = []
    for path in (PROJECT_ROOT / "sett").rglob("*.py"):
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                found.extend(a.name.split(".")[0] for a in node.names if a.name.split(".")[0] in foreign)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.split(".")[0] in foreign
            ):
                found.append(node.module)
    assert found == []
