"""Webhooks tool specifications.

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
        name="list_webhooks",
        resource="webhook",
        operation="list",
        method="GET",
        endpoint="/webhooks",
        description="List all configured webhooks. Use this to enumerate webhooks and obtain their ids.\n\nWraps: GET /webhooks",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="webhooks",
        toolset="webhooks",
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
        name="get_webhook",
        resource="webhook",
        operation="get",
        method="GET",
        endpoint="/webhooks/{webhook_id}",
        description="Get details of a specific webhook configuration. Use this when you already have a webhook id and need its detail.\n\nWraps: GET /webhooks/{webhook_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="webhooks",
        params=(
            Param(
                name="webhook_id",
                annotation=int | str,
                description="The unique identifier of the webhook",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="create_webhook",
        resource="webhook",
        operation="create",
        method="POST",
        endpoint="/webhooks",
        description="Create a new webhook subscription. Use this to add a new webhook record.\n\nWraps: POST /webhooks",
        safety=Safety.ADMIN,
        response=ResponseStrategy.RAW,
        toolset="webhooks",
        params=(
            Param(
                name="target_url",
                annotation=str,
                description="Target URL for webhook POST requests (must be HTTPS in production)",
                location=ParamLocation.BODY,
            ),
            Param(
                name="events",
                annotation=list[str],
                description="List of event types to subscribe to.\n    Available: candidate.created, candidate.updated, candidate.deleted,\n    job.created, job.updated, job.deleted, job.status_changed,\n    pipeline.created, pipeline.deleted, pipeline.status_changed,\n    contact.created, contact.updated, contact.deleted,\n    company.created, company.updated, company.deleted,\n    activity.created, activity.updated, activity.deleted,\n    user.created, user.updated, user.deleted",
                location=ParamLocation.BODY,
            ),
            Param(
                name="signing_key",
                annotation=str,
                description="HMAC-SHA256 key for webhook signature verification",
                location=ParamLocation.BODY,
                wire_name="secret",
            ),
        ),
    ),
    ToolSpec(
        name="delete_webhook",
        resource="webhook",
        operation="delete",
        method="DELETE",
        endpoint="/webhooks/{webhook_id}",
        description="Delete a webhook subscription. Permanently removes this record. This cannot be undone.\n\nWraps: DELETE /webhooks/{webhook_id}",
        safety=Safety.ADMIN,
        response=ResponseStrategy.RAW,
        toolset="webhooks",
        params=(
            Param(
                name="webhook_id",
                annotation=int | str,
                description="The unique identifier of the webhook to delete",
                location=ParamLocation.PATH,
            ),
        ),
    ),
]
