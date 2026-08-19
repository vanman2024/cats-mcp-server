"""The compound candidate query, and the four properties that make it usable.

Issue #11's reproduction is the shape under test: a province whose value is
stored three different ways, an occupational vocabulary the account does not
know is one job, and a contact requirement that cannot be answered from a
search row. The failure modes worth guarding are not "does it return rows" but:

  * the same person found under two spellings is one row, not two
  * a caller's predicate narrows locally rather than through CATS tokenisation
  * per-candidate requests are spent only on survivors, never on the raw sweep
  * a budget stop is visible as a cursor, not silently reported as "no more"

Each test below fails loudly if one of those regresses.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.composites.query import TextPredicate, _evaluate, _normalise
from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.server import create_server


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def collection(rows, *, has_next=False):
    payload = {"count": len(rows), "total": len(rows), "_embedded": {"candidates": rows}}
    if has_next:
        payload["_links"] = {"next": {"href": "?page=2"}}
    return payload


def candidate(cid, title, *, city="Kamloops", state="BC", emails=(), phones=()):
    return {
        "id": cid,
        "first_name": "Pat",
        "last_name": f"Number{cid}",
        "title": title,
        "city": city,
        "state": state,
        "current_employer": "Acme Equipment",
        "emails": list(emails),
        "phones": list(phones),
    }


# --- normalisation is normalisation, not classification ---------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Heavy-Duty Mechanic", "heavy duty mechanic"),
        ("  HD   Tech  ", "hd tech"),
        ("B.C.", "b c"),
        (None, ""),
    ],
)
def test_normalise_only_touches_case_punctuation_and_whitespace(raw, expected):
    assert _normalise(raw) == expected


def test_predicate_does_not_invent_synonyms():
    """'Millwright' is the same job to a recruiter and a different string here.

    If this ever passes, the adapter has grown an occupational taxonomy - the
    exact thing issue #11 says belongs to the caller.
    """
    predicate = TextPredicate(values=["heavy equipment"])
    satisfied, _ = _evaluate("Millwright", predicate)
    assert not satisfied


def test_predicate_match_all_requires_every_value():
    predicate = TextPredicate(values=["heavy", "mechanic"], match="all")
    assert _evaluate("Heavy Duty Mechanic", predicate)[0]
    assert not _evaluate("Heavy Duty Operator", predicate)[0]


def test_contains_is_substring_not_cats_tokenisation():
    """The bug that motivated the tool: contains='Logan Lake' must not match
    Williams Lake. CATS tokenises; this does not."""
    predicate = TextPredicate(values=["logan lake"])
    assert not _evaluate("Williams Lake", predicate)[0]
    assert _evaluate("Logan Lake Depot", predicate)[0]


# --- phase A: the sweep -----------------------------------------------------


async def test_one_person_under_two_province_spellings_is_one_row():
    """'BC' and 'British Columbia' are different stored values naming the same
    province. Candidate 1 is filed under both; returning them twice would
    inflate every count the caller reads."""
    seen: list[str] = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            import json

            value = json.loads(request.content)["value"]
            seen.append(value)
            if value == "BC":
                return httpx2.Response(200, json=collection([candidate(1, "HD Mechanic")]))
            return httpx2.Response(
                200,
                json=collection([candidate(1, "HD Mechanic"), candidate(2, "Driller")]),
            )
        return httpx2.Response(200, json=candidate(1, "HD Mechanic"))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC", "British Columbia"], "include": ["identity"]},
        )

    data = result.data
    assert seen == ["BC", "British Columbia"], "each value gets its own exact filter"
    assert data["scanned"] == 3
    assert data["deduplicated"] == 1
    assert {row["candidate_id"] for row in data["candidates"]} == {1, 2}

    both = next(r for r in data["candidates"] if r["candidate_id"] == 1)
    values = {m["value"] for m in both["matched"]}
    assert values == {"BC", "British Columbia"}, "evidence keeps both spellings"


async def test_a_seed_is_required():
    """Without one this would sweep the whole account, which is the cost the
    request budget exists to prevent."""

    def handler(request):
        return httpx2.Response(200, json=collection([]))

    async with Client(build(handler)) as client:
        with pytest.raises(Exception, match="seed is required"):
            await client.call_tool("query_candidate_facts", {})


# --- phase B: narrowing before spending -------------------------------------


async def test_title_predicate_narrows_and_records_evidence():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(
                200,
                json=collection(
                    [
                        candidate(1, "Heavy Equipment Technician"),
                        candidate(2, "Mobile Equipment Technician"),
                        candidate(3, "Payroll Administrator"),
                    ]
                ),
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {
                "states": ["BC"],
                "title": {"values": ["equipment technician"], "mode": "contains"},
                "include": [],
            },
        )

    data = result.data
    assert {r["candidate_id"] for r in data["candidates"]} == {1, 2}
    assert data["dropped_by"]["title"] == 1
    assert any(m["field"] == "title" for m in data["candidates"][0]["matched"])


async def test_per_candidate_requests_are_spent_only_on_survivors():
    """The whole cost argument. Three people are seeded, one survives the
    title predicate, so exactly one profile read may happen - not three."""
    profile_reads: list[str] = []

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            return httpx2.Response(
                200,
                json=collection(
                    [
                        candidate(1, "Heavy Equipment Technician"),
                        candidate(2, "Payroll Administrator"),
                        candidate(3, "Receptionist"),
                    ]
                ),
            )
        profile_reads.append(path)
        return httpx2.Response(200, json=candidate(1, "Heavy Equipment Technician"))

    async with Client(build(handler)) as client:
        await client.call_tool(
            "query_candidate_facts",
            {
                "states": ["BC"],
                "title": {"values": ["heavy equipment"]},
                "include": ["identity"],
            },
        )

    assert len(profile_reads) == 1, f"spent reads on non-survivors: {profile_reads}"


async def test_contact_requirement_filters_on_the_enriched_record():
    """A search row does not carry emails or phones, so require_phone has to be
    answered after enrichment. Getting this wrong drops everyone."""

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            return httpx2.Response(
                200, json=collection([candidate(1, "HD Tech"), candidate(2, "HD Tech")])
            )
        if path.endswith("/candidates/1"):
            return httpx2.Response(
                200, json=candidate(1, "HD Tech", phones=[{"number": "250-555-0111"}])
            )
        return httpx2.Response(200, json=candidate(2, "HD Tech"))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "require_phone": True, "include": ["identity"]},
        )

    data = result.data
    assert [r["candidate_id"] for r in data["candidates"]] == [1]
    assert data["dropped_by"]["contact_requirements"] == 1
    assert data["candidates"][0]["contact_methods"]["has_phone"] is True


async def test_excluded_list_membership_drops_without_a_per_person_request():
    """Membership costs one pass per list. Candidate 2 is on the Do Not Contact
    list and must not appear; no candidate endpoint may be touched to learn it."""
    per_person: list[str] = []

    def handler(request):
        path = request.url.path
        if path.endswith("/candidates/search"):
            return httpx2.Response(
                200, json=collection([candidate(1, "HD Tech"), candidate(2, "HD Tech")])
            )
        if path.endswith("/candidates/lists/77"):
            return httpx2.Response(200, json={"id": 77, "name": "Do Not Contact"})
        if path.endswith("/candidates/lists/77/items"):
            return httpx2.Response(
                200, json={"_embedded": {"items": [{"id": 9, "candidate_id": 2}]}}
            )
        per_person.append(path)
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "exclude_list_ids": [77], "include": []},
        )

    data = result.data
    assert [r["candidate_id"] for r in data["candidates"]] == [1]
    assert data["dropped_by"]["exclude_list_ids"] == 1
    assert not per_person, f"membership should not cost a per-person read: {per_person}"


# --- budget -----------------------------------------------------------------


async def test_budget_stop_is_reported_as_a_cursor_not_as_exhaustion():
    """The dangerous failure is a truncated sweep that looks complete. When the
    budget runs out the caller must be able to tell, and to resume."""
    pages = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            pages["n"] += 1
            return httpx2.Response(
                200, json=collection([candidate(pages["n"], "HD Tech")], has_next=True)
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "max_requests": 3, "include": []},
        )

    data = result.data
    assert data["requests_used"] == 3
    assert data["truncated"] is True
    assert data["next_cursor"], "a budget stop must hand back a way to continue"


async def test_a_returned_cursor_resumes_rather_than_restarting():
    requested_pages: list[str] = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            requested_pages.append(request.url.params.get("page"))
            return httpx2.Response(
                200, json=collection([candidate(1, "HD Tech")], has_next=True)
            )
        return httpx2.Response(200, json={})

    server = build(handler)
    async with Client(server) as client:
        first = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "max_requests": 2, "include": []},
        )
        requested_pages.clear()
        await client.call_tool(
            "query_candidate_facts",
            {
                "states": ["BC"],
                "max_requests": 2,
                "include": [],
                "cursor": first.data["next_cursor"],
            },
        )

    assert requested_pages[0] == "3", f"resumed at the wrong page: {requested_pages}"


async def test_an_unreadable_cursor_restarts_instead_of_failing():
    """A truncated round trip is far likelier than an attack, and stranding the
    caller with no way to continue is the worse outcome."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(200, json=collection([candidate(1, "HD Tech")]))
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "cursor": "not-a-real-cursor", "include": []},
        )

    assert result.data["count"] == 1


# --- a null field is an answer; a missing field is not ----------------------
#
# Both of these came out of the first live run against a real CATS account.
# The search projection always carries `title`, and 549 of 800 BC candidates
# carried it as null - they genuinely have no title recorded. Treating that as
# "could not evaluate" kept all 549 in a search for heavy-equipment mechanics.


async def test_a_null_field_is_not_a_match():
    """title=None means the record has no title, which answers the predicate."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx2.Response(
                200,
                json=collection(
                    [
                        candidate(1, "Heavy Equipment Mechanic"),
                        candidate(2, None),
                    ]
                ),
            )
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "title": {"values": ["heavy equipment"]}, "include": []},
        )

    data = result.data
    assert [r["candidate_id"] for r in data["candidates"]] == [1]
    assert data["dropped_by"]["title"] == 1
    assert "unevaluated:title" not in data["errors"], (
        "a present-but-null field is answerable, not a projection gap"
    )


async def test_a_missing_field_is_kept_and_reported():
    """If the projection omits the key entirely the predicate is unanswerable,
    and silently dropping everyone would be the worse failure."""

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            rows = [candidate(1, "HD Tech"), candidate(2, "HD Tech")]
            for row in rows:
                del row["title"]
            return httpx2.Response(200, json=collection(rows))
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "query_candidate_facts",
            {"states": ["BC"], "title": {"values": ["heavy equipment"]}, "include": []},
        )

    data = result.data
    assert data["count"] == 2, "a projection gap must not empty the result"
    assert data["candidates"][0]["unevaluated"] == ["title"]
    assert "unevaluated:title" in data["errors"]
