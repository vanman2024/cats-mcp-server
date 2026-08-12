"""Portals tool specifications.

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
        name="list_portals",
        resource="portal",
        operation="list",
        method="GET",
        endpoint="/portals",
        description="List all job portals/boards. Use this to enumerate portals and obtain their ids.\n\nWraps: GET /portals",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="portals",
        toolset="portals",
        params=(
            Param(
                name="per_page",
                annotation=int,
                description="Number of portals per page (default: 25, max: 100)",
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
        name="get_portal",
        resource="portal",
        operation="get",
        method="GET",
        endpoint="/portals/{portal_id}",
        description="Get detailed information about a specific portal. Use this when you already have a portal id and need its detail.\n\nWraps: GET /portals/{portal_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Unique identifier for the portal",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="list_portal_jobs",
        resource="portal",
        operation="list",
        method="GET",
        endpoint="/portals/{portal_id}/jobs",
        description="List all jobs published to a specific portal. Use this to enumerate portals and obtain their ids.\n\nWraps: GET /portals/{portal_id}/jobs",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="jobs",
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Portal ID",
                location=ParamLocation.PATH,
            ),
            Param(
                name="per_page",
                annotation=int,
                description="Results per page",
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
    ToolSpec(
        name="submit_job_application",
        resource="portal",
        operation="submit",
        method="POST",
        endpoint="/portals/{portal_id}/jobs/{job_id}",
        description="Submit a job application through a portal.\n\nWraps: POST /portals/{portal_id}/jobs/{job_id}",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Portal ID",
                location=ParamLocation.PATH,
            ),
            Param(
                name="job_id",
                annotation=int | str,
                description="Job posting ID",
                location=ParamLocation.PATH,
            ),
            Param(
                name="candidate_data",
                annotation=dict[str, Any],
                description="Candidate information (first_name, last_name, email, resume, etc.)",
                location=ParamLocation.BODY,
                transform=Transform.SPREAD,
            ),
        ),
    ),
    ToolSpec(
        name="publish_job_to_portal",
        resource="portal",
        operation="publish",
        method="PUT",
        endpoint="/portals/{portal_id}/jobs/{job_id}",
        description="Publish a job posting to a portal.\n\nWraps: PUT /portals/{portal_id}/jobs/{job_id}",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Portal ID",
                location=ParamLocation.PATH,
            ),
            Param(
                name="job_id",
                annotation=int | str,
                description="Job posting ID",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="unpublish_job_from_portal",
        resource="portal",
        operation="unpublish",
        method="DELETE",
        endpoint="/portals/{portal_id}/jobs/{job_id}",
        description="Remove a job posting from a portal.\n\nWraps: DELETE /portals/{portal_id}/jobs/{job_id}",
        safety=Safety.DESTRUCTIVE,
        response=ResponseStrategy.RAW,
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Portal ID",
                location=ParamLocation.PATH,
            ),
            Param(
                name="job_id",
                annotation=int | str,
                description="Job posting ID",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="get_portal_registration",
        resource="portal",
        operation="get",
        method="GET",
        endpoint="/portals/{portal_id}/registration",
        description="Get portal registration information and requirements. Use this when you already have a portal id and need its detail.\n\nWraps: GET /portals/{portal_id}/registration",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Portal ID",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="submit_portal_registration",
        resource="portal",
        operation="submit",
        method="POST",
        endpoint="/portals/{portal_id}/registration",
        description="Submit portal registration information.\n\nWraps: POST /portals/{portal_id}/registration",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="portals",
        params=(
            Param(
                name="portal_id",
                annotation=int | str,
                description="Portal ID",
                location=ParamLocation.PATH,
            ),
            Param(
                name="registration_data",
                annotation=dict[str, Any],
                description="Registration information (varies by portal)",
                location=ParamLocation.BODY,
                transform=Transform.SPREAD,
            ),
        ),
    ),
]
