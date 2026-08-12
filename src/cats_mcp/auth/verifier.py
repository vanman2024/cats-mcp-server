"""Authentication of the *MCP caller*.

Distinct from the CATS credential the adapter uses downstream (see
`cats_mcp.credentials`). Conflating the two is what limits a deployment to one
CATS account; keeping them apart is what lets a future multi-tenant deployment
verify a caller identity and then resolve the right CATS connection.

The audit found the deployed server listening on 0.0.0.0:3000 with no
authentication configured in the application at all - every tool, including the
destructive ones, reachable by anyone who could reach the URL.

Three ways that gets addressed, and the operator has to say which one applies,
because the server cannot detect it:

* `platform` - a gateway in front authenticates first. Horizon works this way:
  its gateway "runs before your server code" and authentication is on by
  default for hosted endpoints. A reverse proxy doing mTLS or OAuth is the
  same shape.
* `jwt` - this server verifies bearer tokens itself.
* `none` - nobody does. Local development only.
"""

from __future__ import annotations

from typing import Any

from cats_mcp.config import AuthMode, Settings, Transport
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)


class InsecureDeploymentError(RuntimeError):
    """An HTTP listener was started without saying who authenticates callers."""


def _require_mode(settings: Settings) -> AuthMode:
    """Resolve the auth mode, refusing to guess for a self-served HTTP listener."""
    if settings.auth_mode is not None:
        return settings.auth_mode

    # A JWKS URI on its own is an unambiguous statement of intent.
    if settings.auth_jwks_uri:
        return AuthMode.JWT

    if settings.transport is not Transport.HTTP:
        # stdio: the transport is already private to the user who launched it.
        return AuthMode.NONE

    raise InsecureDeploymentError(
        "Refusing to serve HTTP without knowing who authenticates callers.\n"
        "\n"
        "This server exposes destructive CATS tools. Set CATS_AUTH_MODE to one "
        "of:\n"
        "\n"
        "  platform  Something in front of this server authenticates first -\n"
        "            a managed gateway such as Prefect Horizon, or a reverse\n"
        "            proxy. Horizon protects hosted endpoints by default.\n"
        "\n"
        "  jwt       This server verifies bearer tokens itself. Also set:\n"
        "              CATS_AUTH_JWKS_URI=https://<issuer>/.well-known/jwks.json\n"
        "              CATS_AUTH_ISSUER=https://<issuer>/\n"
        "              CATS_AUTH_AUDIENCE=<this server's audience>\n"
        "\n"
        "  none      Nobody authenticates. Local development only.\n"
        "\n"
        "There is no default because guessing wrong is harmful either way: "
        "assume a gateway that is not there and these tools sit on an open URL; "
        "assume none and a correctly-fronted deployment fails to start."
    )


def build_auth_provider(settings: Settings) -> Any | None:
    """Return the FastMCP auth provider, or None when this server does not verify.

    Returning None does not mean "unauthenticated" - under `platform` it means
    the verification happened upstream, before this process was reached.
    """
    mode = _require_mode(settings)

    if mode is AuthMode.JWT:
        if not settings.auth_jwks_uri:
            raise InsecureDeploymentError(
                "CATS_AUTH_MODE=jwt requires CATS_AUTH_JWKS_URI so tokens can be "
                "verified against the issuer's public keys."
            )
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        logger.info(
            "MCP auth: this server verifies JWTs (issuer=%s audience=%s)",
            settings.auth_issuer or "<any>",
            settings.auth_audience or "<any>",
        )
        return JWTVerifier(
            jwks_uri=settings.auth_jwks_uri,
            issuer=settings.auth_issuer or None,
            audience=settings.auth_audience or None,
        )

    if mode is AuthMode.PLATFORM:
        logger.info(
            "MCP auth: delegated to the platform. This server trusts that a "
            "gateway in front of it has already verified the caller, and "
            "performs no verification itself. If nothing is in front of it, "
            "every tool is exposed."
        )
        return None

    if settings.transport is Transport.HTTP:
        logger.warning(
            "MCP auth DISABLED on an HTTP listener (CATS_AUTH_MODE=none). Every "
            "tool, including destructive ones, is callable by anyone who can "
            "reach this URL. Do not use this for a deployment."
        )
    return None


def auth_is_enforced(settings: Settings) -> bool:
    """Whether per-tool scope checks should be attached.

    Only meaningful when this server verifies tokens itself - scopes come from
    the claims it validated. Attaching `require_scopes` with no verifier would
    deny every call, including legitimate local stdio use.

    Under `platform`, the gateway authenticates but this server sees no claims,
    so tool-level scope filtering is not available. Authorization is the
    gateway's to enforce there.
    """
    try:
        return _require_mode(settings) is AuthMode.JWT
    except InsecureDeploymentError:
        return False
