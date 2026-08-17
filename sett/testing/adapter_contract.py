"""Dependency-free checks for external adapter authors.

The helpers intentionally do not import pytest and never contact a provider on
their own.  A project may use them from unittest, pytest, or a plain build
script and remains in control of every call used for behavioral probes.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterHealth,
    AdapterHealthStatus,
    AdapterKind,
)
from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import ExecutionControl
from sett.exceptions import SETTAdapterError, SETTCancelledError


@dataclass(frozen=True, slots=True)
class AdapterContractReport:
    """Static contract findings for one adapter instance."""

    adapter_name: str
    expected_kind: AdapterKind
    violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Whether the adapter satisfied every static contract check."""
        return not self.violations


def inspect_adapter_contract(
    adapter: object,
    expected_kind: AdapterKind,
) -> AdapterContractReport:
    """Inspect capability export and initial passive health without I/O."""
    violations: list[str] = []
    capabilities = getattr(adapter, "capabilities", None)
    if not isinstance(capabilities, AdapterCapabilities):
        violations.append("adapter.capabilities_missing")
    else:
        if capabilities.kind is not expected_kind:
            violations.append("adapter.kind_mismatch")
        try:
            first = capabilities.safe_view()
            json.dumps(first, allow_nan=False)
            first["metadata"]["contract_probe"] = True
            if "contract_probe" in capabilities.safe_view()["metadata"]:
                violations.append("adapter.capabilities_not_defensive")
        except (TypeError, ValueError, KeyError):
            violations.append("adapter.capabilities_not_json_safe")
    health = getattr(adapter, "health", None)
    if not isinstance(health, AdapterHealth):
        violations.append("adapter.health_missing")
    elif health.status is not AdapterHealthStatus.UNKNOWN:
        violations.append("adapter.initial_health_not_unknown")
    check_health = getattr(adapter, "check_health", None)
    if not callable(check_health):
        violations.append("adapter.check_health_missing")
    return AdapterContractReport(
        type(adapter).__name__, expected_kind, tuple(violations)
    )


def assert_adapter_contract(adapter: object, expected_kind: AdapterKind) -> None:
    """Raise AssertionError with stable reason codes when inspection fails."""
    report = inspect_adapter_contract(adapter, expected_kind)
    if not report.passed:
        raise AssertionError(
            f"{report.adapter_name} failed the SETT adapter contract: "
            + ", ".join(report.violations)
        )


def assert_cancellation_preflight(call: Callable[..., Any]) -> None:
    """Verify a call rejects prior cancellation before doing provider work.

    ``call`` must accept the reserved adapter keyword arguments.  Construct it
    around a fake provider client so this probe can also assert zero fake calls.
    """
    control = ExecutionControl.create_root(ExecutionContext.create())
    with control:
        control.cancel(reason="adapter contract probe")
        try:
            call(timeout=1, cancellation_token=control.token)
        except SETTCancelledError:
            return
    raise AssertionError("adapter did not honor cancellation before its provider call")


def assert_normalized_adapter_error(
    call: Callable[[], Any],
    *,
    secrets: tuple[str, ...] = (),
) -> SETTAdapterError:
    """Verify one caller-supplied failing probe uses the safe error contract."""
    try:
        call()
    except SETTAdapterError as error:
        if error.__cause__ is None:
            raise AssertionError("normalized adapter error did not preserve __cause__")
        rendered = repr(error.safe_view())
        if any(secret and secret in rendered for secret in secrets):
            raise AssertionError("adapter error safe_view leaked a supplied secret")
        return error
    raise AssertionError("adapter failure did not raise SETTAdapterError")
