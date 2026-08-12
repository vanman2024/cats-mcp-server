"""Work History tool specifications.

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
        name="get_work_history",
        resource="work_history",
        operation="get",
        method="GET",
        endpoint="/work_history/{work_history_id}",
        description="Get detailed information about a specific work history entry. Use this when you already have a work history entry id and need its detail.\n\nWraps: GET /work_history/{work_history_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="work_history",
        params=(
            Param(
                name="work_history_id",
                annotation=int | str,
                description="Unique identifier for the work history entry",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="update_work_history",
        resource="work_history",
        operation="update",
        method="PUT",
        endpoint="/work_history/{work_history_id}",
        description="Update an existing work history entry. Use this to change fields on an existing work history entry. Only the fields you supply are modified.\n\nWraps: PUT /work_history/{work_history_id}",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="work_history",
        params=(
            Param(
                name="work_history_id",
                annotation=int | str,
                description="ID of work history to update",
                location=ParamLocation.PATH,
            ),
            Param(
                name="company_name",
                annotation=str | None,
                description="Updated company name",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="title",
                annotation=str | None,
                description="Updated job title",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="start_date",
                annotation=str | None,
                description="Updated start date (YYYY-MM-DD)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="end_date",
                annotation=str | None,
                description="Updated end date (YYYY-MM-DD, null if currently employed)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="description",
                annotation=str | None,
                description="Updated job description",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="currently_employed",
                annotation=bool | None,
                description="Whether candidate is currently employed here",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
    ToolSpec(
        name="delete_work_history",
        resource="work_history",
        operation="delete",
        method="DELETE",
        endpoint="/work_history/{work_history_id}",
        description="Delete a work history entry (permanent). Permanently removes this record. This cannot be undone.\n\nWraps: DELETE /work_history/{work_history_id}",
        safety=Safety.DESTRUCTIVE,
        response=ResponseStrategy.RAW,
        toolset="work_history",
        params=(
            Param(
                name="work_history_id",
                annotation=int | str,
                description="ID of work history to delete",
                location=ParamLocation.PATH,
            ),
        ),
    ),
]
