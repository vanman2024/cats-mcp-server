"""Pipelines tool specifications.

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
        name="list_pipelines",
        resource="pipeline",
        operation="list",
        method="GET",
        endpoint="/pipelines",
        description="List all pipelines with pagination. Use this to enumerate pipelines and obtain their ids.\n\nWraps: GET /pipelines",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="pipelines",
        toolset="pipelines",
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
        name="get_pipeline",
        resource="pipeline",
        operation="get",
        method="GET",
        endpoint="/pipelines/{pipeline_id}",
        description="Get detailed information about a specific pipeline. Use this when you already have a pipeline id and need its detail.\n\nWraps: GET /pipelines/{pipeline_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="pipelines",
        params=(
            Param(
                name="pipeline_id",
                annotation=int | str,
                description="The unique identifier of the pipeline",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="create_pipeline",
        resource="pipeline",
        operation="create",
        method="POST",
        endpoint="/pipelines",
        description="Add a candidate to a job's pipeline - submit or shortlist them for that role. Use this to put a candidate forward for a job. Creates the candidate-to-job relationship that all subsequent status changes act on.\n\nWraps: POST /pipelines",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="pipelines",
        params=(
            Param(
                name="candidate_id",
                annotation=int,
                description="The candidate ID to submit",
                location=ParamLocation.BODY,
                transform=Transform.TO_INT,
            ),
            Param(
                name="job_id",
                annotation=int,
                description="The job ID to submit candidate to",
                location=ParamLocation.BODY,
                transform=Transform.TO_INT,
            ),
            Param(
                name="rating",
                annotation=int | None,
                description="Optional rating (1-5)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="status_id",
                annotation=int | None,
                description="Initial pipeline status/stage ID (optional)",
                location=ParamLocation.BODY,
                default=None,
                transform=Transform.TO_INT,
            ),
        ),
    ),
    ToolSpec(
        name="update_pipeline",
        resource="pipeline",
        operation="update",
        method="PUT",
        endpoint="/pipelines/{pipeline_id}",
        description="Update a pipeline's properties. Use this to change fields on an existing pipeline. Only the fields you supply are modified.\n\nWraps: PUT /pipelines/{pipeline_id}",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="pipelines",
        params=(
            Param(
                name="pipeline_id",
                annotation=int | str,
                description="The unique identifier of the pipeline",
                location=ParamLocation.PATH,
            ),
            Param(
                name="name",
                annotation=str | None,
                description="Updated pipeline name (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
            Param(
                name="status_id",
                annotation=int | None,
                description="Updated status/stage ID (optional)",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
    ToolSpec(
        name="delete_pipeline",
        resource="pipeline",
        operation="delete",
        method="DELETE",
        endpoint="/pipelines/{pipeline_id}",
        description="Delete a pipeline entry. Permanently removes this record. This cannot be undone.\n\nWraps: DELETE /pipelines/{pipeline_id}",
        safety=Safety.DESTRUCTIVE,
        response=ResponseStrategy.RAW,
        toolset="pipelines",
        params=(
            Param(
                name="pipeline_id",
                annotation=int | str,
                description="The unique identifier of the pipeline",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="filter_pipelines",
        resource="pipeline",
        operation="filter",
        method="POST",
        endpoint="/pipelines/search",
        description="Filter pipelines by job, candidate, or status. Use this to match pipelines on exact field values.\n\nWraps: POST /pipelines/search",
        safety=Safety.WRITE,
        response=ResponseStrategy.SUMMARY,
        collection_key="pipelines",
        toolset="pipelines",
        params=(
            Param(
                name="filter_field",
                annotation=str,
                description='Field to filter on (e.g., "job_id", "candidate_id", "status_id")',
                location=ParamLocation.BODY,
                wire_name="field",
            ),
            Param(
                name="filter_type",
                annotation=str,
                description=(
                    "Filter operator. WARNING: 'contains' tokenizes the value and "
                    "matches ANY token, so contains='Logan Lake' also returns "
                    "Williams Lake, Slave Lake and Deer Lake, and contains='Cache "
                    "Creek' returns every Creek. For any multi-word value such as a "
                    "municipality, use 'exactly' and run one filter per value. "
                    "Options: 'contains' (reliable only for single words), "
                    "'exactly', 'is_empty', 'greater_than', 'less_than', 'between', "
                    "'geo_distance' (documented as a radius from a postal code, but "
                    "the field it applies to is unconfirmed - it was rejected on both "
                    "'city' and 'postal_code', so prefer an explicit list of values "
                    "with 'exactly')."
                ),
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
        name="list_pipeline_workflows",
        resource="pipeline",
        operation="list",
        method="GET",
        endpoint="/pipelines/workflows",
        description="List the hiring workflows configured in this account. Use this to discover which stages exist before moving a candidate, since status ids are account-specific.\n\nWraps: GET /pipelines/workflows",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="workflows",
        toolset="pipelines",
    ),
    ToolSpec(
        name="get_pipeline_workflow",
        resource="pipeline",
        operation="get",
        method="GET",
        endpoint="/pipelines/workflows/{workflow_id}",
        description="Get details of a specific pipeline workflow. Use this when you already have a pipeline id and need its detail.\n\nWraps: GET /pipelines/workflows/{workflow_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="pipelines",
        params=(
            Param(
                name="workflow_id",
                annotation=int | str,
                description="The unique identifier of the workflow",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="list_pipeline_workflow_statuses",
        resource="pipeline",
        operation="list",
        method="GET",
        endpoint="/pipelines/workflows/{workflow_id}/statuses",
        description="List the stages in a hiring workflow, with the status id for each. Use this to find the correct status id before changing a candidate's pipeline stage.\n\nWraps: GET /pipelines/workflows/{workflow_id}/statuses",
        safety=Safety.READ,
        response=ResponseStrategy.SUMMARY,
        collection_key="statuses",
        toolset="pipelines",
        params=(
            Param(
                name="workflow_id",
                annotation=int | str,
                description="The unique identifier of the workflow",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="get_pipeline_workflow_status",
        resource="pipeline",
        operation="get",
        method="GET",
        endpoint="/pipelines/workflows/{workflow_id}/statuses/{status_id}",
        description="Get details of a specific workflow status. Use this when you already have a pipeline id and need its detail.\n\nWraps: GET /pipelines/workflows/{workflow_id}/statuses/{status_id}",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="pipelines",
        params=(
            Param(
                name="workflow_id",
                annotation=int | str,
                description="The unique identifier of the workflow",
                location=ParamLocation.PATH,
            ),
            Param(
                name="status_id",
                annotation=int | str,
                description="The unique identifier of the status",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="get_pipeline_statuses",
        resource="pipeline",
        operation="get",
        method="GET",
        endpoint="/pipelines/{pipeline_id}/statuses",
        description="Retrieve the full status history of one pipeline, showing every stage the candidate passed through and when. Use this instead of the current status when you need to know whether a candidate ever reached a stage, since the pipeline's status field reflects only where they are now.\n\nWraps: GET /pipelines/{pipeline_id}/statuses",
        safety=Safety.READ,
        response=ResponseStrategy.DETAIL,
        toolset="pipelines",
        params=(
            Param(
                name="pipeline_id",
                annotation=int | str,
                description="The unique identifier of the pipeline",
                location=ParamLocation.PATH,
            ),
        ),
    ),
    ToolSpec(
        name="change_pipeline_status",
        resource="pipeline",
        operation="change",
        method="POST",
        endpoint="/pipelines/{pipeline_id}/status",
        description="Move a candidate's application to a different stage in the hiring workflow - for example to interviewing, submitted to hiring manager, offered, placed or rejected. Use this to advance or reject a candidate for a specific job.\n\nWraps: POST /pipelines/{pipeline_id}/status",
        safety=Safety.WRITE,
        response=ResponseStrategy.RAW,
        toolset="pipelines",
        params=(
            Param(
                name="pipeline_id",
                annotation=int | str,
                description="The unique identifier of the pipeline",
                location=ParamLocation.PATH,
            ),
            Param(
                name="status_id",
                annotation=int | str,
                description="The target status/stage ID",
                location=ParamLocation.BODY,
                transform=Transform.TO_INT,
            ),
            Param(
                name="notes",
                annotation=str | None,
                description="Optional notes about the status change",
                location=ParamLocation.BODY,
                default=None,
            ),
        ),
    ),
]
