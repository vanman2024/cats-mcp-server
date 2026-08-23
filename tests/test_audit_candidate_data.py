"""The data-quality audit, and the four ways it could quietly answer wrongly.

Proving that it "finds anomalies" is not interesting. The failures that matter
are the ones that come back looking like a clean bill of health, or like a
judgement about a person:

  * a record reported as having no email when the row simply never carried one -
    unknown dressed up as clean
  * a real duplicate missed because one record punctuates a phone differently
  * a province cluster that names one spelling as the right one, which is the
    account's call and not this adapter's
  * a finding that describes anybody rather than a stored field

The last group guards the boundary itself: the payload and the published schema
carry evidence and never a measure, the description stays out of recruiting
vocabulary, and the display line carries counts rather than contact detail.

The tool returns a `ToolResult`, so assertions here are on
`result.structured_content` - the raw JSON object that is the wire contract.
`result.data` is that object rebuilt against the published schema into a
synthesised dataclass, which is not subscriptable and would hide a rename behind
whatever the new attribute is called.
"""

from __future__ import annotations

import json
import re

import httpx2
import pytest
from fastmcp import Client, FastMCP
from test_boundary import TOOL_VOCAB, VERIFIED_SAFE_USAGE, _find_banned_keys, _schema_key_names

from cats_mcp.composites import audit
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    """A server carrying this tool alone, so the test is about this tool alone."""
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    mcp = FastMCP("test")
    audit.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(rows, key="candidates", *, has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


def row(cid, *, emails=None, phones=None, **extra):
    """A candidate row.

    `emails=None` omits the key entirely, which is what the candidate search
    projection does and what forces a sub-collection read. `emails=[]` is a row
    that answered the question with "none", which is a different fact.
    """
    record = {"id": cid, "first_name": "Pat", "last_name": f"Number{cid}", "state": "BC"}
    if emails is not None:
        record["emails"] = [{"email": e} for e in emails]
    if phones is not None:
        record["phones"] = [{"number": p} for p in phones]
    record.update(extra)
    return record


def sweep(rows):
    """A handler serving one page of candidates and nothing else."""

    def handler(request):
        if request.url.path.endswith("/candidates"):
            return httpx2.Response(200, json=collection(rows))
        return httpx2.Response(200, json={})

    return handler


async def run(handler, arguments=None):
    async with Client(build(handler)) as client:
        result = await client.call_tool("audit_candidate_data", arguments or {})
    return result


def findings_text(data):
    """The payload without `note`, lowercased, for vocabulary assertions.

    `note` is prose explaining what the result does NOT say - it contains
    "no likelihood is attached" and "which spelling this account treats as
    correct is not stated" on purpose. Scanning it for the words it exists to
    disclaim would fail the boundary tests for stating the boundary.
    """
    return json.dumps({k: v for k, v in data.items() if k != "note"}).lower()


def mentions(text, word):
    """Whole-word search. 'age' must not match 'stage_conflicts'."""
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


# --- normalization is spelling, never meaning -------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("BC", "bc"), ("B.C.", "bc"), ("b c", "bc"), ("  bC  ", "bc"), (None, "")],
)
def test_region_folding_removes_case_punctuation_and_spacing(raw, expected):
    assert audit._normalise_region(raw) == expected


def test_region_folding_does_not_decide_that_two_spellings_name_one_place():
    """Folding "British Columbia" into "BC" would need a table of place names,
    which is an opinion about an account's data. If this ever passes, the module
    has grown a gazetteer."""
    assert audit._normalise_region("British Columbia") != audit._normalise_region("BC")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("250-555-0111", "2505550111"),
        ("(250) 555 0111", "2505550111"),
        ("+1 250 555 0111", "2505550111"),
    ],
)
def test_phone_folding_matches_lookup_candidates_semantics(raw, expected):
    """Identical to composites/lookup.py's `_normalise_phone`. A pair this tool
    reports as a duplicate and `lookup_candidate` denies would be two tools
    disagreeing about the same relation."""
    assert audit._normalise_phone(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("Pat@Example.com", "pat@example.com"), ("  PAT@X.COM ", "pat@x.com")]
)
def test_email_folding_touches_only_case_and_whitespace(raw, expected):
    assert audit._normalise_email(raw) == expected


# --- duplicates: the evidence, and nothing that reads as a measure ----------


async def test_two_records_sharing_a_normalized_email_form_one_cluster():
    """The relation reported is exact and checkable: the folded string, and each
    record's own spelling of it."""
    handler = sweep(
        [
            row(1, emails=["pat@example.com"], phones=[]),
            row(2, emails=["Pat@Example.COM"], phones=[]),
        ]
    )
    data = (await run(handler, {"anomalies": ["duplicates"]})).structured_content

    assert len(data["duplicates"]) == 1
    cluster = data["duplicates"][0]
    assert cluster["field"] == "email"
    assert cluster["normalized"] == "pat@example.com"
    assert cluster["record_count"] == 2
    assert [m["candidate_id"] for m in cluster["members"]] == [1, 2]
    # The evidence: what each record actually stores, and where it was read.
    assert [m["stored_value"] for m in cluster["members"]] == [
        "pat@example.com",
        "Pat@Example.COM",
    ]
    assert {m["source"] for m in cluster["members"]} == {"candidate record"}


async def test_a_duplicate_cluster_carries_no_measure_of_how_likely_it_is():
    """A likelihood is a decision about two records wearing a statistic's
    clothes. The relation here is either true or it is not, and the fields are
    the evidence for it."""
    handler = sweep(
        [
            row(1, emails=["pat@example.com"], phones=[]),
            row(2, emails=["pat@example.com"], phones=[]),
        ]
    )
    data = (await run(handler, {"anomalies": ["duplicates"]})).structured_content

    cluster = data["duplicates"][0]
    assert set(cluster) == {"field", "normalized", "members", "record_count"}
    assert set(cluster["members"][0]) == {"candidate_id", "stored_value", "source"}
    text = findings_text(data)
    for banned in ("confidence", "similarity", "certainty", "probability", "likelihood", "score"):
        assert not mentions(text, banned), (
            f"{banned!r} appears in the payload: a duplicate is reported as evidence, "
            f"not as a measure"
        )


async def test_phone_formatting_differences_still_cluster():
    """The duplicate an account most often holds: one record typed the number
    with dashes, the other with brackets and spaces."""
    handler = sweep(
        [
            row(1, emails=[], phones=["250-555-0111"]),
            row(2, emails=[], phones=["(250) 555 0111"]),
        ]
    )
    data = (await run(handler, {"anomalies": ["duplicates"]})).structured_content

    cluster = data["duplicates"][0]
    assert cluster["field"] == "phone"
    assert cluster["normalized"] == "2505550111"
    assert [m["stored_value"] for m in cluster["members"]] == [
        "250-555-0111",
        "(250) 555 0111",
    ]


async def test_a_value_that_does_not_parse_never_forms_a_cluster():
    """Forty records storing "n/a" in a phone field are forty broken fields, not
    a forty-way relation between the people on them."""
    handler = sweep([row(1, emails=[], phones=["n/a"]), row(2, emails=[], phones=["n/a"])])
    data = (
        await run(handler, {"anomalies": ["duplicates", "contact_methods"]})
    ).structured_content

    assert data["duplicates"] == []
    assert [a["anomaly"] for a in data["contact_methods"]] == [
        "phone_unparseable",
        "phone_unparseable",
    ]


async def test_a_contact_value_the_row_omits_is_read_from_the_sub_collection():
    """CATS keeps emails and phones as sub-resources and the search projection
    carries neither. A row that never answered the question must not be reported
    as a record with no email."""
    paths: list[str] = []

    def handler(request):
        path = request.url.path
        paths.append(path)
        if path.endswith("/candidates"):
            return httpx2.Response(200, json=collection([row(1), row(2)]))
        if path.endswith("/emails"):
            cid = path.split("/")[-2]
            address = "pat@example.com" if cid == "1" else "PAT@example.com"
            return httpx2.Response(200, json=collection([{"email": address}], key="emails"))
        return httpx2.Response(200, json=collection([], key="phones"))

    data = (await run(handler, {"anomalies": ["duplicates"]})).structured_content

    assert "/v3/candidates/1/emails" in paths, f"sub-collection never read: {paths}"
    cluster = data["duplicates"][0]
    assert cluster["normalized"] == "pat@example.com"
    assert {m["source"] for m in cluster["members"]} == {"emails sub-collection"}


# --- unknown is not clean ---------------------------------------------------


async def test_a_record_with_no_contact_value_is_reported_only_once_both_were_read():
    handler = sweep([row(1, emails=[], phones=[])])
    data = (await run(handler, {"anomalies": ["contact_methods"]})).structured_content

    assert [a["anomaly"] for a in data["contact_methods"]] == ["contact_missing"]
    assert data["unchecked"] == {}


async def test_a_contact_sub_collection_the_budget_could_not_read_is_unchecked():
    """The dangerous outcome is "this record has no email" when the truth is
    "nobody looked". One request pays for the sweep, so no sub-collection is
    reachable at all."""
    handler = sweep([row(1), row(2)])
    data = (
        await run(handler, {"anomalies": ["contact_methods"], "max_requests": 1})
    ).structured_content

    assert data["contact_methods"] == []
    assert data["unchecked"]["contact_methods"] == 2
    assert data["execution"]["truncated"] is True
    assert "emails" in data["execution"]["errors"]


# --- region variants: clusters and counts, no canonical spelling ------------


async def test_region_spellings_that_fold_together_cluster_with_their_counts():
    handler = sweep(
        [
            row(1, state="BC"),
            row(2, state="B.C."),
            row(3, state="b c"),
            row(4, state="BC"),
        ]
    )
    data = (await run(handler, {"anomalies": ["region_variants"]})).structured_content

    assert len(data["region_variants"]) == 1
    cluster = data["region_variants"][0]
    assert cluster["field"] == "state"
    assert cluster["normalized"] == "bc"
    counts = {v["value"]: v["count"] for v in cluster["variants"]}
    assert counts == {"BC": 2, "B.C.": 1, "b c": 1}


async def test_no_region_spelling_is_declared_the_correct_one():
    """Which form an account standardises on is the account's decision. A key
    naming one of them - canonical, correct, preferred, suggested - would be
    this adapter making it."""
    handler = sweep([row(1, state="BC"), row(2, state="B.C.")])
    data = (await run(handler, {"anomalies": ["region_variants"]})).structured_content

    cluster = data["region_variants"][0]
    assert set(cluster) == {"field", "normalized", "variants"}
    assert set(cluster["variants"][0]) == {"value", "normalized", "count"}
    text = findings_text(data)
    for banned in ("canonical", "correct", "preferred", "suggested", "should_be"):
        assert not mentions(text, banned), (
            f"{banned!r} names a right answer this tool cannot know"
        )


async def test_spellings_that_fold_apart_stay_apart_but_are_still_reported():
    """"British Columbia" and "BC" are two values here, because folding them
    would need a table of place names. The inventory is what lets a caller see
    both and decide for themselves."""
    handler = sweep([row(1, state="BC"), row(2, state="British Columbia")])
    data = (await run(handler, {"anomalies": ["region_variants"]})).structured_content

    assert data["region_variants"] == [], "a gazetteer has crept in"
    inventory = {v["value"]: v["count"] for v in data["region_values"][0]["values"]}
    assert inventory == {"BC": 1, "British Columbia": 1}


async def test_a_region_field_the_caller_names_is_swept_instead_of_a_built_in_list():
    handler = sweep([row(1, city="Kamloops"), row(2, city="kamloops")])
    data = (
        await run(handler, {"anomalies": ["region_variants"], "region_fields": ["city"]})
    ).structured_content

    assert data["region_variants"][0]["field"] == "city"
    assert data["region_variants"][0]["normalized"] == "kamloops"


# --- titles: the field, and only the field ----------------------------------


async def test_a_null_title_is_reported_as_missing():
    """The candidate projection always carries `title`, often as null. Null is
    an answer: no title is recorded."""
    handler = sweep([row(1, title=None)])
    data = (await run(handler, {"anomalies": ["titles"]})).structured_content

    assert len(data["titles"]) == 1
    finding = data["titles"][0]
    assert finding["anomaly"] == "title_missing"
    assert finding["field"] == "title"
    assert finding["value"] is None
    assert "null" in finding["detail"]
    assert data["unchecked"] == {}


async def test_an_absent_title_key_is_unchecked_rather_than_missing():
    """A present-but-null field is an answer; a missing key is not. Conflating
    the two is a bug this repository has already shipped once."""
    handler = sweep([row(1)])
    data = (await run(handler, {"anomalies": ["titles"]})).structured_content

    assert data["titles"] == [], "an absent key was reported as a recorded absence"
    assert data["unchecked"]["titles"] == 1
    assert data["counts"]["titles"] == 0


async def test_a_title_holding_no_letters_is_reported_as_unparseable():
    """The FIELD does not read as a title. Nothing here is about the work
    someone does."""
    handler = sweep([row(1, title="???"), row(2, title="   "), row(3, title="Welder")])
    data = (await run(handler, {"anomalies": ["titles"]})).structured_content

    by_id = {f["candidate_id"]: f for f in data["titles"]}
    assert by_id[1]["anomaly"] == "title_unparseable"
    assert by_id[2]["anomaly"] == "title_missing"
    assert 3 not in by_id, "a title that parses is not a finding"


# --- links and orphaned references ------------------------------------------


async def test_a_stored_url_that_is_not_well_formed_is_reported():
    handler = sweep(
        [
            row(1, linkedin_url="https://www.linkedin.com/in/pat-lee/"),
            row(2, linkedin_url="linkedin"),
            row(3, website="ftp://example.com/x"),
        ]
    )
    data = (await run(handler, {"anomalies": ["links"]})).structured_content

    by_id = {f["candidate_id"]: f for f in data["links"]}
    assert 1 not in by_id
    assert by_id[2]["anomaly"] == "url_unparseable"
    assert "dot" in by_id[2]["detail"]
    assert "http" in by_id[3]["detail"]


async def test_an_id_beside_an_empty_embedded_target_is_an_orphaned_reference():
    handler = sweep([row(1, owner_id=41, _embedded={"owner": None})])
    data = (await run(handler, {"anomalies": ["links"]})).structured_content

    finding = data["links"][0]
    assert finding["anomaly"] == "reference_empty"
    assert finding["field"] == "owner_id"
    assert "_embedded.owner" in finding["detail"]


# --- the stage check is the caller's definition, twice over -----------------


async def test_the_stage_check_refuses_to_guess_account_specific_ids():
    """A status id means nothing outside the account that issued it, and a stage
    title is not an id. Guessing either would apply one customer's workflow to
    every other customer on the same adapter."""
    handler = sweep([row(1)])
    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="terminal_status_ids"):
            await client.call_tool(
                "audit_candidate_data", {"anomalies": ["stage_conflicts"]}
            )
        with pytest.raises(Exception, match="active_field"):
            await client.call_tool(
                "audit_candidate_data",
                {"anomalies": ["stage_conflicts"], "terminal_status_ids": [9]},
            )


async def test_an_active_record_at_a_stage_the_caller_named_is_reported():
    def handler(request):
        path = request.url.path
        if path.endswith("/candidates"):
            return httpx2.Response(
                200, json=collection([row(1, is_active=True), row(2, is_active=False)])
            )
        if path.endswith("/pipelines"):
            return httpx2.Response(
                200,
                json=collection(
                    [{"id": 55, "job_id": 7, "status_id": 9}], key="pipelines"
                ),
            )
        return httpx2.Response(200, json={})

    data = (
        await run(
            handler,
            {
                "anomalies": ["stage_conflicts"],
                "terminal_status_ids": [9],
                "active_field": "is_active",
            },
        )
    ).structured_content

    assert len(data["stage_conflicts"]) == 1
    conflict = data["stage_conflicts"][0]
    assert conflict["candidate_id"] == 1
    assert conflict["active_field"] == "is_active"
    assert conflict["status_id"] == 9
    assert conflict["pipeline_id"] == 55
    assert conflict["job_id"] == 7


async def test_a_record_without_the_active_marker_is_unchecked():
    handler = sweep([row(1)])
    data = (
        await run(
            handler,
            {
                "anomalies": ["stage_conflicts"],
                "terminal_status_ids": [9],
                "active_field": "is_active",
            },
        )
    ).structured_content

    assert data["stage_conflicts"] == []
    assert data["unchecked"]["stage_conflicts"] == 1


# --- anomaly selection is the cost control ----------------------------------


async def test_selecting_only_free_anomalies_spends_no_per_record_requests():
    """`anomalies` is the knob a caller turns to decide what this costs. If a
    kind they did not ask for still fans out, the knob is decorative."""
    paths: list[str] = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/candidates"):
            return httpx2.Response(200, json=collection([row(1, title=None), row(2, title=None)]))
        return httpx2.Response(200, json={})

    data = (await run(handler, {"anomalies": ["titles"]})).structured_content

    assert paths == ["/v3/candidates"], f"work was done for anomalies nobody asked for: {paths}"
    assert data["execution"]["requests_used"] == 1
    assert data["anomalies_checked"] == ["titles"]
    assert set(data["counts"]) == {"titles"}, "counts report only the kinds that were checked"


async def test_asking_for_duplicates_does_fan_out_so_the_comparison_is_real():
    """The other half of the previous test: the saving is real because the work
    is real when it is asked for."""
    paths: list[str] = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/candidates"):
            return httpx2.Response(200, json=collection([row(1), row(2)]))
        return httpx2.Response(200, json=collection([], key="emails"))

    data = (await run(handler, {"anomalies": ["duplicates"]})).structured_content

    assert len(paths) == 5, f"expected the sweep plus two sub-collections each: {paths}"
    assert data["execution"]["requests_used"] == 5


async def test_an_empty_anomaly_list_looks_for_nothing():
    """`anomalies=[]` is a legitimate way to ask what a sweep alone costs.
    Collapsing it with None would bill a caller who asked for none."""
    handler = sweep([row(1)])
    data = (await run(handler, {"anomalies": []})).structured_content

    assert data["anomalies_checked"] == []
    assert data["counts"] == {}
    assert data["scanned"] == 1


async def test_an_unknown_anomaly_kind_is_refused_by_name():
    handler = sweep([row(1)])
    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="Unknown anomalies"):
            await client.call_tool("audit_candidate_data", {"anomalies": ["age"]})


# --- budget -----------------------------------------------------------------


async def test_the_scan_ceiling_bounds_the_pool_and_is_reported():
    rows = [row(i, title=None) for i in range(1, 6)]
    handler = sweep(rows)
    data = (
        await run(handler, {"anomalies": ["titles"], "max_records": 2})
    ).structured_content

    assert data["scanned"] == 2
    assert data["execution"]["truncated"] is True


async def test_the_request_budget_is_reported_alongside_the_rate_limit():
    handler = sweep([row(1, emails=[], phones=[])])
    data = (await run(handler, {"anomalies": ["titles"]})).structured_content

    assert data["execution"]["requests_used"] == 1
    assert data["execution"]["rate_limit"] == {"limit": None, "remaining": None}
    assert data["execution"]["next_cursor"] is None


async def test_the_seeds_used_say_what_universe_the_answer_covers():
    """"No duplicate exists" and "no duplicate exists among these rows" are
    different answers, and only `seeds_used` tells them apart."""
    seen: list[dict] = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json=collection([row(1, title=None)]))
        return httpx2.Response(200, json={})

    data = (
        await run(
            handler,
            {"anomalies": ["titles"], "seed_field": "city", "seed_values": ["Logan Lake"]},
        )
    ).structured_content

    assert seen == [{"field": "city", "filter": "exactly", "value": "Logan Lake"}], (
        "the CATS contains filter tokenises, so 'Logan Lake' must be asked for exactly "
        "or Williams Lake is audited too"
    )
    assert data["seeds_used"] == [{"field": "city", "value": "Logan Lake"}]


# --- the fact-versus-judgment boundary --------------------------------------


async def test_the_result_carries_no_verdict_shaped_key():
    """Shares its vocabulary with tests/test_boundary.py so the two cannot
    drift. This tool reports what is structurally wrong with a field; deciding
    what to do about it is the caller's."""

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates"):
            return httpx2.Response(
                200,
                json=collection(
                    [
                        row(1, title=None, is_active=True, linkedin_url="linkedin"),
                        row(2, title="???", state="B.C.", is_active=True),
                    ]
                ),
            )
        if path.endswith("/emails"):
            return httpx2.Response(
                200, json=collection([{"email": "pat@example.com"}], key="emails")
            )
        if path.endswith("/phones"):
            return httpx2.Response(200, json=collection([{"number": "bad"}], key="phones"))
        return httpx2.Response(
            200, json=collection([{"id": 55, "job_id": 7, "status_id": 9}], key="pipelines")
        )

    result = await run(
        handler,
        {
            "anomalies": sorted(audit.ANOMALY_OPTIONS),
            "terminal_status_ids": [9],
            "active_field": "is_active",
        },
    )

    hits = _find_banned_keys(result.structured_content)
    assert not hits, f"audit_candidate_data returned verdict-shaped keys: {hits}"


async def test_the_published_schema_declares_no_verdict_shaped_field():
    """The payload alone is not enough: a field named `score` that happens to be
    empty on this fixture never appears in a result, while every client reading
    the schema still sees it advertised."""
    async with Client(build(sweep([]))) as client:
        schema = next(
            t.output_schema
            for t in await client.list_tools()
            if t.name == "audit_candidate_data"
        )

    from test_boundary import BANNED_VERDICT_KEYS

    declared = _schema_key_names(schema)
    banned = sorted(k for k in declared if k.lower() in BANNED_VERDICT_KEYS)
    assert not banned, f"audit_candidate_data declares verdict-shaped fields: {banned}"


async def test_the_description_carries_no_recruiting_policy_vocabulary():
    """tests/test_boundary.py enforces this across the registered surface; this
    tool is checked here too so the boundary holds from the moment it exists
    rather than from the moment it is wired in."""
    async with Client(build(sweep([]))) as client:
        tools = {t.name: t for t in await client.list_tools()}

    text = (tools["audit_candidate_data"].description or "").lower()
    allowed = VERIFIED_SAFE_USAGE.get("audit_candidate_data", frozenset())
    hits = [word for word in TOOL_VOCAB if word in text and word not in allowed]
    assert not hits, f"audit_candidate_data's description contains policy vocabulary: {hits}"


async def test_nothing_in_the_result_characterises_a_person():
    """Issue #12 draws this line explicitly: no age, identity or other
    protected-characteristic judgement. The findings are about stored strings,
    so the vocabulary of describing people has no place in any of them."""
    handler = sweep([row(1, title=None, state="B.C."), row(2, title="???", state="BC")])
    data = (await run(handler, {"anomalies": ["titles", "region_variants"]})).structured_content

    text = findings_text(data)
    for banned in ("age", "gender", "ethnicity", "nationality", "disability", "senior", "junior"):
        assert not mentions(text, banned), (
            f"{banned!r} appears in a data-quality result: {text[:400]}"
        )


# --- the typed contract, and what the caller is shown of it ------------------

#: The top-level shape of AuditResult, spelled out rather than derived from the
#: model. Deriving it would make this test agree with any rename by
#: construction, which is the failure issue #17 is about.
EXPECTED_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "duplicates",
        "region_variants",
        "region_values",
        "contact_methods",
        "titles",
        "stage_conflicts",
        "links",
        "counts",
        "anomalies_checked",
        "unchecked",
        "scanned",
        "seeds_used",
        "execution",
        "note",
    }
)

#: A generous ceiling on the display line. The point is the order of magnitude:
#: a summary is tens of bytes and the payload it summarises is thousands, so a
#: regression that starts serialising the result into the display channel
#: overshoots this by a factor of ten, not by a word or two.
MAX_DISPLAY_BYTES = 200


async def test_the_result_publishes_an_output_schema():
    """Returning `ToolResult` alone publishes no output schema at all - the
    caller gets an untyped blob and FastMCP validates nothing, which is the
    state issue #17 exists to end."""
    async with Client(build(sweep([]))) as client:
        tools = {t.name: t for t in await client.list_tools()}

    schema = tools["audit_candidate_data"].output_schema
    assert schema is not None, "no output schema published: was output_schema= dropped?"
    assert schema["type"] == "object", (
        "the result must be an object at the root: a caller reads "
        "`execution.truncated` by name, not by position"
    )
    assert set(schema["properties"]) == EXPECTED_TOP_LEVEL

    execution = schema["properties"]["execution"]["properties"]
    assert {"requests_used", "rate_limit", "truncated", "errors"} <= set(execution)

    cluster = _schema_key_names(schema)
    assert {"normalized", "members", "stored_value", "variants"} <= cluster, (
        "the duplicate and variant evidence must survive into the published schema"
    )


async def test_the_display_content_summarises_rather_than_repeats_the_result():
    handler = sweep(
        [
            row(1, emails=["pat@example.com"], phones=[], title=None, state="B.C."),
            row(2, emails=["PAT@example.com"], phones=[], title="???", state="BC"),
        ]
    )
    result = await run(
        handler, {"anomalies": ["duplicates", "titles", "region_variants", "contact_methods"]}
    )

    assert len(result.content) == 1
    text = result.content[0].text
    assert len(text.encode()) <= MAX_DISPLAY_BYTES, f"display content is not a summary: {text!r}"

    payload = len(json.dumps(result.structured_content).encode())
    assert len(text.encode()) * 10 < payload, (
        f"display content is {len(text.encode())} bytes against a {payload}-byte "
        f"payload - close enough to suggest the result is being repeated into it"
    )
    for token in ("{", "[", "normalized", "members"):
        assert token not in text, f"the payload is leaking into the display line: {text!r}"


async def test_the_display_content_carries_no_personal_data():
    """Issue #21: the display channel is shown separately, cached and logged
    differently, and read by anyone with the transcript. This tool is handed
    names, addresses and numbers on every call and must put none of them there."""
    handler = sweep(
        [
            row(1, emails=["pat@example.com"], phones=["250-555-0111"], title=None),
            row(2, emails=["pat@example.com"], phones=["250 555 0111"], title=None),
        ]
    )
    result = await run(handler, {"anomalies": ["duplicates", "titles"]})

    text = result.content[0].text
    for secret in ("pat@example.com", "250-555-0111", "2505550111", "Pat", "Number1"):
        assert secret not in text, f"personal data in the display line: {text!r}"
    assert "scanned 2" in text and "duplicates" in text
