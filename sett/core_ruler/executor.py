"""Compatibility reexports for the executor moved in v0.14.0.

The implementation belongs to :mod:`sett.execution_ruler.executor`. This
module remains intentionally so historical direct imports keep resolving.
"""

from sett.execution_ruler.executor import (
    HandlerContext,
    SETTExecutor,
    current_handler_context,
)

__all__ = [
    "HandlerContext",
    "SETTExecutor",
    "current_handler_context",
]
