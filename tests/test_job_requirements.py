"""Job facts, and the difference between "none" and "nobody looked".

Issue #12 item 7 asks for the source facts a candidate query gets built from,
and then draws a line under them: "Do not convert these facts into a hidden
candidate score." Most of what is worth testing here sits on that line.

The failure modes this file guards:

  * a custom field comes back with the account's own name AND its id, because a
    bare `{"41": "Red Seal"}` is unusable and resolving it is the adapter's job
  * a job whose account has no certifications field is reported differently
    from a job whose certifications field is blank - an empty list for both
    would tell a caller "this job needs none" on the strength of never asking
  * a certification written in the description stays in the description; if it
    ever appears in `certifications`, this adapter has started inferring
  * a sub-resource that 404s degrades to a reported error, not a failed call
  * several job ids are read, the budget bounds them, and what it never reached
    is counted rather than returned as empty
  * screening questions, which CATS API v3 has no endpoint for, are named as
    unavailable rather than invented
  * the display line stays a counts line with no job or company names in it
  * the typed contract is declared, and nothing in it is shaped like a verdict
"""

from __future__ import annotations

import dataclasses
import json

import httpx2
import pytest
from fastmcp import Client, FastMCP

# The enforcement lists live in the boundary suite; importing them keeps this
# file honest if either grows a new word or key, rather than letting a
# hand-copied duplicate drift out of date.
from test_boundary import BANNED_VERDICT_KEYS, TOOL_VOCAB, _find_banned_keys

from cats_mcp.composites import jobreqs
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    """A server carrying get_job_requirements alone - it is not in create_server yet."""
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    jobreqs.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(rows, *, key, has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


def job(jid, title, *, company=None, description=None, **extra):
    row = {
        "id": jid,
        "title": title,
        "status_id": 1,
        "workflow_id": 4,
        "city": "Kamloops",
        "state": "BC",
        "company_id": jid * 10,
        "date_modified": "2026-01-05T09:00:00-00:00",
    }
    if company is not None:
        row["_embedded"] = {"company": {"id": jid * 10, "name": company}}
    if description is not None:
        row["description"] = description
    row.update(extra)
    return row


#: A three-job account covering the three certification cases in one sweep:
#: a field with a value, the same field left blank, and no such field at all.
MILLWRIGHT = job(1, "Journeyperson Millwright", company="Artemis Mining Ltd")
PAYROLL = job(2, "Payroll Administrator", company="Artemis Mining Ltd")
SUPERVISOR = job(3, "Shift Supervisor", company="Northline Contracting")

CUSTOM_FIELDS = {
    "1": [
        {"id": 41, "name": "Certifications", "value": ["Red Seal", "First Aid Level 2"]},
        {"id": 42, "name": "Site", "value": "Logan Lake"},
    ],
    # The field exists on this account and holds nothing. Different fact.
    "2": [{"id": 41, "name": "Certifications", "value": None}],
    # No certifications field at all.
    "3": [{"id": 42, "name": "Site", "value": "Kamloops Yard"}],
}

TAGS = {
    "1": [{"id": 3, "title": "camp"}, {"id": 4, "title": "rotational"}],
    "2": [],
    "3": [{"id": 3, "title": "camp"}],
}

WORKFLOWS = {
    "_embedded": {
        "workflows": [
            {
                "id": 4,
                "title": "Standard",
                "statuses": [{"id": 100, "title": "Screening"}, {"id": 101, "title": "Placed"}],
            }
        ]
    }
}


def account(request, *, calls=None):
    """One handler for the three-job account above."""
    path = request.url.path
    if calls is not None:
        calls.append(path)

    if path.endswith("/pipelines/workflows"):
        return httpx2.Response(200, json=WORKFLOWS)
    if path.endswith("/custom_fields"):
        jid = path.split("/")[-2]
        return httpx2.Response(
            200, json=collection(CUSTOM_FIELDS.get(jid, []), key="custom_fields")
        )
    if path.endswith("/tags"):
        jid = path.split("/")[-2]
        return httpx2.Response(200, json=collection(TAGS.get(jid, []), key="tags"))
    for record in (MILLWRIGHT, PAYROLL, SUPERVISOR):
        if path.endswith(f"/jobs/{record['id']}"):
            return httpx2.Response(200, json=record)
    return httpx2.Response(404, json={"message": "Not found"})


#: Every top-level field the structured payload must carry, spelled out rather
#: than read back off JobRequirementsResult - a list derived from the model
#: would agree with it by construction and could never catch a dropped field.
EXPECTED_TOP_LEVEL = frozenset(
    {
        "jobs",
        "count",
        "requested",
        "unread",
        "unavailable",
        "certification_fields_consulted",
        "workflow_status_titles",
        "execution",
        "note",
    }
)

#: Ceiling on the human-readable line, in bytes. Generous next to the ~80 bytes
#: a real call produces, and still far below any payload it could be a copy of.
CONTENT_BYTE_CEILING = 160


def _resolve_ref(schema: dict, node):
    """Follow a JSON Schema `$ref` into the document's own `$defs`.

    Pydantic hoists every nested model into `$defs`, so `properties.execution`
    is a reference rather than the object itself. Asserting on the reference
    would assert that a pointer exists, not that the contract behind it is the
    right shape.
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


async def call(handler, arguments):
    async with Client(build(handler)) as client:
        return await client.call_tool("get_job_requirements", arguments)


# --- a value without its field name and id is not usable --------------------


async def test_a_custom_field_carries_the_account_name_and_id_not_just_a_value():
    """`{"41": "Red Seal"}` is unusable on its own: a field id is
    account-specific, and a caller holding one still has to spend a definitions
    request to learn what it is. Resolving that is what makes this an adapter."""

    result = await call(account, {"job_ids": [1]})

    fields = result.structured_content["jobs"][0]["custom_fields"]
    by_name = {f["name"]: f for f in fields}
    assert set(by_name) == {"Certifications", "Site"}
    assert by_name["Site"]["field_id"] == 42
    assert by_name["Site"]["values"] == ["Logan Lake"]
    assert by_name["Certifications"]["field_id"] == 41
    assert by_name["Certifications"]["values"] == ["Red Seal", "First Aid Level 2"]


async def test_the_job_identity_facts_survive_whatever_shape_the_row_is_in():
    """The client may be `company_name` on the row or an `_embedded` object,
    and a province may be `state` or `province`. Probing one key returns a
    confident null for every account shaped the other way."""

    def handler(request):
        if request.url.path.endswith("/jobs/9"):
            return httpx2.Response(
                200,
                json={
                    "id": 9,
                    "title": "Heavy Duty Mechanic",
                    "company_name": "Northline Contracting",
                    "province": "BC",
                    "zip": "V2C 1A1",
                },
            )
        return httpx2.Response(200, json=collection([], key="custom_fields"))

    result = await call(handler, {"job_ids": [9], "include": []})

    row = result.structured_content["jobs"][0]
    assert row["company"] == "Northline Contracting"
    assert row["site"]["state"] == "BC"
    assert row["site"]["postal_code"] == "V2C 1A1"


# --- the headline: "no field" is not "an empty field" -----------------------


async def test_a_job_with_no_certifications_field_is_not_a_job_whose_field_is_blank():
    """The reason `state` exists. Returning [] for both would tell the caller
    "this job needs no certifications" about an account that was never asked."""

    result = await call(account, {"job_ids": [1, 2, 3]})

    by_id = {row["job_id"]: row for row in result.structured_content["jobs"]}

    stored = by_id[1]["certifications"]
    assert stored["state"] == "stored"
    assert [f["name"] for f in stored["fields"]] == ["Certifications"]
    assert stored["fields"][0]["field_id"] == 41
    assert stored["fields"][0]["values"] == ["Red Seal", "First Aid Level 2"]

    blank = by_id[2]["certifications"]
    assert blank["state"] == "field_empty"
    assert blank["fields"][0]["name"] == "Certifications", (
        "the field the caller can go and fill in has to be named"
    )
    assert blank["fields"][0]["values"] == []

    absent = by_id[3]["certifications"]
    assert absent["state"] == "no_matching_field"
    assert absent["fields"] == []

    assert blank["state"] != absent["state"], "the two cases must not collapse into one"


async def test_certifications_are_not_read_when_custom_fields_were_not_read():
    """A third case, and the one an unwary caller would misread as "none": the
    custom fields were never fetched, so nothing is known either way."""

    result = await call(account, {"job_ids": [1], "include": ["tags"]})

    row = result.structured_content["jobs"][0]
    assert row["custom_fields"] is None, "null, not [], for a sub-resource nobody read"
    assert row["certifications"]["state"] == "not_read"
    assert row["certifications"]["fields"] == []


async def test_a_certification_written_in_the_description_stays_in_the_description():
    """The sharpest edge of "do not convert these facts into a hidden score".

    Pulling "must hold a valid Red Seal" out of prose is inference dressed as a
    fact - it looks stored, it is not, and the caller cannot tell. If this test
    ever fails, the adapter has started reading requirements out of text."""
    prose = "Rotational camp role. Applicants must hold a valid Red Seal and a Class 5."

    def handler(request):
        path = request.url.path
        if path.endswith("/jobs/3"):
            return httpx2.Response(200, json=job(3, "Shift Supervisor", description=prose))
        return httpx2.Response(200, json=collection(CUSTOM_FIELDS["3"], key="custom_fields"))

    result = await call(handler, {"job_ids": [3], "include": ["custom_fields"]})

    row = result.structured_content["jobs"][0]
    assert "Red Seal" in row["description"], "the prose itself is a fact and is returned"
    assert row["certifications"]["state"] == "no_matching_field"
    assert row["certifications"]["fields"] == []
    assert "Red Seal" not in json.dumps(row["certifications"])


async def test_the_field_names_consulted_are_reported_and_can_be_replaced():
    """Which field was read is a decision, so it is reported rather than
    implied. An account that calls the field something else can say so."""

    def handler(request):
        path = request.url.path
        if path.endswith("/jobs/5"):
            return httpx2.Response(200, json=job(5, "Millwright"))
        return httpx2.Response(
            200,
            json=collection(
                [{"id": 77, "name": "Trade Papers", "value": "Red Seal"}], key="custom_fields"
            ),
        )

    default = await call(handler, {"job_ids": [5], "include": ["custom_fields"]})
    assert default.structured_content["jobs"][0]["certifications"]["state"] == "no_matching_field"
    assert "certification" in default.structured_content["certification_fields_consulted"]

    override = await call(
        handler,
        {
            "job_ids": [5],
            "include": ["custom_fields"],
            "certification_field_names": ["trade papers"],
        },
    )
    data = override.structured_content
    assert data["certification_fields_consulted"] == ["trade papers"]
    facts = data["jobs"][0]["certifications"]
    assert facts["state"] == "stored"
    assert facts["fields"][0]["field_id"] == 77
    assert facts["fields"][0]["values"] == ["Red Seal"]


# --- screening questions: named as missing, never invented ------------------


async def test_screening_questions_are_reported_unavailable_rather_than_invented():
    """CATS API v3 has no per-job screening-question endpoint. Leaving them out
    silently would read as "this job has none"; inventing an endpoint would be
    worse. The gap is data, and it says where the labels actually live."""
    paths: list[str] = []

    result = await call(lambda r: account(r, calls=paths), {"job_ids": [1]})

    unavailable = result.structured_content["unavailable"]
    assert "screening_questions" in unavailable
    assert "applications" in unavailable["screening_questions"], (
        "saying it is missing is only half of it; say where the labels do live"
    )
    assert not [p for p in paths if "question" in p or "screening" in p], (
        f"an endpoint that does not exist was called: {paths}"
    )


# --- partial failure is reported, not raised --------------------------------


async def test_a_sub_resource_that_404s_degrades_to_a_reported_error():
    """One dead sub-resource must not discard the requests already spent. The
    tags come back null - unknown - and the rest of the job is still answered."""

    def handler(request):
        if request.url.path.endswith("/tags"):
            return httpx2.Response(404, json={"message": "Not found"})
        return account(request)

    result = await call(handler, {"job_ids": [1]})

    data = result.structured_content
    row = data["jobs"][0]
    assert data["count"] == 1, "the call still answered"
    assert row["tags"] is None, "null, not [] - nobody successfully read them"
    assert row["custom_fields"], "the facts that did arrive are still here"
    assert "tags:1" in data["execution"]["errors"]


async def test_an_unknown_job_id_is_reported_rather_than_raised():
    result = await call(account, {"job_ids": [1, 999]})

    data = result.structured_content
    assert [row["job_id"] for row in data["jobs"]] == [1]
    assert data["count"] == 1
    assert data["requested"] == 2, "the gap between these two is the whole report"
    assert "job:999" in data["execution"]["errors"]


async def test_an_unknown_include_is_refused_rather_than_ignored():
    with pytest.raises(Exception, match="Unknown include"):
        await call(account, {"job_ids": [1], "include": ["screening_questions"]})


# --- several ids, and the budget that bounds them ---------------------------


async def test_several_job_ids_are_answered_in_the_order_they_were_given():
    result = await call(account, {"job_ids": [3, 1, 2, 1]})

    data = result.structured_content
    assert [row["job_id"] for row in data["jobs"]] == [3, 1, 2], "given order, deduped"
    assert data["requested"] == 3
    # 3 records + 3 custom fields + 3 tags + 1 workflow read.
    assert data["execution"]["requests_used"] == 10
    assert data["execution"]["truncated"] is False
    assert data["unread"] == {}


async def test_the_budget_bounds_the_sub_resources_and_says_what_it_missed():
    """A budget stop has to be visible. A job whose tags were never read has
    unknown tags, and reporting that as an empty list is how a caller ends up
    certain a job carries none."""

    result = await call(account, {"job_ids": [1, 2, 3], "max_requests": 5})

    data = result.structured_content
    assert data["execution"]["requests_used"] == 5, "three records, then two custom field reads"
    assert data["execution"]["truncated"] is True
    assert data["unread"] == {"custom_fields": 1, "tags": 3, "workflow": 1}
    assert "tags" in data["execution"]["errors"]

    by_id = {row["job_id"]: row for row in data["jobs"]}
    assert by_id[3]["custom_fields"] is None, "the job the budget did not reach"
    assert by_id[1]["custom_fields"] is not None, "the ones it did are still answered"
    assert all(row["tags"] is None for row in data["jobs"])
    assert data["workflow_status_titles"] == {}


async def test_the_record_reads_themselves_are_bounded():
    result = await call(account, {"job_ids": [1, 2, 3], "include": [], "max_requests": 2})

    data = result.structured_content
    assert data["count"] == 2
    assert data["requested"] == 3
    assert data["unread"] == {"job_records": 1}
    assert data["execution"]["truncated"] is True
    assert "job_records" in data["execution"]["errors"]


async def test_more_ids_than_the_batch_ceiling_are_refused_by_the_schema():
    with pytest.raises(Exception, match="job_ids"):
        await call(account, {"job_ids": list(range(1, jobreqs.MAX_BATCH + 2))})


async def test_workflow_titles_cost_one_request_for_the_call_not_one_per_job():
    """The one sub-resource whose cost does not scale with the number of jobs.
    Reading it per job would triple its price for nothing."""
    paths: list[str] = []

    result = await call(lambda r: account(r, calls=paths), {"job_ids": [1, 2, 3]})

    assert [p for p in paths if p.endswith("/pipelines/workflows")] == ["/v3/pipelines/workflows"]
    data = result.structured_content
    assert data["workflow_status_titles"] == {"100": "Screening", "101": "Placed"}
    assert data["jobs"][0]["workflow_id"] == 4


async def test_include_narrows_what_the_call_spends():
    paths: list[str] = []

    result = await call(
        lambda r: account(r, calls=paths), {"job_ids": [1, 2], "include": ["custom_fields"]}
    )

    assert not [p for p in paths if p.endswith("/tags")]
    assert not [p for p in paths if p.endswith("/pipelines/workflows")]
    data = result.structured_content
    assert data["execution"]["requests_used"] == 4
    assert all(row["tags"] is None for row in data["jobs"])
    assert data["unread"] == {}, "not asking for tags is not the same as running out of budget"


async def test_the_description_is_bounded_and_says_when_it_was_cut():
    """Ten job descriptions is most of a context window. The bound is the
    caller's, and whether it bit is a fact they need."""
    body = "Rotational camp role. " * 200

    def handler(request):
        if request.url.path.endswith("/jobs/1"):
            return httpx2.Response(200, json=job(1, "Millwright", description=body))
        return httpx2.Response(200, json={})

    result = await call(handler, {"job_ids": [1], "include": [], "max_description_chars": 100})

    row = result.structured_content["jobs"][0]
    assert len(row["description"]) == 100
    assert row["description_truncated"] is True

    def short(request):
        if request.url.path.endswith("/jobs/1"):
            return httpx2.Response(200, json=job(1, "Millwright", description="Camp role."))
        return httpx2.Response(200, json={})

    whole = await call(short, {"job_ids": [1], "include": []})
    intact = whole.structured_content["jobs"][0]
    assert intact["description"] == "Camp role."
    assert intact["description_truncated"] is False, "a description that fit was not cut"


# --- progress is counts, never record data ----------------------------------


async def test_progress_messages_carry_no_job_or_company_names():
    """Issue #21. A progress notification is a different audience from the tool
    result, and a title in one is a fact leaving by a side door."""
    seen: list[str] = []

    async def on_progress(progress, total, message):
        seen.append(message or "")

    async with Client(build(account), progress_handler=on_progress) as client:
        await client.call_tool("get_job_requirements", {"job_ids": [1, 2, 3]})

    assert seen, "no progress reported, so the assertion below proves nothing"
    for leaked in ("Millwright", "Payroll", "Supervisor", "Artemis", "Northline", "Logan Lake"):
        assert not [m for m in seen if leaked in m], f"record data reached progress: {leaked!r}"


# --- the typed contract (issue #17) -----------------------------------------


async def test_the_tool_declares_an_object_rooted_output_schema():
    """Without `output_schema=`, a tool returning ToolResult declares nothing:
    the call still works, the payload still arrives, and a caller reading the
    listing is told only that something comes back."""
    tool = await build(account).get_tool("get_job_requirements")

    schema = tool.output_schema
    assert schema is not None, "get_job_requirements declares no output schema at all"
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

    row = _resolve_ref(schema, schema["properties"]["jobs"]["items"])
    assert {
        "job_id",
        "title",
        "description",
        "company",
        "site",
        "custom_fields",
        "certifications",
        "tags",
        "workflow_id",
    } <= set(row["properties"])

    certifications = _resolve_ref(schema, row["properties"]["certifications"])
    assert set(certifications["properties"]) == {"state", "fields"}


async def test_the_display_content_is_a_counts_line_not_a_second_copy():
    """The payload goes in structured content once. Repeating it doubles the
    tokens, and a job title in the summary hands a reader one record as though
    the tool had picked it."""
    result = await call(account, {"job_ids": [1, 2, 3]})

    assert len(result.content) == 1, "one summary line, not a block per record"
    text = result.content[0].text
    assert len(text.encode()) <= CONTENT_BYTE_CEILING, f"display content is not short: {text!r}"

    payload = json.dumps(result.structured_content)
    assert len(text.encode()) * 4 < len(payload.encode()), (
        "the display line is within a factor of four of the payload; it is a copy, "
        "not a summary"
    )

    for leaked in ("Millwright", "Payroll", "Artemis", "Northline", "Red Seal", "Logan Lake"):
        assert leaked not in text, f"record data leaked into the display line: {leaked!r}"

    assert "jobs 3/3" in text
    assert "requests" in text


async def test_the_same_payload_is_reachable_as_a_typed_object():
    """`data` is the model FastMCP synthesises from the declared schema; a
    caller reading attributes and one reading the dict must see one answer."""
    result = await call(account, {"job_ids": [1, 2]})

    assert [row.job_id for row in result.data.jobs] == [1, 2]
    assert result.data.jobs[0].certifications.state == "stored"
    assert result.data.jobs[0].custom_fields[0].name == "Certifications"
    assert result.data.jobs[1].certifications.state == "field_empty"
    assert result.data.execution.rate_limit.remaining is None
    assert dataclasses.asdict(result.data) == result.structured_content


# --- the boundary -----------------------------------------------------------


async def test_no_verdict_shaped_key_appears_anywhere_in_the_output():
    """Facts, at every nesting depth. A `score` or a `priority` here would be
    this tool building the caller's query for them after all.

    Walks `structured_content` rather than `data`: `data` is a synthesised
    model, and `_find_banned_keys` only descends dicts and lists, so pointing it
    at `data` would pass by walking nothing at all.
    """
    result = await call(account, {"job_ids": [1, 2, 3]})

    hits = _find_banned_keys(result.structured_content)
    assert not hits, f"get_job_requirements returned verdict-shaped keys: {hits}"
    assert BANNED_VERDICT_KEYS, "the banned-key list must not be empty"


async def test_no_verdict_shaped_key_is_declared_in_the_output_schema():
    """A field named `qualified` on an empty result never appears in
    `structured_content`, so the payload check above would pass while the tool
    advertised the key to every client that read its schema."""
    tool = await build(account).get_tool("get_job_requirements")

    properties = _schema_property_names(tool.output_schema)
    hits = sorted(name for name in properties if name.lower() in BANNED_VERDICT_KEYS)
    assert not hits, f"the output schema declares verdict-shaped fields: {hits}"


async def test_the_description_contains_no_recruiting_policy_vocabulary():
    """A tool description is guidance a model reads, exactly like a prompt. This
    tool is the one most tempted by it: its whole subject is what a job asks
    for, and describing that in recruiting-policy terms would turn a facts
    endpoint into an instruction to apply them."""
    async with Client(build(account)) as client:
        tool = next(t for t in await client.list_tools() if t.name == "get_job_requirements")

    text = (tool.description or "").lower()
    hits = [word for word in TOOL_VOCAB if word in text]
    assert not hits, f"the description contains recruiting-policy vocabulary: {hits}"
    assert "caller's job" in text, "the description has to say who does the interpreting"


async def test_the_tool_is_annotated_as_a_read():
    async with Client(build(account)) as client:
        tool = next(t for t in await client.list_tools() if t.name == "get_job_requirements")

    annotations = tool.annotations
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is True


def test_register_reports_how_many_tools_it_added():
    mcp = FastMCP("test")
    assert jobreqs.register(mcp, lambda: None, enforce_auth=False) == 1
