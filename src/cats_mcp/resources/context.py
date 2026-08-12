"""MCP resources - read-only context about this adapter and the connected account.

Tools are actions. Resources are data a client can read without invoking an
action, and they are lazy-loaded, so they cost nothing until something asks.

Three kinds live here:

1. **Adapter self-description.** What this server is, what it owns, what it
   deliberately does not, and how it is currently configured. A consumer -
   Mastra, ADK, ChatGPT, a human reading the client UI - can answer "what am I
   connected to and what is it for" without calling anything.

2. **Account context.** Which CATS site the credential resolves to, and the live
   rate-limit budget.

3. **Account-specific reference data.** Workflow status ids and custom field
   definitions. These are the ids nothing else works without, they differ per
   CATS account, and they change rarely - which is exactly the profile of a
   resource rather than a tool.

Nothing here encodes recruiting policy. These describe the adapter and the
account, not what to do with them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from cats_mcp.config import Settings
from cats_mcp.http.correlation import get_logger
from cats_mcp.http.errors import CATSAPIError
from cats_mcp.registry.catalog import REGISTRY
from cats_mcp.registry.models import Safety

logger = get_logger(__name__)

#: Reference data changes rarely; a short cache keeps repeated reads off the
#: 500/hour budget without risking a long-stale view.
REFERENCE_CACHE_TTL = 300

OWNS = [
    "CATS API authentication and requests",
    "CATS endpoint coverage and accurate tool schemas",
    "tool discovery metadata",
    "compact, structured responses and pagination",
    "rate-limit handling and request validation",
    "read/write/destructive safety classification",
    "audit and correlation metadata",
]

DOES_NOT_OWN = [
    "recruiting workflows",
    "agent orchestration",
    "agent memory",
    "candidate outreach campaigns",
    "email or SMS delivery",
    "follow-up sequences",
    "scheduling",
    "client-specific recruiting rules",
    "candidate-ranking policy",
    "a frontend",
    "a mirrored CATS database",
]


def register(
    mcp: Any,
    settings: Settings,
    credentials: Any,
    client_getter: Callable[[], Any],
    tool_count: int,
) -> int:
    """Register the context resources. Returns how many were added."""

    @mcp.resource(
        uri="cats://server/capabilities",
        name="AdapterCapabilities",
        description=(
            "What this MCP server is, what it is responsible for, what it "
            "deliberately leaves to the calling orchestrator, and how it is "
            "currently configured. Read this first to understand what you are "
            "connected to."
        ),
        mime_type="application/json",
    )
    def capabilities() -> str:
        from cats_mcp import __version__

        return json.dumps(
            {
                "name": "CATS MCP",
                "version": __version__,
                "purpose": (
                    "A universal MCP adapter for the CATS (CatsOne) applicant "
                    "tracking system API v3. One integration among several; CATS "
                    "is one possible ATS."
                ),
                "owns": OWNS,
                "does_not_own": DOES_NOT_OWN,
                "boundary": (
                    "This server exposes CATS capabilities. It does not decide who "
                    "to contact, when, or why. Those decisions belong to the "
                    "calling agent or orchestrator."
                ),
                "configuration": {
                    "discovery_mode": settings.discovery_mode.value,
                    "tools_registered": tool_count,
                    "transport": settings.transport.value,
                    "authentication": "jwt" if settings.auth_jwks_uri else "none",
                    "credentials": credentials.describe(),
                },
                "discovery_modes": {
                    "raw": "full catalog; for orchestrators doing their own tool discovery",
                    "search": "search_tools/call_tool plus pinned tools; for direct clients",
                    "code": "Code Mode sandbox; optional extra, experimental",
                },
                "notes": [
                    "Every tool remains callable in every discovery mode; only "
                    "visibility changes.",
                    "List and search tools return compact summaries. Use the "
                    "dedicated detail tools for one record rather than widening a "
                    "list.",
                    "The CATS standard rate limit is 500 requests/hour. Read "
                    "cats://account/rate-limit before a large batch.",
                ],
            },
            indent=2,
        )

    @mcp.resource(
        uri="cats://server/tools",
        name="ToolCatalogSummary",
        description=(
            "A summary of the tool catalog by toolset and safety class, without "
            "the full schemas. Use this to understand the shape of what is "
            "available before searching for a specific tool."
        ),
        mime_type="application/json",
    )
    def tool_catalog() -> str:
        by_safety: dict[str, int] = {}
        for spec in REGISTRY:
            by_safety[spec.safety.value] = by_safety.get(spec.safety.value, 0) + 1
        return json.dumps(
            {
                "endpoint_tools": len(REGISTRY),
                "by_toolset": REGISTRY.counts(),
                "by_safety": by_safety,
                "scopes": {s.value: s.required_scope for s in Safety},
                "safety_meaning": {
                    "read": "no side effects",
                    "write": "creates or modifies a record",
                    "destructive": "deletes data; cannot be undone",
                    "bulk": "replaces a whole set; omitted items are removed",
                    "admin": "account-level configuration",
                },
            },
            indent=2,
        )

    @mcp.resource(
        uri="cats://account/rate-limit",
        name="RateLimitBudget",
        description=(
            "The current CATS rate-limit budget for this connection, as reported "
            "by the API itself. Read this before starting a large batch of "
            "lookups. The CATS standard ceiling is 500 requests/hour; some "
            "accounts are raised."
        ),
        mime_type="application/json",
    )
    def rate_limit() -> str:
        state = client_getter().rate_limit
        return json.dumps(
            {
                **state.snapshot(),
                "standard_ceiling": 500,
                "note": (
                    "null means no CATS request has been made yet this session. "
                    "The ceiling is read from response headers, not assumed."
                ),
            },
            indent=2,
        )

    @mcp.resource(
        uri="cats://account/site",
        name="ConnectedAccount",
        description=(
            "Information about the CATS account this server is connected to. Use "
            "this to confirm which account you are acting against."
        ),
        mime_type="application/json",
    )
    async def site() -> str:
        try:
            return json.dumps(await client_getter().request("GET", "/site"), indent=2)
        except CATSAPIError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

    @mcp.resource(
        uri="cats://reference/workflows",
        name="PipelineWorkflows",
        description=(
            "The hiring workflows configured on this CATS account and the status "
            "id for every stage in each. Status ids are account-specific, so read "
            "this before changing any pipeline status - the correct id cannot be "
            "guessed and a wrong one silently moves a candidate to the wrong stage."
        ),
        mime_type="application/json",
    )
    async def workflows() -> str:
        client = client_getter()
        try:
            payload = await client.request("GET", "/pipelines/workflows")
            entries = ((payload or {}).get("_embedded") or {}).get("workflows") or []
            out = []
            for workflow in entries:
                if not isinstance(workflow, dict):
                    continue
                wid = workflow.get("id")
                statuses = []
                try:
                    status_payload = await client.request(
                        "GET", f"/pipelines/workflows/{wid}/statuses"
                    )
                    raw = ((status_payload or {}).get("_embedded") or {}).get("statuses") or []
                    statuses = [
                        {"id": s.get("id"), "title": s.get("title")}
                        for s in raw
                        if isinstance(s, dict)
                    ]
                except CATSAPIError as exc:
                    statuses = [{"error": str(exc)}]
                out.append(
                    {"workflow_id": wid, "title": workflow.get("title"), "statuses": statuses}
                )
            return json.dumps({"workflows": out, "count": len(out)}, indent=2)
        except CATSAPIError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

    @mcp.resource(
        uri="cats://reference/custom-fields/{resource}",
        name="CustomFieldDefinitions",
        description=(
            "The custom field definitions configured on this CATS account for a "
            "given resource - 'candidates' or 'jobs'. Custom fields hold the "
            "account-specific data that standard fields do not, and their ids "
            "differ per account. Read this to discover which fields exist and "
            "their ids before reading or writing a value."
        ),
        mime_type="application/json",
    )
    async def custom_fields(resource: str) -> str:
        supported = {"candidates", "jobs"}
        if resource not in supported:
            return json.dumps(
                {
                    "error": f"Unsupported resource {resource!r}.",
                    "supported": sorted(supported),
                },
                indent=2,
            )
        try:
            payload = await client_getter().request("GET", f"/{resource}/custom_fields")
            raw = ((payload or {}).get("_embedded") or {}).get("custom_fields") or []
            fields = [
                {
                    "id": f.get("id"),
                    "name": f.get("name") or f.get("title"),
                    "type": f.get("type"),
                }
                for f in raw
                if isinstance(f, dict)
            ]
            return json.dumps(
                {"resource": resource, "fields": fields, "count": len(fields)}, indent=2
            )
        except CATSAPIError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

    return 6
