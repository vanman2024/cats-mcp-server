"""Custom field values, joined with names CATS never sends alongside them.

CATS answers a candidate's or job's custom field values with only
{"id": ..., "value": ...} per row - confirmed against the documented schema.
The name lives only in a separate definitions call that nothing joins
automatically. These tests guard the join itself, the case where an id no
longer has a definition, and that the definitions call is not repeated once
cached.
"""

from __future__ import annotations

import httpx2
from fastmcp import Client, FastMCP

from cats_mcp.composites import custom_field_values
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.http.custom_fields import CustomFieldResolver


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    resolver = CustomFieldResolver(lambda: client, StubCredentials())
    custom_field_values.register(mcp, lambda: client, resolver, enforce_auth=False)
    return mcp


def collection(key, rows):
    return {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}


DEFINITIONS = collection(
    "custom_fields",
    [
        {"id": 359950, "name": "LinkedIn Messaging Stage", "type": "dropdown"},
        {"id": 351005, "name": "Has Red Seal", "type": "dropdown"},
    ],
)


async def test_a_value_is_returned_with_its_name_and_type():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/custom_fields") and "candidates/42" not in request.url.path:
            return httpx2.Response(200, json=DEFINITIONS)
        if request.url.path == "/v3/candidates/42/custom_fields":
            return httpx2.Response(
                200, json=collection("custom_fields", [{"id": 359950, "value": "Message 1 Sent"}])
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_custom_field_values", {"candidate_ids": [42]}
        )

    record = result.structured_content["records"][0]
    field = record["fields"][0]
    assert field["id"] == "359950"
    assert field["name"] == "LinkedIn Messaging Stage"
    assert field["type"] == "dropdown"
    assert field["value"] == "Message 1 Sent"
    assert field["resolved"] is True


async def test_a_deleted_field_is_reported_unresolved_not_dropped():
    """The value CATS still returns must not be silently thrown away."""

    def handler(request):
        if request.url.path.endswith("/custom_fields") and "candidates/42" not in request.url.path:
            return httpx2.Response(200, json=DEFINITIONS)
        if request.url.path == "/v3/candidates/42/custom_fields":
            return httpx2.Response(
                200,
                json=collection(
                    "custom_fields", [{"id": 999999, "value": "orphaned answer"}]
                ),
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_custom_field_values", {"candidate_ids": [42]}
        )

    field = result.structured_content["records"][0]["fields"][0]
    assert field["resolved"] is False
    assert field["name"] == ""
    assert field["value"] == "orphaned answer", "the value must survive even when unresolved"


async def test_definitions_are_fetched_once_for_a_batch_not_per_candidate():
    definition_calls = 0

    def handler(request):
        nonlocal definition_calls
        if request.url.path == "/v3/candidates/custom_fields":
            definition_calls += 1
            return httpx2.Response(200, json=DEFINITIONS)
        return httpx2.Response(
            200, json=collection("custom_fields", [{"id": 351005, "value": "Yes"}])
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_custom_field_values", {"candidate_ids": [1, 2, 3]}
        )

    assert definition_calls == 1, "one definitions call for the whole batch, not one per candidate"
    assert len(result.structured_content["records"]) == 3
    assert result.structured_content["execution"]["requests_used"] == 4  # 1 defs + 3 records


async def test_a_failed_record_reports_an_error_not_a_crash():
    def handler(request):
        if request.url.path == "/v3/candidates/custom_fields":
            return httpx2.Response(200, json=DEFINITIONS)
        if "candidates/1/" in request.url.path:
            return httpx2.Response(404, json={"message": "not found"})
        return httpx2.Response(200, json=collection("custom_fields", []))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_custom_field_values", {"candidate_ids": [1, 2]}
        )

    records = {r["record_id"]: r for r in result.structured_content["records"]}
    assert records[1]["error"] is not None
    assert records[1]["fields"] == []
    assert records[2]["error"] is None
