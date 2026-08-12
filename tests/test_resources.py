"""MCP resources - the adapter's read-only self-description and account context.

A tools-only server can be used but not understood. These resources let a
consumer answer "what am I connected to, what is it for, and what are this
account's ids" without invoking an action.

They are also where the architectural boundary is stated in machine-readable
form, so a consumer does not have to infer it from tool names.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.server import create_server


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key="test-key", base_url="https://api.catsone.com/v3", account_label="test"
        )

    def describe(self) -> str:
        return "stub"


def build(handler=None, **overrides):
    overrides.setdefault("discovery_mode", DiscoveryMode.RAW)
    settings = Settings(api_key="test-key", **overrides)
    handler = handler or (lambda request: httpx2.Response(200, json={}))
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


async def read_json(client: Client, uri: str) -> dict:
    contents = await client.read_resource(uri)
    return json.loads(contents[0].text)


# --- discovery -------------------------------------------------------------


async def test_resources_are_listed():
    async with Client(build()) as client:
        uris = {str(r.uri) for r in await client.list_resources()}
    assert "cats://server/capabilities" in uris
    assert "cats://account/rate-limit" in uris


async def test_templated_resource_is_listed():
    async with Client(build()) as client:
        templates = {str(t.uri_template) for t in await client.list_resource_templates()}
    assert "cats://reference/custom-fields/{resource}" in templates


# --- self-description ------------------------------------------------------


async def test_capabilities_states_what_the_server_owns_and_does_not():
    async with Client(build()) as client:
        data = await read_json(client, "cats://server/capabilities")

    assert "CATS API authentication and requests" in data["owns"]
    for excluded in ("recruiting workflows", "agent orchestration", "agent memory"):
        assert excluded in data["does_not_own"]
    assert "does not decide who to contact" in data["boundary"]


async def test_capabilities_reports_live_configuration():
    async with Client(build(discovery_mode=DiscoveryMode.RAW)) as client:
        data = await read_json(client, "cats://server/capabilities")

    assert data["configuration"]["discovery_mode"] == "raw"
    assert data["configuration"]["tools_registered"] > 100


async def test_capabilities_never_leaks_the_credential():
    async with Client(build()) as client:
        data = await read_json(client, "cats://server/capabilities")
    assert "test-key" not in json.dumps(data)


async def test_tool_catalog_summarises_without_dumping_schemas():
    async with Client(build()) as client:
        data = await read_json(client, "cats://server/tools")

    assert data["endpoint_tools"] > 100
    assert data["by_safety"]["destructive"] > 0
    assert data["scopes"]["destructive"] == "cats:destructive"
    # A summary, not the catalog itself.
    assert "inputSchema" not in json.dumps(data)


# --- account context -------------------------------------------------------


async def test_rate_limit_resource_reports_the_standard_ceiling():
    async with Client(build()) as client:
        data = await read_json(client, "cats://account/rate-limit")
    assert data["standard_ceiling"] == 500


async def test_rate_limit_resource_reflects_observed_headers():
    def handler(request):
        return httpx2.Response(
            200,
            json={},
            headers={"X-Rate-Limit-Limit": "1500", "X-Rate-Limit-Remaining": "1200"},
        )

    server = build(handler)
    async with Client(server) as client:
        await client.call_tool("get_site", {})
        data = await read_json(client, "cats://account/rate-limit")

    assert data["limit"] == 1500
    assert data["remaining"] == 1200


async def test_site_resource_returns_account_info():
    def handler(request):
        return httpx2.Response(200, json={"id": 42, "name": "Big Country"})

    async with Client(build(handler)) as client:
        data = await read_json(client, "cats://account/site")

    assert data["name"] == "Big Country"


async def test_resource_surfaces_api_errors_without_raising():
    """A failed resource read should explain itself, not break the client."""

    def handler(request):
        return httpx2.Response(500, text="boom")

    async with Client(build(handler, max_retries=1)) as client:
        data = await read_json(client, "cats://account/site")

    assert "error" in data


# --- account-specific reference data ---------------------------------------


async def test_workflows_resource_resolves_status_ids():
    """Status ids are account-specific; nothing can change a stage without them."""

    def handler(request):
        path = request.url.path
        if path.endswith("/statuses"):
            return httpx2.Response(
                200,
                json={
                    "_embedded": {
                        "statuses": [
                            {"id": 6377094, "title": "New Candidate"},
                            {"id": 6377103, "title": "Placed"},
                        ]
                    }
                },
            )
        return httpx2.Response(
            200,
            json={"_embedded": {"workflows": [{"id": 5691190, "title": "General"}]}},
        )

    async with Client(build(handler)) as client:
        data = await read_json(client, "cats://reference/workflows")

    workflow = data["workflows"][0]
    assert workflow["workflow_id"] == 5691190
    titles = {s["title"] for s in workflow["statuses"]}
    assert "Placed" in titles


async def test_custom_field_definitions_resolve_ids_to_names():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "_embedded": {
                    "custom_fields": [
                        {"id": 351005, "name": "Has Red Seal", "type": "dropdown"}
                    ]
                }
            },
        )

    async with Client(build(handler)) as client:
        data = await read_json(client, "cats://reference/custom-fields/candidates")

    assert data["fields"][0]["id"] == 351005
    assert data["fields"][0]["name"] == "Has Red Seal"


async def test_custom_fields_rejects_an_unsupported_resource_clearly():
    async with Client(build()) as client:
        data = await read_json(client, "cats://reference/custom-fields/aliens")

    assert "error" in data
    assert "candidates" in data["supported"]


@pytest.mark.parametrize(
    "uri",
    [
        "cats://server/capabilities",
        "cats://server/tools",
        "cats://account/rate-limit",
    ],
)
async def test_offline_resources_make_no_api_calls(uri):
    """Self-description must work even when CATS is unreachable."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx2.Response(500, text="unavailable")

    async with Client(build(handler)) as client:
        await read_json(client, uri)

    assert calls["n"] == 0
