"""Compatibility imports for the v0.12 retry module.

The implementation moved to :mod:`sett.core_ruler.resilience` in v0.13.0
because retry and circuit breaking share one error classification.  This
module remains intentionally so direct v0.12 imports continue to work.
"""
from sett.core_ruler.resilience import (
    ErrorClass,
    RetryPolicy,
    classify_error,
    is_retryable,
)

__all__ = ["ErrorClass", "RetryPolicy", "classify_error", "is_retryable"]
