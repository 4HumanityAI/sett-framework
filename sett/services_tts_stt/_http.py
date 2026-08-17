"""Shared, dependency-light HTTP mechanics for official voice adapters."""
from __future__ import annotations

import os
from collections.abc import Callable
from time import monotonic, sleep
from typing import Any

from sett.core_ruler.adapter import (
    AdapterErrorKind,
    normalized_adapter_error,
)
from sett.exceptions import SETTServiceAdapterError

try:
    import requests
except ImportError:  # pragma: no cover - exercised in dependency audits
    requests = None


def require_requests(*, adapter_name: str, extra: str) -> Any:
    """Return requests or raise one actionable configuration error."""
    if requests is None:
        raise SETTServiceAdapterError(
            f"The 'requests' package is required to use {adapter_name}. "
            f"Install it with: pip install sett-framework[{extra}]",
            kind=AdapterErrorKind.CONFIGURATION,
            adapter_name=adapter_name,
            reason_code="adapter.dependency_missing",
        )
    return requests


def credential(
    value: str | None,
    environment_name: str,
    *,
    provider: str,
    adapter_name: str,
) -> str:
    """Resolve a credential without retaining whitespace-only values."""
    resolved = (value or os.getenv(environment_name, "")).strip()
    if not resolved:
        raise SETTServiceAdapterError(
            f"{provider} credential not found. Pass it explicitly or set "
            f"the {environment_name} environment variable.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider=provider,
            adapter_name=adapter_name,
            reason_code="adapter.credentials_missing",
        )
    return resolved


def response_json(response: Any, *, provider: str, adapter_name: str, operation: str) -> Any:
    """Decode JSON and reject provider success responses with invalid bodies."""
    try:
        return response.json()
    except (TypeError, ValueError) as error:
        raise normalized_adapter_error(
            error,
            error_type=SETTServiceAdapterError,
            provider=provider,
            adapter_name=adapter_name,
            operation=operation,
            kind=AdapterErrorKind.INVALID_RESPONSE,
        ) from error


def checked_request(
    method: str,
    url: str,
    *,
    provider: str,
    adapter_name: str,
    operation: str,
    timeout: float | None,
    expected_statuses: tuple[int, ...] = (200,),
    **kwargs: Any,
) -> Any:
    """Perform one request and normalize transport and HTTP failures."""
    client = require_requests(adapter_name=adapter_name, extra="voice-http")
    try:
        response = client.request(method, url, timeout=timeout, **kwargs)
    except client.exceptions.RequestException as error:
        raise normalized_adapter_error(
            error,
            error_type=SETTServiceAdapterError,
            provider=provider,
            adapter_name=adapter_name,
            operation=operation,
            kind=AdapterErrorKind.UNAVAILABLE,
        ) from error
    if response.status_code not in expected_statuses:
        error = RuntimeError(getattr(response, "text", ""))
        error.status_code = response.status_code  # type: ignore[attr-defined]
        error.response = response  # type: ignore[attr-defined]
        raise normalized_adapter_error(
            error,
            error_type=SETTServiceAdapterError,
            provider=provider,
            adapter_name=adapter_name,
            operation=operation,
            status_code=response.status_code,
        ) from error
    return response


def poll_json(
    get_result: Callable[[float | None], Any],
    *,
    is_pending: Callable[[Any], bool],
    lifecycle: Any,
    timeout: float | None,
    poll_interval: float,
    provider: str,
    adapter_name: str,
    operation: str,
) -> Any:
    """Poll an asynchronous provider job within one caller-owned budget."""
    started = monotonic()
    while True:
        lifecycle.throw_if_cancelled()
        elapsed = monotonic() - started
        if timeout is not None and elapsed >= timeout:
            error = TimeoutError(f"{provider} job did not finish in time")
            raise normalized_adapter_error(
                error,
                error_type=SETTServiceAdapterError,
                provider=provider,
                adapter_name=adapter_name,
                operation=operation,
                kind=AdapterErrorKind.PROVIDER_TIMEOUT,
            ) from error
        remaining = None if timeout is None else max(0.001, timeout - elapsed)
        result = get_result(remaining)
        if not is_pending(result):
            return result
        pause = poll_interval if remaining is None else min(poll_interval, remaining)
        slept = 0.0
        while slept < pause:
            lifecycle.throw_if_cancelled()
            step = min(0.1, pause - slept)
            sleep(step)
            slept += step
