"""Backups tool specifications.

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
        name="list_backups",
        resource="backup",
        operation="list",
        method="GET",
        endpoint="/backups",
        description="List all system backups. Use this to enumerate backups and obtain their ids.\n\nWraps: GET /backups",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="backups",
        toolset="backups",
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
            Param(
                name="status",
                annotation=str | None,
                description="Filter by status (pending, processing, completed, expired)",
                location=ParamLocation.QUERY,
                default=None,
            ),
        ),
    ),
    ToolSpec(
        name="get_backup",
        resource="backup",
        operation="get",
        method="GET",
        endpoint="/backups/{backup_id}",
        description="Get details of a specific backup. Use this when you already have a backup id and need its detail.\n\nWraps: GET /backups/{backup_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="backups",
        params=(
            Param(
                name="backup_id",
                annotation=int | str,
                description="The unique identifier of the backup",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="create_backup",
        resource="backup",
        operation="create",
        method="POST",
        endpoint="/backups",
        description="Create a new system backup. Use this to add a new backup record.\n\nWraps: POST /backups",
        safety=Safety.ADMIN,
        response=ResponseStrategy.RAW,
        toolset="backups",
        params=(
            Param(
                name="include_attachments",
                annotation=bool,
                description="Include attachment files (default: True)",
                location=ParamLocation.BODY,
                default=True,
            ),
            Param(
                name="include_emails",
                annotation=bool,
                description="Include email history (default: True)",
                location=ParamLocation.BODY,
                default=True,
            ),
            Param(
                name="description",
                annotation=str | None,
                description="Optional description for the backup",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
]
