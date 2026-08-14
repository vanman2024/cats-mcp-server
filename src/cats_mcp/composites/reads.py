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

#: Ids per call when only list membership is asked for.
#:
#: Screening is the one thing whose cost does not scale with the number of
#: people: CATS has no candidate -> lists lookup, so membership is answered by
#: fetching each list once and intersecting in memory. 500 candidates against a
#: 299-member list is three requests and a set operation.
#:
#: Higher than MAX_BATCH on purpose - capping a screen at 50 would force the
#: caller into the per-candidate loop this exists to prevent.
MAX_SCREEN = 500

#: What `include` accepts, and what each costs.
#:
#: The asymmetry is the whole design. Screening is nearly free and deep review
#: is not, so a caller who screens first spends real requests only on the people
#: who survive it.
INCLUDE_OPTIONS: dict[str, str] = {
    "identity": "name, title, city, state and current employer - one request per candidate",
    "custom_fields": "account-specific fields such as certifications - free alongside identity",
    "lists": "saved-list membership for the list_ids given - one request per list, not per person",
    "pipelines": "job applications and their current stage - one request per candidate",
}

#: The `include` values that cost a request per candidate.
PER_CANDIDATE_INCLUDES = frozenset({"identity", "custom_fields", "pipelines"})


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


#: Page size when sweeping a saved list. CATS honours this, so a 299-member list
#: is three requests rather than twelve at the default of 25.
LIST_PAGE_SIZE = 100

#: Pages per list. A stop, so a pathological list cannot consume a whole budget.
MAX_LIST_PAGES = 20


async def _list_memberships(
    client: Any, list_ids: list[int | str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], int]:
    """Map candidate id -> the given lists they belong to.

    CATS has no candidate -> lists lookup; it answers only "who is on this
    list". So membership is resolved by sweeping each list once and inverting
    it, which is why screening costs one pass per *list* rather than one request
    per person. Checking 500 candidates against a 299-member list is three
    requests and a dictionary lookup.

    Each row's `id` is the membership row and the person is in `candidate_id` -
    reading the wrong one silently produces a different person, which on a Do
    Not Contact list means clearing someone who should not be contacted.
    """
    membership: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    requests_used = 0

    for list_id in _dedupe(list_ids):
        try:
            detail = await client.request("GET", f"/candidates/lists/{list_id}")
            requests_used += 1
            name = detail.get("name") if isinstance(detail, dict) else None
        except CATSAPIError as exc:
            errors[f"list:{list_id}"] = str(exc)
            continue

        page = 1
        while page <= MAX_LIST_PAGES:
            try:
                payload = await client.request(
                    "GET",
                    f"/candidates/lists/{list_id}/items",
                    params={"per_page": LIST_PAGE_SIZE, "page": page},
                )
            except CATSAPIError as exc:
                errors[f"list:{list_id}:page:{page}"] = str(exc)
                break
            requests_used += 1

            rows = _embedded_rows(payload)
            for row in rows:
                candidate_id = row.get("candidate_id")
                if candidate_id is None:
                    continue
                membership.setdefault(str(candidate_id), []).append(
                    {"id": list_id, "name": name}
                )

            if not _has_next_page(payload) or not rows:
                break
            page += 1

    return membership, errors, requests_used


def _embedded_rows(payload: Any) -> list[dict[str, Any]]:
    """Rows out of a HAL collection, whatever the embedded key is called."""
    if not isinstance(payload, dict):
        return []
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    for value in embedded.values():
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _has_next_page(payload: Any) -> bool:
    links = payload.get("_links") if isinstance(payload, dict) else None
    return isinstance(links, dict) and "next" in links


async def _status_titles(client: Any) -> tuple[dict[str, str], int]:
    """Map pipeline status id -> its human title.

    A status id is account-specific and means nothing to a reader: "6377104"
    does not say Placed. Resolving it is normalization, which is the adapter's
    job, and it costs one request that the reference-data cache serves for ten
    minutes afterwards.

    A failure here is not worth failing the call over - the ids are still
    returned, just unlabelled.
    """
    try:
        payload = await client.request("GET", "/pipelines/workflows")
    except CATSAPIError as exc:
        logger.warning("could not resolve pipeline status titles: %s", exc)
        return {}, 1

    titles: dict[str, str] = {}
    for workflow in _embedded_rows(payload):
        for status in workflow.get("statuses") or []:
            if isinstance(status, dict) and status.get("id") is not None:
                titles[str(status["id"])] = status.get("title") or status.get("name") or ""
    return titles, 1


def _pipeline_rows(payload: Any, titles: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for row in _embedded_rows(payload):
        entry = _project(row, ["id", "job_id", "status_id", "rating", "date_modified"])
        status_id = entry.get("status_id")
        if status_id is not None:
            title = titles.get(str(status_id))
            if title:
                entry["status"] = title
        rows.append(entry)
    return rows


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

    @mcp.tool(
        name="get_candidate_context",
        description=(
            "Retrieve a compact, factual view of many candidates in one call: identity, "
            "saved-list membership, and current pipeline stages.\n\n"
            "Use this to screen a set of candidates before spending requests on any of "
            "them. Screening on list membership alone costs one request per list rather "
            "than one per person, so several hundred candidates can be checked against a "
            "Do Not Contact list in about three requests.\n\n"
            "Returns facts, not verdicts. It reports which lists someone is on and what "
            "stage their applications are at; deciding what that means is the caller's.\n\n"
            "Recommended order: screen the whole set with include=['lists'], discard "
            "whoever your rules exclude, then call again with include=['identity',"
            "'pipelines'] for the survivors only."
        ),
        tags={"ats", "candidate", "read", "batch", "search", "screening"},
        annotations=read_annotations,
        **tool_kwargs,
    )
    async def get_candidate_context(
        candidate_ids: Annotated[
            list[int | str],
            Field(
                description=(
                    f"Candidate ids. Up to {MAX_SCREEN} when only 'lists' is requested, "
                    f"{MAX_BATCH} when any per-candidate data is requested."
                )
            ),
        ],
        include: Annotated[
            list[str] | None,
            Field(
                description=(
                    "What to retrieve. "
                    + "; ".join(f"'{k}': {v}" for k, v in INCLUDE_OPTIONS.items())
                    + ". Defaults to ['lists'], the cheap screen."
                )
            ),
        ] = None,
        list_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Saved lists to check membership against, required when 'lists' is "
                    "included. Find ids with list_candidate_lists. These are the lists "
                    "your policy cares about - a Do Not Contact list, for example."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        set_run_id()
        client = client_getter()
        wanted = [i.strip().lower() for i in (include or ["lists"]) if i.strip()]

        unknown = [i for i in wanted if i not in INCLUDE_OPTIONS]
        if unknown:
            raise ValueError(
                f"Unknown include values {unknown}. Valid options: "
                f"{sorted(INCLUDE_OPTIONS)}."
            )

        per_candidate = bool(set(wanted) & PER_CANDIDATE_INCLUDES)
        ceiling = MAX_BATCH if per_candidate else MAX_SCREEN
        ids = _dedupe(candidate_ids)[:ceiling]

        if "lists" in wanted and not list_ids:
            raise ValueError(
                "include=['lists'] needs list_ids. Call list_candidate_lists to find "
                "the ids of the lists your policy cares about, then pass them here."
            )

        if per_candidate and len(_dedupe(candidate_ids)) > MAX_BATCH:
            raise ValueError(
                f"{len(_dedupe(candidate_ids))} candidates is too many for "
                f"{sorted(set(wanted) & PER_CANDIDATE_INCLUDES)}, which costs one "
                f"request each. Screen first with include=['lists'] - that costs one "
                f"request per list regardless of how many people you check - then call "
                f"again with at most {MAX_BATCH} survivors."
            )

        context: dict[str, dict[str, Any]] = {str(i): {"candidate_id": i} for i in ids}
        errors: dict[str, str] = {}
        requests_used = 0

        # --- membership: one pass per list, not per candidate ---------------
        if "lists" in wanted:
            memberships, list_errors, used = await _list_memberships(client, list_ids or [])
            errors.update(list_errors)
            requests_used += used
            for key, row in context.items():
                row["lists"] = memberships.get(key, [])

        # --- per-candidate record -------------------------------------------
        if "identity" in wanted or "custom_fields" in wanted:
            fields = ["id", "first_name", "last_name", "title", "city", "state"]
            fields += ["current_employer", "date_modified"]
            if "custom_fields" in wanted:
                fields.append("custom_fields")

            async def fetch_record(cid):
                return await client.request("GET", f"/candidates/{cid}")

            records, record_errors = await _gather_by_id(ids, fetch_record)
            errors.update(record_errors)
            requests_used += len(ids)
            for key, record in records.items():
                context[key].update(_project(record, fields))

        # --- pipelines, with status ids resolved to names --------------------
        if "pipelines" in wanted:
            statuses, status_calls = await _status_titles(client)
            requests_used += status_calls

            async def fetch_pipelines(cid):
                return await client.request("GET", f"/candidates/{cid}/pipelines")

            found, pipeline_errors = await _gather_by_id(ids, fetch_pipelines)
            errors.update(pipeline_errors)
            requests_used += len(ids)
            for key, payload in found.items():
                context[key]["pipelines"] = _pipeline_rows(payload, statuses)

        return {
            "candidates": list(context.values()),
            "count": len(context),
            "requested": len(candidate_ids),
            "truncated": len(_dedupe(candidate_ids)) > ceiling,
            "included": wanted,
            "errors": errors,
            "requests_used": requests_used,
            "rate_limit": client.rate_limit.snapshot(),
        }

    return 6
