"""Timing, caching and pacing.

The load-bearing test here is the one asserting that record data is never
cached. Everything else in this file is ordinary wiring.

A stale answer about list membership is worse than a slow one: a recruiter
screens a Do Not Contact list, somebody is added to it, the screen runs again a
minute later and returns the old answer. Nothing errors. That is the same class
of silent-wrong-answer the rest of this codebase exists to prevent, and caching
is an easy way to reintroduce it while believing you made things better.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.observability import CACHEABLE_REFERENCE_TOOLS, build_middleware
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import Safety
from cats_mcp.server import create_server


class StubCredentials(CredentialProvider):
    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

    def describe(self) -> str:
        return "stub"


def build(handler, **overrides):
    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW, **overrides)
    client = CATSClient(settings, StubCredentials(), transport=httpx2.MockTransport(handler))
    return create_server(settings, credential_provider=StubCredentials(), client=client)


# --- what may never be cached ----------------------------------------------

#: Path segments that name account *configuration* rather than records: the
#: workflows an account defines, the statuses a job can hold, the custom fields
#: it declares. These change when an administrator edits a setting.
CONFIGURATION_SEGMENTS = ("workflows", "statuses", "custom_fields")

#: Path placeholders identifying a *record*. An endpoint scoped to one of these
#: returns that record's data, whatever the rest of the path says.
#:
#: This is the half that matters: `/candidates/{candidate_id}/custom_fields`
#: contains "custom_fields" and is emphatically not configuration - it is one
#: person's values, and caching it would serve a stale answer about them.
RECORD_PLACEHOLDERS = (
    "{candidate_id}",
    "{job_id}",
    "{contact_id}",
    "{company_id}",
    "{pipeline_id}",
    "{list_id}",
)


def test_no_record_data_is_ever_cached():
    """The allowlist may only hold account configuration, never records.

    Both halves are required. A configuration segment says the endpoint is
    *about* configuration; the absence of a record placeholder says it is not
    scoped *to* one record.
    """
    offenders = []
    for name in CACHEABLE_REFERENCE_TOOLS:
        spec = REGISTRY.by_name(name)
        assert spec is not None, name
        endpoint = spec.endpoint
        names_configuration = any(seg in endpoint for seg in CONFIGURATION_SEGMENTS)
        scoped_to_a_record = any(ph in endpoint for ph in RECORD_PLACEHOLDERS)
        if not names_configuration or scoped_to_a_record:
            offenders.append(f"{name} ({endpoint})")

    assert not offenders, (
        f"these cache entries are not account configuration: {offenders}. "
        f"A stale answer about a record is a wrong answer."
    )


def test_the_rule_would_actually_reject_a_persons_custom_fields():
    """Guards the guard: the tempting near-miss must fail the rule.

    `/candidates/{candidate_id}/custom_fields` names a configuration segment and
    is still one person's data. A rule checking only the segment would let it in.
    """
    spec = REGISTRY.by_name("list_candidate_custom_fields")
    assert spec is not None
    assert any(seg in spec.endpoint for seg in CONFIGURATION_SEGMENTS)
    assert any(ph in spec.endpoint for ph in RECORD_PLACEHOLDERS), (
        "the record-placeholder half of the rule is what excludes this"
    )


def test_every_cacheable_tool_exists_and_is_a_read():
    """A typo would silently cache nothing; a write would cache a mutation."""
    for name in CACHEABLE_REFERENCE_TOOLS:
        spec = REGISTRY.by_name(name)
        assert spec is not None, f"{name} is not a registered tool"
        assert spec.safety is Safety.READ, f"{name} is {spec.safety}, not READ"


def test_candidate_and_list_tools_are_not_in_the_allowlist():
    """Named explicitly because these are the ones it would be tempting to add."""
    for name in (
        "list_candidates",
        "get_candidate",
        "list_candidate_list_items",
        "list_candidate_custom_fields",
        "list_candidate_pipelines",
    ):
        assert name not in CACHEABLE_REFERENCE_TOOLS, name


# --- stack construction -----------------------------------------------------


def test_timing_is_on_by_default():
    stack = build_middleware(Settings(api_key="k"))
    assert any("Timing" in type(m).__name__ for m in stack)


def test_there_is_no_mcp_level_rate_limiting():
    """Deliberately absent, and the reason is worth keeping.

    RateLimitingMiddleware counts MCP messages, not CATS requests. The two do
    not correspond - get_connection_status makes zero CATS calls, a paginated
    sweep makes three - and pacing at the CATS rate (0.14/s) throttles the
    protocol itself: the initialize handshake is refused and the connection
    never opens. Pacing belongs in CATSClient.request.
    """
    stack = build_middleware(Settings(api_key="k"))
    assert not any("RateLimit" in type(m).__name__ for m in stack)


def test_caching_can_be_turned_off_entirely():
    stack = build_middleware(Settings(api_key="k", cache_reference_data=False))
    assert not any("Caching" in type(m).__name__ for m in stack)


def test_everything_can_be_turned_off():
    stack = build_middleware(Settings(api_key="k", log_timing=False, cache_reference_data=False))
    assert stack == []


# --- through a running server ----------------------------------------------


async def test_the_server_still_works_with_the_stack_registered():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {"candidates": [{"id": 1, "first_name": "Dana"}]},
            },
        )

    async with Client(build(handler)) as client:
        result = await client.call_tool("list_candidates", {})

    assert result.data["items"][0]["id"] == 1


async def test_record_reads_still_hit_the_api_every_time():
    """The cache must not intercept a candidate lookup, however tempting."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(
            200,
            json={
                "count": 1,
                "total": 1,
                "_links": {},
                "_embedded": {"candidates": [{"id": 1, "first_name": "Dana"}]},
            },
        )

    server = build(handler)
    async with Client(server) as client:
        for _ in range(3):
            await client.call_tool("list_candidates", {})

    candidate_calls = [p for p in calls if p.endswith("/candidates")]
    assert len(candidate_calls) == 3, (
        f"expected 3 live calls, saw {len(candidate_calls)} - a record read was cached"
    )


@pytest.mark.parametrize("caching", [True, False])
async def test_the_server_answers_with_caching_either_way(caching):
    def handler(request):
        return httpx2.Response(200, json={"id": 1})

    async with Client(build(handler, cache_reference_data=caching)) as client:
        result = await client.call_tool("get_candidate", {"candidate_id": 1})

    assert result.data["id"] == 1


def test_component_listings_are_never_cached():
    """Caching list_tools breaks discovery and per-session visibility.

    The search transform builds its BM25 index from the tool listing. With the
    listing cached, the index is built from the already-transformed set and a
    search returns `search_tools` itself rather than the tool asked for -
    discovery fails silently. Progressive disclosure fails the same way, because
    it needs list_tools to reflect what *this* session can currently see.

    The behavioural guard is test_authorization.py, which failed outright when
    the listing was cached. This is the fast, explicit version of the same rule.

    It reads private attributes deliberately: if FastMCP renames them this fails
    loudly rather than passing while checking nothing.
    """
    stack = build_middleware(Settings(api_key="k"))
    caching = [m for m in stack if "Caching" in type(m).__name__]
    assert caching, "expected the caching middleware to be present"

    for attr in ("_list_tools_settings", "_list_resources_settings", "_list_prompts_settings"):
        assert hasattr(caching[0], attr), (
            f"{attr} is gone - FastMCP renamed it. Re-point this test rather "
            f"than deleting it; caching listings breaks discovery."
        )
        assert getattr(caching[0], attr).get("enabled") is False, f"{attr} must stay disabled"


def test_reference_data_caching_is_actually_configured():
    """The half that is meant to be on."""
    stack = build_middleware(Settings(api_key="k"))
    caching = [m for m in stack if "Caching" in type(m).__name__][0]
    settings = caching._call_tool_settings
    assert settings["enabled"] is True
    assert set(settings["included_tools"]) == set(CACHEABLE_REFERENCE_TOOLS)
