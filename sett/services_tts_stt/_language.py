"""Internal language-argument resolution for official voice adapters."""
from __future__ import annotations

from typing import Any

from sett.core_ruler.adapter import AdapterErrorKind
from sett.exceptions import SETTServiceAdapterError


def _normalized_tag(
    value: Any,
    *,
    name: str,
    provider: str,
    adapter_name: str,
    operation: str,
    primary_only: bool,
    lowercase: bool,
) -> str:
    normalized = str(value).strip().replace("_", "-")
    if primary_only:
        normalized = normalized.split("-")[0]
    if lowercase:
        normalized = normalized.lower()
    if normalized:
        return normalized
    raise SETTServiceAdapterError(
        f"{name} must be a non-empty language identifier.",
        kind=AdapterErrorKind.CONFIGURATION,
        provider=provider,
        adapter_name=adapter_name,
        operation=operation,
        retryable=False,
        reason_code="adapter.language_missing",
    )


def resolve_language_code(
    language_code: Any | None,
    language: Any | None,
    *,
    default: Any | None,
    provider: str,
    adapter_name: str,
    operation: str,
    primary_only: bool = False,
    lowercase: bool = False,
) -> str | None:
    """Resolve ``language_code`` and its historical ``language`` alias.

    Official voice adapters use ``language_code`` at their public boundary
    even when a provider calls the same value ``language`` internally. Both
    spellings remain accepted where ``language`` existed before this helper;
    conflicting values fail before provider work.
    """
    canonical = (
        None
        if language_code is None
        else _normalized_tag(
            language_code,
            name="language_code",
            provider=provider,
            adapter_name=adapter_name,
            operation=operation,
            primary_only=primary_only,
            lowercase=lowercase,
        )
    )
    legacy = (
        None
        if language is None
        else _normalized_tag(
            language,
            name="language",
            provider=provider,
            adapter_name=adapter_name,
            operation=operation,
            primary_only=primary_only,
            lowercase=lowercase,
        )
    )
    if (
        canonical is not None
        and legacy is not None
        and canonical.casefold() != legacy.casefold()
    ):
        raise SETTServiceAdapterError(
            "language_code and language identify different languages.",
            kind=AdapterErrorKind.CONFIGURATION,
            provider=provider,
            adapter_name=adapter_name,
            operation=operation,
            retryable=False,
            reason_code="adapter.language_arguments_conflict",
        )
    selected = canonical or legacy
    if selected is not None or default is None:
        return selected
    return _normalized_tag(
        default,
        name="default language_code",
        provider=provider,
        adapter_name=adapter_name,
        operation=operation,
        primary_only=primary_only,
        lowercase=lowercase,
    )
