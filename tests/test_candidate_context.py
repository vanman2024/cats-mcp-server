"""One compact view of many candidates, and the staging that makes it cheap.

The point of this tool is an asymmetry in the CATS API. There is no
candidate -> lists lookup, only list -> items, so membership is answered by
sweeping each list once and inverting it. That makes screening cost one pass per
*list* instead of one request per person - and it is why a set of 200 can be
checked against a Do Not Contact list for about the price of three requests,
while pulling their profiles would cost 200.

Everything here guards one of two properties: that the cheap path stays cheap,
and that the tool reports facts rather than verdicts.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.server import create_server

DNC_LIST = 1600001
ON_THE_LIST = 400000002
NOT_ON_THE_LIST = 400000009


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def membership_page(rows, has_next=False):
    return {
        "count": len(rows),
        "total": 299,
        "_links": {"next": {"href": "/x?page=2"}} if has_next else {},
        "_embedded": {"items": rows},
    }


def row(row_id, candidate_id):
    return {"id": row_id, "candidate_id": candidate_id, "date_created": "2023-05-27"}


def screening_handler(calls):
    """A DNC list of 299 across three pages of 100."""

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith(f"/lists/{DNC_LIST}"):
            return httpx2.Response(200, json={"id": DNC_LIST, "name": "Do Not Contact"})
        if path.endswith("/items"):
            page = int(request.url.params.get("page", 1))
            if page == 1:
                return httpx2.Response(
                    200, json=membership_page([row(390000001, ON_THE_LIST)], has_next=True)
                )
            if page == 2:
                return httpx2.Response(
                    200, json=membership_page([row(390000002, 400000003)], has_next=True)
                )
            return httpx2.Response(200, json=membership_page([row(390000003, 400000004)]))
        return httpx2.Response(200, json={})

    return handler


# --- the cheap screen -------------------------------------------------------


async def test_screening_costs_one_pass_per_list_not_per_candidate():
    """The claim the whole design rests on."""
    calls: list[str] = []
    many = list(range(400000001, 400000201))  # 200 candidates

    async with Client(build(screening_handler(calls))) as client:
        result = await client.call_tool(
            "get_candidate_context",
            {"candidate_ids": many, "include": ["lists"], "list_ids": [DNC_LIST]},
        )

    assert result.data["count"] == 200
    # One list detail + three pages. Not 200.
    assert len(calls) == 4, f"screening 200 candidates took {len(calls)} requests: {calls}"
    assert result.data["requests_used"] == 4


async def test_the_person_on_the_list_is_flagged_and_the_others_are_not():
    calls: list[str] = []
    async with Client(build(screening_handler(calls))) as client:
        result = await client.call_tool(
            "get_candidate_context",
            {
                "candidate_ids": [ON_THE_LIST, NOT_ON_THE_LIST],
                "include": ["lists"],
                "list_ids": [DNC_LIST],
            },
        )

    by_id = {c["candidate_id"]: c for c in result.data["candidates"]}
    assert by_id[ON_THE_LIST]["lists"] == [{"id": DNC_LIST, "name": "Do Not Contact"}]
    assert by_id[NOT_ON_THE_LIST]["lists"] == []


async def test_membership_is_keyed_on_candidate_id_not_the_row_id():
    """The row id and the candidate id are both real ids on the account.

    Keying on the wrong one silently reports the wrong person as blocked - or,
    worse, reports a blocked person as clear.
    """
    calls: list[str] = []
    async with Client(build(screening_handler(calls))) as client:
        result = await client.call_tool(
            "get_candidate_context",
            # 390000001 is the membership *row* id from page one.
            {"candidate_ids": [390000001], "include": ["lists"], "list_ids": [DNC_LIST]},
        )

    assert result.data["candidates"][0]["lists"] == [], (
        "a membership row id was treated as a candidate id"
    )


# --- facts, not verdicts ----------------------------------------------------


async def test_the_result_carries_no_verdict():
    """Deciding what membership means belongs to the caller, not here."""
    calls: list[str] = []
    async with Client(build(screening_handler(calls))) as client:
        result = await client.call_tool(
            "get_candidate_context",
            {"candidate_ids": [ON_THE_LIST], "include": ["lists"], "list_ids": [DNC_LIST]},
        )

    candidate = result.data["candidates"][0]
    for banned in ("eligibility", "excluded", "exclusion_reasons", "fit", "recommendation"):
        assert banned not in candidate, f"the adapter rendered a judgement: {banned}"


# --- staging is enforced, not suggested -------------------------------------


async def test_asking_for_per_candidate_data_on_a_large_set_is_refused():
    calls: list[str] = []
    many = list(range(400000001, 400000201))

    async with Client(build(screening_handler(calls))) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "get_candidate_context",
                {"candidate_ids": many, "include": ["identity"]},
            )

    message = str(excinfo.value)
    assert "Screen first" in message or "screen first" in message.lower()
    assert "lists" in message


async def test_the_refusal_explains_the_cheaper_path():
    """The error is the teaching channel for a model that read no documentation."""
    calls: list[str] = []
    many = list(range(400000001, 400000201))

    async with Client(build(screening_handler(calls))) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "get_candidate_context",
                {"candidate_ids": many, "include": ["pipelines"]},
            )

    message = str(excinfo.value)
    assert "one request per list" in message
    assert "50" in message


async def test_lists_without_list_ids_is_refused_with_a_way_forward():
    calls: list[str] = []
    async with Client(build(screening_handler(calls))) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "get_candidate_context",
                {"candidate_ids": [ON_THE_LIST], "include": ["lists"]},
            )

    assert "list_candidate_lists" in str(excinfo.value)


async def test_an_unknown_include_value_names_the_valid_ones():
    calls: list[str] = []
    async with Client(build(screening_handler(calls))) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "get_candidate_context",
                {"candidate_ids": [ON_THE_LIST], "include": ["resume"]},
            )

    message = str(excinfo.value)
    assert "resume" in message
    assert "pipelines" in message


# --- normalization ----------------------------------------------------------


async def test_pipeline_status_ids_are_resolved_to_names():
    """A status id is account-specific; 6377104 does not say Placed."""

    def handler(request):
        path = request.url.path
        if path.endswith("/pipelines/workflows"):
            return httpx2.Response(
                200,
                json={
                    "_embedded": {
                        "workflows": [
                            {"id": 1, "statuses": [{"id": 6377104, "title": "Placed"}]}
                        ]
                    }
                },
            )
        if "/pipelines" in path:
            return httpx2.Response(
                200,
                json={
                    "_embedded": {
                        "pipelines": [
                            {"id": 9, "job_id": 16796514, "status_id": 6377104}
                        ]
                    }
                },
            )
        return httpx2.Response(200, json={"id": ON_THE_LIST, "first_name": "Dana"})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_context",
            {"candidate_ids": [ON_THE_LIST], "include": ["pipelines"]},
        )

    pipeline = result.data["candidates"][0]["pipelines"][0]
    assert pipeline["status"] == "Placed"
    assert pipeline["status_id"] == 6377104, "the raw id must survive alongside the name"


async def test_an_unreadable_workflow_list_does_not_fail_the_call():
    """Unlabelled ids are worse than labelled ones and better than no answer."""

    def handler(request):
        path = request.url.path
        if path.endswith("/pipelines/workflows"):
            return httpx2.Response(403, json={"message": "no"})
        if "/pipelines" in path:
            return httpx2.Response(
                200,
                json={"_embedded": {"pipelines": [{"id": 9, "status_id": 6377104}]}},
            )
        return httpx2.Response(200, json={"id": ON_THE_LIST})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_context",
            {"candidate_ids": [ON_THE_LIST], "include": ["pipelines"]},
        )

    pipeline = result.data["candidates"][0]["pipelines"][0]
    assert pipeline["status_id"] == 6377104
    assert "status" not in pipeline


# --- partial failure --------------------------------------------------------


async def test_one_unreadable_list_does_not_discard_the_other():
    calls: list[str] = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/lists/999"):
            return httpx2.Response(404, json={"message": "gone"})
        if path.endswith(f"/lists/{DNC_LIST}"):
            return httpx2.Response(200, json={"id": DNC_LIST, "name": "Do Not Contact"})
        if path.endswith("/items"):
            return httpx2.Response(200, json=membership_page([row(390000001, ON_THE_LIST)]))
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_context",
            {
                "candidate_ids": [ON_THE_LIST],
                "include": ["lists"],
                "list_ids": [999, DNC_LIST],
            },
        )

    assert result.data["candidates"][0]["lists"] == [
        {"id": DNC_LIST, "name": "Do Not Contact"}
    ]
    assert any("999" in key for key in result.data["errors"])
