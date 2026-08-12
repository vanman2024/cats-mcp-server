"""Links back into the CATS web UI.

Consumers were building these by hand and getting them wrong - a REST-looking
`https://{account}.catsone.com/candidates/{id}` path that does not exist in
CATS. Those links look correct in a spreadsheet and 404 when clicked, which is
the worst kind of wrong: nothing fails until a person tries to use it.

The adapter knows the account and the id, so it emits the link itself.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.responses.links import record_url
from cats_mcp.server import create_server

UI = "https://bigcountryequipmentrepair.catsone.com"
CANDIDATE_ID = 407813885


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler, ui_base_url=UI):
    settings = Settings(
        api_key="k", discovery_mode=DiscoveryMode.RAW, ui_base_url=ui_base_url
    )
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def candidate_list(request):
    return httpx2.Response(
        200,
        json={
            "count": 1,
            "total": 1,
            "_links": {},
            "_embedded": {
                "candidates": [{"id": CANDIDATE_ID, "first_name": "Dana", "city": "Kamloops"}]
            },
        },
    )


# --- the format ------------------------------------------------------------


def test_candidate_url_uses_the_query_string_form():
    """CATS uses index.php?m=...&a=show, not a REST-style path."""
    url = record_url(UI, "candidate", CANDIDATE_ID)
    assert url == f"{UI}/index.php?m=candidates&a=show&candidateID={CANDIDATE_ID}"


def test_the_rest_style_path_is_never_produced():
    """`/candidates/{id}` was the wrong form consumers kept inventing."""
    url = record_url(UI, "candidate", CANDIDATE_ID)
    assert f"/candidates/{CANDIDATE_ID}" not in url


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    assert "//index.php" not in record_url(UI + "/", "candidate", 1)


# --- when it cannot be built correctly -------------------------------------


def test_no_link_without_a_configured_domain():
    """The subdomain is account-specific and cannot be derived from the API URL."""
    assert record_url("", "candidate", CANDIDATE_ID) is None


def test_job_url_uses_the_joborders_module():
    """The UI vocabulary does not match the API's: joborders, not jobs."""
    url = record_url(UI, "job", 16796514)
    assert url == f"{UI}/index.php?m=joborders&a=show&jobOrderID=16796514"


def test_job_url_does_not_borrow_the_api_naming():
    url = record_url(UI, "job", 16796514)
    assert "m=jobs" not in url
    assert "jobId" not in url


def test_no_link_for_a_resource_with_no_confirmed_format():
    """Guessing a pattern would reproduce the exact bug this prevents."""
    for resource in ("company", "contact", "pipeline", "activity"):
        assert record_url(UI, resource, 1) is None, resource


def test_no_link_without_an_id():
    assert record_url(UI, "candidate", None) is None
    assert record_url(UI, "candidate", "") is None


# --- through a real tool call ----------------------------------------------


async def test_list_results_carry_a_working_link():
    async with Client(build(candidate_list)) as client:
        result = await client.call_tool("list_candidates", {})

    item = result.data["items"][0]
    assert item["url"] == (
        f"{UI}/index.php?m=candidates&a=show&candidateID={CANDIDATE_ID}"
    )


async def test_results_carry_no_link_when_the_domain_is_unset():
    """A missing link is recoverable; a wrong one gets pasted into a spreadsheet."""
    async with Client(build(candidate_list, ui_base_url="")) as client:
        result = await client.call_tool("list_candidates", {})

    assert "url" not in result.data["items"][0]


@pytest.mark.parametrize("summary_level", ["compact", "standard", "full"])
async def test_the_link_survives_every_summary_level(summary_level):
    async with Client(build(candidate_list)) as client:
        result = await client.call_tool(
            "list_candidates", {"summary_level": summary_level}
        )

    assert "url" in result.data["items"][0], summary_level


async def test_a_detail_result_also_carries_the_link():
    def handler(request):
        return httpx2.Response(200, json={"id": CANDIDATE_ID, "first_name": "Dana"})

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate", {"candidate_id": CANDIDATE_ID})

    assert str(CANDIDATE_ID) in result.data["url"]
    assert "index.php" in result.data["url"]


async def test_job_results_carry_a_working_link():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {"jobs": [{"id": 16796514, "title": "Heavy Duty Mechanic"}]},
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("list_jobs", {})

    assert result.data["items"][0]["url"] == (
        f"{UI}/index.php?m=joborders&a=show&jobOrderID=16796514"
    )
