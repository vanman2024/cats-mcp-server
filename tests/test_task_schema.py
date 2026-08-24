"""The task write path, and the wire names it actually sends.

Issue #15: `create_task` failed against live CATS with two validation errors at
once - `assigned_to_id must be positive` and `priority must not be optional`.
Neither was a bad value. The assignee was sent under the wrong key, so the
field CATS wanted arrived empty; and `priority` was not in the spec at all, so
no caller could supply it even knowing it was required.

That failure mode is the reason these tests assert on the outgoing request body
rather than on the spec alone. A spec can name a parameter correctly and still
send it under the wrong key - `wire_name` is exactly the seam where the two
disagree, and the 400 that resulted read like a value problem rather than a
naming one. Checking the body is what closes the gap.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.registry.catalog import REGISTRY
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


def _param(tool_name: str, param_name: str):
    spec = REGISTRY.by_name(tool_name)
    for param in spec.params:
        if param.name == param_name:
            return param
    raise AssertionError(f"{tool_name} has no parameter {param_name!r}")


# --- the spec says the right thing ------------------------------------------


@pytest.mark.parametrize("tool_name", ["create_task", "update_task"])
def test_the_assignee_is_sent_as_assigned_to_id(tool_name):
    """CATS calls this field assigned_to_id. Sending assigned_to leaves the
    required field empty and produces 'must be positive' - an error about a
    value, for a field that was never transmitted."""
    assert _param(tool_name, "assigned_to").outbound_name == "assigned_to_id"


def test_create_task_exposes_priority():
    """CATS: 'priority must not be optional'. It was absent from the spec, so a
    caller could not satisfy that even deliberately."""
    priority = _param("create_task", "priority")
    assert priority.outbound_name == "priority"
    assert priority.default == 5, "the default should be the value the account already uses"


def test_the_assignee_is_required_on_create():
    """Optional would only defer the same 400 to runtime."""
    assert _param("create_task", "assigned_to").required


# --- and the request body actually carries it -------------------------------


async def test_create_task_sends_the_shape_cats_actually_accepts():
    """Every mapping below was established by creating real tasks against the
    live account and deleting them again. Each wrong one failed differently:

      due_date     -> accepted with 201, then silently dropped. No due date on
                      the record and nothing said so.
      candidate_id -> 500. A task attaches through data_item, and omitting the
                      association entirely is also a 500, so it is required.
      title        -> accepted, discarded, absent from the stored record. CATS
                      tasks have no title; description is the content.
    """
    sent: dict[str, object] = {}

    def handler(request):
        if request.url.path.endswith("/tasks") and request.method == "POST":
            sent.update(json.loads(request.content))
            return httpx2.Response(201, json={"id": 999})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        await client.call_tool(
            "create_task",
            {
                "description": "Follow up",
                "assigned_to": 595874,
                "candidate_id": 401138551,
                "due_date": "2026-09-01",
            },
        )

    assert sent.get("date_due") == "2026-09-01", f"due date must go as date_due: {sent}"
    assert "due_date" not in sent, "due_date is accepted and silently dropped by CATS"
    assert sent.get("data_item") == {"id": 401138551, "type": "candidate"}, sent
    assert "candidate_id" not in sent, "candidate_id as a scalar is a 500"
    assert "title" not in sent, "CATS tasks have no title field"


def test_description_and_candidate_are_required_on_create():
    """CATS answers 'description must not be empty' without one, and 500s
    without an association. Optional here would only defer both to runtime."""
    assert _param("create_task", "description").required
    assert _param("create_task", "candidate_id").required


async def test_create_task_sends_assigned_to_id_and_priority_on_the_wire():
    sent: dict[str, object] = {}

    def handler(request):
        if request.url.path.endswith("/tasks") and request.method == "POST":
            sent.update(json.loads(request.content))
            return httpx2.Response(201, json={"id": 999, "priority": 5})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        await client.call_tool(
            "create_task",
            {
                "description": "Follow up",
                "assigned_to": 595874,
                "candidate_id": 401138551,
            },
        )

    assert sent.get("assigned_to_id") == 595874, f"body was {sent}"
    assert "assigned_to" not in sent, "the tool-facing name must not reach CATS"
    assert sent.get("priority") == 5, "priority is required and must be sent"


async def test_update_task_also_sends_assigned_to_id():
    sent: dict[str, object] = {}

    def handler(request):
        if request.method == "PUT" and "/tasks/" in request.url.path:
            sent.update(json.loads(request.content))
            return httpx2.Response(200, json={"id": 8352717})
        return httpx2.Response(200, json={})

    async with Client(build(handler)) as client:
        await client.call_tool("update_task", {"task_id": 8352717, "assigned_to": 594157})

    assert sent.get("assigned_to_id") == 594157, f"body was {sent}"
    assert "assigned_to" not in sent
