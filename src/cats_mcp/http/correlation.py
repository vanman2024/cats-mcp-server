"""Correlation IDs and structured logging.

Every CATS request gets an id that appears in the logs and in any error
surfaced to the caller, so a failure reported by an agent can be traced to the
exact upstream call. The previous implementation logged with `print()` on the
deployed path, which is unstructured, unfilterable, and - as the audit found -
crashes outright on a Windows console when the message contains an emoji.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from contextvars import ContextVar

#: Set per MCP tool invocation so all CATS calls made while serving one tool
#: share a run id.
_run_id: ContextVar[str | None] = ContextVar("cats_mcp_run_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def set_run_id(run_id: str | None = None) -> str:
    value = run_id or new_correlation_id()
    _run_id.set(value)
    return value


def get_run_id() -> str | None:
    return _run_id.get()


def configure_logging(level: str | None = None) -> logging.Logger:
    """Configure structured logging on stderr.

    stderr, not stdout: under the stdio transport, stdout carries the MCP
    protocol itself. Anything written there corrupts the stream - which is
    exactly what the previous `print()` calls did.
    """
    resolved = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    logger = logging.getLogger("cats_mcp")
    if logger.handlers:
        logger.setLevel(resolved)
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(resolved)
    # Do not also emit through the root logger's handlers.
    logger.propagate = False
    return logger


def get_logger(name: str = "cats_mcp") -> logging.Logger:
    return logging.getLogger(name)
