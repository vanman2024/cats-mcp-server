"""Deriving the CATS web-UI domain from the account, not from configuration.

`CATS_UI_BASE_URL` is one value for the whole process. That is fine while one
deployment serves one CATS account, and wrong the moment two callers bring
different accounts: one of them gets links into the other company's CATS.

`GET /site` returns the subdomain for whichever credential made the call, so the
domain can be derived per account instead. These tests pin that, and pin the
failure behaviour - a link is a convenience and must never cost an answer.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.http.site import UIDomainResolver
from cats_mcp.server import create_server

CANDIDATE_ID = 400000001


class KeyedCredentials(CredentialProvider):
    """Returns whichever key it is currently set to, like a per-request provider."""

    def __init__(self, api_key: str = "key-a") -> None:
        self.api_key = api_key

    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key=self.api_key, base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "keyed"


def site_response(subdomain: str) -> dict:
    return {"id": 91508, "mode": "hr", "subdomain": subdomain, "default_company_id": 1}


def make_client(handler, credentials) -> CATSClient:
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    return CATSClient(settings, credentials, transport=httpx2.MockTransport(handler))


# --- deriving the domain ---------------------------------------------------


async def test_the_domain_comes_from_the_accounts_own_subdomain():
    def handler(request):
        return httpx2.Response(200, json=site_response("acme"))

    credentials = KeyedCredentials()
    client = make_client(handler, credentials)
    resolver = UIDomainResolver(lambda: client, credentials)

    assert await resolver.resolve() == "https://acme.catsone.com"


async def test_the_lookup_happens_once_per_account():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(200, json=site_response("acme"))

    credentials = KeyedCredentials()
    client = make_client(handler, credentials)
    resolver = UIDomainResolver(lambda: client, credentials)

    for _ in range(5):
        await resolver.resolve()

    assert len(calls) == 1, f"/site was fetched {len(calls)} times"


async def test_two_accounts_get_their_own_domains():
    """The reason this is derived rather than configured."""

    def handler(request):
        token = request.headers["Authorization"]
        subdomain = "acme" if token.endswith("key-a") else "globex"
        return httpx2.Response(200, json=site_response(subdomain))

    credentials = KeyedCredentials("key-a")
    client = make_client(handler, credentials)
    resolver = UIDomainResolver(lambda: client, credentials)

    assert await resolver.resolve() == "https://acme.catsone.com"

    credentials.api_key = "key-b"
    assert await resolver.resolve() == "https://globex.catsone.com"

    # ...and the first account is still cached under its own key, not evicted.
    credentials.api_key = "key-a"
    assert await resolver.resolve() == "https://acme.catsone.com"


async def test_a_configured_value_wins_and_costs_no_request():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(200, json=site_response("derived"))

    credentials = KeyedCredentials()
    client = make_client(handler, credentials)
    resolver = UIDomainResolver(lambda: client, credentials, "https://vanity.example.com")

    assert await resolver.resolve() == "https://vanity.example.com"
    assert calls == [], "an explicit domain should not trigger a lookup"


def test_a_configured_trailing_slash_does_not_survive():
    resolver = UIDomainResolver(lambda: None, KeyedCredentials(), "https://acme.catsone.com/")
    assert resolver._configured == "https://acme.catsone.com"


# --- failing safely --------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda r: httpx2.Response(403, json={"message": "no"}), id="forbidden"),
        pytest.param(lambda r: httpx2.Response(404, json={"message": "gone"}), id="missing"),
        pytest.param(lambda r: httpx2.Response(200, json={"id": 1}), id="no-subdomain"),
    ],
)
async def test_an_unavailable_site_endpoint_yields_no_domain(handler):
    credentials = KeyedCredentials()
    client = make_client(handler, credentials)
    resolver = UIDomainResolver(lambda: client, credentials)

    assert await resolver.resolve() == ""


async def test_a_failed_lookup_is_not_retried_on_every_call():
    """Otherwise one missing link becomes a steady drain on a 500/hour budget."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(403, json={"message": "no"})

    credentials = KeyedCredentials()
    client = make_client(handler, credentials)
    resolver = UIDomainResolver(lambda: client, credentials)

    for _ in range(4):
        await resolver.resolve()

    assert len(calls) == 1, f"a failing /site was fetched {len(calls)} times"


# --- end to end ------------------------------------------------------------


async def test_a_tool_result_carries_a_link_with_nothing_configured():
    """No CATS_UI_BASE_URL anywhere - the domain comes from the account."""

    def handler(request):
        if request.url.path.endswith("/site"):
            return httpx2.Response(200, json=site_response("acme"))
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {"candidates": [{"id": CANDIDATE_ID, "first_name": "Dana"}]},
            },
        )

    credentials = KeyedCredentials()
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, credentials, transport=httpx2.MockTransport(handler))
    server = create_server(settings, credential_provider=credentials, client=client)

    async with Client(server) as mcp_client:
        result = await mcp_client.call_tool("list_candidates", {})

    assert result.data["items"][0]["url"] == (
        f"https://acme.catsone.com"
        f"/index.php?m=candidates&a=show&candidateID={CANDIDATE_ID}"
    )


async def test_a_tool_that_cannot_link_never_probes_for_the_domain():
    """Most tools can never attach a URL, so they must not pay for looking one up."""
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {"tags": [{"id": 55, "title": "Red Seal"}]},
            },
        )

    credentials = KeyedCredentials()
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, credentials, transport=httpx2.MockTransport(handler))
    server = create_server(settings, credential_provider=credentials, client=client)

    async with Client(server) as mcp_client:
        await mcp_client.call_tool("list_candidate_tags", {"candidate_id": CANDIDATE_ID})

    assert not [p for p in paths if p.endswith("/site")], paths
