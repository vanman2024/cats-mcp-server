"""Saved-list membership rows are not the records they point at.

A candidate list row has its own `id` - the membership row - and carries the
candidate in `candidate_id`. Treating the row id as a candidate id produces
answers that look right and are wrong.

This mattered in practice on a Do Not Contact list: the compact projection was
applying the *candidate* summary fields to membership rows, which kept the row
id in `id` and dropped `candidate_id` entirely. Anyone comparing candidate ids
against that output would conclude nobody was on the list. On a DNC list that
means contacting someone who asked not to be.

It was also emitting a CATS link built from the row id, pointing at a different
person's record.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.responses.links import record_url
from cats_mcp.server import create_server

UI = "https://acme.catsone.com"
ROW_ID = 88881111
CANDIDATE_ID = 400000001


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW, ui_base_url=UI)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def membership_rows(request):
    return httpx2.Response(
        200,
        json={
            "count": 2,
            "total": 299,
            "_links": {"next": {"href": f"{UI}/x?page=2"}},
            "_embedded": {
                "items": [
                    {"id": ROW_ID, "candidate_id": CANDIDATE_ID, "date_created": "2026-01-01"},
                    {"id": ROW_ID + 1, "candidate_id": 111222333, "date_created": "2026-01-02"},
                ]
            },
        },
    )


# --- the field that matters ------------------------------------------------


async def test_membership_rows_expose_the_candidate_id():
    """Without this the whole endpoint is unusable for a membership check."""
    async with Client(build(membership_rows)) as client:
        result = await client.call_tool("list_candidate_list_items", {"list_id": 1})

    ids = [row["candidate_id"] for row in result.data["items"]]
    assert ids == [CANDIDATE_ID, 111222333]


async def test_the_row_id_is_not_mistaken_for_a_candidate_id():
    async with Client(build(membership_rows)) as client:
        result = await client.call_tool("list_candidate_list_items", {"list_id": 1})

    row = result.data["items"][0]
    assert row["id"] == ROW_ID
    assert row["id"] != row["candidate_id"]


async def test_no_link_is_built_from_a_membership_row_id():
    """It previously produced a candidate URL pointing at a different person.

    The row id and the candidate id are both real ids on this account, so the
    wrong one still resolves - to somebody else. Nothing 404s and nothing warns.
    """
    async with Client(build(membership_rows)) as client:
        result = await client.call_tool("list_candidate_list_items", {"list_id": 1})

    for row in result.data["items"]:
        assert str(row["id"]) not in row.get("url", ""), (
            "a link built from a row id points at the wrong record"
        )


async def test_a_membership_row_links_to_the_person_it_names():
    """`candidate_id` is a real top-level field, so the link can be built correctly."""
    async with Client(build(membership_rows)) as client:
        result = await client.call_tool("list_candidate_list_items", {"list_id": 1})

    for row in result.data["items"]:
        assert row["url"].endswith(f"candidateID={row['candidate_id']}")


def test_list_membership_resources_have_no_url_format():
    for resource in ("list_item", "record_list"):
        assert record_url(UI, resource, 1) is None, resource


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "list_candidate_list_items",
        "get_candidate_list_item",
        "list_job_list_items",
        "get_job_list_item",
    ],
)
def test_membership_tools_are_not_classified_as_the_record_type(name):
    """resource='candidate' made the candidate summary fields apply, which is
    what dropped candidate_id in the first place."""
    spec = REGISTRY.by_name(name)
    assert spec is not None, name
    assert spec.resource == "list_item", f"{name} is {spec.resource!r}"


@pytest.mark.parametrize(
    "name",
    ["list_candidate_lists", "get_candidate_list", "list_job_lists", "get_job_list"],
)
def test_list_tools_are_classified_as_lists(name):
    spec = REGISTRY.by_name(name)
    assert spec is not None, name
    assert spec.resource == "record_list", f"{name} is {spec.resource!r}"


async def test_saved_lists_return_their_names():
    """The candidate projection kept only `id`, so lists came back unnamed."""

    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {
                    "lists": [{"id": 5, "name": "Do Not Contact", "total": 299}]
                },
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("list_candidate_lists", {})

    assert result.data["items"][0]["name"] == "Do Not Contact"


# --- the description an agent reads ----------------------------------------


def test_the_description_warns_about_the_id_confusion():
    description = REGISTRY.by_name("list_candidate_list_items").description
    assert "NOT the candidate id" in description
    assert "candidate_id" in description


def test_the_description_no_longer_promises_candidate_ids():
    """It used to say 'enumerate candidates and obtain their ids', which is false."""
    description = REGISTRY.by_name("list_candidate_list_items").description
    assert "obtain their ids" not in description


def test_the_description_points_at_the_bulk_route():
    """299 rows is three calls at per_page=100, or 299 one at a time."""
    description = REGISTRY.by_name("list_candidate_list_items").description
    assert "per_page=100" in description


# --- recovering ids from HAL plumbing --------------------------------------
# A membership row may carry the candidate only in `_links.candidate.href` or
# `_embedded.candidate`. Stripping HAL without harvesting those left a row that
# identified nobody - which is what made the endpoint look like it could only
# be resolved one item at a time. It cannot: 299 rows is 3 calls at per_page=100.


def _row(item):
    from cats_mcp.responses.shaping import shape_list

    payload = {"count": 1, "total": 299, "_links": {}, "_embedded": {"items": [item]}}
    return shape_list(REGISTRY.by_name("list_candidate_list_items"), payload, {})["items"][0]


def test_candidate_id_is_recovered_from_a_link_href():
    row = _row(
        {
            "id": ROW_ID,
            "_links": {
                "self": {"href": f"/candidates/lists/1600001/items/{ROW_ID}"},
                "candidate": {"href": f"https://api.catsone.com/v3/candidates/{CANDIDATE_ID}"},
            },
        }
    )
    assert row["candidate_id"] == CANDIDATE_ID


def test_candidate_id_is_recovered_from_an_embedded_record():
    row = _row(
        {"id": ROW_ID, "_embedded": {"candidate": {"id": CANDIDATE_ID, "first_name": "Brian"}}}
    )
    assert row["candidate_id"] == CANDIDATE_ID


def test_a_top_level_field_wins_over_the_link():
    """Harvesting fills gaps; it never overwrites what the API stated directly."""
    row = _row(
        {
            "id": ROW_ID,
            "candidate_id": 1,
            "_links": {"candidate": {"href": "/v3/candidates/999"}},
        }
    )
    assert row["candidate_id"] == 1


def test_self_and_navigation_links_are_not_harvested():
    """`self` points at the row itself; treating it as a reference is circular."""
    row = _row(
        {
            "id": ROW_ID,
            "_links": {
                "self": {"href": f"/candidates/lists/1/items/{ROW_ID}"},
                "next": {"href": "/candidates/lists/1/items?page=2"},
            },
        }
    )
    assert "self_id" not in row
    assert "next_id" not in row


def test_hal_plumbing_is_still_stripped_from_the_result():
    row = _row({"id": ROW_ID, "_links": {"candidate": {"href": "/v3/candidates/5"}}})
    assert "_links" not in row
    assert "_embedded" not in row


def test_a_href_without_a_numeric_tail_yields_nothing():
    row = _row({"id": ROW_ID, "_links": {"candidate": {"href": "/v3/candidates/search"}}})
    assert "candidate_id" not in row


async def test_a_whole_list_is_retrievable_in_one_page():
    """The premise being corrected: this endpoint is not one-item-at-a-time."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx2.Response(
            200,
            json={
                "count": 100,
                "total": 299,
                "_links": {"next": {"href": "https://api.catsone.com/v3/x?page=2"}},
                "_embedded": {
                    "items": [
                        {"id": i, "_links": {"candidate": {"href": f"/v3/candidates/{i + 1000}"}}}
                        for i in range(100)
                    ]
                },
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "list_candidate_list_items", {"list_id": 1600001, "per_page": 100}
        )

    assert "per_page=100" in seen["url"]
    assert len(result.data["items"]) == 100
    assert result.data["total"] == 299
    assert result.data["has_more"] is True
    # Every row resolves to a candidate - no per-item lookup needed.
    assert all(row.get("candidate_id") for row in result.data["items"])
