"""`update_candidate` could change a name and nothing else.

CATS documents `title` and `current_employer` as plain string fields on both
`POST /candidates` and `PUT /candidates/{id}`
(https://docs.catsone.com/api/v3/#candidates). Neither was in either spec -
`create_candidate` had no `title` at all, and `update_candidate` had no way
to change title or employer once a record existed. There was no live
reproduction needed here the way there was for create_task or
create_candidate_work_history: these are undecorated string fields, the same
treatment first_name/last_name already get successfully in this same body.
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


def test_create_candidate_exposes_title_and_current_employer():
    spec = REGISTRY.by_name("create_candidate")
    names = {p.name for p in spec.params}
    assert "title" in names
    assert "current_employer" in names


def test_update_candidate_exposes_title_and_current_employer():
    spec = REGISTRY.by_name("update_candidate")
    names = {p.name for p in spec.params}
    assert "title" in names
    assert "current_employer" in names


async def test_update_candidate_sends_title_and_employer_on_the_wire():
    sent: dict[str, object] = {}

    def handler(request):
        if request.method == "PUT" and "/candidates/" in request.url.path:
            sent.update(json.loads(request.content))
            return httpx2.Response(200, json={"id": 401138551})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        await client.call_tool(
            "update_candidate",
            {
                "candidate_id": 401138551,
                "title": "Heavy Duty Equipment Technician",
                "current_employer": "Mader Group Canada",
            },
        )

    assert sent.get("title") == "Heavy Duty Equipment Technician", sent
    assert sent.get("current_employer") == "Mader Group Canada", sent
