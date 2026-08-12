"""Activities tool specifications.

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
        name="list_activities",
        resource="activity",
        operation="list",
        method="GET",
        endpoint="/activities",
        description="List all activities with pagination. Use this to enumerate activities and obtain their ids.\n\nWraps: GET /activities",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="activities",
        toolset="activities",
        params=(
            Param(
                name="per_page",
                annotation=int,
                description="Number of activities per page (default: 25, max: 100)",
                location=ParamLocation.QUERY,
                default=25,
            ),
            Param(
                name="page",
                annotation=int,
                description="Page number for pagination (default: 1)",
                location=ParamLocation.QUERY,
                default=1,
            ),
        ),
    ),
    ToolSpec(
        name="get_activity",
        resource="activity",
        operation="get",
        method="GET",
        endpoint="/activities/{activity_id}",
        description="Get detailed information about a specific activity. Use this when you already have a activity id and need its detail.\n\nWraps: GET /activities/{activity_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="activities",
        params=(
            Param(
                name="activity_id",
                annotation=int | str,
                description="Unique identifier for the activity",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="update_activity",
        resource="activity",
        operation="update",
        method="PUT",
        endpoint="/activities/{activity_id}",
        description="Update an existing activity. Use this to change fields on an existing activity. Only the fields you supply are modified.\n\nWraps: PUT /activities/{activity_id}",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="activities",
        params=(
            Param(
                name="activity_id",
                annotation=int | str,
                description="ID of activity to update",
                location=ParamLocation.PATH,
            ),
            Param(
                name="activity_type",
                annotation=str | None,
                description="Updated type (email, meeting, call_talked, call_lvm, call_missed, text_message, other)",
                location=ParamLocation.BODY,
                default=None,
                wire_name="type",
            ),
            Param(
                name="description",
                annotation=str | None,
                description="Updated description",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="notes",
                annotation=str | None,
                description="Updated notes",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="completed",
                annotation=bool | None,
                description="Mark as completed (true/false)",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
    ToolSpec(
        name="delete_activity",
        resource="activity",
        operation="delete",
        method="DELETE",
        endpoint="/activities/{activity_id}",
        description="Delete an activity record (permanent). Permanently removes this record. This cannot be undone.\n\nWraps: DELETE /activities/{activity_id}",
        safety=Safety.DESTRUCTIVE,
        response=ResponseStrategy.RAW,
        toolset="activities",
        params=(
            Param(
                name="activity_id",
                annotation=int | str,
                description="ID of activity to delete",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="search_activities",
        resource="activity",
        operation="search",
        method="GET",
        endpoint="/activities/search",
        description="Search activities by description or other criteria. Use this for keyword searches across activities.\n\nWraps: GET /activities/search",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="activities",
        toolset="activities",
        params=(
            Param(
                name="query",
                annotation=str,
                description="Search query string",
                location=ParamLocation.QUERY,
            ),
            Param(
                name="per_page",
                annotation=int,
                description="Number of results per page (max: 100)",
                location=ParamLocation.QUERY,
                default=25,
            ),
        ),
    ),
    ToolSpec(
        name="filter_activities",
        resource="activity",
        operation="filter",
        method="POST",
        endpoint="/activities/search",
        description="Filter activities using advanced criteria. Use this to match activities on exact field values.\n\nWraps: POST /activities/search",
        safety=Safety.WRITE,
        response=ResponseStrategy.SUMMARY,
        collection_key="activities",
        toolset="activities",
        params=(
            Param(
                name="filter_field",
                annotation=str,
                description='Field to filter on (e.g., "type", "subject", "date")',
                location=ParamLocation.BODY,
                wire_name="field",
            ),
            Param(
                name="filter_type",
                annotation=str,
                description='Filter operator ("contains", "exactly", "is_empty", "greater_than", "less_than", "between")',
                location=ParamLocation.BODY,
                wire_name="filter",
            ),
            Param(
                name="filter_value",
                annotation=Any,
                description="Value to filter by",
                location=ParamLocation.BODY,
                wire_name="value",
            ),
            Param(
                name="per_page",
                annotation=int,
                description="Results per page (max: 100)",
                location=ParamLocation.QUERY,
                default=25,
            ),
            Param(
                name="page",
                annotation=int,
                description="Page number",
                location=ParamLocation.QUERY,
                default=1,
            ),
        ),
    ),
]
