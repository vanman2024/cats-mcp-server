"""Credential resolution for downstream CATS calls.

Three layers are deliberately kept apart:

1. Authentication of the *MCP caller*        -> `cats_mcp.auth.verifier`
2. Authorization to discover/call a tool     -> `cats_mcp.auth.policies`
3. The *CATS* credential the adapter uses    -> this module

Conflating (1) and (3) is what limits the current server to a single CATS
account per deployment. Keeping them separate means request-scoped resolution
can be added later without touching a single tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_REDACTED = "***redacted***"


@dataclass(frozen=True)
class CATSCredential:
    """A resolved CATS credential plus the non-secret context it belongs to.

    The secret is never included in `repr`, `str`, or any dict/JSON form. Tool
    results are serialised straight to the model, so a credential that
    stringifies to its own secret is one careless log line away from leaking
    into a transcript.
    """

    api_key: str = field(repr=False)
    base_url: str
    # Non-secret label used for logging and correlation, e.g. a tenant slug.
    # Never a key, never a token.
    account_label: str = "default"

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.api_key}"}

    # --- leak guards --------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"CATSCredential(base_url={self.base_url!r}, "
            f"account_label={self.account_label!r}, api_key={_REDACTED})"
        )

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        # f"{cred}" must not become a way to bypass the redacted repr.
        return self.__repr__()

    def redacted(self) -> dict[str, Any]:
        """Safe to log or return. Contains no secret."""
        return {
            "base_url": self.base_url,
            "account_label": self.account_label,
            "api_key": _REDACTED,
        }


class CredentialError(RuntimeError):
    """Raised when no usable CATS credential can be resolved."""


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolves the CATS credential to use for one request.

    Implementations must never return the secret through any other channel, and
    must raise `CredentialError` rather than returning a placeholder or empty
    key - an empty key produces a confusing 401 from CATS far from the cause.
    """

    async def resolve(self, context: Any | None = None) -> CATSCredential:
        """Return the credential for this request.

        `context` is the FastMCP request context when one is available. It is
        optional so that startup-time validation and tests can call `resolve()`
        without constructing a request.
        """
        ...

    def describe(self) -> str:
        """Short non-secret description, used in startup logs."""
        ...
