"""Keep templates/adapter_template.py honest against the live contract.

The template exists to be copied, so the only guarantee worth having is
that it still satisfies the contract it claims to demonstrate. These
tests run the shipped contract kit against the template itself: if the
adapter contract changes and the template is not updated with it, this
file fails instead of the template quietly teaching the wrong shape.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from sett.core_ruler.adapter import AdapterHealthStatus, AdapterKind
from sett.core_ruler.execution_context import ExecutionContext
from sett.core_ruler.lifecycle import ExecutionControl
from sett.exceptions import SETTCancelledError, SETTServiceAdapterError
from sett.testing.adapter_contract import (
    assert_adapter_contract,
    assert_cancellation_preflight,
    assert_normalized_adapter_error,
    inspect_adapter_contract,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "templates"))

from adapter_template import MyAdapter

_SECRET = "template-credential-value"


class _ExplodingAdapter(MyAdapter):
    """A copy of the template whose provider call fails, as one will."""

    def _call_provider(self, text: str, timeout: float | None, **kwargs: Any) -> Any:
        raise RuntimeError(f"provider exploded and quoted {_SECRET}")


def test_template_fails_closed_until_provider_is_implemented() -> None:
    with pytest.raises(SETTServiceAdapterError) as error:
        MyAdapter().analyze("any text")
    assert error.value.reason_code == "adapter.template_not_configured"


def test_template_satisfies_the_static_contract() -> None:
    assert_adapter_contract(MyAdapter(), AdapterKind.SENTIMENT)
    assert inspect_adapter_contract(MyAdapter(), AdapterKind.SENTIMENT).passed


def test_template_declares_itself_as_a_template() -> None:
    capabilities = MyAdapter().capabilities
    assert capabilities.metadata["template"] is True
    assert capabilities.connectivity.value == "none"


def test_capabilities_are_not_shared_between_accesses() -> None:
    adapter = MyAdapter()
    assert adapter.capabilities is not adapter.capabilities


def test_health_starts_unknown_and_records_failed_template_call() -> None:
    adapter = MyAdapter()
    assert adapter.health.status is AdapterHealthStatus.UNKNOWN
    with pytest.raises(SETTServiceAdapterError):
        adapter.analyze("any text")
    assert adapter.health.status is not AdapterHealthStatus.HEALTHY


def test_cancelled_run_never_reaches_the_provider() -> None:
    adapter = MyAdapter()
    assert_cancellation_preflight(lambda **kw: adapter.analyze("any text", **kw))


def test_cancellation_propagates_untranslated() -> None:
    adapter = MyAdapter()
    control = ExecutionControl.create_root(ExecutionContext.create())
    with control:
        control.cancel(reason="test")
        with pytest.raises(SETTCancelledError):
            adapter.analyze("t", timeout=1, cancellation_token=control.token)


def test_provider_failure_is_normalized_without_leaking_the_credential() -> None:
    adapter = _ExplodingAdapter(api_key=_SECRET)
    error = assert_normalized_adapter_error(
        lambda: adapter.analyze("any text"), secrets=(_SECRET,)
    )
    assert error.__cause__ is not None
    assert _SECRET not in repr(adapter)
    assert adapter.health.status is not AdapterHealthStatus.HEALTHY
