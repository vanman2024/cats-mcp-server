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
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import ResponseStrategy
from cats_mcp.responses.links import (
    endpoint_returns_the_record,
    has_url_format,
    record_url,
)
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


# --- linking the right id --------------------------------------------------
#
# A tool is tagged with the resource it belongs to, not the shape of the rows it
# returns: `list_candidate_attachments` is resource="candidate" while each row is
# an attachment. Building the link from `spec.resource` alone therefore produced
# a candidate URL out of an attachment id - a working link to an unrelated real
# person. That is a 200 OK, so nothing anywhere reports a problem.


@pytest.mark.parametrize(
    "endpoint",
    [
        "/candidates",  # the collection itself
        "/candidates/{candidate_id}",  # one member of it
        "/candidates/search",  # still returns candidates
    ],
)
def test_endpoints_that_return_the_record_itself(endpoint):
    assert endpoint_returns_the_record("candidate", endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "/candidates/{candidate_id}/attachments",  # rows are attachments
        "/candidates/{candidate_id}/tags",  # rows are tags
        "/candidates/{candidate_id}/work_history",  # rows are work history
        "/candidates/{candidate_id}/emails/{email_id}",  # one email
        "/candidates/custom_fields",  # rows are field definitions
        "/candidates/custom_fields/{field_id}",  # one definition
    ],
)
def test_endpoints_whose_rows_are_something_else(endpoint):
    assert not endpoint_returns_the_record("candidate", endpoint)


def test_a_sub_collection_of_jobs_is_not_a_job():
    assert endpoint_returns_the_record("job", "/jobs")
    assert not endpoint_returns_the_record("job", "/jobs/statuses")
    assert not endpoint_returns_the_record("job", "/jobs/{job_id}/pipelines")


def test_an_unlinkable_resource_never_qualifies():
    assert not endpoint_returns_the_record("company", "/companies")


#: The complete set of tools permitted to emit a UI link. Pinned by name rather
#: than counted, so adding a tool that silently starts linking fails here with
#: the name of the offender.
MAY_EMIT_A_LINK = {
    "list_candidates",
    "get_candidate",
    "search_candidates",
    "filter_candidates",
    "list_jobs",
    "get_job",
    "search_jobs",
    "filter_jobs",
}


def test_only_the_approved_tools_emit_a_link():
    emitting = {
        spec.name
        for spec in REGISTRY
        if spec.response in (ResponseStrategy.SUMMARY, ResponseStrategy.DETAIL)
        and has_url_format(spec.resource)
        and endpoint_returns_the_record(spec.resource, spec.endpoint)
    }
    assert emitting == MAY_EMIT_A_LINK


async def test_a_sub_collection_row_gets_no_link():
    """An attachment id must never be dressed up as a candidate id."""

    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {
                    "attachments": [{"id": 55501, "filename": "resume.pdf"}]
                },
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "list_candidate_attachments", {"candidate_id": CANDIDATE_ID}
        )

    item = result.data["items"][0]
    assert "url" not in item, f"attachment 55501 was linked as a candidate: {item}"


async def test_a_membership_row_links_to_the_candidate_not_the_row():
    """Confirmed against the live account: row 390055557 holds candidate 397943414."""

    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 299,
                "_links": {},
                "_embedded": {
                    "items": [
                        {
                            "id": 390055557,
                            "candidate_id": 397943414,
                            "date_created": "2023-05-27T12:34:36-05:00",
                        }
                    ]
                },
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("list_candidate_list_items", {"list_id": 1610515})

    item = result.data["items"][0]
    assert item["url"].endswith("candidateID=397943414")
    assert "390055557" not in item["url"], "linked the membership row, not the person"
