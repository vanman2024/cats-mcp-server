"""Attachments tool specifications.

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
        name="get_attachment",
        resource="attachment",
        operation="get",
        method="GET",
        endpoint="/attachments/{attachment_id}",
        description="Get metadata for a specific attachment. Use this when you already have a attachment id and need its detail.\n\nWraps: GET /attachments/{attachment_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="attachments",
        params=(
            Param(
                name="attachment_id",
                annotation=int | str,
                description="The unique identifier of the attachment",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="delete_attachment",
        resource="attachment",
        operation="delete",
        method="DELETE",
        endpoint="/attachments/{attachment_id}",
        description="Delete an attachment. Permanently removes this record. This cannot be undone.\n\nWraps: DELETE /attachments/{attachment_id}",
        safety=Safety.DESTRUCTIVE,
        response=ResponseStrategy.RAW,
        toolset="attachments",
        params=(
            Param(
                name="attachment_id",
                annotation=int | str,
                description="The unique identifier of the attachment to delete",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="download_attachment",
        resource="attachment",
        operation="download",
        method="GET",
        endpoint="/attachments/{attachment_id}/download",
        description="Download an attachment file. Retrieves file content. Prefer the list tool first to find the right file.\n\nWraps: GET /attachments/{attachment_id}/download",
        safety=Safety.READ,
        response=ResponseStrategy.RAW,
        toolset="attachments",
        params=(
            Param(
                name="attachment_id",
                annotation=int | str,
                description="The unique identifier of the attachment",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="parse_resume",
        resource="attachment",
        operation="parse",
        method="POST",
        endpoint="/attachments/parse",
        description="Extract structured candidate data from a resume file - name, contact details, work history and skills. Use this to read a resume before deciding whether to create a candidate record from it.\n\nWraps: POST /attachments/parse",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="attachments",
        params=(
            Param(
                name="file_content",
                annotation=str,
                description="Base64-encoded file content",
                location=ParamLocation.BODY,
                wire_name="file",
            ),
            Param(
                name="filename",
                annotation=str,
                description="Original filename with extension",
                location=ParamLocation.BODY,
            ),
        ),
    ),
]
