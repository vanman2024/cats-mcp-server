"""Job resolution, and the one property the whole tool exists for.

Issue #12 item 4's worked example is three jobs that all answer to "Artemis":
one names it in the title, one has it as the client company, one holds it in a
site custom field. A caller who is handed the first of those and told nothing
else submits people to the wrong req, and no error is ever raised.

So the failure modes worth guarding are not "does it return rows" but:

  * an ambiguous reference returns every record, never a single guess
  * every row says which field matched, so the caller can tell them apart
  * a record found by company or custom field is found at all
  * an id is a direct read, not a sweep
  * the request budget bounds the per-job reads and reports what it missed
  * nothing in the output is shaped like a verdict

Each test below fails loudly if one of those regresses.

Issue #17 added a second axis to all of that. The tool now returns a typed
`ResolveResult` through `ToolResult`, so the assertions read
`result.structured_content` - the dict the tool actually put on the wire -
rather than a shape the test invented. `result.data` carries the same payload
as a model FastMCP synthesises from the declared output schema, which is what
makes the schema itself worth asserting on: if `output_schema` ever went
missing, `data` would silently degrade and every other test here would still
pass.
"""

from __future__ import annotations

import dataclasses
import json

import httpx2
import pytest
from fastmcp import Client, FastMCP

# The two enforcement lists live in the boundary suites; importing them keeps
# this file honest if either grows a new word or key, rather than letting a
# hand-copied duplicate drift out of date.
from test_boundary import BANNED_VERDICT_KEYS, TOOL_VOCAB, _find_banned_keys

from cats_mcp.composites import resolve
from cats_mcp.composites.resolve import TextPredicate, _evaluate, _normalise
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    """A server carrying resolve_job alone - it is not wired into create_server yet."""
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    resolve.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(rows, *, key="jobs", has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


def job(jid, title, *, company=None, city="Kamloops", state="BC", description=None, owner=None):
    row = {
        "id": jid,
        "title": title,
        "status_id": 1,
        "city": city,
        "state": state,
        "date_modified": "2026-01-05T09:00:00-00:00",
    }
    if company is not None:
        row["_embedded"] = {"company": {"id": jid * 10, "name": company}}
    if description is not None:
        row["description"] = description
    if owner is not None:
        row["owner"] = owner
    return row


#: Every top-level field the structured payload must carry, spelled out rather
#: than read back off ResolveResult - a list derived from the model would agree
#: with it by construction and could never catch a field being dropped.
EXPECTED_TOP_LEVEL = frozenset(
    {
        "jobs",
        "count",
        "ambiguous",
        "total_matched",
        "scanned",
        "fields_searched",
        "seeds_used",
        "unsearched",
        "execution",
        "note",
    }
)

#: Ceiling on the human-readable line, in bytes. Generous next to the ~30 bytes
#: a real call produces, and still far below any payload it could be a copy of.
CONTENT_BYTE_CEILING = 160


def _resolve_ref(schema: dict, node):
    """Follow a JSON Schema `$ref` into the document's own `$defs`.

    Pydantic hoists every nested model into `$defs`, so `properties.execution`
    is a reference rather than the object itself. A test that asserted on the
    reference would be asserting that a pointer exists, not that the contract
    behind it is the right shape.
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


#: The issue #12 account in miniature: three jobs answering to "Artemis" through
#: three different fields, plus one that has nothing to do with it.
ARTEMIS_TITLE = job(1, "Artemis Site Millwright", company="Northline Contracting")
ARTEMIS_COMPANY = job(2, "Heavy Duty Mechanic", company="Artemis Mining Ltd")
ARTEMIS_CUSTOM_FIELD = job(3, "Shift Supervisor", company="Northline Contracting")
UNRELATED = job(4, "Payroll Administrator", company="Northline Contracting")

ALL_JOBS = [ARTEMIS_TITLE, ARTEMIS_COMPANY, ARTEMIS_CUSTOM_FIELD, UNRELATED]


def artemis_handler(request, *, calls=None):
    """One handler for the four-job account above."""
    path = request.url.path
    if calls is not None:
        calls.append(path)
    if path.endswith("/jobs"):
        return httpx2.Response(200, json=collection(ALL_JOBS))
    if path.endswith("/jobs/3/custom_fields"):
        return httpx2.Response(
            200,
            json=collection(
                [{"id": 7, "name": "Site", "value": "Artemis Pit"}], key="custom_fields"
            ),
        )
    if path.endswith("/custom_fields"):
        return httpx2.Response(
            200,
            json=collection(
                [{"id": 7, "name": "Site", "value": "Williams Lake Yard"}], key="custom_fields"
            ),
        )
    if path.endswith("/tags"):
        return httpx2.Response(200, json=collection([], key="tags"))
    return httpx2.Response(200, json={})


# --- normalisation is normalisation, not classification ---------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Artemis Mining Ltd.", "artemis mining ltd"),
        ("  LOGAN   LAKE  ", "logan lake"),
        ("B.C.", "b c"),
        (None, ""),
    ],
)
def test_normalise_only_touches_case_punctuation_and_whitespace(raw, expected):
    assert _normalise(raw) == expected


def test_contains_is_substring_not_cats_tokenisation():
    """The defect this repo has been bitten by: CATS `contains` tokenises, so
    contains='Logan Lake' also returns Williams Lake. Local matching must not."""
    predicate = TextPredicate(values=["logan lake"])
    assert not _evaluate("Williams Lake Yard", predicate)[0]
    assert _evaluate("Logan Lake Depot", predicate)[0]


def test_a_reference_is_not_expanded_into_synonyms():
    """If this ever passes, the adapter has grown a taxonomy of project names."""
    assert not _evaluate("ARTM-2024 Pit", TextPredicate(values=["artemis"]))[0]


# --- the headline: an ambiguous reference is reported as ambiguous ----------


async def test_an_ambiguous_reference_returns_every_job_never_one():
    """The reason the tool exists. Three jobs answer to "Artemis" through three
    different fields; handing back any one of them would be a silent wrong
    answer that ends with a person submitted to the wrong req."""

    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    data = result.structured_content
    assert [row["job_id"] for row in data["jobs"]] == [1, 2, 3]
    assert data["count"] == 3
    assert data["total_matched"] == 3
    assert data["ambiguous"] is True


async def test_every_returned_job_names_the_field_that_matched():
    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    by_id = {row["job_id"]: row for row in result.structured_content["jobs"]}
    for row in by_id.values():
        assert row["matched_fields"], f"job {row['job_id']} was returned with no evidence"
        for entry in row["matched_fields"]:
            assert entry["matched"] == "Artemis"

    assert {e["field"] for e in by_id[1]["matched_fields"]} == {"title"}
    assert {e["field"] for e in by_id[3]["matched_fields"]} == {"custom_fields"}


async def test_a_job_matched_by_company_says_company():
    """Job 2's title is "Heavy Duty Mechanic" - nothing about Artemis is in it.
    Finding it at all, and saying why, is item 4 of the issue."""

    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    row = next(r for r in result.structured_content["jobs"] if r["job_id"] == 2)
    evidence = row["matched_fields"]
    assert [e["field"] for e in evidence] == ["company"]
    assert evidence[0]["value"] == "Artemis Mining Ltd"
    assert row["company"] == "Artemis Mining Ltd"


async def test_a_job_matched_by_a_custom_field_names_that_field():
    """Job 3 is filed under a Site custom field. The evidence has to say which
    custom field, or the caller cannot tell why it came back."""

    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    row = next(r for r in result.structured_content["jobs"] if r["job_id"] == 3)
    entry = row["matched_fields"][0]
    assert entry["field"] == "custom_fields"
    assert entry["source"] == "Site"
    assert entry["value"] == "Artemis Pit"


async def test_a_tag_match_is_found_and_labelled():
    def handler(request):
        path = request.url.path
        if path.endswith("/jobs"):
            return httpx2.Response(200, json=collection([job(5, "Shift Supervisor")]))
        if path.endswith("/tags"):
            return httpx2.Response(
                200, json=collection([{"id": 3, "title": "Artemis"}], key="tags")
            )
        return httpx2.Response(200, json=collection([], key="custom_fields"))

    async with Client(build(handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    row = result.structured_content["jobs"][0]
    assert row["job_id"] == 5
    assert row["matched_fields"][0]["field"] == "tags"


async def test_a_description_match_returns_a_window_not_the_whole_document():
    """Evidence has to show the hit. Returning a whole job description to prove
    one word appeared in it pushes the answer out of view."""
    body = "Rotational camp role. " * 40 + "Located at the Artemis pit. " + "Boots supplied. " * 40

    def handler(request):
        if request.url.path.endswith("/jobs"):
            return httpx2.Response(
                200, json=collection([job(6, "Shift Supervisor", description=body)])
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "resolve_job",
            {"query": {"values": ["Artemis"]}, "fields": ["description"]},
        )

    entry = result.structured_content["jobs"][0]["matched_fields"][0]
    assert entry["field"] == "description"
    assert "Artemis" in entry["value"]
    assert len(entry["value"]) < len(body)


# --- the direct id read -----------------------------------------------------


async def test_an_exact_id_lookup_returns_exactly_that_job():
    paths: list[str] = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/jobs/2"):
            return httpx2.Response(200, json=ARTEMIS_COMPANY)
        return httpx2.Response(200, json=collection(ALL_JOBS))

    async with Client(build(handler)) as client:
        result = await client.call_tool("resolve_job", {"job_id": 2})

    data = result.structured_content
    assert [row["job_id"] for row in data["jobs"]] == [2]
    assert data["ambiguous"] is False
    assert data["jobs"][0]["matched_fields"][0]["field"] == "job_id"
    assert data["execution"]["requests_used"] == 1
    assert paths == ["/v3/jobs/2"], f"an id must not trigger a sweep: {paths}"


async def test_an_unknown_id_is_reported_rather_than_raised():
    def handler(request):
        return httpx2.Response(404, json={"message": "Not found"})

    async with Client(build(handler)) as client:
        result = await client.call_tool("resolve_job", {"job_id": 999})

    data = result.structured_content
    assert data["jobs"] == []
    assert data["count"] == 0
    assert data["execution"]["errors"], "a missing id must be reported, not silently empty"


async def test_neither_a_query_nor_an_id_is_refused():
    def handler(request):
        return httpx2.Response(200, json=collection(ALL_JOBS))

    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="query"):
            await client.call_tool("resolve_job", {})


# --- seeds use one exact filter per value -----------------------------------


async def test_a_status_seed_uses_one_exact_filter_per_value():
    """CATS `contains` tokenises and matches any token, so every value gets its
    own `exactly` filter and its own request."""
    bodies: list[dict] = []

    def handler(request):
        if request.url.path.endswith("/jobs/search"):
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, json=collection([ARTEMIS_TITLE]))
        return httpx2.Response(200, json=collection([], key="custom_fields"))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "resolve_job",
            {"query": {"values": ["Artemis"]}, "status_ids": [11, 12], "fields": ["title"]},
        )

    assert [b["filter"] for b in bodies] == ["exactly", "exactly"]
    assert [b["value"] for b in bodies] == [11, 12]
    assert [b["field"] for b in bodies] == ["status_id", "status_id"]
    assert result.structured_content["seeds_used"] == [
        {"field": "status_id", "value": 11},
        {"field": "status_id", "value": 12},
    ]
    assert result.structured_content["count"] == 1, "the same job under two seeds is one row"


# --- the request budget -----------------------------------------------------


async def test_per_job_reads_are_spent_only_on_jobs_no_free_field_matched():
    """Job 1 and 2 are already found by title and company. Spending a custom
    field read on them would buy nothing; the remainder is where the search is."""
    reads: list[str] = []

    def handler(request):
        return artemis_handler(request, calls=reads)

    async with Client(build(handler)) as client:
        await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    custom_field_reads = [p for p in reads if p.endswith("/custom_fields")]
    assert sorted(custom_field_reads) == ["/v3/jobs/3/custom_fields", "/v3/jobs/4/custom_fields"]


async def test_the_budget_is_respected_and_what_it_missed_is_reported():
    """A budget stop must be visible. An unreached job is not a job that failed
    to match, and reporting it as one is how a caller ends up sure there is only
    one Artemis job."""

    def handler(request):
        path = request.url.path
        if path.endswith("/jobs"):
            return httpx2.Response(200, json=collection([job(i, f"Role {i}") for i in range(1, 9)]))
        if path.endswith("/jobs/8/custom_fields"):
            return httpx2.Response(
                200,
                json=collection(
                    [{"id": 7, "name": "Site", "value": "Artemis Pit"}], key="custom_fields"
                ),
            )
        return httpx2.Response(200, json=collection([], key="custom_fields"))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "resolve_job",
            {
                "query": {"values": ["Artemis"]},
                "fields": ["title", "custom_fields"],
                "max_requests": 4,
            },
        )

    data = result.structured_content
    assert data["execution"]["requests_used"] == 4, "one sweep page plus three per-job reads"
    assert data["execution"]["truncated"] is True
    assert data["count"] == 0, "job 8 was never reached, so it is not reported as found"
    assert data["unsearched"] == {"custom_fields": 5}, (
        "five jobs were never looked at; saying so is the only thing between the "
        "caller and a confident wrong answer"
    )
    assert "custom_fields" in data["execution"]["errors"]
    assert data["execution"]["rate_limit"] == {"limit": None, "remaining": None}
    assert data["execution"]["next_cursor"] is None, (
        "this sweep has no resumable position; `unsearched` is what reports the gap"
    )


async def test_max_jobs_caps_the_rows_but_never_the_reported_total():
    """Being handed two of three matches without being told is the same silent
    wrong answer as being handed one."""

    def handler(request):
        if request.url.path.endswith("/jobs"):
            return httpx2.Response(
                200, json=collection([job(i, f"Artemis Role {i}") for i in range(1, 6)])
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "resolve_job",
            {"query": {"values": ["Artemis"]}, "fields": ["title"], "max_jobs": 2},
        )

    data = result.structured_content
    assert data["count"] == 2
    assert data["total_matched"] == 5
    assert data["execution"]["truncated"] is True


async def test_the_sweep_follows_pagination_within_the_budget():
    pages: list[str | None] = []

    def handler(request):
        if request.url.path.endswith("/jobs"):
            page = request.url.params.get("page")
            pages.append(page)
            return httpx2.Response(
                200,
                json=collection([job(int(page), "Artemis Role")], has_next=page != "3"),
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "resolve_job",
            {"query": {"values": ["Artemis"]}, "fields": ["title"], "max_requests": 5},
        )

    assert pages == ["1", "2", "3"]
    assert result.structured_content["scanned"] == 3
    assert result.structured_content["execution"]["truncated"] is False


# --- the typed contract (issue #17) -----------------------------------------


async def test_the_tool_declares_an_object_rooted_output_schema():
    """Without `output_schema=`, a tool returning ToolResult declares nothing.

    That is the issue #17 gap in its exact form: the call still works, the
    payload still arrives, and a caller reading the tool listing is told only
    that something comes back. Returning the model directly would declare a
    schema but also serialise the whole payload into display content, so the
    schema is passed explicitly and ToolResult is returned.
    """
    tool = await build(artemis_handler).get_tool("resolve_job")

    schema = tool.output_schema
    assert schema is not None, "resolve_job declares no output schema at all"
    assert schema.get("type") == "object", f"the schema root must be an object: {schema!r}"
    assert set(schema["properties"]) == EXPECTED_TOP_LEVEL, (
        "the declared top-level fields drifted from what callers depend on"
    )
    assert schema["properties"]["jobs"]["type"] == "array"

    # The execution facts are a nested object, not scattered at the root.
    execution = _resolve_ref(schema, schema["properties"]["execution"])
    assert execution.get("type") == "object"
    assert {"requests_used", "rate_limit", "truncated", "next_cursor", "errors"} <= set(
        execution["properties"]
    )

    # And a job row declares its own fields plus the evidence for it.
    row = _resolve_ref(schema, schema["properties"]["jobs"]["items"])
    assert {"job_id", "title", "company", "matched_fields"} <= set(row["properties"])


async def test_the_display_content_is_a_counts_line_not_a_second_copy():
    """The payload goes in structured content once. Repeating it in the display
    text doubles the tokens and hands a reader the first row as though the tool
    had picked it - which is the one thing this tool refuses to do."""
    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    assert len(result.content) == 1, "one summary line, not a block per record"
    text = result.content[0].text
    assert len(text.encode()) <= CONTENT_BYTE_CEILING, f"display content is not short: {text!r}"

    payload = json.dumps(result.structured_content)
    assert len(text.encode()) * 4 < len(payload.encode()), (
        "the display line is within a factor of four of the payload; it is a copy, "
        "not a summary"
    )

    for leaked in ("Artemis Mining Ltd", "Artemis Site Millwright", "Northline", "Site"):
        assert leaked not in text, f"record data leaked into the display line: {leaked!r}"

    assert "matched 3" in text
    assert "ambiguous" in text, "the flag that matters most has to survive the summary"


async def test_the_same_payload_is_reachable_as_a_typed_object():
    """`data` is the model FastMCP synthesises from the declared schema; a caller
    that reads attributes and one that reads the dict must see one answer."""
    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    assert [row.job_id for row in result.data.jobs] == [1, 2, 3]
    assert result.data.ambiguous is True
    assert result.data.total_matched == 3
    assert result.data.jobs[1].company == "Artemis Mining Ltd"
    assert result.data.jobs[0].matched_fields[0].field == "title"
    assert result.data.execution.rate_limit.remaining is None
    assert dataclasses.asdict(result.data) == result.structured_content


# --- the boundary -----------------------------------------------------------


async def test_no_verdict_shaped_key_appears_anywhere_in_the_output():
    """Facts and evidence, at every nesting depth. A `score` or a `rank` here
    would be this tool quietly choosing for the caller after all.

    Walks `structured_content` rather than `data`: `data` is now a synthesised
    model, and `_find_banned_keys` only descends dicts and lists, so pointing it
    at `data` would pass by walking nothing at all.
    """
    async with Client(build(artemis_handler)) as client:
        result = await client.call_tool("resolve_job", {"query": {"values": ["Artemis"]}})

    hits = _find_banned_keys(result.structured_content)
    assert not hits, f"resolve_job returned verdict-shaped keys: {hits}"
    assert BANNED_VERDICT_KEYS, "the banned-key list must not be empty"


async def test_no_verdict_shaped_key_is_declared_in_the_output_schema():
    """The typed conversion moved the risk from the payload to the model.

    A field named `score` on an empty result never appears in
    `structured_content`, so the payload check above would pass while the schema
    advertised the key to every caller. The declared contract is checked here.
    """
    tool = await build(artemis_handler).get_tool("resolve_job")

    properties = _schema_property_names(tool.output_schema)
    hits = sorted(name for name in properties if name.lower() in BANNED_VERDICT_KEYS)
    assert not hits, f"resolve_job's output schema declares verdict-shaped fields: {hits}"


async def test_the_description_carries_no_recruiting_policy_vocabulary():
    """The same check tests/test_boundary.py applies to every registered tool,
    run here because this tool is not registered in create_server yet."""
    async with Client(build(artemis_handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    text = (tools["resolve_job"].description or "").lower()
    hits = [word for word in TOOL_VOCAB if word in text]
    assert not hits, f"resolve_job's description contains recruiting-policy vocabulary: {hits}"


async def test_the_tool_is_tagged_and_annotated_read_only():
    tool = await build(artemis_handler).get_tool("resolve_job")

    assert tool.tags == {"ats", "job", "read", "search"}
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True
