"""Events tool specifications.

Generated from the pre-refactor hand-written toolsets, then reviewed. Edit this
file directly; it is the source of truth for these tools.
"""

from __future__ import annotations

from typing import Any  # noqa: F401  (used in parameter annotations)

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
        name="list_events",
        resource="event",
        operation="list",
        method="GET",
        endpoint="/events",
        description="Retrieve the chronological stream of changes across the CATS account - records created, updated, deleted, and status changes. Use this to find what changed since a previous check, instead of re-listing whole record sets. This is the efficient way to stay in sync under a rate limit.\n\nWraps: GET /events",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="events",
        toolset="events",
        params=(
            Param(
                name="starting_after_id",
                annotation=int | None,
                description="Return events after this event ID (for cursor-based pagination)",
                location=ParamLocation.QUERY,
                default=None,
            ),
            Param(
                name="starting_after_timestamp",
                annotation=str | None,
                description="Return events after this timestamp (ISO 8601 / RFC 3339)",
                location=ParamLocation.QUERY,
                default=None,
            ),
        ),
    ),
]
