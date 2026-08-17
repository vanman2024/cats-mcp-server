"""Two composite tools: bounded activity history, and finding the resume.

get_candidate_activity exists because the alternative is pulling a whole
unbounded activity feed per candidate into context. It costs one CATS request
per candidate on purpose - there is no cheaper way to read someone's activity
log - so the tests pin the O(N) cost and the client-side bounds that keep the
result itself small.

find_candidate_resume exists because getting a resume today is two calls plus
guessing which attachment is the one that matters. The tests pin that the
is_resume flag always wins over the filename guess, that the guess is labelled
as a guess, and that "no resume" is an answer, not an exception.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.build import MAX_BINARY_BYTES
from cats_mcp.server import create_server

PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


#: A date well inside any lookback window used below, so tests that are not
#: themselves about lookback filtering do not rot as real time moves past a
#: hardcoded year.
RECENT = _iso(timedelta(days=1))


def activity(activity_id, date_created=RECENT, activity_type="call_talked", **extra):
    row = {"id": activity_id, "type": activity_type, "notes": "n", "date_created": date_created}
    row.update(extra)
    return row


def activities_page(rows):
    return {"total": len(rows), "_embedded": {"activities": rows}}


# --- get_candidate_activity: cost -------------------------------------------


async def test_activity_costs_one_request_per_candidate():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(200, json=activities_page([activity(1)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_activity", {"candidate_ids": [1, 2, 3]}
        )

    assert len(calls) == 3
    assert result.data["requests_used"] == 3


async def test_duplicate_candidate_ids_are_not_fetched_twice():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx2.Response(200, json=activities_page([]))

    async with Client(build(handler)) as client:
        await client.call_tool("get_candidate_activity", {"candidate_ids": [1, 1, 1, 2]})

    assert calls["n"] == 2


async def test_batch_is_capped_and_says_so():
    def handler(request):
        return httpx2.Response(200, json=activities_page([]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_activity", {"candidate_ids": list(range(1, 200))}
        )

    assert result.data["truncated"] is True
    assert result.data["requests_used"] <= 50


# --- grouping and shaping -----------------------------------------------


async def test_activity_is_grouped_by_candidate():
    def handler(request):
        cid = int(request.url.path.split("/")[-2])
        return httpx2.Response(200, json=activities_page([activity(cid * 10)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_activity", {"candidate_ids": [1, 2]})

    by_id = {row["candidate_id"]: row for row in result.data["candidates"]}
    assert by_id["1"]["activities"][0]["id"] == 10
    assert by_id["2"]["activities"][0]["id"] == 20
    assert by_id["1"]["activity_count"] == 1


async def test_activity_rows_are_compact():
    """Only SUMMARY_FIELDS['activity'] survives - not whatever else CATS attaches."""

    def handler(request):
        row = activity(1, regarding_id=55, extra_field="x" * 5000)
        return httpx2.Response(200, json=activities_page([row]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_activity", {"candidate_ids": [1]})

    row = result.data["candidates"][0]["activities"][0]
    assert set(row) == {"id", "type", "notes", "date_created", "regarding_id"}
    assert "extra_field" not in row


# --- lookback_days -----------------------------------------------------


async def test_lookback_days_excludes_activity_outside_the_window():
    def handler(request):
        rows = [
            activity(1, _iso(timedelta(days=3))),
            activity(2, _iso(timedelta(days=30))),  # outside a 7 day window
        ]
        return httpx2.Response(200, json=activities_page(rows))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_activity", {"candidate_ids": [1], "lookback_days": 7}
        )

    kept = result.data["candidates"][0]["activities"]
    assert [row["id"] for row in kept] == [1]


async def test_lookback_default_is_365_days():
    def handler(request):
        rows = [
            activity(1, _iso(timedelta(days=5))),
            activity(2, _iso(timedelta(days=400))),  # over a year old
        ]
        return httpx2.Response(200, json=activities_page(rows))

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_activity", {"candidate_ids": [1]})

    assert result.data["lookback_days"] == 365
    kept = result.data["candidates"][0]["activities"]
    assert [row["id"] for row in kept] == [1]


async def test_a_row_with_no_date_is_kept_rather_than_dropped():
    """A lookback filter should never silently discard data it cannot judge."""

    def handler(request):
        row = {"id": 1, "type": "call_talked", "notes": "n", "date_created": None}
        return httpx2.Response(200, json=activities_page([row]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_activity", {"candidate_ids": [1], "lookback_days": 1}
        )

    kept = result.data["candidates"][0]["activities"]
    assert [row["id"] for row in kept] == [1]


# --- types filter --------------------------------------------------------


async def test_types_filter_matches_as_a_case_insensitive_substring():
    def handler(request):
        rows = [
            activity(1, activity_type="call_talked"),
            activity(2, activity_type="call_missed"),
            activity(3, activity_type="email"),
        ]
        return httpx2.Response(200, json=activities_page(rows))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "get_candidate_activity", {"candidate_ids": [1], "types": ["Call"]}
        )

    kept = {row["id"] for row in result.data["candidates"][0]["activities"]}
    assert kept == {1, 2}


async def test_omitting_types_keeps_every_activity():
    def handler(request):
        rows = [
            activity(1, activity_type="call_talked"),
            activity(2, activity_type="email"),
        ]
        return httpx2.Response(200, json=activities_page(rows))

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_activity", {"candidate_ids": [1]})

    assert result.data["candidates"][0]["activity_count"] == 2


# --- partial failure -------------------------------------------------------


async def test_one_failing_candidate_does_not_discard_the_others():
    def handler(request):
        cid = int(request.url.path.split("/")[-2])
        if cid == 2:
            return httpx2.Response(404, text="gone")
        return httpx2.Response(200, json=activities_page([activity(1)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("get_candidate_activity", {"candidate_ids": [1, 2]})

    assert result.data["count"] == 1
    assert "2" in result.data["errors"]


# --- registration ------------------------------------------------------


async def test_get_candidate_activity_is_registered_and_read_only():
    def handler(request):
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    assert "get_candidate_activity" in tools
    assert tools["get_candidate_activity"].annotations.read_only_hint is True
    assert tools["get_candidate_activity"].annotations.destructive_hint is False


# =============================================================================
# find_candidate_resume
# =============================================================================


def attachment(attachment_id, filename, *, is_resume=None, date_created=RECENT):
    row = {
        "id": attachment_id,
        "filename": filename,
        "content_type": "application/pdf",
        "date_created": date_created,
    }
    if is_resume is not None:
        row["is_resume"] = is_resume
    return row


def attachments_page(rows):
    return {"total": len(rows), "_embedded": {"attachments": rows}}


def serve_attachments_then_file(rows, content=PDF_BYTES, content_type="application/pdf"):
    def handler(request):
        if request.url.path.endswith("/attachments"):
            return httpx2.Response(200, json=attachments_page(rows))
        if "/download" in request.url.path:
            return httpx2.Response(200, content=content, headers={"Content-Type": content_type})
        return httpx2.Response(200, json={})

    return handler


async def test_the_flagged_attachment_is_returned_as_the_document():
    rows = [attachment(1, "misc.pdf"), attachment(2, "cv.pdf", is_resume=True)]

    async with Client(build(serve_attachments_then_file(rows))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert result.data["found"] is True
    assert result.data["attachment"]["id"] == 2
    assert result.data["selection_method"] == "is_resume flag"
    assert result.content, "no document content returned"
    block = result.content[0]
    assert block.type in {"resource", "resource_link"}, f"got {block.type}"


async def test_the_flag_wins_over_a_newer_unflagged_attachment():
    """The flag is ground truth from CATS; a newer date is not a reason to override it."""
    rows = [
        attachment(1, "resume.pdf", is_resume=True, date_created="2020-01-01T00:00:00-00:00"),
        attachment(2, "cover-letter.pdf", date_created="2026-08-01T00:00:00-00:00"),
    ]

    async with Client(build(serve_attachments_then_file(rows))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert result.data["attachment"]["id"] == 1


async def test_falls_back_to_filename_heuristic_when_nothing_is_flagged():
    rows = [attachment(1, "candidate-resume.pdf"), attachment(2, "photo.jpg")]

    async with Client(build(serve_attachments_then_file(rows))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert result.data["found"] is True
    assert result.data["attachment"]["id"] == 1
    assert "guess" in result.data["selection_method"]


async def test_multiple_flagged_resumes_returns_the_newest_and_reports_the_count():
    rows = [
        attachment(1, "old.pdf", is_resume=True, date_created="2020-01-01T00:00:00-00:00"),
        attachment(2, "new.pdf", is_resume=True, date_created="2026-08-01T00:00:00-00:00"),
    ]

    async with Client(build(serve_attachments_then_file(rows))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert result.data["attachment"]["id"] == 2
    assert result.data["matched_count"] == 2
    assert "2" in result.data["note"]


async def test_no_attachments_returns_a_structured_answer_not_an_error():
    async with Client(build(serve_attachments_then_file([]))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert result.data["found"] is False
    assert result.data["attachment_count"] == 0
    # No document was fetched, so nothing came back as an embedded resource.
    assert all(block.type != "resource" for block in result.content)


async def test_attachments_exist_but_none_look_like_a_resume():
    rows = [attachment(1, "headshot.jpg"), attachment(2, "reference-check.mp3")]

    async with Client(build(serve_attachments_then_file(rows))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert result.data["found"] is False
    assert result.data["attachment_count"] == 2


async def test_finding_a_resume_costs_two_requests():
    rows = [attachment(1, "resume.pdf", is_resume=True)]

    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return serve_attachments_then_file(rows)(request)

    async with Client(build(handler)) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert len(calls) == 2
    assert result.data["requests_used"] == 2


async def test_the_returned_bytes_survive_the_round_trip():
    rows = [attachment(1, "resume.pdf", is_resume=True)]

    async with Client(build(serve_attachments_then_file(rows))) as client:
        result = await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    resource = result.content[0].resource
    blob = getattr(resource, "blob", None)
    assert blob, "embedded resource has no blob"
    assert base64.b64decode(blob) == PDF_BYTES


async def test_an_oversized_resume_is_refused():
    rows = [attachment(1, "resume.pdf", is_resume=True)]
    oversized = b"%PDF-1.4\n" + b"x" * (MAX_BINARY_BYTES + 1)

    async with Client(build(serve_attachments_then_file(rows, content=oversized))) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("find_candidate_resume", {"candidate_id": 100})

    assert "5MB" in str(excinfo.value)


async def test_find_candidate_resume_is_registered_and_read_only():
    def handler(request):
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    assert "find_candidate_resume" in tools
    assert tools["find_candidate_resume"].annotations.read_only_hint is True
    assert tools["find_candidate_resume"].annotations.destructive_hint is False
