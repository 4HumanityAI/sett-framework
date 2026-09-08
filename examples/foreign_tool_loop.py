"""Govern a foreign tool loop without depending on it.

Every tool-calling framework shares one shape: a name plus arguments.
SETT governs that shape through `SETTExecutor.submit(Action(...))`, whose
only types are `str` and a JSON-safe `dict`. That is the whole contract.

The dependency arrow matters more than the code. SETT imports nothing
from the caller, so a foreign framework changing its interface breaks the
adapter on the caller's side and never breaks SETT. This file therefore
imports nothing outside the standard library and SETT itself: the
"foreign" loop below is written here only so the example runs on its own.

See `docs/foreign_callers.md` for the guarantees this relies on.

Run:
    python -B examples/foreign_tool_loop.py
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sett import (
    Action,
    EthicalFilter,
    SETTExecutor,
    SETTOrchestrator,
)
from sett.ethics_ruler.ethic_kernel.rules import default_ruleset


class ForeignToolLoop:
    """A stand-in for any external agent framework.

    It knows nothing about SETT. It calls one callable that takes a tool
    name and its arguments, which is the same signature such frameworks
    already use for their own tool dispatch.
    """

    def __init__(self, dispatch: Callable[[str, dict[str, Any]], Any]) -> None:
        self._dispatch = dispatch
        self.transcript: list[tuple[str, str, str]] = []

    def run(self, plan: Sequence[tuple[str, dict[str, Any]]]) -> list[tuple[str, str, str]]:
        """Attempt each planned tool call and record what happened."""
        for name, arguments in plan:
            try:
                result = self._dispatch(name, arguments)
            except Exception as refusal:  # noqa: BLE001 - any refusal ends this call
                self.transcript.append(("refused", name, type(refusal).__name__))
            else:
                self.transcript.append(("executed", name, str(result)))
        return self.transcript


def governed_dispatch(executor: SETTExecutor) -> Callable[[str, dict[str, Any]], Any]:
    """Return the five lines that put a foreign tool loop under governance.

    This function is the entire integration. It belongs on the caller's
    side of the boundary, not inside SETT, so that a change to the foreign
    framework's tool interface is a change to this function alone.
    """

    def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return executor.submit(Action(action_type=name, payload=arguments))

    return dispatch


def build_governed_executor() -> SETTExecutor:
    """Wire one executor whose effects are refused before they happen."""
    ruleset = default_ruleset()
    ruleset.reject_threshold = 2.0
    ruleset.warn_threshold = 1.0
    executor = SETTExecutor()
    executor.register_handler(
        "summarize the report", lambda payload: {"summary": "ok", **payload}
    )
    executor.register_handler(
        "delete all the user's savings", lambda payload: {"deleted": True}
    )
    SETTOrchestrator(ethical_filter=EthicalFilter(ruleset=ruleset)).register_executor(
        executor
    )
    return executor


def main() -> None:
    executor = build_governed_executor()
    loop = ForeignToolLoop(governed_dispatch(executor))
    transcript = loop.run(
        [
            ("summarize the report", {"length": "short"}),
            ("delete all the user's savings", {"confirm": True}),
            ("a tool nobody registered", {}),
        ]
    )
    for outcome, name, detail in transcript:
        print(f"{outcome:9} {name:34} {detail}")


if __name__ == "__main__":
    main()
