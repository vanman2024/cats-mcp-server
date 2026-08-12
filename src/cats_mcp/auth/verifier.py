"""Authentication of the *MCP caller*.

Distinct from the CATS credential the adapter uses downstream (see
`cats_mcp.credentials`). Conflating the two is what limits a deployment to one
CATS account; keeping them apart is what lets a future multi-tenant deployment
verify a StaffHive identity token and then resolve the right CATS connection.

The audit found the deployed server listening on 0.0.0.0:3000 with no
authentication at all - every tool, including the 26 destructive ones, callable
by anyone who could reach the URL.
"""

from __future__ import annotations

from typing import Any

from cats_mcp.config import Settings, Transport
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)


class InsecureDeploymentError(RuntimeError):
    """An HTTP deployment was started without authentication."""


def build_auth_provider(settings: Settings) -> Any | None:
    """Return the FastMCP auth provider, or None when auth is not required.

    stdio needs no MCP-layer authentication: the transport is a pipe to a
    process the user already started, so there is no network surface to protect.
    HTTP is the opposite, and refusing to start is safer than quietly serving
    destructive tools to the internet.
    """
    if settings.auth_jwks_uri:
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        logger.info(
            "MCP auth: JWT verification (issuer=%s audience=%s)",
            settings.auth_issuer or "<unset>",
            settings.auth_audience or "<unset>",
        )
        return JWTVerifier(
            jwks_uri=settings.auth_jwks_uri,
            issuer=settings.auth_issuer or None,
            audience=settings.auth_audience or None,
        )

    if settings.transport is Transport.HTTP and not settings.allow_unauthenticated_http:
        raise InsecureDeploymentError(
            "Refusing to start an unauthenticated HTTP server.\n"
            "\n"
            "This server exposes destructive CATS tools. Over HTTP it must "
            "verify who is calling it.\n"
            "\n"
            "Configure JWT verification:\n"
            "    CATS_AUTH_JWKS_URI=https://<issuer>/.well-known/jwks.json\n"
            "    CATS_AUTH_ISSUER=https://<issuer>/\n"
            "    CATS_AUTH_AUDIENCE=<this server's audience>\n"
            "\n"
            "Or, for local development only, set "
            "CATS_ALLOW_UNAUTHENTICATED_HTTP=true."
        )

    if settings.transport is Transport.HTTP:
        logger.warning(
            "MCP auth DISABLED on an HTTP listener. Every tool, including "
            "destructive ones, is callable by anyone who can reach this URL. "
            "This must not be used for a deployment."
        )
    return None


def auth_is_enforced(settings: Settings) -> bool:
    """Whether per-tool scope checks should be attached.

    Attaching `require_scopes` with no auth provider configured would deny every
    call - including legitimate local stdio use, where the transport is already
    private to the user who launched it.
    """
    return bool(settings.auth_jwks_uri)
