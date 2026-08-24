"""Identity lookup, and the four things that make it trustworthy.

The question is "is this person already in CATS, and more than once". Getting
"does it return rows" right is not interesting; the failure modes that matter
are the ones that quietly answer the wrong question:

  * a match that CATS surfaced but the stored value does not actually support
  * a real duplicate missed because one record punctuates a phone differently
  * a criterion silently reported as "no match" when it was never checked -
    a search row carries no emails or phones at all
  * a budget stop that reads as "nothing else exists"

Every test below fails loudly if one of those regresses. The last group guards
the boundary itself: the result carries evidence, never a verdict, the tool's
own description stays out of the business of recruiting judgement, and the
display line the caller sees carries no contact detail.

The tool returns a `ToolResult`, so the payload arrives in two forms and both
are asserted on here. `result.structured_content` is the raw JSON object;
`result.data` is that object rebuilt against the published output schema, which
means attribute access rather than subscripting. Asserting on the dict is what
checks the wire contract - a key renamed in `LookupResult` changes it, and the
rebuilt object would hide that behind whatever the new attribute is called.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client, FastMCP
from test_boundary import TOOL_VOCAB, VERIFIED_SAFE_USAGE, _find_banned_keys

from cats_mcp.composites import lookup
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
    lookup.register(mcp, lambda: client, enforce_auth=False)
    return mcp


def collection(rows, key="candidates", *, has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {key: rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


def candidate(cid, *, first="Pat", last=None, emails=None, phones=None, **extra):
    """A candidate record. `emails=None` omits the key entirely, which is what a
    search row does and what forces the sub-collection read."""
    row = {
        "id": cid,
        "first_name": first,
        "last_name": f"Number{cid}" if last is None else last,
        "title": "Heavy Duty Mechanic",
        "city": "Kamloops",
        "state": "BC",
    }
    if emails is not None:
        row["emails"] = [{"email": e} for e in emails]
    if phones is not None:
        row["phones"] = [{"number": p} for p in phones]
    row.update(extra)
    return row


def probe_body(request):
    """What a probe asked for, whichever endpoint it used.

    Email probes go out as GET /candidates/search?query=, because CATS refuses
    `email` as a structured filter field - it is a sub-collection, not a column,
    and POST returns "Validation failed". Everything else is still a POST with a
    field/filter/value body. Reading only the body made these tests pass while
    email lookup returned nothing against a live account.
    """
    if request.method == "GET":
        return {"value": request.url.params.get("query"), "field": "email"}
    return json.loads(request.content)


# --- normalization is spelling, never identity ------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Pat@Example.com", "pat@example.com"),
        ("  PAT@EXAMPLE.COM  ", "pat@example.com"),
        (None, ""),
    ],
)
def test_email_normalisation_touches_only_case_and_whitespace(raw, expected):
    assert lookup._normalise_email(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("250-555-0111", "2505550111"),
        ("(250) 555 0111", "2505550111"),
        ("+1 250 555 0111", "2505550111"),
        ("250.555.0111", "2505550111"),
    ],
)
def test_phone_normalisation_ignores_punctuation_and_a_country_code(raw, expected):
    assert lookup._normalise_phone(raw) == expected


def test_normalisation_does_not_decide_that_two_values_mean_one_person():
    """Each of these is a real-world "same person" call that belongs to the
    caller, not here. If any of them ever passes, this tool has grown an
    identity heuristic instead of a normalizer.
    """
    # Gmail folds dots and +tags; most providers do not.
    assert lookup._normalise_email("pat.lee@gmail.com") != lookup._normalise_email(
        "patlee@gmail.com"
    )
    assert lookup._normalise_email("pat+jobs@x.com") != lookup._normalise_email("pat@x.com")
    # Reordering or dropping a token is a claim about who somebody is.
    assert lookup._normalise_text("Lee, Pat") != lookup._normalise_text("Pat Lee")
    assert lookup._normalise_text("Pat A Lee") != lookup._normalise_text("Pat Lee")


def test_url_normalisation_folds_scheme_www_and_trailing_slash_only():
    folded = lookup._normalise_url("https://www.linkedin.com/in/pat-lee/")
    assert folded == "linkedin.com/in/pat-lee"
    assert lookup._normalise_url("HTTP://LinkedIn.com/in/pat-lee") == folded
    # A query string is part of the address as stored, not decoration to drop.
    assert lookup._normalise_url("linkedin.com/in/pat-lee?trk=x") != folded


# --- the question the tool exists for ---------------------------------------


async def test_two_records_sharing_one_email_are_both_returned_with_the_evidence():
    """The duplicate case. Both records must come back, each saying that the
    email is what matched, with the value on the record quoted alongside."""

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(1), candidate(2)]))
        if path.endswith("/candidates/1"):
            return httpx2.Response(200, json=candidate(1, emails=["pat@example.com"]))
        return httpx2.Response(200, json=candidate(2, emails=["Pat@Example.COM"]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"emails": ["pat@example.com"]})

    data = result.structured_content
    assert [r["candidate_id"] for r in data["candidates"]] == [1, 2]
    for row in data["candidates"]:
        assert row["matched_fields"] == ["email"], row
        assert row["evidence"][0]["normalized"] == "pat@example.com"
    # The second record stores a differently-cased spelling of the same address.
    assert data["candidates"][1]["evidence"][0]["value"] == "Pat@Example.COM"
    # And the same answer, rebuilt from the published output schema.
    assert result.data.count == 2
    assert result.data.candidates[1].evidence[0].value == "Pat@Example.COM"


async def test_an_email_differing_only_in_case_still_matches():
    """CATS may or may not fold case on an exact filter, so the caller's
    spelling is asked for and the stored value is checked locally."""
    asked: list[str] = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            asked.append(probe_body(request)["value"])
            return httpx2.Response(200, json=collection([candidate(3)]))
        return httpx2.Response(200, json=candidate(3, emails=["pat@example.com"]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"emails": ["PAT@Example.com"]})

    data = result.structured_content
    assert asked == ["PAT@Example.com", "pat@example.com"], (
        "both the caller's spelling and its normalized form must be asked for, "
        "because CATS indexes the stored spelling"
    )
    row = data["candidates"][0]
    assert row["matched_fields"] == ["email"]
    assert row["evidence"][0]["matched"] == "PAT@Example.com"
    assert row["evidence"][0]["value"] == "pat@example.com"


async def test_phone_matching_ignores_formatting():
    """The duplicate this tool most often has to find: one record typed the
    number with dashes, the other with brackets and spaces."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(4)]))
        return httpx2.Response(200, json=candidate(4, phones=["(250) 555 0111"]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"phones": ["250-555-0111"]})

    row = result.structured_content["candidates"][0]
    assert row["matched_fields"] == ["phone"]
    assert row["evidence"][0]["value"] == "(250) 555 0111"
    assert row["evidence"][0]["normalized"] == "2505550111"


async def test_a_phone_the_record_omits_is_read_from_the_sub_collection():
    """CATS keeps phones as a sub-resource. A record that does not carry them
    inline leaves the criterion unanswered, and "unanswered" reported as "no
    match" is the wrong answer this leg exists to prevent."""
    paths: list[str] = []

    def handler(request):
        path = request.url.path
        paths.append(path)
        if path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(5)]))
        if path.endswith("/candidates/5/phones"):
            return httpx2.Response(
                200,
                json=collection([{"id": 90, "number": "+1 (250) 555-0111"}], key="phones"),
            )
        return httpx2.Response(200, json=candidate(5))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"phones": ["2505550111"]})

    data = result.structured_content
    assert "/v3/candidates/5/phones" in paths, f"sub-collection never read: {paths}"
    row = data["candidates"][0]
    assert row["matched_fields"] == ["phone"]
    assert row["evidence"][0]["source"] == "phones sub-collection"
    assert row["phones"] == ["+1 (250) 555-0111"]


async def test_every_field_that_matched_is_reported_not_just_the_one_searched():
    """A record found by email is still checked against the phone that was
    asked for. Two matching identities is a materially different fact from one,
    and reporting only the field that surfaced the row would hide it."""

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            value = probe_body(request)["value"]
            if "@" in value:
                return httpx2.Response(200, json=collection([candidate(6)]))
            return httpx2.Response(200, json=collection([]))
        return httpx2.Response(
            200,
            json=candidate(6, emails=["pat@example.com"], phones=["(250) 555 0111"]),
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "lookup_candidate",
            {"emails": ["pat@example.com"], "phones": ["250-555-0111"]},
        )

    row = result.structured_content["candidates"][0]
    assert sorted(row["matched_fields"]) == ["email", "phone"]
    assert [f["field"] for f in row["found_by"]] == ["email"], (
        "found_by records what surfaced the row; matched_fields records what held up"
    )


async def test_a_name_probes_the_last_token_and_confirms_the_whole_name():
    """The single-condition search cannot AND a first name to a last one, so
    the last name is what CATS is asked for - and everyone else called Lee must
    then fail the local check rather than be returned as a match."""
    probes: list[dict] = []

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            probes.append(probe_body(request))
            return httpx2.Response(
                200,
                json=collection(
                    [
                        candidate(1, first="Pat", last="Lee"),
                        candidate(2, first="Chris", last="Lee"),
                    ]
                ),
            )
        cid = int(path.rsplit("/", 1)[-1])
        first = "Pat" if cid == 1 else "Chris"
        return httpx2.Response(200, json=candidate(cid, first=first, last="Lee"))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"names": ["Pat Lee"]})

    data = result.structured_content
    assert probes == [{"field": "last_name", "filter": "exactly", "value": "lee"}]
    assert [r["candidate_id"] for r in data["candidates"]] == [1]
    assert data["candidates"][0]["matched_fields"] == ["name"]
    assert [u["candidate_id"] for u in data["unconfirmed"]] == [2]


async def test_a_row_cats_surfaced_whose_value_does_not_match_is_not_a_match():
    """`contains` tokenises and `exactly` is only as exact as the stored
    spelling, so CATS decides what to surface and this decides what counts.
    Reporting an unsupported row as a duplicate is the failure that matters."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(7)]))
        return httpx2.Response(200, json=candidate(7, emails=["someone.else@example.com"]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"emails": ["pat@example.com"]})

    data = result.structured_content
    assert data["candidates"] == []
    assert data["count"] == 0
    assert data["unconfirmed"][0]["candidate_id"] == 7
    assert "normalized" in data["unconfirmed"][0]["reason"]


async def test_an_id_the_caller_already_has_is_read_directly():
    def handler(request):
        assert not request.url.path.endswith("/search"), "an id needs no search"
        return httpx2.Response(200, json=candidate(8, emails=["pat@example.com"]))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"candidate_ids": [8]})

    row = result.structured_content["candidates"][0]
    assert row["matched_fields"] == ["candidate_id"]
    assert result.structured_content["execution"]["requests_used"] == 1


# --- budget -----------------------------------------------------------------


async def test_the_request_budget_is_respected_and_reported():
    """The dangerous failure is a lookup that stopped early and reads as
    complete. Three identities are asked about, two requests are allowed."""
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([]))
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "lookup_candidate",
            {
                "emails": ["a@example.com", "b@example.com", "c@example.com"],
                "max_requests": 2,
            },
        )

    execution = result.structured_content["execution"]
    assert len(calls) == 2, f"spent more than the budget allowed: {calls}"
    assert execution["requests_used"] == 2
    assert execution["truncated"] is True
    assert "probes" in execution["errors"]
    assert execution["rate_limit"] == {"limit": None, "remaining": None}


async def test_an_overflowing_probe_says_so_rather_than_reporting_a_whole_answer():
    """A last name shared by hundreds overflows one page. Silently examining
    the first hundred and returning them would answer a different question."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(
                200, json=collection([candidate(1, last="Lee")], has_next=True)
            )
        return httpx2.Response(200, json=candidate(1, first="Pat", last="Lee"))

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"names": ["Pat Lee"]})

    execution = result.structured_content["execution"]
    assert execution["truncated"] is True
    assert any("first page" in message for message in execution["errors"].values())


async def test_a_failed_probe_is_reported_rather_than_read_as_no_match():
    """If CATS will not filter on a field, "nobody has this email" is a wrong
    answer. The error is surfaced, and the field can be redirected."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(400, json={"message": "invalid field"})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"emails": ["pat@example.com"]})

    data = result.structured_content
    assert data["candidates"] == []
    assert data["execution"]["errors"], "a rejected probe must not look like an empty result"
    assert data["probes"][0]["rows"] is None, (
        "a probe CATS refused must report null rows, not zero - zero is an answer"
    )


async def test_a_probe_field_can_be_redirected_without_changing_the_matching():
    sent: list[str] = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            sent.append(probe_body(request)["field"])
            return httpx2.Response(200, json=collection([candidate(9)]))
        return httpx2.Response(200, json=candidate(9, emails=["pat@example.com"]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "lookup_candidate",
            {
                "emails": ["pat@example.com"],
                "probe_field_overrides": {"email": "email_address"},
            },
        )

    assert sent == ["email_address"]
    data = result.structured_content
    assert data["candidates"][0]["matched_fields"] == ["email"]
    assert data["probes"][0]["for"] == "email", (
        "the probe report says which identity kind it was looking for, whatever "
        "CATS field the override pointed it at"
    )


async def test_an_identity_is_required():
    """Without one this would sweep the whole account."""

    def handler(request):
        return httpx2.Response(200, json=collection([]))

    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="identity is required"):
            await client.call_tool("lookup_candidate", {})


# --- the fact-versus-judgment boundary --------------------------------------


async def test_the_result_carries_no_verdict_shaped_key():
    """Shares its vocabulary with tests/test_boundary.py so the two cannot
    drift. This tool reports which fields matched; deciding which record is the
    one to keep is the caller's, and a key like `score` or `recommendation`
    would be this server making that call instead."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(1), candidate(2)]))
        return httpx2.Response(
            200, json=candidate(1, emails=["pat@example.com"], phones=["250-555-0111"])
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "lookup_candidate",
            {
                "emails": ["pat@example.com"],
                "phones": ["250-555-0111"],
                "names": ["Pat Number1"],
                "profile_urls": ["https://www.linkedin.com/in/pat-lee/"],
                "candidate_ids": [3],
            },
        )

    # The raw object, deliberately: result.data is rebuilt into a typed object
    # whose attributes _find_banned_keys cannot walk, so checking it would pass
    # by looking at nothing. The wire payload is what a caller actually reads.
    hits = _find_banned_keys(result.structured_content)
    assert not hits, f"lookup_candidate returned verdict-shaped keys: {hits}"


async def test_the_description_carries_no_recruiting_policy_vocabulary():
    """tests/test_boundary.py enforces this across the registered surface; this
    tool is checked here too so the boundary holds from the moment it exists
    rather than from the moment it is wired in."""

    def handler(request):
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    text = (tools["lookup_candidate"].description or "").lower()
    allowed = VERIFIED_SAFE_USAGE.get("lookup_candidate", frozenset())
    hits = [word for word in TOOL_VOCAB if word in text and word not in allowed]
    assert not hits, f"lookup_candidate's description contains policy vocabulary: {hits}"


# --- the typed contract, and what the caller is shown of it ------------------

#: The top-level shape of LookupResult, spelled out here rather than derived
#: from the model. Deriving it would make this test agree with any rename by
#: construction, which is the failure issue #17 is about.
EXPECTED_TOP_LEVEL: frozenset[str] = frozenset(
    {"candidates", "count", "unconfirmed", "asked_for", "probes", "execution", "note"}
)

#: A generous ceiling on the display line. The point is the order of magnitude:
#: a summary is tens of bytes and the payload it summarises is thousands, so a
#: regression that starts serialising the result into the display channel
#: overshoots this by a factor of ten, not by a word or two.
MAX_DISPLAY_BYTES = 120


async def test_the_result_publishes_an_output_schema():
    """Returning `ToolResult` alone publishes no output schema at all - the
    caller gets back an untyped blob and FastMCP validates nothing, which is
    the state issue #17 exists to end. The schema is passed explicitly for
    exactly that reason, so its absence has to fail loudly here."""

    def handler(request):
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        tools = {t.name: t for t in await client.list_tools()}

    schema = tools["lookup_candidate"].output_schema
    assert schema is not None, "no output schema published: was output_schema= dropped?"
    assert schema["type"] == "object", (
        "the result must be an object at the root, not a wrapped scalar: a caller "
        "reads `execution.truncated` by name, not by position"
    )
    assert set(schema["properties"]) == EXPECTED_TOP_LEVEL

    execution = schema["properties"]["execution"]["properties"]
    assert {"requests_used", "rate_limit", "truncated", "errors"} <= set(execution), (
        "the shared ExecutionFacts fields must survive into the published schema; "
        "without them a caller cannot tell a complete answer from a stopped one"
    )
    row = schema["properties"]["candidates"]["items"]["properties"]
    assert {"candidate_id", "matched_fields", "evidence", "found_by"} <= set(row)


async def test_the_display_content_summarises_rather_than_repeats_the_result():
    """The failure this guards is the one that made #17 worth filing: returning
    the model directly gets the whole payload serialised into the display
    channel as well, so every caller pays for it twice and a human reading the
    transcript sees a wall of JSON instead of an answer."""

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(1), candidate(2)]))
        return httpx2.Response(
            200, json=candidate(1, emails=["pat@example.com"], phones=["250-555-0111"])
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("lookup_candidate", {"emails": ["pat@example.com"]})

    assert len(result.content) == 1
    text = result.content[0].text
    assert len(text.encode()) <= MAX_DISPLAY_BYTES, f"display content is not a summary: {text!r}"

    payload = len(json.dumps(result.structured_content).encode())
    assert len(text.encode()) * 10 < payload, (
        f"display content is {len(text.encode())} bytes against a {payload}-byte "
        f"payload - close enough to suggest the result is being repeated into it"
    )
    for token in ("matched_fields", "evidence", "candidate_id", "{", "["):
        assert token not in text, f"the payload is leaking into the display line: {text!r}"


async def test_the_display_content_carries_no_contact_detail():
    """Issue #21: the display channel is shown separately from the structured
    result, cached and logged differently, and read by anyone with the
    transcript. A name, an address or a number in it is a disclosure the caller
    never asked for - and this tool is handed all three on every call."""

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(1, last="Lee")]))
        return httpx2.Response(
            200,
            json=candidate(
                1,
                first="Pat",
                last="Lee",
                emails=["pat@example.com"],
                phones=["250-555-0111"],
                linkedin_url="https://www.linkedin.com/in/pat-lee/",
            ),
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "lookup_candidate",
            {
                "emails": ["pat@example.com"],
                "phones": ["250-555-0111"],
                "names": ["Pat Lee"],
                "profile_urls": ["https://www.linkedin.com/in/pat-lee/"],
            },
        )

    text = result.content[0].text.lower()
    leaked = [
        secret
        for secret in (
            "pat",
            "lee",
            "example.com",
            "250-555-0111",
            "2505550111",
            "555",
            "linkedin",
            "kamloops",
        )
        if secret in text
    ]
    assert not leaked, f"display content carries contact detail {leaked}: {text!r}"
    # It still has to say something useful, or it is not a summary either.
    assert "1 confirmed" in text and "requests" in text
