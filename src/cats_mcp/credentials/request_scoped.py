"""Request-scoped credential resolution - the multi-tenant seam.

Deliberately a stub. Building a StaffHive credential vault inside this
repository is out of scope: CATS-MCP is a CATS adapter, not StaffHive product
state. What belongs here is the *interface* so that adding tenancy later is a
single new class rather than a change to all 186 tools.

The intended shape, once StaffHive is ready:

    1. `cats_mcp.auth.verifier` validates the caller's identity token and puts
       the verified claims on the request context.
    2. This provider reads a tenant identifier from those claims.
    3. It exchanges that identifier for a CATS credential via whatever secret
       store StaffHive uses, and caches per tenant with a short TTL.

Two invariants that must survive any implementation:

* The tenant identifier comes from *verified* token claims, never from a tool
  argument. A tool argument is model-controlled and would let one caller read
  another tenant's data by asking.
* A resolution failure raises `CredentialError`. It must never fall back to the
  environment credential, because that would silently serve the wrong tenant's
  data with no error.
"""

from __future__ import annotations

from typing import Any

from cats_mcp.credentials.base import CATSCredential, CredentialError, CredentialProvider


class RequestScopedCredentialProvider(CredentialProvider):
    """Resolves a per-request CATS credential from verified caller identity."""

    def __init__(self, resolver: Any | None = None) -> None:
        # `resolver` is the StaffHive-side lookup, injected rather than imported
        # so this repository never depends on StaffHive.
        self._resolver = resolver

    async def resolve(self, context: Any | None = None) -> CATSCredential:
        if self._resolver is None:
            raise CredentialError(
                "Request-scoped credential resolution is not configured. Either "
                "run this deployment single-tenant with EnvCredentialProvider, or "
                "inject a resolver that maps verified caller claims to a CATS "
                "credential."
            )
        raise CredentialError(
            "RequestScopedCredentialProvider is a documented seam, not a working "
            "implementation. See the module docstring for the required shape."
        )

    def describe(self) -> str:
        state = "resolver injected" if self._resolver else "no resolver"
        return f"request-scoped ({state}, not implemented)"
