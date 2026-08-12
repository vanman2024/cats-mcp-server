"""Composite read primitives.

A small number of tools that answer a question requiring several CATS calls, so
that the intermediate records never pass through model context.

Strictly data access. These tools do not decide who should be contacted, rank
candidates, launch campaigns, or schedule anything - that is the orchestrator's
job. Each returns facts and lets the caller decide.

Why they exist: the CATS standard rate limit is 500 requests/hour. "Find
candidates in Kamloops, then check when each was last contacted" is one search
plus N activity calls. For 50 candidates that is 51 requests - ten minutes of
budget - and, done naively, 50 full activity lists through the model's context.
These tools make the same work one tool call returning one compact table.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError
from cats_mcp.responses.shaping import SUMMARY_FIELDS

logger = get_logger(__name__)

#: Hard ceiling on ids per call. Each id costs at least one CATS request, and an
#: unbounded batch could consume an entire hourly budget in a single tool call.
MAX_BATCH = 50

#: Concurrent in-flight requests. Enough to be useful, low enough to avoid
#: tripping the rate limiter on the caller's behalf.
CONCURRENCY = 5


async def _gather_by_id(
    ids: list[int | str],
    fetch: Callable[[int | str], Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch per id concurrently, returning results and per-id errors.

    Partial failure returns partial data. Failing the whole batch because one id
    was deleted would waste every other request already spent.
    """
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    async def one(identifier: int | str) -> None:
        async with semaphore:
            try:
                results[str(identifier)] = await fetch(identifier)
            except CATSAPIError as exc:
                errors[str(identifier)] = str(exc)

    await asyncio.gather(*(one(i) for i in ids))
    return results, errors


def _project(record: Any, fields: list[str]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {k: record.get(k) for k in fields if k in record}


def _dedupe(ids: list[int | str]) -> list[int | str]:
    seen: set[str] = set()
    out: list[int | str] = []
    for i in ids:
        key = str(i)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the composite tools. Returns how many were added."""
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
        name="get_candidate_summaries",
        description=(
            "Retrieve compact profiles for a batch of candidates in one call, given their "
            "ids. Use this after a search returns ids and you need a few fields about many "
            "people at once - it avoids one tool call per candidate and returns a compact "
            "table rather than full records. Ids that no longer exist are reported "
            "individually instead of failing the batch."
        ),
        tags={"ats", "candidate", "read", "batch", "search"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_candidate_summaries(
        candidate_ids: Annotated[
            list[int | str],
            Field(description=f"Candidate ids to fetch. Maximum {MAX_BATCH} per call."),
        ],
        fields: Annotated[
            str | None,
            Field(description="Comma-separated fields to return. Defaults to a compact profile."),
        ] = None,
    ) -> dict[str, Any]:
        set_run_id()
        ids = _dedupe(candidate_ids)[:MAX_BATCH]
        client = client_getter()
        selected = (
            [f.strip() for f in fields.split(",") if f.strip()]
            if fields
            else SUMMARY_FIELDS["candidate"]
        )

        async def fetch(cid):
            return await client.request("GET", f"/candidates/{cid}")

        results, errors = await _gather_by_id(ids, fetch)
        return {
            "candidates": [_project(r, selected) for r in results.values()],
            "count": len(results),
            "requested": len(candidate_ids),
            "truncated": len(candidate_ids) > MAX_BATCH,
            "errors": errors,
            "requests_used": len(ids),
            "rate_limit": client.rate_limit.snapshot(),
        }

    @mcp.tool(
        name="get_candidate_engagement",
        description=(
            "For a batch of candidates, report when each was last contacted and how much "
            "recruiting activity they have - the most recent activity date, its type, and "
            "a total count. Use this to determine who has gone cold, who was contacted "
            "recently, or which candidates have never been approached, without pulling "
            "every activity record into context. Returns facts only; deciding who to "
            "contact is the caller's decision."
        ),
        tags={"ats", "candidate", "activity", "read", "batch"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_candidate_engagement(
        candidate_ids: Annotated[
            list[int | str],
            Field(description=f"Candidate ids to check. Maximum {MAX_BATCH} per call."),
        ],
    ) -> dict[str, Any]:
        set_run_id()
        ids = _dedupe(candidate_ids)[:MAX_BATCH]
        client = client_getter()

        async def fetch(cid):
            return await client.request(
                "GET", f"/candidates/{cid}/activities", params={"per_page": 100}
            )

        results, errors = await _gather_by_id(ids, fetch)

        rows = []
        for cid, payload in results.items():
            activities = []
            if isinstance(payload, dict):
                embedded = payload.get("_embedded") or {}
                activities = embedded.get("activities") or []
            dated = [a for a in activities if isinstance(a, dict) and a.get("date_created")]
            latest = max(dated, key=lambda a: a["date_created"], default=None)
            rows.append(
                {
                    "candidate_id": cid,
                    "activity_count": (payload or {}).get("total", len(activities)),
                    "last_activity_date": (latest or {}).get("date_created"),
                    "last_activity_type": (latest or {}).get("type"),
                    "has_been_contacted": bool(activities),
                }
            )

        rows.sort(key=lambda r: (r["last_activity_date"] or ""))
        return {
            "engagement": rows,
            "count": len(rows),
            "errors": errors,
            "requests_used": len(ids),
            "rate_limit": client.rate_limit.snapshot(),
            "note": (
                "Sorted oldest contact first. A null last_activity_date means no recorded "
                "activity at all."
            ),
        }

    @mcp.tool(
        name="get_job_candidate_pool",
        description=(
            "Retrieve the candidates currently in a job's pipeline as one compact table, "
            "combining each pipeline entry with the candidate's basic profile. Use this to "
            "review who is being considered for a role without making a separate call per "
            "candidate."
        ),
        tags={"ats", "job", "pipeline", "candidate", "read", "batch"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_job_candidate_pool(
        job_id: Annotated[int | str, Field(description="The job whose pipeline to retrieve")],
        limit: Annotated[
            int, Field(description=f"Maximum candidates to enrich. Maximum {MAX_BATCH}.")
        ] = 25,
    ) -> dict[str, Any]:
        set_run_id()
        client = client_getter()
        capped = min(limit, MAX_BATCH)

        pipelines_payload = await client.request(
            "GET", f"/jobs/{job_id}/pipelines", params={"per_page": capped}
        )
        pipelines = ((pipelines_payload or {}).get("_embedded") or {}).get("pipelines") or []

        candidate_ids = _dedupe(
            [
                p.get("candidate_id")
                for p in pipelines
                if isinstance(p, dict) and p.get("candidate_id")
            ]
        )[:capped]

        async def fetch(cid):
            return await client.request("GET", f"/candidates/{cid}")

        candidates, errors = await _gather_by_id(candidate_ids, fetch)

        rows = []
        for pipeline in pipelines:
            if not isinstance(pipeline, dict):
                continue
            cid = str(pipeline.get("candidate_id"))
            profile = _project(candidates.get(cid, {}), SUMMARY_FIELDS["candidate"])
            rows.append(
                {
                    "pipeline_id": pipeline.get("id"),
                    "candidate_id": pipeline.get("candidate_id"),
                    "status_id": pipeline.get("status_id"),
                    "rating": pipeline.get("rating"),
                    "candidate": profile,
                }
            )

        return {
            "job_id": job_id,
            "pool": rows,
            "count": len(rows),
            "total_in_pipeline": (pipelines_payload or {}).get("total"),
            "errors": errors,
            "requests_used": 1 + len(candidate_ids),
            "rate_limit": client.rate_limit.snapshot(),
            "note": (
                "status_id values are account-specific. Use list_pipeline_workflow_statuses "
                "to resolve them to stage names."
            ),
        }

    @mcp.tool(
        name="get_changed_records",
        description=(
            "Retrieve what has changed in the CATS account since a given event id or "
            "timestamp - records created, updated, deleted and status changes. Use this to "
            "stay in sync instead of re-listing whole record sets, which is far cheaper "
            "against the request budget. Returns a compact change list plus the latest "
            "event id to pass on the next call."
        ),
        tags={"ats", "event", "read", "sync"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_changed_records(
        since_event_id: Annotated[
            int | None, Field(description="Return events after this event id")
        ] = None,
        since_timestamp: Annotated[
            str | None, Field(description="Return events after this RFC 3339 timestamp")
        ] = None,
        event_types: Annotated[
            str | None,
            Field(description="Comma-separated event names to keep, e.g. 'candidate.created'"),
        ] = None,
    ) -> dict[str, Any]:
        set_run_id()
        if since_event_id is None and since_timestamp is None:
            return {
                "error": (
                    "Provide since_event_id or since_timestamp. Without a starting point "
                    "this would return the entire event history."
                )
            }

        client = client_getter()
        params: dict[str, Any] = {}
        if since_event_id is not None:
            params["starting_after_id"] = since_event_id
        if since_timestamp is not None:
            params["starting_after_timestamp"] = since_timestamp

        payload = await client.request("GET", "/events", params=params)
        events = ((payload or {}).get("_embedded") or {}).get("events") or []

        wanted = {e.strip() for e in event_types.split(",")} if event_types else None
        rows = [
            {
                "id": e.get("id"),
                "event": e.get("event"),
                "regarding_id": e.get("regarding_id"),
                "date_created": e.get("date_created"),
            }
            for e in events
            if isinstance(e, dict) and (wanted is None or e.get("event") in wanted)
        ]

        return {
            "changes": rows,
            "count": len(rows),
            "latest_event_id": max((r["id"] for r in rows if r["id"] is not None), default=None),
            "requests_used": 1,
            "rate_limit": client.rate_limit.snapshot(),
            "note": "Pass latest_event_id as since_event_id on the next call to continue.",
        }

    @mcp.tool(
        name="get_pipeline_summaries",
        description=(
            "Retrieve compact status information for a batch of pipelines by id - current "
            "stage, rating, and the candidate and job each belongs to. Use this to check "
            "the state of many submissions at once."
        ),
        tags={"ats", "pipeline", "read", "batch"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_pipeline_summaries(
        pipeline_ids: Annotated[
            list[int | str],
            Field(description=f"Pipeline ids to fetch. Maximum {MAX_BATCH} per call."),
        ],
    ) -> dict[str, Any]:
        set_run_id()
        ids = _dedupe(pipeline_ids)[:MAX_BATCH]
        client = client_getter()

        async def fetch(pid):
            return await client.request("GET", f"/pipelines/{pid}")

        results, errors = await _gather_by_id(ids, fetch)
        return {
            "pipelines": [_project(r, SUMMARY_FIELDS["pipeline"]) for r in results.values()],
            "count": len(results),
            "errors": errors,
            "requests_used": len(ids),
            "rate_limit": client.rate_limit.snapshot(),
        }

    return 5
