"""A 2xx is not proof the record changed.

CATS accepting a write means the request was accepted. Whether the record now
says what you intended is a separate question, and for most writes the
distinction is academic.

For a Do Not Contact list it is not. Believing somebody was added when they were
not means contacting a person who asked you to stop, and nothing in a 200
response would have said so. These writes read the list back and report
`verified`, so the caller is told rather than left to assume.
"""

from __future__ import annotations

import httpx2
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import Presence, Safety
from cats_mcp.server import create_server

LIST_ID = 1600001
ADDED = 400000002
ALSO_ADDED = 400000003
ROW_ID = 390000001


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


def items(rows):
    return {"count": len(rows), "total": len(rows), "_links": {}, "_embedded": {"items": rows}}


def row(row_id, candidate_id):
    return {"id": row_id, "candidate_id": candidate_id}


# --- the write landed -------------------------------------------------------


async def test_a_confirmed_addition_reports_verified():
    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True})
        return httpx2.Response(200, json=items([row(ROW_ID, ADDED)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED]},
        )

    assert result.data["verified"] is True
    assert result.data["confirmed"] == [str(ADDED)]


async def test_the_original_response_is_still_returned():
    """Verification adds to the result; it must not replace what CATS said."""

    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True, "added": 1})
        return httpx2.Response(200, json=items([row(ROW_ID, ADDED)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED]},
        )

    assert result.data["result"]["added"] == 1


# --- the write did not land -------------------------------------------------


async def test_an_accepted_write_that_did_not_persist_is_reported():
    """The whole reason this exists: 200 OK, nothing changed."""

    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True})
        return httpx2.Response(200, json=items([]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED]},
        )

    assert result.data["verified"] is False
    assert result.data["missing"] == [str(ADDED)]
    assert "accepted the write" in result.data["verification"]


async def test_a_partial_addition_names_exactly_who_is_missing():
    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True})
        return httpx2.Response(200, json=items([row(ROW_ID, ADDED)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED, ALSO_ADDED]},
        )

    assert result.data["verified"] is False
    assert result.data["confirmed"] == [str(ADDED)]
    assert result.data["missing"] == [str(ALSO_ADDED)]


# --- verification is checked against candidate_id, not the row id -----------


async def test_membership_is_confirmed_on_candidate_id_not_the_row_id():
    """A row's id is the row. Checking it would confirm the wrong thing.

    Here the list contains the candidate that was added, but under a row whose
    own id is a different number. Verifying on `id` would report the addition as
    failed; verifying on `candidate_id` reports the truth.
    """

    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True})
        return httpx2.Response(200, json=items([row(999999999, ADDED)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED]},
        )

    assert result.data["verified"] is True


# --- removals verify the opposite -------------------------------------------


async def test_a_confirmed_removal_reports_verified():
    def handler(request):
        if request.method == "DELETE":
            return httpx2.Response(204)
        return httpx2.Response(200, json=items([]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "delete_candidate_list_item",
            {"list_id": LIST_ID, "item_id": ROW_ID},
        )

    assert result.data["verified"] is True


async def test_a_removal_that_did_not_take_effect_is_reported():
    def handler(request):
        if request.method == "DELETE":
            return httpx2.Response(204)
        return httpx2.Response(200, json=items([row(ROW_ID, ADDED)]))

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "delete_candidate_list_item",
            {"list_id": LIST_ID, "item_id": ROW_ID},
        )

    assert result.data["verified"] is False
    assert result.data["still_present"] == [str(ROW_ID)]


# --- failing safely ---------------------------------------------------------


async def test_an_unreadable_verification_does_not_raise():
    """The write may well have succeeded.

    Turning a successful mutation into an exception invites a retry, which for
    an addition is harmless and for anything else is not.
    """

    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True})
        return httpx2.Response(403, json={"message": "no"})

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED]},
        )

    assert result.data["verified"] is False
    assert "could not be confirmed" in result.data["verification"]
    assert result.data["result"]["success"] is True


async def test_verification_sweeps_every_page():
    """A member on page two is still a member."""
    pages = {
        1: {
            "count": 1,
            "_links": {"next": {"href": "/x?page=2"}},
            "_embedded": {"items": [row(1, 111111111)]},
        },
        2: {"count": 1, "_links": {}, "_embedded": {"items": [row(ROW_ID, ADDED)]}},
    }

    def handler(request):
        if request.method == "POST":
            return httpx2.Response(200, json={"success": True})
        page = int(request.url.params.get("page", 1))
        return httpx2.Response(200, json=pages[page])

    async with Client(build(handler)) as client:
        result = await client.call_tool(
            "create_candidate_list_items",
            {"list_id": LIST_ID, "candidate_ids": [ADDED]},
        )

    assert result.data["verified"] is True


# --- registry integrity -----------------------------------------------------


def test_only_mutations_carry_verification():
    """Verifying a read would be a second request for no information."""
    for spec in REGISTRY:
        if spec.verification is not None:
            assert spec.safety is not Safety.READ, spec.name


def test_a_verification_endpoint_only_uses_placeholders_the_spec_supplies():
    """Otherwise the read fails at call time with a KeyError-shaped surprise."""
    import re

    for spec in REGISTRY:
        if spec.verification is None:
            continue
        needed = set(re.findall(r"\{(\w+)\}", spec.verification.endpoint))
        from cats_mcp.registry.models import ParamLocation

        supplied = {p.outbound_name for p in spec.params_at(ParamLocation.PATH)}
        assert needed <= supplied, f"{spec.name}: verification needs {needed - supplied}"


def test_a_verification_reads_a_field_the_caller_supplies():
    for spec in REGISTRY:
        if spec.verification is None:
            continue
        names = {p.name for p in spec.params}
        assert spec.verification.expect_from in names, (
            f"{spec.name}: expect_from={spec.verification.expect_from!r} is not a parameter"
        )


def test_the_do_not_contact_write_path_is_verified():
    """Named explicitly: this is the pair that motivated the mechanism."""
    add = REGISTRY.by_name("create_candidate_list_items")
    remove = REGISTRY.by_name("delete_candidate_list_item")

    assert add is not None and add.verification is not None
    assert add.verification.identity_field == "candidate_id", (
        "a membership row's id is the row, not the person"
    )
    assert add.verification.presence is Presence.PRESENT

    assert remove is not None and remove.verification is not None
    assert remove.verification.presence is Presence.ABSENT
