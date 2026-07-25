"""Observable handling for deliberately non-fatal runtime failures."""

from __future__ import annotations

import logging


def log_suppressed_exception(module: str, operation: str) -> None:
    """Record the active exception without changing best-effort control flow."""

    try:
        logging.getLogger(module).debug(
            "Best-effort operation failed: %s",
            operation,
            exc_info=True,
        )
    except (AttributeError, RuntimeError):
        # Logging may already be shut down while object finalizers still run.
        return
