"""Tasks tool specifications.

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
        name="list_tasks",
        resource="task",
        operation="list",
        method="GET",
        endpoint="/tasks",
        description="List all tasks with pagination. Use this to enumerate tasks and obtain their ids.\n\nWraps: GET /tasks",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="tasks",
        toolset="tasks",
        params=(
            Param(
                name="per_page",
                annotation=int,
                description="Number of results per page (default: 25)",
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
        name="get_task",
        resource="task",
        operation="get",
        method="GET",
        endpoint="/tasks/{task_id}",
        description="Get detailed information about a specific task. Use this when you already have a task id and need its detail.\n\nWraps: GET /tasks/{task_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="tasks",
        params=(
            Param(
                name="task_id",
                annotation=int | str,
                description="The unique identifier of the task",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="create_task",
        resource="task",
        operation="create",
        method="POST",
        endpoint="/tasks",
        description="Create a new task. Use this to add a new task record.\n\nWraps: POST /tasks",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="tasks",
        params=(
            Param(
                name="title",
                annotation=str,
                description="Task title",
                location=ParamLocation.BODY,
            ),
            Param(
                name="due_date",
                annotation=str | None,
                description="Due date in ISO format (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="candidate_id",
                annotation=int | None,
                description="Associated candidate ID (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="job_id",
                annotation=int | None,
                description="Associated job ID (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            # CATS names this assigned_to_id on the wire. Without the mapping the
            # body carried "assigned_to", the required field arrived empty, and
            # CATS answered "assigned_to_id must be positive" - an error that
            # reads like a bad value when the field was never sent at all.
            #
            # Required, not optional: that same 400 is what CATS returns when it
            # is missing, so offering it as optional only defers the failure.
            Param(
                name="assigned_to",
                annotation=int,
                description=(
                    "CATS user id to assign the task to. Required by CATS. Find "
                    "ids with list_users."
                ),
                location=ParamLocation.BODY,
                wire_name="assigned_to_id",
            ),
            # Also required by CATS ("priority must not be optional"), and also
            # missing from this spec entirely, so a caller could not supply it
            # even knowing it was needed.
            #
            # The default is measured rather than guessed: all 100 tasks sampled
            # from the live account carry priority 5. CATS does not document the
            # scale, so this exposes the value the account already uses instead
            # of inventing a range.
            Param(
                name="priority",
                annotation=int,
                description=(
                    "Task priority. CATS requires a value and does not document "
                    "the scale; 5 is what every existing task in the account uses."
                ),
                location=ParamLocation.BODY,
                default=5,
            ),
            Param(
                name="description",
                annotation=str | None,
                description="Task description (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
    ToolSpec(
        name="update_task",
        resource="task",
        operation="update",
        method="PUT",
        endpoint="/tasks/{task_id}",
        description="Update an existing task. Use this to change fields on an existing task. Only the fields you supply are modified.\n\nWraps: PUT /tasks/{task_id}",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="tasks",
        params=(
            Param(
                name="task_id",
                annotation=int | str,
                description="The unique identifier of the task",
                location=ParamLocation.PATH,
            ),
            Param(
                name="title",
                annotation=str | None,
                description="Updated task title (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="due_date",
                annotation=str | None,
                description="Updated due date (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="status",
                annotation=str | None,
                description="Updated task status (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            # Same wire-name mapping as create_task (issue #15). Optional here,
            # because a partial update that does not mean to reassign should not
            # have to resend the assignee.
            Param(
                name="assigned_to",
                annotation=int | None,
                description="CATS user id to reassign the task to.",
                location=ParamLocation.BODY,
                wire_name="assigned_to_id",
                default=None,
            ),
            Param(
                name="description",
                annotation=str | None,
                description="Updated description (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
    ToolSpec(
        name="delete_task",
        resource="task",
        operation="delete",
        method="DELETE",
        endpoint="/tasks/{task_id}",
        description="Delete a task. Permanently removes this record. This cannot be undone.\n\nWraps: DELETE /tasks/{task_id}",
        safety=Safety.DESTRUCTIVE,
        response=ResponseStrategy.RAW,
        toolset="tasks",
        params=(
            Param(
                name="task_id",
                annotation=int | str,
                description="The unique identifier of the task",
                location=ParamLocation.PATH,
            ),
        ),
    ),
]
