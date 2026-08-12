"""Users tool specifications.

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
        name="list_users",
        resource="user",
        operation="list",
        method="GET",
        endpoint="/users",
        description="List all users in the organization. Use this to enumerate users and obtain their ids.\n\nWraps: GET /users",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="users",
        toolset="users",
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
        name="get_user",
        resource="user",
        operation="get",
        method="GET",
        endpoint="/users/{user_id}",
        description="Get details of a specific user. Use this when you already have a user id and need its detail.\n\nWraps: GET /users/{user_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="users",
        params=(
            Param(
                name="user_id",
                annotation=int | str,
                description="The unique identifier of the user",
                location=ParamLocation.PATH,
            ),
        ),
    ),
]
