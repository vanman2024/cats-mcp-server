"""Regressions for defects found by running the write path against a live account.

None of these could have been found by a test. Every one is CATS accepting a
request and doing something other than what it was asked, which no mock would
have reproduced because the mock was written from the same wrong assumption as
the code.

1. Sub-collection rows were projected through their *parent's* summary fields.
   `list_candidate_emails` on a candidate with two emails returned two ids and
   no addresses. 35 tools were affected.
2. `create_candidate` and `update_candidate` accepted `email` and `phone` and
   silently discarded them. CATS stores both as sub-resources.
3. A create answers with an empty 201, so the new record's id was thrown away
   with the Location header.
"""

from __future__ import annotations

import httpx2
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import ResponseStrategy
from cats_mcp.responses.shaping import SUMMARY_FIELDS, row_resource
from cats_mcp.server import create_server

CANDIDATE_ID = 400000001


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def collection(key, rows):
    return {"count": len(rows), "total": len(rows), "_links": {}, "_embedded": {key: rows}}


# --- 1. rows keep their own fields ------------------------------------------


async def test_an_email_row_returns_the_email_address():
    """The observed failure: two emails, two ids, no addresses."""

    def handler(request):
        return httpx2.Response(
            200,
            json=collection(
                "emails",
                [
                    {"id": 273478985, "email": "someone@example.com", "type": "personal"},
                    {"id": 273478988, "email": "other@example.com", "type": "work"},
                ],
            ),
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "list_candidate_emails", {"candidate_id": CANDIDATE_ID}
        )

    addresses = [row.get("email") for row in result.data["items"]]
    assert addresses == ["someone@example.com", "other@example.com"], (
        f"the address was dropped: {result.data['items']}"
    )


async def test_a_phone_row_returns_the_number():
    def handler(request):
        return httpx2.Response(
            200, json=collection("phones", [{"id": 1, "phone": "250-555-0142"}])
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "list_candidate_phones", {"candidate_id": CANDIDATE_ID}
        )

    assert result.data["items"][0]["phone"] == "250-555-0142"


async def test_an_attachment_row_returns_its_filename():
    """It was being projected through the candidate fields, so only `id` survived."""

    def handler(request):
        return httpx2.Response(
            200,
            json=collection(
                "attachments",
                [{"id": 744768441, "filename": "resume.pdf", "is_resume": True}],
            ),
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "list_candidate_attachments", {"candidate_id": CANDIDATE_ID}
        )

    row = result.data["items"][0]
    assert row["filename"] == "resume.pdf"
    assert row["is_resume"] is True


async def test_a_work_history_row_returns_the_employer():
    def handler(request):
        return httpx2.Response(
            200,
            json=collection(
                "work_history",
                [{"id": 5, "company_name": "Acme Mining", "title": "Electrician"}],
            ),
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "list_candidate_work_history", {"candidate_id": CANDIDATE_ID}
        )

    assert result.data["items"][0]["company_name"] == "Acme Mining"


def test_no_tool_projects_rows_through_a_mismatched_resource():
    """The general rule, so this cannot reappear on a tool nobody thought about.

    A projection is only applied when it belongs to the rows. Where no
    projection fits, the whole row is returned - verbose, and strictly better
    than dropping the field the caller asked for.
    """
    wrong = []
    for spec in REGISTRY:
        if spec.response is not ResponseStrategy.SUMMARY:
            continue
        rows = row_resource(spec)
        if rows is None:
            continue  # returns the whole row
        if rows != spec.resource and rows not in SUMMARY_FIELDS:
            wrong.append(f"{spec.name}: rows={rows} has no projection")
    assert not wrong, wrong


def test_sub_collections_no_longer_borrow_the_parent_projection():
    """Named cases, because these are the ones that were observably broken."""
    for name, must_not_be in (
        ("list_candidate_emails", "candidate"),
        ("list_candidate_phones", "candidate"),
        ("list_candidate_attachments", "candidate"),
        ("list_candidate_work_history", "candidate"),
        ("list_contact_emails", "contact"),
        ("list_company_phones", "company"),
    ):
        spec = REGISTRY.by_name(name)
        assert spec is not None, name
        assert row_resource(spec) != must_not_be, f"{name} still projects as {must_not_be}"


# --- 2. contact fields are not silently accepted ----------------------------


def test_create_candidate_no_longer_accepts_a_field_it_discards():
    """CATS took the email, returned 201, and stored nothing."""
    spec = REGISTRY.by_name("create_candidate")
    assert spec is not None
    names = {p.name for p in spec.params}
    assert "email" not in names, "email is discarded by CATS; do not advertise it"
    assert "phone" not in names, "phone is discarded by CATS; do not advertise it"


def test_update_candidate_no_longer_accepts_a_field_it_discards():
    spec = REGISTRY.by_name("update_candidate")
    assert spec is not None
    names = {p.name for p in spec.params}
    assert "email" not in names
    assert "phone" not in names


def test_the_descriptions_point_at_the_tools_that_do_work():
    for name in ("create_candidate", "update_candidate"):
        spec = REGISTRY.by_name(name)
        assert spec is not None
        assert "candidate_email" in spec.description, name
        assert "candidate_phone" in spec.description, name


def test_the_sub_resource_writers_still_exist():
    """Removing the flat fields is only safe because these are the real path."""
    for name in (
        "create_candidate_email",
        "create_candidate_phone",
        "update_candidate_email",
        "update_candidate_phone",
    ):
        assert REGISTRY.by_name(name) is not None, name


# --- 3. a create returns something you can act on ---------------------------


async def test_a_created_records_id_is_recovered_from_the_location_header():
    def handler(request):
        return httpx2.Response(
            201, headers={"Location": "/v3/candidates/413428437"}, content=b""
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate", {"first_name": "Dana", "last_name": "Example"}
        )

    assert result.data["created_id"] == 413428437
    assert result.data["location"] == "/v3/candidates/413428437"


async def test_an_absent_location_header_is_not_an_error():
    """CATS may simply not send one. That is a missing convenience, not a failure."""

    def handler(request):
        return httpx2.Response(201, content=b"")

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate", {"first_name": "Dana", "last_name": "Example"}
        )

    assert result.data["status"] == "success"
    assert "created_id" not in result.data


async def test_a_location_without_a_numeric_tail_is_kept_but_not_parsed():
    def handler(request):
        return httpx2.Response(
            201, headers={"Location": "/v3/candidates/pending"}, content=b""
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate", {"first_name": "Dana", "last_name": "Example"}
        )

    assert result.data["location"] == "/v3/candidates/pending"
    assert "created_id" not in result.data
