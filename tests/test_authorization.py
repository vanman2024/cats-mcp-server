"""Authorization: does it filter *discovery* as well as *execution*?

This is the security question the design could not answer from documentation.
FastMCP's docs state that component-level `auth` controls list filtering and
direct access, but say nothing about whether the BM25 search transform respects
it. If the search index is built from the full catalog before authorization
runs, then a caller who cannot *call* `delete_candidate` could still discover it
through `search_tools` - and worse, the search result includes the full schema.

These tests answer it empirically rather than assuming either way.

`StaticTokenVerifier` is used because it is the documented way to map fixed
tokens to claims in tests. It stores tokens in plain text and is explicitly
development-only; nothing here is a deployment pattern.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.utilities.tests import asgi_client

from cats_mcp.config import AuthMode, DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.models import Safety
from cats_mcp.server import create_server

READER_TOKEN = "reader-token"
FULL_TOKEN = "full-token"


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key="test-key", base_url="https://api.catsone.com/v3", account_label="test"
        )

    def describe(self) -> str:
        return "stub"


def verifier() -> StaticTokenVerifier:
    return StaticTokenVerifier(
        tokens={
            # Read-only integration: may list and read, nothing else.
            READER_TOKEN: {"client_id": "reader", "scopes": [Safety.READ.required_scope]},
            FULL_TOKEN: {
                "client_id": "full",
                "scopes": [s.required_scope for s in Safety],
            },
        }
    )


def build_authed_server(**overrides):
    settings = Settings(api_key="test-key", auth_mode=AuthMode.JWT, **overrides)
    client = CATSClient(
        settings,
        StubCredentials(),
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={"ok": True})),
    )
    return create_server(
        settings,
        credential_provider=StubCredentials(),
        client=client,
        auth_provider=verifier(),
    )


# --- listing ---------------------------------------------------------------


async def test_reader_cannot_see_destructive_tools_in_the_listing():
    server = build_authed_server(discovery_mode=DiscoveryMode.RAW)
    async with asgi_client(server, auth=READER_TOKEN) as client:
        names = {t.name for t in await client.list_tools()}

    assert "list_candidates" in names, "a reader must still see read tools"
    assert "delete_candidate" not in names
    assert "create_pipeline" not in names


async def test_full_scope_caller_sees_everything():
    server = build_authed_server(discovery_mode=DiscoveryMode.RAW)
    async with asgi_client(server, auth=FULL_TOKEN) as client:
        names = {t.name for t in await client.list_tools()}

    assert "delete_candidate" in names
    assert "create_pipeline" in names


# --- execution -------------------------------------------------------------


async def test_reader_cannot_execute_a_destructive_tool():
    server = build_authed_server(discovery_mode=DiscoveryMode.RAW)
    async with asgi_client(server, auth=READER_TOKEN) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("delete_candidate", {"candidate_id": 1})

    # Hidden components are reported as not-found rather than as forbidden,
    # which avoids confirming that the tool exists.
    assert excinfo.value is not None


async def test_reader_can_execute_a_read_tool():
    server = build_authed_server(discovery_mode=DiscoveryMode.RAW)
    async with asgi_client(server, auth=READER_TOKEN) as client:
        result = await client.call_tool("get_candidate", {"candidate_id": 1})
    assert result is not None


# --- the unproven one: does search leak? -----------------------------------


async def test_search_does_not_leak_unauthorized_tools():
    """The critical one. A schema leaked through search is still a leak."""
    server = build_authed_server(discovery_mode=DiscoveryMode.SEARCH)
    async with asgi_client(server, auth=READER_TOKEN) as client:
        result = await client.call_tool("search_tools", {"query": "delete a candidate"})

    text = str(result.data or result.content)
    assert "delete_candidate" not in text, (
        "BM25 search surfaced a tool the caller is not authorized to call. "
        "The search index must be built after authorization filtering."
    )


async def test_search_still_finds_authorized_tools():
    """Guards against the filter being so aggressive it breaks discovery.

    The target is a read tool that is *not* pinned. A pinned tool is already
    visible, so the transform drops it from search results and it would pass
    this test without the index being consulted at all.
    """
    server = build_authed_server(discovery_mode=DiscoveryMode.SEARCH)
    async with asgi_client(server, auth=READER_TOKEN) as client:
        result = await client.call_tool(
            "search_tools", {"query": "previous applicants for a job"}
        )

    text = str(result.data or result.content)
    assert "list_job_applications" in text


async def test_call_tool_meta_tool_enforces_the_same_authorization():
    """`call_tool` must not become a way around per-tool scopes."""
    server = build_authed_server(discovery_mode=DiscoveryMode.SEARCH)
    async with asgi_client(server, auth=READER_TOKEN) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "call_tool", {"name": "delete_candidate", "arguments": {"candidate_id": 1}}
            )


async def test_unauthenticated_caller_is_rejected():
    server = build_authed_server(discovery_mode=DiscoveryMode.RAW)
    with pytest.raises(Exception):
        async with asgi_client(server) as client:
            await client.list_tools()
