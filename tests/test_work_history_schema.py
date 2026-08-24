"""The work-history write path, and the wire shape it actually accepts.

`create_candidate_work_history` failed against live CATS with two validation
errors at once - `employer.name must not be empty` and `employer.linked must
be of type boolean` - while sending a `company` field that was neither empty
nor a candidate for a boolean. The value was fine; the shape was wrong. CATS
attaches an employer through a nested `{"name": ..., "linked": ...}` object,
not a flat `company` string, and no mock built from the flat assumption would
have caught it.

Same failure mode as issue #15's `create_task` bug, so the same fix: assert on
the outgoing request body, not just the spec.
"""

from __future__ import annotations

import json

import httpx2
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.server import create_server


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def test_the_company_param_is_sent_as_a_nested_employer_object():
    spec = REGISTRY.by_name("create_candidate_work_history")
    company = next(p for p in spec.params if p.name == "company")
    assert company.outbound_name == "employer"


async def test_create_candidate_work_history_sends_the_shape_cats_accepts():
    sent: dict[str, object] = {}

    def handler(request):
        if request.url.path.endswith("/work_history") and request.method == "POST":
            sent.update(json.loads(request.content))
            return httpx2.Response(201, json={"id": 999})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        await client.call_tool(
            "create_candidate_work_history",
            {
                "candidate_id": 401138551,
                "company": "Acme Mining",
                "title": "Electrician",
                "start_date": "2022-01-01",
            },
        )

    assert sent.get("employer") == {"name": "Acme Mining", "linked": False}, sent
    assert "company" not in sent, "the tool-facing name must not reach CATS"
