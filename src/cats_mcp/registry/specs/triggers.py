"""Triggers tool specifications.

Generated from the pre-refactor hand-written toolsets, then reviewed. Edit this
file directly; it is the source of truth for these tools.
"""

from __future__ import annotations

from typing import Any, Optional  # noqa: F401  (used in parameter annotations)

from cats_mcp.registry.models import (
    Param,
    ParamLocation,
    ResponseStrategy,
    Safety,
    ToolSpec,
    Transform,  # noqa: F401  (used by some specs)
)

SPECS: list[ToolSpec] = [
    ToolSpec(
        name="list_triggers",
        resource="trigger",
        operation="list",
        method="GET",
        endpoint="/triggers",
        description="List all configured triggers. Use this to enumerate triggers and obtain their ids.\n\nWraps: GET /triggers",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="triggers",
        toolset="triggers",
        params=(
            Param(
                name="per_page",
                annotation=int,
                description="Number of results per page (default: 25, max: 100)",
                location=ParamLocation.QUERY,
                default=25,
            ),
            Param(
                name="page",
                annotation=int,
                description="Page number to retrieve (default: 1)",
                location=ParamLocation.QUERY,
                default=1,
            ),
        ),
    ),
    ToolSpec(
        name="get_trigger",
        resource="trigger",
        operation="get",
        method="GET",
        endpoint="/triggers/{trigger_id}",
        description="Get details of a specific trigger configuration. Use this when you already have a trigger id and need its detail.\n\nWraps: GET /triggers/{trigger_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="triggers",
        params=(
            Param(
                name="trigger_id",
                annotation=int | str,
                description="The unique identifier of the trigger",
                location=ParamLocation.PATH,
            ),
        ),
    ),
]
