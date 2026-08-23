"""The job snapshot, and the four ways it could quietly lie.

Issue #12 item 1 asks for "the factual state of one or more jobs". The failure
modes worth guarding are not "does it return rows" but the ones that produce a
confident, readable, wrong answer:

  * a stage count computed over half a pipeline, reported as the whole thing
  * a status id rendered as a stage name the account never used
  * an opening or headcount figure derived from the pipeline, when CATS stores
    no such field anywhere
  * per-candidate enrichment silently stopping at the budget, so a candidate
    nobody read looks like a candidate with nothing recorded

Each test below fails loudly if one of those regresses.

Issue #17 adds the typed axis: the tool returns a `JobSnapshotResult` through
`ToolResult`, so assertions read `result.structured_content` - the dict that
actually went on the wire - rather than a shape the test invented. `result.data`
is a dataclass FastMCP synthesises from the declared output schema, which is
what makes the schema itself worth asserting on: if `output_schema` went
missing, `data` would degrade and every other test here would still pass.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client, FastMCP

# The two enforcement lists live in the boundary suite; importing them keeps
# this file honest if either grows a new word or key, rather than letting a
# hand-copied duplicate drift out of date.
from test_boundary import BANNED_VERDICT_KEYS, TOOL_VOCAB, _find_banned_keys

from cats_mcp.composites import jobsnapshot
from cats_mcp.composites.jobsnapshot import _fold
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    """A server carrying get_job_recruiting_snapshot alone - it is not wired
    into create_server yet."""
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    jobsnapshot.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(rows, *, key, total=None, has_next=False):
    payload = {
        "count": len(rows),
        "total": len(rows) if total is None else total,
        "_embedded": {key: rows},
    }
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


# --- the account, in miniature ----------------------------------------------

#: Account-specific status ids, exactly as unreadable as the real ones. The
#: last is deliberately absent from the workflow below.
SCREENING = 6377101
INTERVIEW = 6377104
OFFER = 6377109
UNNAMED = 9999999

WORKFLOWS = {
    "_embedded": {
        "workflows": [
            {
                "id": 1,
                "statuses": [
                    {"id": SCREENING, "title": "Screening"},
                    {"id": INTERVIEW, "title": "Phone Interview"},
                    {"id": OFFER, "title": "Offer"},
                ],
            }
        ]
    }
}

JOB = {
    "id": 7,
    "title": "Site Millwright",
    "status_id": 3,
    "status": "Active",
    "city": "Kamloops",
    "state": "BC",
    "company_id": 70,
    "owner_id": 91,
    "date_created": "2025-11-02T08:00:00-00:00",
    "date_modified": "2026-01-05T09:00:00-00:00",
    "_embedded": {
        "company": {"id": 70, "name": "Artemis Mining Ltd"},
        "owner": {"id": 91, "first_name": "Kim", "last_name": "Alvarez"},
    },
}


def pipeline(pid, cid, status_id, first, last):
    """A pipeline row shaped the way CATS returns them - `rating` included,
    because the point is that this tool declines to carry it through."""
    return {
        "id": pid,
        "candidate_id": cid,
        "job_id": 7,
        "status_id": status_id,
        "rating": 4,
        "date_modified": "2026-01-05T09:00:00-00:00",
        "_embedded": {"candidate": {"id": cid, "first_name": first, "last_name": last}},
    }


PIPELINE_ROWS = [
    pipeline(501, 101, SCREENING, "Dana", "Lee"),
    pipeline(502, 102, SCREENING, "Sam", "Ortiz"),
    pipeline(503, 103, INTERVIEW, "Jo", "Nakamura"),
    pipeline(504, 104, INTERVIEW, "Rae", "Bell"),
    pipeline(505, 105, UNNAMED, "Pat", "Singh"),
]

CUSTOM_FIELDS = [
    {"id": 21, "name": "Site", "value": "Logan Lake"},
    {"id": 22, "name": "Shift Length", "value": 12},
]

TASKS = [
    {
        "id": 8352717,
        "description": "Confirm start date paperwork",
        "date_due": "2020-01-01",
        "priority": 5,
        "is_completed": False,
        "assigned_to_id": 595874,
        "data_item": {"id": 103, "type": "candidate"},
    },
    {
        "id": 8352718,
        "description": "Send site orientation pack",
        "date_due": "2099-01-01",
        "priority": 5,
        "is_completed": False,
        "assigned_to_id": 595874,
        "data_item": {"id": 104, "type": "candidate"},
    },
]

ACTIVITIES = [
    {"id": 900, "type": "email", "date_created": "2026-02-01T10:00:00-00:00"},
    {"id": 901, "type": "call_talked", "date_created": "2026-03-01T10:00:00-00:00"},
]


def snapshot_handler(request, *, calls=None):
    """One handler for the account above."""
    path = request.url.path
    if calls is not None:
        calls.append(path)
    if path.endswith("/pipelines/workflows"):
        return httpx2.Response(200, json=WORKFLOWS)
    if path.endswith("/jobs/7/pipelines"):
        return httpx2.Response(200, json=collection(PIPELINE_ROWS, key="pipelines"))
    if path.endswith("/jobs/7/custom_fields"):
        return httpx2.Response(200, json=collection(CUSTOM_FIELDS, key="custom_fields"))
    if path.endswith("/jobs/7/tasks"):
        return httpx2.Response(200, json=collection(TASKS, key="tasks"))
    if path.endswith("/jobs/7"):
        return httpx2.Response(200, json=JOB)
    if path.endswith("/activities"):
        return httpx2.Response(200, json=collection(ACTIVITIES, key="activities"))
    if "/candidates/" in path:
        return httpx2.Response(
            200,
            json={"id": 103, "first_name": "Jo", "last_name": "Nakamura", "title": "Millwright",
                  "city": "Kamloops", "state": "BC"},
        )
    return httpx2.Response(200, json={})


#: Every top-level field the structured payload must carry, spelled out rather
#: than read back off JobSnapshotResult - a list derived from the model would
#: agree with it by construction and could never catch a field being dropped.
EXPECTED_TOP_LEVEL = frozenset(
    {
        "jobs",
        "count",
        "requested",
        "stages_requested",
        "stages_matched",
        "included",
        "unenriched",
        "execution",
        "note",
    }
)

#: Ceiling on the human-readable line, in bytes. Generous next to the ~40 bytes
#: a real call produces, and far below any payload it could be a copy of.
CONTENT_BYTE_CEILING = 160

#: Words for a thing CATS does not store. A job has no openings field, no
#: headcount and no slot count, so any of these appearing in the payload or the
#: declared schema means a number was derived and dressed up as a record.
FABRICATION_WORDS = ("opening", "slot", "headcount", "position", "vacanc", "seats")


def _resolve_ref(schema: dict, node):
    """Follow a JSON Schema `$ref` into the document's own `$defs`.

    Pydantic hoists every nested model into `$defs`, so `properties.execution`
    is a reference rather than the object itself. Asserting on the reference
    would assert that a pointer exists, not that the contract is right.
    """
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref:
        return node
    return schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})


def _schema_property_names(schema) -> set[str]:
    """Every property name a JSON schema declares, at any nesting depth."""
    found: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            found.update(str(key) for key in properties)
        for value in schema.values():
            found |= _schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= _schema_property_names(item)
    return found


async def snapshot(arguments, handler=snapshot_handler):
    async with Client(build(handler)) as client:
        return await client.call_tool("get_job_recruiting_snapshot", arguments)


# --- stage folding is folding, not classification ---------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("Phone Interview", "phone interview"), ("  OFFER  ", "offer"), (None, "")],
)
def test_stage_names_are_only_case_and_whitespace_folded(raw, expected):
    assert _fold(raw) == expected


# --- the headline: counts, and stage ids resolved to titles -----------------


async def test_stage_counts_are_correct_and_status_ids_are_resolved_to_titles():
    """A count against a raw id is not a snapshot of anything: 6377104 is a
    number until the account's workflow says it is "Phone Interview"."""
    result = await snapshot({"job_ids": [7]})

    job = result.structured_content["jobs"][0]
    assert job["job_id"] == 7
    assert job["title"] == "Site Millwright"
    assert job["company"] == "Artemis Mining Ltd"
    assert job["owner"] == "Kim Alvarez"
    assert job["status"] == "Active"
    assert job["total_candidates"] == 5
    assert job["candidates_counted"] == 5

    counts = {row["status_id"]: row for row in job["stage_counts"]}
    assert counts[SCREENING]["status"] == "Screening"
    assert counts[SCREENING]["candidates"] == 2
    assert counts[INTERVIEW]["status"] == "Phone Interview"
    assert counts[INTERVIEW]["candidates"] == 2
    assert counts[UNNAMED]["candidates"] == 1
    assert sum(row["candidates"] for row in job["stage_counts"]) == job["candidates_counted"]


async def test_an_unresolvable_status_id_comes_back_unlabelled_never_invented():
    """The account's workflow does not name 9999999. A plausible-looking guess
    at a stage title is worse information than an honest gap, and the id has to
    survive so the caller can resolve it themselves."""
    result = await snapshot({"job_ids": [7]})

    unnamed = next(
        row
        for row in result.structured_content["jobs"][0]["stage_counts"]
        if row["status_id"] == UNNAMED
    )
    assert unnamed["status"] is None
    assert unnamed["candidates"] == 1

    person = next(
        row
        for row in result.structured_content["jobs"][0]["candidates"]
        if row["status_id"] == UNNAMED
    )
    assert person["status"] is None
    assert person["candidate_id"] == 105


async def test_a_pipelines_rating_is_never_carried_through():
    """`rating` is a real CATS field and the one number on a pipeline row shaped
    like a verdict. A snapshot is not where a caller should meet it."""
    result = await snapshot({"job_ids": [7]})

    assert "rating" not in json.dumps(result.structured_content)


# --- selecting the stages people are listed for ------------------------------


async def test_candidates_are_listed_for_the_requested_stage_only():
    result = await snapshot({"job_ids": [7], "stages": ["interview"]})

    data = result.structured_content
    job = data["jobs"][0]
    assert [row["candidate_id"] for row in job["candidates"]] == [103, 104]
    assert job["candidates_selected"] == 2
    # The counts still cover every stage - narrowing who is listed must not
    # narrow what the job is reported to contain.
    assert job["candidates_counted"] == 5
    assert len(job["stage_counts"]) == 3

    assert [e["matched"] for e in data["stages_matched"]] == ["interview"]
    assert data["stages_matched"][0]["value"] == "Phone Interview"
    assert data["stages_matched"][0]["source"] == str(INTERVIEW)


async def test_a_stage_term_the_account_never_uses_is_visibly_unmatched():
    """Silence would read as "nobody is at that stage". `stages_requested`
    against `stages_matched` is what separates that from "no such stage"."""
    result = await snapshot({"job_ids": [7], "stages": ["negotiation"]})

    data = result.structured_content
    assert data["stages_requested"] == ["negotiation"]
    assert data["stages_matched"] == []
    assert data["jobs"][0]["candidates"] == []
    assert data["jobs"][0]["candidates_selected"] == 0
    assert data["jobs"][0]["candidates_counted"] == 5, "the counts are unaffected"


async def test_an_exact_status_id_selects_without_the_vocabulary():
    result = await snapshot({"job_ids": [7], "stage_status_ids": [SCREENING]})

    data = result.structured_content
    assert [row["candidate_id"] for row in data["jobs"][0]["candidates"]] == [101, 102]
    assert data["stages_matched"][0]["mode"] == "exact"


async def test_no_stage_selector_lists_everyone():
    result = await snapshot({"job_ids": [7]})

    ids = [row["candidate_id"] for row in result.structured_content["jobs"][0]["candidates"]]
    assert ids == [101, 102, 103, 104, 105]


# --- per-candidate enrichment, and its budget --------------------------------


async def test_latest_activity_is_the_most_recent_row_not_the_last_one():
    result = await snapshot(
        {"job_ids": [7], "stages": ["interview"], "include": ["latest_activity"]}
    )

    row = result.structured_content["jobs"][0]["candidates"][0]
    assert row["latest_activity"]["type"] == "call_talked"
    assert row["latest_activity"]["date"] == "2026-03-01T10:00:00-00:00"


async def test_per_candidate_enrichment_is_bounded_and_the_overflow_is_reported():
    """One request per candidate is the phase that can eat an hourly allowance.
    A candidate the budget never read must not look like a candidate with
    nothing recorded - that is the same silent wrong answer as an invented
    stage name, one level down."""
    calls: list[str] = []

    def handler(request):
        return snapshot_handler(request, calls=calls)

    result = await snapshot(
        {
            "job_ids": [7],
            "include": ["latest_activity"],
            "max_candidate_reads": 2,
        },
        handler,
    )

    data = result.structured_content
    assert data["unenriched"] == {"latest_activity": 3}, (
        "three of five listed candidates were never read; saying so is the only thing "
        "between the caller and a confident wrong answer"
    )
    assert "latest_activity" in data["execution"]["errors"]
    assert data["execution"]["truncated"] is True
    assert len([p for p in calls if p.endswith("/activities")]) == 2

    enriched = [r for r in data["jobs"][0]["candidates"] if r["latest_activity"] is not None]
    assert len(enriched) == 2, "only the candidates actually read carry an activity"


async def test_identity_fills_in_names_and_costs_one_request_each():
    calls: list[str] = []

    def handler(request):
        return snapshot_handler(request, calls=calls)

    result = await snapshot(
        {"job_ids": [7], "stages": ["interview"], "include": ["identity"]}, handler
    )

    row = result.structured_content["jobs"][0]["candidates"][0]
    assert row["title"] == "Millwright"
    assert row["city"] == "Kamloops"
    detail_reads = [p for p in calls if p.startswith("/v3/candidates/") and "activities" not in p]
    assert len(detail_reads) == 2


async def test_names_ride_along_free_when_cats_embeds_them():
    """No per-candidate request was spent, so a name must come from the row or
    stay null - never be fetched implicitly."""
    calls: list[str] = []

    def handler(request):
        return snapshot_handler(request, calls=calls)

    result = await snapshot({"job_ids": [7]}, handler)

    names = [row["name"] for row in result.structured_content["jobs"][0]["candidates"]]
    assert names[0] == "Dana Lee"
    assert not [p for p in calls if p.startswith("/v3/candidates/")]


# --- job-level extras --------------------------------------------------------


async def test_custom_fields_come_back_with_their_account_labels():
    result = await snapshot({"job_ids": [7]})

    fields = {f["name"]: f["value"] for f in result.structured_content["jobs"][0]["custom_fields"]}
    assert fields["Site"] == "Logan Lake"
    assert fields["Shift Length"] == "12", "a numeric custom field must not read as empty"


async def test_tasks_report_overdue_dates_without_a_priority_key():
    """CATS tasks carry a `priority` column. The number is a fact and the key is
    not: tests/test_boundary.py fails the build on a response field called
    `priority`, so it is surfaced as `urgency_value`."""
    result = await snapshot({"job_ids": [7], "include": ["tasks"]})

    job = result.structured_content["jobs"][0]
    assert job["task_count"] == 2
    assert job["overdue_task_count"] == 1

    overdue = next(t for t in job["tasks"] if t["overdue"])
    assert overdue["task_id"] == 8352717
    # A CATS task has no title field; its text lives in description.
    assert overdue["description"] == "Confirm start date paperwork"
    assert "title" not in overdue
    assert overdue["due_date"] == "2020-01-01"
    assert overdue["days_overdue"] > 0
    # It associates through data_item, not a scalar candidate_id.
    assert overdue["regarding_id"] == 103
    assert overdue["regarding_type"] == "candidate"
    assert overdue["urgency_value"] == 5
    assert "priority" not in overdue


# --- CATS stores no openings, so neither does this ---------------------------


async def test_no_opening_or_headcount_figure_is_invented():
    """CATS has no field for how many positions a job is hiring for. Deriving
    one from the pipeline would read exactly like a stored fact and be a number
    this adapter made up."""
    async with Client(build(snapshot_handler)) as client:
        result = await client.call_tool("get_job_recruiting_snapshot", {"job_ids": [7]})
        tool = await build(snapshot_handler).get_tool("get_job_recruiting_snapshot")

    declared = " ".join(_schema_property_names(tool.output_schema)).lower()
    payload_keys = " ".join(_all_keys(result.structured_content)).lower()
    for word in FABRICATION_WORDS:
        assert word not in declared, f"the schema declares a fabricated field about {word!r}"
        assert word not in payload_keys, f"the payload carries a fabricated field about {word!r}"

    assert result.structured_content["jobs"][0]["total_candidates"] == 5


def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            keys.add(str(key))
            keys |= _all_keys(sub)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# --- the request budget ------------------------------------------------------


async def test_the_budget_stops_the_core_sweep_and_says_so():
    """A budget stop must be visible. Stage counts computed over nothing, with
    nothing said about it, would read as an empty pipeline."""
    result = await snapshot({"job_ids": [7], "max_requests": 2})

    data = result.structured_content
    assert data["execution"]["requests_used"] == 2, "the job record plus the stage vocabulary"
    assert data["execution"]["truncated"] is True
    assert "pipelines" in data["execution"]["errors"]
    assert data["jobs"][0]["candidates_counted"] == 0
    assert data["execution"]["rate_limit"] == {"limit": None, "remaining": None}
    assert data["execution"]["next_cursor"] is None, (
        "this snapshot has no resumable position; the counts are what report the gap"
    )


async def test_a_default_call_spends_four_requests_for_one_job():
    calls: list[str] = []

    def handler(request):
        return snapshot_handler(request, calls=calls)

    result = await snapshot({"job_ids": [7]}, handler)

    assert result.structured_content["execution"]["requests_used"] == 4
    assert sorted(calls) == [
        "/v3/jobs/7",
        "/v3/jobs/7/custom_fields",
        "/v3/jobs/7/pipelines",
        "/v3/pipelines/workflows",
    ]


async def test_the_cheapest_call_asks_for_nothing_optional():
    calls: list[str] = []

    def handler(request):
        return snapshot_handler(request, calls=calls)

    await snapshot({"job_ids": [7], "include": []}, handler)

    assert not [p for p in calls if p.endswith("/custom_fields")]


# --- failures are reported, not raised ---------------------------------------


async def test_a_missing_job_is_reported_rather_than_raised():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(404, json={"message": "Not found"})

    result = await snapshot({"job_ids": [999]}, handler)

    data = result.structured_content
    assert data["jobs"] == []
    assert data["count"] == 0
    assert data["execution"]["errors"], "a missing id must be reported, not silently empty"
    assert not [p for p in calls if p.endswith("/pipelines")], (
        "a job that could not be read must not have its pipelines swept anyway"
    )


async def test_no_job_ids_is_refused():
    with pytest.raises(Exception, match="job id"):
        await snapshot({"job_ids": []})


async def test_an_unknown_include_value_is_refused_by_name():
    with pytest.raises(Exception, match="interviews"):
        await snapshot({"job_ids": [7], "include": ["interviews"]})


# --- the typed contract (issue #17) ------------------------------------------


async def test_the_tool_declares_an_object_rooted_output_schema():
    """Without `output_schema=`, a tool returning ToolResult declares nothing:
    the call works, the payload arrives, and a caller reading the tool listing
    is told only that something comes back."""
    tool = await build(snapshot_handler).get_tool("get_job_recruiting_snapshot")

    schema = tool.output_schema
    assert schema is not None, "get_job_recruiting_snapshot declares no output schema at all"
    assert schema.get("type") == "object", f"the schema root must be an object: {schema!r}"
    assert set(schema["properties"]) == EXPECTED_TOP_LEVEL, (
        "the declared top-level fields drifted from what callers depend on"
    )
    assert schema["properties"]["jobs"]["type"] == "array"

    execution = _resolve_ref(schema, schema["properties"]["execution"])
    assert execution.get("type") == "object"
    assert {"requests_used", "rate_limit", "truncated", "next_cursor", "errors"} <= set(
        execution["properties"]
    )

    job = _resolve_ref(schema, schema["properties"]["jobs"]["items"])
    assert {
        "job_id",
        "title",
        "company",
        "total_candidates",
        "stage_counts",
        "candidates",
        "tasks",
    } <= set(job["properties"])


async def test_the_display_content_is_a_counts_line_not_a_second_copy():
    """The payload goes in structured content once. Repeating it doubles the
    tokens for nothing, and a candidate or client name in the display line hands
    a reader a sample of the data as though it were the point."""
    result = await snapshot({"job_ids": [7], "include": ["tasks"]})

    assert len(result.content) == 1, "one summary line, not a block per record"
    text = result.content[0].text
    assert len(text.encode()) <= CONTENT_BYTE_CEILING, f"display content is not short: {text!r}"

    payload = json.dumps(result.structured_content)
    assert len(text.encode()) * 4 < len(payload.encode()), (
        "the display line is within a factor of four of the payload; it is a copy, "
        "not a summary"
    )

    leaks = (
        "Artemis Mining Ltd",
        "Site Millwright",
        "Kim Alvarez",
        "Dana",
        "Nakamura",
        "Logan Lake",
    )
    for leaked in leaks:
        assert leaked not in text, f"record data leaked into the display line: {leaked!r}"

    assert "jobs 1" in text
    assert "overdue tasks 1" in text, "the flag that prompts action has to survive the summary"


async def test_the_same_payload_is_reachable_as_a_typed_object():
    """`data` is the model FastMCP synthesises from the declared schema; a caller
    reading attributes and one reading the dict must see one answer."""
    result = await snapshot({"job_ids": [7], "stages": ["interview"]})

    assert result.data.count == 1
    assert result.data.jobs[0].job_id == 7
    assert result.data.jobs[0].candidates[0].status == "Phone Interview"
    assert result.data.execution.rate_limit.remaining is None


# --- the boundary ------------------------------------------------------------


async def test_no_verdict_shaped_key_appears_anywhere_in_the_output():
    """Facts and counts, at every nesting depth. A `score`, a `rank` or a
    `priority` here would be this tool quietly ordering the caller's work.

    Walks `structured_content` rather than `data`: `data` is a synthesised
    model, and `_find_banned_keys` only descends dicts and lists, so pointing it
    at `data` would pass by walking nothing at all.
    """
    result = await snapshot({"job_ids": [7], "include": ["tasks", "latest_activity"]})

    hits = _find_banned_keys(result.structured_content)
    assert not hits, f"get_job_recruiting_snapshot returned verdict-shaped keys: {hits}"
    assert BANNED_VERDICT_KEYS, "the banned-key list must not be empty"


async def test_no_verdict_shaped_key_is_declared_in_the_output_schema():
    """A field named `priority` on a task nobody fetched never appears in
    `structured_content`, so the payload check above would pass while the schema
    advertised the key to every caller."""
    tool = await build(snapshot_handler).get_tool("get_job_recruiting_snapshot")

    properties = _schema_property_names(tool.output_schema)
    hits = sorted(name for name in properties if name.lower() in BANNED_VERDICT_KEYS)
    assert not hits, f"the output schema declares verdict-shaped fields: {hits}"


async def test_the_description_carries_no_recruiting_policy_vocabulary():
    """The same check tests/test_boundary.py applies to every registered tool,
    run here because this tool is not registered in create_server yet."""
    async with Client(build(snapshot_handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    text = (tools["get_job_recruiting_snapshot"].description or "").lower()
    hits = [word for word in TOOL_VOCAB if word in text]
    assert not hits, f"the description contains recruiting-policy vocabulary: {hits}"


async def test_the_tool_is_tagged_and_annotated_read_only():
    tool = await build(snapshot_handler).get_tool("get_job_recruiting_snapshot")

    assert tool.tags == {"ats", "job", "pipeline", "read", "batch"}
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True
