"""The migration safety net.

`tests/fixtures/tool_schema_baseline.json` is a snapshot of every tool the
pre-refactor server registered, captured under fastmcp 4.0.0b2. These tests
assert the declarative registry reproduces it exactly.

If a change here is intentional, regenerate the baseline deliberately and say so
in the commit - do not loosen the assertions.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastmcp import FastMCP

from cats_mcp.registry.build import register_all
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import ParamLocation, Safety

BASELINE_PATH = pathlib.Path(__file__).parent / "fixtures" / "tool_schema_baseline.json"

#: Removed deliberately: both call endpoints that do not exist in CATS v3 and
#: fail at runtime today. See docs/architecture/00-audit-gap-report.md 11a.
INTENTIONALLY_REMOVED = {
    "get_me": "GET /users/current returns 404; use get_site",
    "authorize_user": "POST /authorization does not exist",
}

#: Added deliberately: documented CATS v3 endpoints that were never implemented.
#: Pre-existing coverage gaps, not migration artifacts. See gap report 11b.
#:
#: Listing them by name rather than relaxing the assertion is the point: a tool
#: appearing here that nobody added on purpose is exactly what this test is for.
INTENTIONALLY_ADDED = {
    "list_candidate_applications": "GET /candidates/{id}/applications",
    "list_candidate_tasks": "GET /candidates/{id}/tasks",
    "list_candidate_custom_field_definitions": "GET /candidates/custom_fields",
    "get_candidate_custom_field_definition": "GET /candidates/custom_fields/{id}",
    "get_candidate_email": "GET /candidates/{id}/emails/{id}",
    "get_candidate_phone": "GET /candidates/{id}/phones/{id}",
    "list_job_custom_field_definitions": "GET /jobs/custom_fields",
    "get_job_custom_field_definition": "GET /jobs/custom_fields/{id}",
    "upload_job_attachment": "POST /jobs/{id}/attachments",
    "replace_job_tags": "POST /jobs/{id}/tags",
    "list_contact_tasks": "GET /contacts/{id}/tasks",
    "update_contact_custom_field": "PUT /contacts/{id}/custom_fields/{id}",
    "update_company_custom_field": "PUT /companies/{id}/custom_fields/{id}",
}


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def expected(baseline) -> dict:
    return {k: v for k, v in baseline.items() if k not in INTENTIONALLY_REMOVED}


@pytest.fixture
async def registered_tools() -> dict:
    mcp = FastMCP("parity")
    register_all(mcp, list(REGISTRY), lambda: None, enforce_auth=False)
    return {t.name: t for t in await mcp.list_tools()}


# --- coverage --------------------------------------------------------------


async def test_no_endpoint_coverage_was_lost(registered_tools, expected):
    missing = sorted(set(expected) - set(registered_tools))
    assert not missing, f"tools lost in migration: {missing}"


async def test_no_tools_appeared_unexpectedly(registered_tools, expected):
    extra = sorted(set(registered_tools) - set(expected) - set(INTENTIONALLY_ADDED))
    assert not extra, f"unexpected new tools: {extra}"


async def test_the_documented_coverage_gaps_are_now_closed(registered_tools):
    """Each of these is a CATS v3 endpoint the previous server never exposed."""
    missing = sorted(set(INTENTIONALLY_ADDED) - set(registered_tools))
    assert not missing, f"gap-closing tools not registered: {missing}"


async def test_custom_field_definitions_are_reachable(registered_tools):
    """Without these, an agent cannot discover a custom field's id.

    The per-record value tools all require a field id, and those ids are
    account-specific, so shipping the value tools without the definition tools
    left that whole surface unusable.
    """
    for name in (
        "list_candidate_custom_field_definitions",
        "list_job_custom_field_definitions",
    ):
        assert name in registered_tools


async def test_removed_tools_are_gone(registered_tools):
    for name, why in INTENTIONALLY_REMOVED.items():
        assert name not in registered_tools, f"{name} should be removed: {why}"


async def test_schemas_are_byte_identical(registered_tools, expected):
    differences = [
        name
        for name in sorted(set(expected) & set(registered_tools))
        if (expected[name].get("input_schema") or {}) != (registered_tools[name].parameters or {})
    ]
    assert not differences, f"tool schemas changed: {differences}"


# --- registry integrity ----------------------------------------------------


def test_registry_is_structurally_valid():
    problems = REGISTRY.validate()
    assert not problems, "\n".join(problems)


def test_tool_count_is_generated_not_hardcoded():
    """The audit found five different hand-maintained counts, four of them wrong."""
    assert len(REGISTRY) == sum(REGISTRY.counts().values())


def test_every_toolset_has_tools():
    assert all(count > 0 for count in REGISTRY.counts().values())


def test_no_duplicate_tool_names():
    names = [s.name for s in REGISTRY]
    assert len(names) == len(set(names))


# --- safety metadata -------------------------------------------------------


def test_every_delete_is_classified_destructive():
    for spec in REGISTRY:
        if spec.method.upper() == "DELETE":
            assert spec.safety in {Safety.DESTRUCTIVE, Safety.ADMIN}, spec.name


def test_read_tools_are_never_mutations():
    for spec in REGISTRY:
        if spec.method.upper() == "GET":
            assert spec.safety is Safety.READ, spec.name
            assert not spec.safety.is_mutation


async def test_destructive_tools_carry_the_destructive_hint(registered_tools):
    for spec in REGISTRY:
        if spec.safety is not Safety.DESTRUCTIVE:
            continue
        annotations = registered_tools[spec.name].annotations
        assert annotations.destructive_hint is True, spec.name
        assert annotations.read_only_hint is False, spec.name


async def test_read_tools_carry_the_read_only_hint(registered_tools):
    for spec in REGISTRY:
        if spec.safety is not Safety.READ:
            continue
        annotations = registered_tools[spec.name].annotations
        assert annotations.read_only_hint is True, spec.name
        assert annotations.destructive_hint is False, spec.name


async def test_every_tool_is_tagged_for_discovery(registered_tools):
    """BM25 indexes tags; an untagged tool is far harder to surface."""
    for name, tool in registered_tools.items():
        assert "ats" in tool.tags, name
        assert {"read", "write"} & set(tool.tags), name


async def test_tools_expose_endpoint_metadata(registered_tools):
    for spec in REGISTRY:
        meta = (registered_tools[spec.name].meta or {}).get("cats")
        assert meta, spec.name
        assert meta["endpoint"] == spec.endpoint
        assert meta["safety"] == spec.safety.value


# --- request construction --------------------------------------------------


def test_get_requests_never_carry_a_body():
    for spec in REGISTRY:
        if spec.method.upper() == "GET":
            assert not spec.params_at(ParamLocation.BODY), spec.name


def test_path_placeholders_all_have_parameters():
    """Covered by validate(), asserted separately because a mismatch here
    produces a KeyError at call time rather than a clear error."""
    assert not [p for p in REGISTRY.validate() if "placeholder" in p]


def test_summary_responses_declare_a_collection_key():
    from cats_mcp.registry.models import ResponseStrategy

    for spec in REGISTRY:
        if spec.response is ResponseStrategy.SUMMARY:
            assert spec.collection_key, spec.name
