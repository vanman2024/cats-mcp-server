"""End-to-end server tests through a real MCP client.

Follows FastMCP's documented testing guidance: an in-memory `Client(server)`
exercises the full request path - validation, transforms, auth, serialisation -
in one process, and `asgi_client` runs the real HTTP stack without a socket.

Per that guidance, Clients are opened *inside* tests rather than in fixtures,
which avoids event-loop lifetime problems.

This file replaces the previous `tests/test_server.py`, which imported the
module-level toolset registration and could not exercise a running server.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import AuthMode, DiscoveryMode, Settings, Transport
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.discovery.profiles import PINNED_TOOLS
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.server import create_server


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key="test-key", base_url="https://api.catsone.com/v3", account_label="test"
        )

    def describe(self) -> str:
        return "stub"


def build_server(handler=None, **overrides):
    """A server wired to a mock CATS transport, never touching the network."""
    settings = Settings(api_key="test-key", **overrides)
    handler = handler or (lambda request: httpx2.Response(200, json={}))
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


# --- catalog ---------------------------------------------------------------


#: Tools that exist outside the endpoint registry: the composite read
#: primitives plus the synthetic connection-status tool.
NON_REGISTRY_TOOLS = 17  # 16 composite reads + get_connection_status


async def test_raw_profile_exposes_the_whole_catalog():
    server = build_server(discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert len(names) == len(REGISTRY) + NON_REGISTRY_TOOLS, (
        "expected every endpoint spec, the composite reads, and the status tool"
    )
    assert "get_connection_status" in names


async def test_server_registers_tools_when_merely_imported():
    """The previous server registered zero tools unless run as __main__.

    Asserted through the status tool rather than `list_tools`, because the
    default discovery profile is `search` and therefore deliberately lists only
    the meta-tools. Registration and visibility are different things.
    """
    from cats_mcp import app

    async with Client(app.mcp) as client:
        result = await client.call_tool("get_connection_status", {})
    assert result.data["tools_registered"] > 100


async def test_toolsets_selection_is_actually_honoured():
    """CATS_TOOLSETS was documented but silently ignored by the deployed server."""
    server = build_server(discovery_mode=DiscoveryMode.RAW, toolsets="tags,users")
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert "list_tags" in names
    assert "list_candidates" not in names
    # 2 tags + 2 users + the status tool
    assert len(names) == 5


async def test_removed_broken_tools_are_absent():
    server = build_server(discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert "get_me" not in names, "GET /users/current returns 404"
    assert "authorize_user" not in names, "POST /authorization does not exist"


# --- discovery profiles ----------------------------------------------------


async def test_search_profile_hides_the_catalog_behind_meta_tools():
    server = build_server(discovery_mode=DiscoveryMode.SEARCH)
    async with Client(server) as client:
        visible = {t.name for t in await client.list_tools()}

    assert "search_tools" in visible
    assert "call_tool" in visible
    # The long tail stays hidden - that is what the mode is for. The real
    # ceiling is the byte budget below; this pins the shape.
    assert len(visible) < 40, f"search profile leaked {len(visible)} tools"
    assert "list_company_departments" not in visible
    assert "delete_candidate" not in visible, "destructive tools stay behind search"
    assert "update_company_custom_field" not in visible


#: Resident schema budget for `search` mode, in bytes.
#:
#: The real constraint is context, not tool count, so guard the bytes. The full
#: catalog is ~192KB; the pinned working set is ~27KB. This ceiling leaves room
#: to pin a few more and fails loudly if someone pins half the catalog and
#: quietly re-creates the problem the mode exists to solve.
MAX_RESIDENT_SCHEMA_BYTES = 45_000


async def test_the_pinned_set_stays_affordable():
    server = build_server(discovery_mode=DiscoveryMode.SEARCH)
    async with Client(server) as client:
        tools = await client.list_tools()

    resident = sum(
        len(json.dumps({"n": t.name, "d": t.description, "s": t.input_schema})) for t in tools
    )
    assert resident < MAX_RESIDENT_SCHEMA_BYTES, (
        f"pinned tools cost {resident} bytes up front, over the "
        f"{MAX_RESIDENT_SCHEMA_BYTES} budget"
    )


async def test_search_profile_pins_a_working_entry_point():
    server = build_server(discovery_mode=DiscoveryMode.SEARCH)
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
    for pinned in PINNED_TOOLS:
        assert pinned in names, f"{pinned} is named in PINNED_TOOLS but not registered"
    assert "get_me" not in names, "never pin a tool that 404s"


async def test_the_batch_tools_are_visible_without_searching():
    """The tools that exist to collapse N calls into one must be findable.

    An unpinned tool costs an extra model round trip every time it is used. For
    the composites that is self-defeating: a client that never discovers
    get_candidate_summaries falls back to one get_candidate per person, which is
    the exact cost these tools were written to remove.
    """
    server = build_server(discovery_mode=DiscoveryMode.SEARCH)
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}

    for batch_tool in (
        "get_candidate_context",
        "get_candidate_summaries",
        "get_candidate_engagement",
        "get_job_candidate_pool",
        "get_pipeline_summaries",
        "get_changed_records",
    ):
        assert batch_tool in names, f"{batch_tool} is hidden behind search"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("add a candidate to a job pipeline", "create_pipeline"),
        ("move an application to a new workflow status", "change_pipeline_status"),
        ("delete a candidate", "delete_candidate"),
        ("previous applicants for a job", "list_job_applications"),
    ],
)
async def test_bm25_finds_the_right_tool_for_recruiting_language(query, expected):
    """Discovery has to work on how a recruiter talks, not on endpoint names.

    Every tool here is deliberately *not* pinned: a pinned tool is already
    visible, so the transform excludes it from search results. Adding a pinned
    name to this list tests nothing.
    """
    server = build_server(discovery_mode=DiscoveryMode.SEARCH)
    async with Client(server) as client:
        result = await client.call_tool("search_tools", {"query": query})
    text = str(result.data or result.content)
    assert expected in text, f"{query!r} did not surface {expected}; got: {text[:400]}"


async def test_call_tool_reaches_a_hidden_tool_in_search_mode():
    """Hiding a tool from the listing must not make it uncallable."""

    def handler(request):
        return httpx2.Response(200, json={"id": 42, "first_name": "Dana"})

    server = build_server(handler, discovery_mode=DiscoveryMode.SEARCH)
    async with Client(server) as client:
        result = await client.call_tool(
            "call_tool", {"name": "get_candidate", "arguments": {"candidate_id": 42}}
        )
    assert "Dana" in str(result.data or result.content)


# --- tool execution --------------------------------------------------------


async def test_tool_calls_the_expected_cats_endpoint():
    """Recorded as a list: a linkable tool may also probe /site for the UI domain."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx2.Response(200, json={"id": 7})

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        await client.call_tool("get_candidate", {"candidate_id": 7})

    assert ("GET", "/v3/candidates/7") in seen, seen


async def test_tag_ids_are_wrapped_as_cats_requires():
    """CATS expects [{"id": 1}], not [1]. Regression guard for commit ea735c8."""
    seen = {}

    def handler(request):
        import json

        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={})

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        await client.call_tool("attach_candidate_tags", {"candidate_id": 1, "tag_ids": [10, 20]})

    assert seen["body"]["tags"] == [{"id": 10}, {"id": 20}]


async def test_list_results_are_compact_not_full_records():
    """A page of full candidate records would swamp a context window."""
    fat_candidate = {
        "id": 1,
        "first_name": "Dana",
        "last_name": "Reid",
        "city": "Kamloops",
        "resume_text": "x" * 20_000,
        "custom_fields": {"351005": "Yes"},
        "_links": {"self": {"href": "..."}},
    }

    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 300,
                "_links": {"next": {"href": "https://api.catsone.com/v3/candidates?page=2"}},
                "_embedded": {"candidates": [fat_candidate]},
            },
        )

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        result = await client.call_tool("list_candidates", {})

    payload = str(result.data or result.content)
    assert "resume_text" not in payload
    assert "custom_fields" not in payload
    assert "_links" not in payload
    assert "Kamloops" in payload, "useful summary fields must survive"


async def test_pagination_metadata_comes_from_hal_links():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 25,
                "total": 300,
                "_links": {"next": {"href": "https://api.catsone.com/v3/candidates?page=4"}},
                "_embedded": {"candidates": []},
            },
        )

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        result = await client.call_tool("list_candidates", {})

    data = result.data
    assert data["total"] == 300
    assert data["has_more"] is True
    assert data["next_page"] == 4


async def test_last_page_reports_no_more():
    def handler(request):
        return httpx2.Response(
            200,
            json={"count": 2, "total": 2, "_links": {}, "_embedded": {"candidates": []}},
        )

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        result = await client.call_tool("list_candidates", {})

    assert result.data["has_more"] is False


async def test_empty_update_is_rejected_rather_than_silently_no_opping():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx2.Response(200, json={})

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("update_candidate", {"candidate_id": 1})

    assert called["n"] == 0, "no CATS request should be made for an empty update"
    assert "no fields" in str(excinfo.value).lower()


async def test_cats_errors_surface_as_tool_errors_not_raw_bodies():
    def handler(request):
        return httpx2.Response(404, text="<html>Not Found</html>" * 500)

    server = build_server(handler, discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("get_candidate", {"candidate_id": 999})

    message = str(excinfo.value)
    assert "No such record" in message
    assert len(message) < 1000, "error bodies must be truncated before the model sees them"


async def test_credentials_never_leak_through_a_tool_result():
    server = build_server(discovery_mode=DiscoveryMode.RAW)
    async with Client(server) as client:
        result = await client.call_tool("get_connection_status", {})
    assert "test-key" not in str(result.data)


# --- deployment safety -----------------------------------------------------


def test_http_refuses_to_start_without_an_explicit_auth_mode():
    """The pre-refactor server listened on 0.0.0.0:3000 with no auth at all.

    There is no default, because guessing wrong is harmful in both directions:
    assume a gateway that is not there and destructive tools sit on an open
    URL; assume none and a correctly-fronted deployment fails to start.
    """
    from cats_mcp.auth.verifier import InsecureDeploymentError

    with pytest.raises(InsecureDeploymentError) as excinfo:
        build_server(transport=Transport.HTTP)

    message = str(excinfo.value)
    for mode in ("platform", "jwt", "none"):
        assert mode in message, "the error must name every option"


def test_platform_mode_starts_and_records_the_assumption():
    """Horizon authenticates at its gateway before reaching server code."""
    server = build_server(transport=Transport.HTTP, auth_mode=AuthMode.PLATFORM)
    assert server is not None


def test_platform_mode_does_not_enforce_tool_scopes():
    """The gateway authenticates, but this server sees no claims to scope on.

    Attaching require_scopes with no verifier would deny every call.
    """
    from cats_mcp.auth.verifier import auth_is_enforced

    settings = Settings(
        api_key="k", transport=Transport.HTTP, auth_mode=AuthMode.PLATFORM
    )
    assert auth_is_enforced(settings) is False


def test_jwt_mode_requires_a_jwks_uri():
    from cats_mcp.auth.verifier import InsecureDeploymentError

    with pytest.raises(InsecureDeploymentError) as excinfo:
        build_server(transport=Transport.HTTP, auth_mode=AuthMode.JWT)
    assert "CATS_AUTH_JWKS_URI" in str(excinfo.value)


def test_a_jwks_uri_alone_implies_jwt_mode():
    """Configuring key verification is an unambiguous statement of intent."""
    from cats_mcp.auth.verifier import auth_is_enforced

    settings = Settings(
        api_key="k",
        transport=Transport.HTTP,
        auth_jwks_uri="https://issuer.example/.well-known/jwks.json",
    )
    assert auth_is_enforced(settings) is True


def test_none_mode_starts_for_local_development():
    server = build_server(transport=Transport.HTTP, auth_mode=AuthMode.NONE)
    assert server is not None


def test_stdio_does_not_require_an_auth_mode():
    """stdio is a pipe to a process the user started; there is no network surface."""
    server = build_server(transport=Transport.STDIO)
    assert server is not None


async def test_no_pinned_tool_is_listed_as_a_bm25_target():
    """Guards the trap the pinned-set change sprang.

    Pinning search_candidates silently broke a BM25 case: the transform drops
    already-visible tools from search results, so the assertion started failing
    on a tool that was working better than before.
    """
    targets = {
        expected
        for _query, expected in
        test_bm25_finds_the_right_tool_for_recruiting_language.pytestmark[0].args[1]
    }
    overlap = targets & set(PINNED_TOOLS)
    assert not overlap, f"pinned tools cannot be BM25 search targets: {sorted(overlap)}"
