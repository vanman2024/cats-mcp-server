"""Composite read primitives.

These exist because of the rate limit, so the tests assert the two properties
that justify them: fewer round trips than doing it by hand, and no oversized
intermediate data reaching the caller.

They also assert the boundary: composites return facts, never decisions about
who to contact or how to rank people. That belongs to the orchestrator.
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


def build(handler, **overrides):
    settings = Settings(api_key="test-key", discovery_mode=DiscoveryMode.RAW, **overrides)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def candidate(cid: int) -> dict:
    """A realistically fat candidate record."""
    return {
        "id": cid,
        "first_name": f"Cand{cid}",
        "last_name": "Reid",
        "title": "Journeyman Welder",
        "city": "Kamloops",
        "state": "BC",
        "is_hot": False,
        "date_modified": "2026-08-01T10:00:00-00:00",
        "resume_text": "x" * 10_000,
        "custom_fields": {"351005": "Yes"},
        "_links": {"self": {"href": "..."}},
    }


# --- batch summaries -------------------------------------------------------


async def test_batch_summaries_returns_compact_rows_not_full_records():
    def handler(request):
        cid = int(request.url.path.rsplit("/", 1)[-1])
        return httpx2.Response(200, json=candidate(cid))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_summaries", {"candidate_ids": [1, 2, 3]}
        )

    payload = json.dumps(result.data)
    assert result.data["count"] == 3
    assert "resume_text" not in payload
    assert "custom_fields" not in payload
    assert "_links" not in payload
    assert "Kamloops" in payload


async def test_batch_summaries_reports_request_cost():
    """Callers need to know what a batch spent against a 500/hour budget."""

    def handler(request):
        return httpx2.Response(
            200,
            json=candidate(1),
            headers={"X-Rate-Limit-Limit": "500", "X-Rate-Limit-Remaining": "480"},
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_summaries", {"candidate_ids": [1, 2]})

    assert result.data["requests_used"] == 2
    assert result.data["rate_limit"]["remaining"] == 480


async def test_batch_is_capped_and_says_so():
    """An unbounded batch could spend an entire hourly budget in one call."""

    def handler(request):
        return httpx2.Response(200, json=candidate(1))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_summaries", {"candidate_ids": list(range(1, 200))}
        )

    assert result.data["truncated"] is True
    assert result.data["requests_used"] <= 50


async def test_duplicate_ids_are_not_fetched_twice():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx2.Response(200, json=candidate(1))

    async with Client(build(handler)) as client:
        await client.call_tool("get_candidate_summaries", {"candidate_ids": [1, 1, 1, 2]})

    assert calls["n"] == 2


async def test_one_bad_id_does_not_fail_the_whole_batch():
    """Failing the batch would waste every request already spent."""

    def handler(request):
        cid = int(request.url.path.rsplit("/", 1)[-1])
        if cid == 2:
            return httpx2.Response(404, text="gone")
        return httpx2.Response(200, json=candidate(cid))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_summaries", {"candidate_ids": [1, 2, 3]}
        )

    assert result.data["count"] == 2
    assert "2" in result.data["errors"]


# --- engagement ------------------------------------------------------------


async def test_engagement_reports_last_contact_without_dumping_activities():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "total": 3,
                "_embedded": {
                    "activities": [
                        {"id": 1, "type": "Call", "date_created": "2026-01-05T09:00:00-00:00",
                         "notes": "n" * 5000},
                        {"id": 2, "type": "Email", "date_created": "2026-07-20T09:00:00-00:00",
                         "notes": "n" * 5000},
                    ]
                },
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_engagement", {"candidate_ids": [1]})

    row = result.data["engagement"][0]
    assert row["last_activity_date"] == "2026-07-20T09:00:00-00:00"
    assert row["last_activity_type"] == "Email"
    assert row["activity_count"] == 3
    assert row["has_been_contacted"] is True
    # The activity bodies must not come along for the ride.
    assert "nnnn" not in json.dumps(result.data)


async def test_engagement_flags_never_contacted_candidates():
    def handler(request):
        return httpx2.Response(200, json={"total": 0, "_embedded": {"activities": []}})

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_engagement", {"candidate_ids": [1]})

    row = result.data["engagement"][0]
    assert row["has_been_contacted"] is False
    assert row["last_activity_date"] is None


async def test_engagement_sorts_coldest_first():
    dates = {1: "2026-07-01T00:00:00-00:00", 2: "2026-01-01T00:00:00-00:00"}

    def handler(request):
        cid = int(request.url.path.split("/")[-2])
        return httpx2.Response(
            200,
            json={
                "total": 1,
                "_embedded": {
                    "activities": [
                        {"id": 1, "type": "Call", "date_created": dates[cid]}
                    ]
                },
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_engagement", {"candidate_ids": [1, 2]})

    order = [r["candidate_id"] for r in result.data["engagement"]]
    assert order == ["2", "1"], "oldest contact should come first"


async def test_engagement_does_not_recommend_who_to_contact():
    """Boundary check: this server reports facts, it does not decide outreach."""

    def handler(request):
        return httpx2.Response(200, json={"total": 0, "_embedded": {"activities": []}})

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_engagement", {"candidate_ids": [1]})

    payload = json.dumps(result.data).lower()
    for word in ("recommend", "should_contact", "priority", "score", "ranking"):
        assert word not in payload, f"composite leaked a policy decision: {word}"


# --- change feed -----------------------------------------------------------


async def test_changed_records_requires_a_starting_point():
    """Without one this would return the entire event history."""

    def handler(request):
        return httpx2.Response(200, json={"_embedded": {"events": []}})

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_changed_records", {})

    assert "error" in result.data


async def test_changed_records_returns_a_cursor_for_the_next_call():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "_embedded": {
                    "events": [
                        {"id": 10, "event": "candidate.created", "regarding_id": 1,
                         "date_created": "2026-08-01T00:00:00-00:00"},
                        {"id": 12, "event": "candidate.updated", "regarding_id": 2,
                         "date_created": "2026-08-02T00:00:00-00:00"},
                    ]
                }
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_changed_records", {"since_event_id": 1})

    assert result.data["latest_event_id"] == 12
    assert result.data["count"] == 2


async def test_changed_records_can_filter_by_event_type():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "_embedded": {
                    "events": [
                        {"id": 10, "event": "candidate.created", "regarding_id": 1,
                         "date_created": "2026-08-01T00:00:00-00:00"},
                        {"id": 11, "event": "job.updated", "regarding_id": 5,
                         "date_created": "2026-08-01T00:00:00-00:00"},
                    ]
                }
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_changed_records", {"since_event_id": 1, "event_types": "candidate.created"}
        )

    assert result.data["count"] == 1
    assert result.data["changes"][0]["event"] == "candidate.created"


# --- job pool --------------------------------------------------------------


async def test_job_pool_joins_pipelines_to_candidates_in_one_call():
    def handler(request):
        path = request.url.path
        if path.endswith("/pipelines"):
            return httpx2.Response(
                200,
                json={
                    "total": 2,
                    "_embedded": {
                        "pipelines": [
                            {"id": 100, "candidate_id": 1, "status_id": 6377094, "rating": 3},
                            {"id": 101, "candidate_id": 2, "status_id": 6377098, "rating": 4},
                        ]
                    },
                },
            )
        cid = int(path.rsplit("/", 1)[-1])
        return httpx2.Response(200, json=candidate(cid))

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_job_candidate_pool", {"job_id": 55})

    assert result.data["count"] == 2
    assert result.data["requests_used"] == 3, "1 pipeline call + 2 candidate calls"
    first = result.data["pool"][0]
    assert first["status_id"] == 6377094
    assert first["candidate"]["city"] == "Kamloops"
    assert "resume_text" not in json.dumps(result.data)


async def test_job_pool_warns_that_status_ids_are_account_specific():
    def handler(request):
        if request.url.path.endswith("/pipelines"):
            return httpx2.Response(200, json={"total": 0, "_embedded": {"pipelines": []}})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_job_candidate_pool", {"job_id": 55})

    assert "account-specific" in result.data["note"]


# --- registration ----------------------------------------------------------


async def test_composites_are_absent_under_a_narrowed_toolset_selection():
    """They span resources, so a narrowed selection must not expose them."""

    def handler(request):
        return httpx2.Response(200, json={})

    async with Client(build(handler, toolsets="tags")) as client:
        names = {t.name for t in await client.list_tools()}

    assert "get_candidate_summaries" not in names
    assert "list_tags" in names


@pytest.mark.parametrize(
    "name",
    [
        "get_candidate_summaries",
        "get_candidate_engagement",
        "get_job_candidate_pool",
        "get_changed_records",
        "get_pipeline_summaries",
    ],
)
async def test_composite_is_registered_and_read_only(name):
    def handler(request):
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    assert name in tools
    assert tools[name].annotations.read_only_hint is True
    assert tools[name].annotations.destructive_hint is False
