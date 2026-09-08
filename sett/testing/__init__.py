"""Reusable contract checks for third-party SETT integrations."""

from sett.testing.adapter_contract import (
    AdapterContractReport,
    assert_adapter_contract,
    assert_cancellation_preflight,
    assert_normalized_adapter_error,
    inspect_adapter_contract,
)
from sett.testing.persistence_contract import (
    assert_evidence_stream_contract,
    assert_state_record_store_contract,
)

__all__ = [
    "AdapterContractReport",
    "assert_adapter_contract",
    "assert_cancellation_preflight",
    "assert_evidence_stream_contract",
    "assert_normalized_adapter_error",
    "assert_state_record_store_contract",
    "inspect_adapter_contract",
]
