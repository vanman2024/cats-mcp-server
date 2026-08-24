"""Custom field values, joined with the names CATS never sends alongside them.

`GET /candidates/{id}/custom_fields` - and its job equivalent - answers each
row with only `{"id": ..., "value": ...}` (confirmed against the documented
schema: https://docs.catsone.com/api/v3/#candidates). The id is account-
specific and meaningless on its own: nothing about "359950" says LinkedIn
Messaging Stage. The name lives only in the separate definitions endpoint,
`GET /candidates/custom_fields`, which nothing joins automatically - so a
caller either makes two calls and matches them up by hand, or reads the
account's values as a wall of unlabelled ids.

These two tools do that join, using `CustomFieldResolver` (definitions are
account configuration, cached per account for ten minutes - see
http/custom_fields.py) so the cost is one extra request per call, not one
per field.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field

from cats_mcp.composites.models import ExecutionFacts, RateLimit
from cats_mcp.composites.reads import CONCURRENCY, MAX_BATCH, _dedupe, _gather_by_id
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.custom_fields import CustomFieldResolver

logger = get_logger(__name__)


class CustomFieldValue(BaseModel):
    """One field, resolved."""

    id: str = Field(description="The custom field definition id.")
    name: str = Field(description="The field's label, e.g. 'LinkedIn Messaging Stage'.")
    type: str = Field(description="The field's type, e.g. 'dropdown' or 'checkboxes'.")
    value: Any = Field(description="The value stored on this record, exactly as CATS sent it.")
    resolved: bool = Field(
        description="False when this id was not found in the account's current "
        "definitions - the field may have been deleted since the value was set. "
        "name and type are empty in that case; value is still reported."
    )


class RecordCustomFields(BaseModel):
    """One record's custom field values, all resolved."""

    record_id: int | str = Field(description="The candidate or job id these fields belong to.")
    fields: list[CustomFieldValue] = Field(default_factory=list)
    error: str | None = Field(
        default=None, description="Set instead of fields when this record could not be read."
    )


class CustomFieldValuesResult(BaseModel):
    """Custom field values for a batch of records, joined with their names."""

    records: list[RecordCustomFields]
    execution: ExecutionFacts


async def _values_for(
    client_getter: Callable[[], Any],
    resolver: CustomFieldResolver,
    resource: str,
    ids: list[int | str],
    context: Any,
) -> CustomFieldValuesResult:
    client = client_getter()
    deduped = _dedupe(ids)
    ids = deduped[:MAX_BATCH]
    truncated = len(deduped) > len(ids)

    definitions, def_requests = await resolver.resolve(resource, context)
    requests_used = def_requests

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def fetch(record_id: int | str):
        async with semaphore:
            return await client.request(
                "GET",
                f"/{resource}/{record_id}/custom_fields",
                params={"per_page": 100},
            )

    found, errors = await _gather_by_id(ids, fetch)
    requests_used += len(ids)

    records: list[RecordCustomFields] = []
    for record_id in ids:
        key = str(record_id)
        if key in errors:
            records.append(RecordCustomFields(record_id=record_id, error=errors[key]))
            continue
        payload = found.get(key) or {}
        rows = ((payload or {}).get("_embedded") or {}).get("custom_fields") or []
        fields = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            field_id = str(row["id"])
            definition = definitions.get(field_id)
            fields.append(
                CustomFieldValue(
                    id=field_id,
                    name=(definition or {}).get("name", ""),
                    type=(definition or {}).get("type", ""),
                    value=row.get("value"),
                    resolved=definition is not None,
                )
            )
        records.append(RecordCustomFields(record_id=record_id, fields=fields))

    return CustomFieldValuesResult(
        records=records,
        execution=ExecutionFacts(
            requests_used=requests_used,
            rate_limit=RateLimit(**client.rate_limit.snapshot()),
            truncated=truncated,
            errors=errors,
        ),
    )


def register(
    mcp: Any,
    client_getter: Callable[[], Any],
    resolver: CustomFieldResolver,
    *,
    enforce_auth: bool,
) -> int:
    """Register the custom-field-value composites. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    read_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }

    @mcp.tool(
        name="get_candidate_custom_field_values",
        output_schema=CustomFieldValuesResult.model_json_schema(),
        description=(
            "Read a batch of candidates' custom field values, each labelled with its "
            "field name and type - not the bare {id, value} CATS actually returns. Use "
            "this instead of list_candidate_custom_fields when the field names matter, "
            "which is nearly always: an id on its own does not say whether it holds "
            f"'Has Red Seal' or 'Mining Experience'. Up to {MAX_BATCH} candidates per call.\n\n"
            "resolved=false on a field means its id was not found in the account's "
            "current definitions - likely deleted since the value was set. The value is "
            "still reported; name and type are empty."
        ),
        tags={"ats", "candidate", "read", "custom-fields"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_candidate_custom_field_values(
        candidate_ids: Annotated[
            list[int | str], Field(description=f"Candidate ids, up to {MAX_BATCH}.")
        ],
    ) -> ToolResult:
        set_run_id()
        result = await _values_for(
            client_getter, resolver, "candidates", candidate_ids, None
        )
        summary = (
            f"{len(result.records)} candidate(s), "
            f"{sum(len(r.fields) for r in result.records)} field(s) resolved."
        )
        return ToolResult(content=summary, structured_content=result.model_dump())

    @mcp.tool(
        name="get_job_custom_field_values",
        output_schema=CustomFieldValuesResult.model_json_schema(),
        description=(
            "Read a batch of jobs' custom field values, each labelled with its field "
            "name and type - not the bare {id, value} CATS actually returns. Same "
            f"reasoning as get_candidate_custom_field_values. Up to {MAX_BATCH} jobs "
            "per call."
        ),
        tags={"ats", "job", "read", "custom-fields"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_job_custom_field_values(
        job_ids: Annotated[list[int | str], Field(description=f"Job ids, up to {MAX_BATCH}.")],
    ) -> ToolResult:
        set_run_id()
        result = await _values_for(client_getter, resolver, "jobs", job_ids, None)
        summary = (
            f"{len(result.records)} job(s), "
            f"{sum(len(r.fields) for r in result.records)} field(s) resolved."
        )
        return ToolResult(content=summary, structured_content=result.model_dump())

    return 2
