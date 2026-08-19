"""The merged candidate history, and the properties that make it trustworthy.

A timeline that merges five CATS collections is only worth having if a reader
can still tell the collections apart afterwards, and if nothing went missing on
the way in. Those are the two failure modes worth guarding, and neither is
"does it return rows":

  * every row says which collection it came from, and which field its date is
  * an event with a broken or absent date is surfaced, never quietly dropped
  * the window the caller asked for is the window they get
  * an account-specific status id arrives with its human title attached
  * the request budget actually bounds the call, and the response says what it
    could not afford rather than passing a short history off as a complete one

The tool is not registered in server.py here; the tests build a server around
`timeline.register` directly, so this file exercises the module rather than the
wiring.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client, FastMCP
from test_boundary import BANNED_VERDICT_KEYS
from test_prompts import POLICY_WORDS

from cats_mcp.composites import timeline
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient

#: The tool-surface vocabulary, matching tests/test_boundary.py. Checked here
#: too because that file cannot see this tool until registration lands, and a
#: description is guidance a model reads either way.
TOOL_VOCAB: tuple[str, ...] = POLICY_WORDS + ("exclud",)


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    timeline.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(key, rows, *, has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


def workflows(statuses):
    return {"_embedded": {"workflows": [{"id": 1, "statuses": statuses}]}}


def route(
    *,
    activities=(),
    pipelines=(),
    tasks=(),
    statuses=(),
    workflow_statuses=({"id": 3, "title": "Screening"}, {"id": 9, "title": "Placed"}),
    record=None,
    calls=None,
):
    """One handler covering every endpoint the timeline reads.

    Ordered most specific first: `/candidates/{id}/pipelines` and
    `/pipelines/workflows` both end in a word this would otherwise confuse.
    """

    def handler(request):
        path = request.url.path
        if calls is not None:
            calls.append(path)
        if path.endswith("/pipelines/workflows"):
            return httpx2.Response(200, json=workflows(list(workflow_statuses)))
        if path.endswith("/statuses"):
            return httpx2.Response(200, json=collection("statuses", list(statuses)))
        if path.endswith("/activities"):
            return httpx2.Response(200, json=collection("activities", list(activities)))
        if path.endswith("/pipelines"):
            return httpx2.Response(200, json=collection("pipelines", list(pipelines)))
        if path.endswith("/tasks"):
            return httpx2.Response(200, json=collection("tasks", list(tasks)))
        return httpx2.Response(
            200, json=record or {"id": 1, "date_created": "2020-01-01T00:00:00-00:00"}
        )

    return handler


async def call(handler, arguments):
    async with Client(build(handler)) as client:
        return (await client.call_tool("get_candidate_timeline", arguments)).data


# --- ordering across sources -------------------------------------------------


async def test_events_from_two_sources_interleave_in_time_order():
    """The reason the tool exists. Two collections, read separately, whose rows
    only make sense once they are in one order - and the application in the
    middle must land between the two activities, not after both."""
    handler = route(
        activities=[
            {"id": 501, "type": "call_talked", "date_created": "2026-01-05T09:00:00-00:00"},
            {"id": 502, "type": "email", "date_created": "2026-03-01T09:00:00-00:00"},
        ],
        pipelines=[
            {
                "id": 900,
                "job_id": 77,
                "status_id": 9,
                "date_created": "2026-02-01T09:00:00-00:00",
            }
        ],
    )

    data = await call(handler, {"candidate_ids": [1], "sources": ["activity", "application"]})

    assert [(r["source"], r["date"]) for r in data["timeline"]] == [
        ("activity", "2026-01-05T09:00:00-00:00"),
        ("application", "2026-02-01T09:00:00-00:00"),
        ("activity", "2026-03-01T09:00:00-00:00"),
    ]


async def test_every_row_says_which_collection_it_came_from():
    """A merged row whose provenance is not on the row is not auditable, which
    would make this strictly worse than four separate calls."""
    handler = route(
        activities=[{"id": 501, "type": "call_talked", "date_created": "2026-01-05"}],
        pipelines=[{"id": 900, "job_id": 77, "status_id": 9, "date_created": "2026-02-01"}],
        tasks=[{"id": 700, "title": "Send paperwork", "due_date": "2026-04-01"}],
        record={
            "id": 1,
            "date_created": "2025-01-01",
            "date_modified": "2026-05-01",
        },
    )

    data = await call(
        handler,
        {
            "candidate_ids": [1],
            "sources": ["activity", "application", "task", "record"],
        },
    )

    assert {r["source"] for r in data["timeline"]} == {
        "activity",
        "application",
        "task",
        "record",
    }
    for row in data["timeline"]:
        assert row["source"], f"row without a source: {row}"
        assert row["date_field"], f"row that does not say which field it dated from: {row}"
        assert "candidate_id" in row


async def test_a_task_says_it_dated_from_due_date_when_there_is_no_created_date():
    """Sources disagree about which field holds the date. Silently picking one
    would make two rows with the same `date` mean different things."""
    handler = route(tasks=[{"id": 700, "title": "Call back", "due_date": "2026-04-01"}])

    data = await call(handler, {"candidate_ids": [1], "sources": ["task"]})

    assert data["timeline"][0]["date_field"] == "due_date"
    assert data["timeline"][0]["date"] == "2026-04-01"


# --- nothing is dropped for being awkward -----------------------------------


async def test_an_unreadable_date_is_surfaced_not_dropped():
    """The worst thing this tool could do is lose an event. A junk timestamp
    and a missing one both stay in the result, at the end, flagged."""
    handler = route(
        activities=[
            {"id": 501, "type": "call_talked", "date_created": "2026-01-05T09:00:00-00:00"},
            {"id": 502, "type": "email", "date_created": "yesterday-ish"},
            {"id": 503, "type": "note"},
        ]
    )

    data = await call(handler, {"candidate_ids": [1], "sources": ["activity"]})

    assert data["count"] == 3, "an event was lost"
    assert data["undated"] == 2

    ids = [r.get("event_id") for r in data["timeline"]]
    assert ids[0] == 501, "the dated row must come first"
    assert set(ids[1:]) == {502, 503}, "undated rows sort last, they do not disappear"

    junk = next(r for r in data["timeline"] if r.get("event_id") == 502)
    assert junk["date"] is None, "a date that did not parse must not look like one that did"
    assert junk["date_raw"] == "yesterday-ish", "the original value is kept for fixing"


async def test_an_undated_row_survives_the_window_filter():
    """A window cannot answer a question about a row with no date. Dropping it
    would silently remove an event the caller never got a chance to see."""
    handler = route(
        activities=[
            {"id": 501, "type": "call_talked", "date_created": "2020-01-01T09:00:00-00:00"},
            {"id": 502, "type": "email", "date_created": "not-a-date"},
        ]
    )

    data = await call(
        handler,
        {"candidate_ids": [1], "sources": ["activity"], "since": "2026-01-01"},
    )

    assert [r["event_id"] for r in data["timeline"]] == [502]
    assert data["out_of_window"] == 1
    assert data["undated"] == 1


# --- the window the caller asked for ----------------------------------------


async def test_the_date_range_excludes_events_outside_it():
    handler = route(
        activities=[
            {"id": 501, "type": "call_talked", "date_created": "2025-12-01T09:00:00-00:00"},
            {"id": 502, "type": "email", "date_created": "2026-01-05T09:00:00-00:00"},
            {"id": 503, "type": "note", "date_created": "2026-06-01T09:00:00-00:00"},
        ]
    )

    data = await call(
        handler,
        {
            "candidate_ids": [1],
            "sources": ["activity"],
            "since": "2026-01-01",
            "until": "2026-02-01",
        },
    )

    assert [r["event_id"] for r in data["timeline"]] == [502]
    assert data["out_of_window"] == 2
    assert data["window"] == {"since": "2026-01-01", "until": "2026-02-01"}


async def test_an_unreadable_window_bound_is_refused_rather_than_ignored():
    """A dropped filter returns the whole history, which reads as 'everything
    happened inside your dates'. That is a wrong answer, not a missing one."""
    handler = route(activities=[])

    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="ISO 8601"):
            await client.call_tool(
                "get_candidate_timeline",
                {"candidate_ids": [1], "sources": ["activity"], "since": "last tuesday"},
            )


async def test_asking_for_no_sources_is_refused():
    """An empty result from an empty read is indistinguishable from a candidate
    with no history at all."""
    handler = route()

    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="At least one source"):
            await client.call_tool(
                "get_candidate_timeline", {"candidate_ids": [1], "sources": []}
            )


# --- status ids are account-specific and mean nothing unlabelled -------------


async def test_pipeline_status_ids_are_resolved_to_titles():
    """"9" does not say Placed. Resolving it is normalization, and it is the
    difference between a readable history and a column of account-local ids."""
    handler = route(
        pipelines=[
            {"id": 900, "job_id": 77, "status_id": 9, "date_created": "2026-01-01"}
        ],
        statuses=[
            {"id": 1, "status_id": 3, "date_created": "2026-01-02T09:00:00-00:00"},
            {
                "id": 2,
                "status_id": 9,
                "from_status_id": 3,
                "date_created": "2026-01-03T09:00:00-00:00",
            },
        ],
    )

    data = await call(
        handler,
        {"candidate_ids": [1], "sources": ["application", "pipeline_status"]},
    )

    application = next(r for r in data["timeline"] if r["source"] == "application")
    assert application["status"] == "Placed"

    changes = [r for r in data["timeline"] if r["source"] == "pipeline_status"]
    assert [c["status"] for c in changes] == ["Screening", "Placed"]
    assert changes[1]["from_status"] == "Screening"
    assert changes[0]["candidate_id"] == "1", "a stage change is attributed to its candidate"
    assert changes[0]["pipeline_id"] == application["pipeline_id"] == 900, (
        "a stage change and the application it belongs to must be joinable"
    )


async def test_an_unresolvable_status_id_is_returned_unlabelled():
    """An id the workflow does not describe is still a fact about the record."""
    handler = route(
        pipelines=[{"id": 900, "job_id": 77, "status_id": 4242, "date_created": "2026-01-01"}],
        statuses=[{"id": 1, "status_id": 4242, "date_created": "2026-01-02"}],
    )

    data = await call(
        handler,
        {"candidate_ids": [1], "sources": ["application", "pipeline_status"]},
    )

    change = next(r for r in data["timeline"] if r["source"] == "pipeline_status")
    assert change["status_id"] == 4242
    assert "status" not in change, "an unknown id must not be given an invented label"


async def test_a_workflow_failure_does_not_fail_the_call():
    """Losing the labels is a degraded answer. Losing the history is not an
    answer at all."""

    def handler(request):
        path = request.url.path
        if path.endswith("/pipelines/workflows"):
            return httpx2.Response(404, json={"message": "gone"})
        if path.endswith("/pipelines"):
            return httpx2.Response(
                200,
                json=collection(
                    "pipelines",
                    [{"id": 900, "job_id": 77, "status_id": 9, "date_created": "2026-01-01"}],
                ),
            )
        return httpx2.Response(200, json={})

    data = await call(handler, {"candidate_ids": [1], "sources": ["application"]})

    row = data["timeline"][0]
    assert row["status_id"] == 9
    assert "status" not in row


async def test_stage_history_implies_the_pipeline_read_that_finds_the_ids():
    """`/pipelines/{id}/statuses` needs an id that only the candidate's pipeline
    collection knows. Asking for stage history without it would silently return
    nothing, which reads as 'this person never moved'."""
    calls: list[str] = []
    handler = route(
        pipelines=[{"id": 900, "job_id": 77, "status_id": 9, "date_created": "2026-01-01"}],
        statuses=[{"id": 1, "status_id": 9, "date_created": "2026-01-02"}],
        calls=calls,
    )

    data = await call(handler, {"candidate_ids": [1], "sources": ["pipeline_status"]})

    assert any(p.endswith("/candidates/1/pipelines") for p in calls)
    assert any(p.endswith("/pipelines/900/statuses") for p in calls)
    assert [r["source"] for r in data["timeline"]] == ["pipeline_status"], (
        "only the requested source may produce rows"
    )


# --- the budget --------------------------------------------------------------


async def test_the_request_budget_bounds_the_call_and_says_what_it_missed():
    """Three candidates over three sources is nine requests plus a workflow
    read. A budget of four must stop at four and name what it did not reach -
    a short history presented as a complete one is the dangerous failure."""
    calls: list[str] = []
    handler = route(
        activities=[{"id": 501, "type": "call_talked", "date_created": "2026-01-05"}],
        pipelines=[{"id": 900, "job_id": 77, "status_id": 9, "date_created": "2026-01-01"}],
        tasks=[{"id": 700, "title": "Call back", "due_date": "2026-04-01"}],
        calls=calls,
    )

    data = await call(handler, {"candidate_ids": [1, 2, 3], "max_requests": 4})

    assert len(calls) == 4, f"spent more than the budget: {calls}"
    assert data["requests_used"] == 4
    assert set(data["sources_skipped"]) == {"activity", "task"}
    assert data["rate_limit"] is not None


async def test_a_partial_source_read_is_reported_per_source():
    """Covering some candidates and not others is not an error worth failing
    on, but it is a fact the caller has to be told."""
    handler = route(
        activities=[{"id": 501, "type": "call_talked", "date_created": "2026-01-05"}]
    )

    data = await call(
        handler, {"candidate_ids": [1, 2, 3], "sources": ["activity"], "max_requests": 2}
    )

    assert data["source_requests"]["activity"] == 2
    assert "activity" in data["errors"]
    assert "2 of 3" in data["errors"]["activity"]


async def test_more_history_than_one_page_is_reported_not_silently_shortened():
    def handler(request):
        if request.url.path.endswith("/activities"):
            return httpx2.Response(
                200,
                json=collection(
                    "activities",
                    [{"id": 501, "type": "call_talked", "date_created": "2026-01-05"}],
                    has_next=True,
                ),
            )
        return httpx2.Response(200, json={})

    data = await call(handler, {"candidate_ids": [1], "sources": ["activity"]})

    assert data["incomplete"], "a truncated page must be visible to the caller"
    assert data["incomplete"][0]["source"] == "activity"


async def test_the_row_cap_keeps_the_most_recent_events_and_counts_the_rest():
    handler = route(
        activities=[
            {"id": 500 + n, "type": "note", "date_created": f"2026-01-{n:02d}"}
            for n in range(1, 6)
        ]
    )

    data = await call(
        handler, {"candidate_ids": [1], "sources": ["activity"], "max_rows": 2}
    )

    assert [r["event_id"] for r in data["timeline"]] == [504, 505]
    assert data["omitted_by_row_cap"] == 3
    assert data["row_cap_reached"] is True


# --- the fact/judgment boundary ---------------------------------------------


def _banned_keys(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, sub in value.items():
            where = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in BANNED_VERDICT_KEYS:
                found.append(where)
            found.extend(_banned_keys(sub, where))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_banned_keys(item, f"{path}[{index}]"))
    return found


async def test_the_output_carries_no_verdict_shaped_key():
    """A history reports what happened. Whether that was good, and whether
    anyone has gone cold, is the caller's to decide."""
    handler = route(
        activities=[{"id": 501, "type": "call_talked", "date_created": "2026-01-05"}],
        pipelines=[{"id": 900, "job_id": 77, "status_id": 9, "date_created": "2026-01-01"}],
        tasks=[{"id": 700, "title": "Call back", "due_date": "2026-04-01"}],
        statuses=[{"id": 1, "status_id": 9, "date_created": "2026-01-02"}],
        record={"id": 1, "date_created": "2025-01-01", "date_modified": "2026-05-01"},
    )

    data = await call(
        handler,
        {
            "candidate_ids": [1],
            "sources": ["activity", "application", "pipeline_status", "task", "record"],
        },
    )

    assert not _banned_keys(data), f"verdict-shaped keys in the result: {_banned_keys(data)}"


async def test_the_description_carries_no_recruiting_policy_vocabulary():
    """A tool description is guidance a model reads, exactly like a prompt."""
    async with Client(build(route())) as client:
        tool = next(t for t in await client.list_tools() if t.name == "get_candidate_timeline")

    text = (tool.description or "").lower()
    hits = [word for word in TOOL_VOCAB if word in text]
    assert not hits, f"the description contains recruiting-policy vocabulary: {hits}"


async def test_the_tool_is_annotated_as_a_read():
    async with Client(build(route())) as client:
        tool = next(t for t in await client.list_tools() if t.name == "get_candidate_timeline")

    annotations = tool.annotations
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is True


def test_register_reports_how_many_tools_it_added():
    mcp = FastMCP("test")
    assert timeline.register(mcp, lambda: None, enforce_auth=False) == 1
