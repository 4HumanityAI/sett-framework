"""Shared, dependency-light HTTP mechanics for official voice adapters."""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit

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
            "SETT installs from source; from your checkout run: "
            f"pip install -e \".[{extra}]\"",
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


_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _configuration_error(
    message: str, *, provider: str, adapter_name: str, reason_code: str
) -> SETTServiceAdapterError:
    return SETTServiceAdapterError(
        message,
        kind=AdapterErrorKind.CONFIGURATION,
        provider=provider,
        adapter_name=adapter_name,
        retryable=False,
        reason_code=reason_code,
    )


def path_identifier(
    value: object, *, provider: str, adapter_name: str, parameter: str
) -> str:
    """Return ``value`` if it can only ever be one URL path segment.

    The rule is structural, not a guess about a provider's identifier
    format: the value must consist of the URL "unreserved" characters
    (letters, digits, ``-``, ``.``, ``_``, ``~``) and must not be a dot
    segment. Nothing that passes can add a path separator, a query, a
    fragment, an escape sequence or a control character to the request
    URL, so the identifier cannot move the request to another resource.
    """
    if (
        isinstance(value, str)
        and _PATH_SEGMENT.fullmatch(value) is not None
        and value not in {".", ".."}
    ):
        return value
    raise _configuration_error(
        f"{adapter_name} {parameter} must be an identifier made of letters, "
        "digits, '-', '.', '_' or '~'.",
        provider=provider,
        adapter_name=adapter_name,
        reason_code="adapter.configuration_invalid",
    )


def host_label(
    value: object, *, provider: str, adapter_name: str, parameter: str
) -> str:
    """Return ``value`` if it is a single DNS label.

    Used for identifiers that become part of a host name, such as a cloud
    region. A label cannot contain a dot, a slash, a query, a fragment or
    user information, so it can select a host inside the provider's
    domain but cannot replace that domain.
    """
    if isinstance(value, str) and _DNS_LABEL.fullmatch(value) is not None:
        return value
    raise _configuration_error(
        f"{adapter_name} {parameter} must be a single host label: letters, "
        "digits and '-', for example 'westeurope'.",
        provider=provider,
        adapter_name=adapter_name,
        reason_code="adapter.configuration_invalid",
    )


def validated_endpoint(
    value: object, *, provider: str, adapter_name: str, parameter: str = "endpoint"
) -> str:
    """Return a deliberately configured endpoint after structural checks.

    The application may point an adapter at any host it chooses, including
    a proxy or a local test server, so no host list is applied. What is
    refused is a value that is not an absolute ``http`` or ``https`` URL
    naming a host, or one that carries user information, a fragment, a
    backslash, whitespace or a control character, or an unusable port.
    ``http`` is accepted because local and proxied deployments use it; the
    credential then travels without transport encryption, which is the
    application's decision to make.
    """
    problem = False
    if not isinstance(value, str) or not value:
        problem = True
    elif "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        problem = True
    else:
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            problem = True
        else:
            del port
            problem = (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or bool(parts.fragment)
            )
    if problem:
        raise _configuration_error(
            f"{adapter_name} {parameter} must be an absolute http(s) URL that "
            "names a host, without credentials or a fragment.",
            provider=provider,
            adapter_name=adapter_name,
            reason_code="adapter.invalid_base_url",
        )
    return value


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
        response = client.request(method, url, timeout=timeout, allow_redirects=False, **kwargs)
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
