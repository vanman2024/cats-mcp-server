"""Tags tool specifications.

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
        name="list_tags",
        resource="tag",
        operation="list",
        method="GET",
        endpoint="/tags",
        description="List all tags in the system. Use this to enumerate tags and obtain their ids.\n\nWraps: GET /tags",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="tags",
        toolset="tags",
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
        name="get_tag",
        resource="tag",
        operation="get",
        method="GET",
        endpoint="/tags/{tag_id}",
        description="Get details of a specific tag. Use this when you already have a tag id and need its detail.\n\nWraps: GET /tags/{tag_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="tags",
        params=(
            Param(
                name="tag_id",
                annotation=int | str,
                description="The unique identifier of the tag",
                location=ParamLocation.PATH,
            ),
        ),
    ),
]
