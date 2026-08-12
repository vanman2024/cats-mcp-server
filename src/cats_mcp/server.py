"""The single server factory.

Every entrypoint calls `create_server()`. There is exactly one place a `FastMCP`
instance is constructed and exactly one place tools are registered.

Two structural bugs this design makes impossible:

* The previous `server.py` called `load_toolsets()` only under
  `if __name__ == "__main__"`, so importing it - `fastmcp run server.py`,
  `server:mcp`, or any test - produced a server with **zero tools**.
* `server_all_tools.py` existed to work around that by registering at import
  time, and drifted into a second, differently-behaved implementation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from cats_mcp.auth.verifier import auth_is_enforced, build_auth_provider
from cats_mcp.composites import reads as composite_reads
from cats_mcp.config import Settings, load_settings
from cats_mcp.credentials.base import CredentialProvider
from cats_mcp.credentials.env import EnvCredentialProvider
from cats_mcp.discovery.profiles import transforms_for
from cats_mcp.http.client import CATSClient
from cats_mcp.http.correlation import configure_logging, get_logger
from cats_mcp.registry.build import register_all
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.resources import context as context_resources
from cats_mcp.resources import prompts as usage_prompts

logger = get_logger(__name__)

SERVER_NAME = "CATS API v3"

INSTRUCTIONS = """\
A universal MCP adapter for the CATS (CatsOne) applicant tracking system.

This server provides atomic, comprehensive access to the CATS API v3. It does
not implement recruiting workflows, outreach, scheduling, or candidate ranking -
those decisions belong to the calling agent or orchestrator.

Working with candidate data:
  List and search tools return compact summaries by default. They give you ids;
  use the dedicated detail tools to fetch more about a specific record. Do not
  request full records for a whole list.

Rate limits:
  The CATS standard is 500 requests/hour. Prefer one filtered query over many
  individual lookups, and use the events tool to poll for changes rather than
  re-listing records.
"""


def create_server(
    settings: Settings | None = None,
    *,
    credential_provider: CredentialProvider | None = None,
    client: CATSClient | None = None,
    auth_provider: Any | None = None,
) -> FastMCP:
    """Build a fully configured CATS MCP server.

    Args are injectable so tests can supply a stub credential provider and a
    mock-transport client without touching the environment.

    `auth_provider` overrides the verifier built from settings. Supplying one
    also turns on per-tool scope enforcement, which is how the test suite proves
    that unauthorized tools are hidden from discovery as well as from execution.
    """
    settings = settings or load_settings()
    configure_logging()

    credentials = credential_provider or EnvCredentialProvider(settings)
    cats_client = client or CATSClient(settings, credentials)

    # Tools resolve the client lazily. The lifespan owns its connection pool, so
    # the closure must not capture a started/stopped instance.
    def client_getter() -> CATSClient:
        return cats_client

    # Resolve configuration before defining the lifespan, so the closure does
    # not depend on names bound further down.
    if auth_provider is None:
        auth_provider = build_auth_provider(settings)
        enforce_auth = auth_is_enforced(settings)
    else:
        # An explicitly supplied verifier always implies scope enforcement.
        enforce_auth = True
    selected = REGISTRY.select(settings.requested_toolsets)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        await cats_client.start()
        logger.info(
            "CATS MCP ready: %d tools, discovery=%s, credentials=%s, auth=%s",
            len(selected),
            settings.discovery_mode.value,
            credentials.describe(),
            "enforced" if enforce_auth else "none",
        )
        try:
            yield {"cats_client": cats_client}
        finally:
            await cats_client.aclose()

    mcp = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=_version(),
        auth=auth_provider,
        transforms=transforms_for(settings),
        lifespan=lifespan,
    )

    registered = register_all(
        mcp,
        selected,
        client_getter,
        enforce_auth=enforce_auth,
        ui_base_url=settings.ui_base_url,
    )

    # Composite read primitives, registered only alongside a full catalog.
    # They span resources, so exposing them under a narrowed CATS_TOOLSETS
    # selection would let a caller reach data the selection was meant to exclude.
    if settings.requested_toolsets is None or "all" in settings.requested_toolsets:
        registered += composite_reads.register(
            mcp, client_getter, enforce_auth=enforce_auth
        )

    _register_status_tool(mcp, settings, credentials, cats_client, registered)

    # Read-only context: what this adapter is, which account it is attached to,
    # and the account-specific ids nothing else works without. Resources are
    # lazy-loaded, so these cost nothing until a client reads one.
    context_resources.register(mcp, settings, credentials, client_getter, registered)

    # Prompts describe how to operate the CATS API correctly - resolving
    # account-specific ids, staying inside the request budget. They deliberately
    # say nothing about who to contact or how to rank anyone; that is the
    # orchestrator's domain, and a test enforces the distinction.
    usage_prompts.register(mcp)

    return mcp


def _version() -> str:
    from cats_mcp import __version__

    return __version__


def _register_status_tool(
    mcp: FastMCP,
    settings: Settings,
    credentials: CredentialProvider,
    client: CATSClient,
    tool_count: int,
) -> None:
    """A connection-status tool that works without calling CATS.

    Pinned in `search` mode as the entry point to the server. This replaces
    `get_me`, which the audit found calls `GET /users/current` - an endpoint
    that does not exist in CATS v3 and returns 404.
    """

    @mcp.tool(
        name="get_connection_status",
        description=(
            "Report this adapter's configuration and its current CATS rate-limit "
            "budget without calling the CATS API. Use this first when tools are "
            "failing, to distinguish a configuration problem from a data problem, "
            "or to check how much request budget remains before starting a large "
            "batch of lookups."
        ),
        tags={"ats", "read", "status", "diagnostics"},
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def get_connection_status() -> dict[str, Any]:
        return {
            "server": SERVER_NAME,
            "version": _version(),
            "tools_registered": tool_count,
            "discovery_mode": settings.discovery_mode.value,
            "credentials": credentials.describe(),
            "rate_limit": client.rate_limit.snapshot(),
            "rate_limit_note": (
                "null values mean no CATS request has been made yet this session. "
                "The CATS standard ceiling is 500 requests/hour; some accounts are "
                "raised."
            ),
        }
