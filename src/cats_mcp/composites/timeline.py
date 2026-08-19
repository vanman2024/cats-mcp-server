"""One merged, source-labelled candidate history.

CATS keeps what happened to a candidate in five different places. An activity
is under `/candidates/{id}/activities`, the fact that they went onto a job is a
row under `/candidates/{id}/pipelines`, how that application moved through the
stages is `/pipelines/{pipeline_id}/statuses`, follow-ups are
`/candidates/{id}/tasks`, and when the record itself last changed is a field on
`/candidates/{id}`. Nothing in the API joins them.

So "what has happened with this person" is currently four to five calls per
candidate, each returning a differently shaped collection with a differently
named date field, and the caller reassembles the order by hand. Done for a
batch it is also the fastest way to spend an hourly budget: four sources over
fifty candidates is 200 requests against a 500/hour allowance.

`get_candidate_timeline` does the join here and returns one ordered table.

Two properties matter more than anything else about it:

  * **Every row says where it came from.** `source` names the collection,
    `type` is what CATS called the event inside it, and `date_field` names the
    field the date was read from. A merged row that cannot be traced back to
    the record it came from is not auditable, and an unauditable history is
    worse than four separate calls, because it looks authoritative.

  * **Nothing is dropped for being awkward.** A row whose date is missing or
    unparseable is returned, sorted last, with a null date and a count in
    `undated`. Silently losing an event is the single worst thing a history
    can do - the caller has no way to notice the gap.

Cost model, and why the sources are ordered the way they are:

    pipelines        one request per candidate. Read first, because
                     pipeline_status needs the pipeline ids it produces.
    pipeline_status  one request per *pipeline*. The only source whose cost is
                     not per candidate: one person can hold several pipelines,
                     so this is the one that overruns a budget. Read last.
    activity/task/   one request per candidate each.
    record

CATS API limits this design cannot work around, and does not hide:

  * There is no interview resource. An interview is either an activity whose
    type says so or a move into whatever stage the account uses for one, so it
    arrives under `activity` or `pipeline_status` with its raw type intact.
  * `status_id` on a pipeline is the *current* status only. Stage history needs
    the per-pipeline call, which is why it is opt-in rather than default.
  * `/events` is account-wide and cannot be filtered to a candidate, so record
    modifications are reported from the candidate record's own timestamps -
    when it last changed, not which field changed.
  * One page per source per candidate. A candidate with more history than that
    is reported in `incomplete`, never silently shortened.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from cats_mcp.composites.reads import (
    MAX_BATCH,
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _has_next_page,
    _parse_iso,
    _pipeline_rows,
    _project,
    _status_titles,
)
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.responses.shaping import SUMMARY_FIELDS

logger = get_logger(__name__)

#: Rows per source page. CATS honours this, so one request covers a candidate's
#: hundred most recent activities rather than twenty-five at the default.
SOURCE_PAGE_SIZE = 100

#: Default and hard ceiling on CATS requests for one call.
#:
#: The budget is the point. Every per-candidate source costs one request per
#: candidate, so the naive shape of this tool is sources x candidates: four
#: sources over fifty candidates is 200 requests against a 500/hour allowance,
#: most of an hour spent inside a single tool call. pipeline_status is worse
#: again, being one request per pipeline rather than per candidate. The ceiling
#: stops that, and the response names the sources it could not afford instead
#: of presenting a short history as a complete one.
DEFAULT_MAX_REQUESTS = 40
MAX_REQUESTS_CEILING = 200

#: Default and hard ceiling on rows returned from one call.
DEFAULT_MAX_ROWS = 200
MAX_ROWS_CEILING = 1000

#: What `sources` accepts - the event types this tool can merge - and what each
#: costs. The asymmetry is the thing to read: four of the five scale with the
#: number of people, and pipeline_status scales with the number of applications.
SOURCE_OPTIONS: dict[str, str] = {
    "activity": (
        "logged calls, emails, notes and other recorded touches - one request per candidate"
    ),
    "application": (
        "each pipeline the candidate holds, dated when they went onto that job - "
        "one request per candidate"
    ),
    "pipeline_status": (
        "stage history for each of those pipelines - one request per PIPELINE, not per "
        "candidate, and it implies the pipeline read that finds the ids"
    ),
    "task": "tasks filed against the candidate - one request per candidate",
    "record": (
        "the candidate record's own created and modified timestamps - one request per candidate"
    ),
}

#: Read unless the caller says otherwise: the three whose cost is exactly one
#: request per candidate, so the bill for a default call is 3 x candidates and
#: nothing else. pipeline_status is opt-in because its cost depends on how many
#: jobs each person has applied to, which the caller cannot predict; `record`
#: is opt-in because it yields only two timestamps per person.
DEFAULT_SOURCES: tuple[str, ...] = ("activity", "application", "task")

#: Sources whose rows come from a per-candidate collection read.
_PER_CANDIDATE_SOURCES = frozenset({"activity", "task", "record"})


def _event(
    candidate_id: Any,
    source: str,
    event_type: Any,
    date_value: Any,
    date_field: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build one timeline row, always labelled with where it came from.

    `source` is the CATS collection, `type` is what CATS called the event
    inside it, and `date_field` is the field the date was read from - sources
    disagree about that (`date_created`, `due_date`, `date_modified`), and a
    merged row that does not say which one it used cannot be checked against
    the underlying record.

    `event_id` is the id of the *source row*, not of the candidate. A
    sub-collection row in CATS carries its own id, and reading one as if it
    were the parent's resolves to an unrelated real record.

    `date` is null unless the value actually parsed, so a reader can trust that
    a non-null date is a real point in time. An unparseable value is not thrown
    away - it is kept verbatim in `date_raw`, which is what makes a bad
    timestamp a thing you can go and fix rather than a row that vanished.

    `_at` is the parsed timestamp used for ordering. It is stripped before the
    result is returned; None means the date could not be read, which makes the
    row undated rather than absent.
    """
    parsed = _parse_iso(date_value)
    row: dict[str, Any] = {
        "candidate_id": candidate_id,
        "source": source,
        "type": event_type,
        "date": date_value if parsed is not None else None,
        "date_field": date_field,
    }
    if parsed is None and date_value is not None:
        row["date_raw"] = date_value
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    row["_at"] = parsed
    return row


def _status_change_row(row: dict[str, Any], titles: dict[str, str]) -> dict[str, Any]:
    """Pull a stage change out of a `/pipelines/{id}/statuses` row.

    The shape varies by account age: some rows carry `status_id`, others record
    the move as `from_status_id`/`to_status_id`, and the date is `date_created`
    on newer rows and `date_modified` on older ones. All of them are checked
    because picking one and returning a null date for the rest would push real
    stage changes into the undated bucket for no reason.
    """
    status_id = row.get("status_id")
    if status_id is None:
        status_id = row.get("to_status_id")

    date_field = "date_created" if row.get("date_created") else "date_modified"
    fields: dict[str, Any] = {
        "event_id": row.get("id"),
        "status_id": status_id,
        "from_status_id": row.get("from_status_id"),
    }
    if status_id is not None:
        fields["status"] = titles.get(str(status_id)) or None
    if row.get("from_status_id") is not None:
        fields["from_status"] = titles.get(str(row["from_status_id"])) or None
    return {"date_value": row.get(date_field), "date_field": date_field, "fields": fields}


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the timeline primitive. Returns how many tools were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="get_candidate_timeline",
        description=(
            "Retrieve one merged, chronologically ordered history for a batch of "
            "candidates, assembled from the CATS collections that otherwise have to be "
            "read and stitched together one at a time: logged activities, pipeline "
            "entries, pipeline stage history, tasks, and the candidate record's own "
            "timestamps.\n\n"
            "Every row says where it came from. `source` names the collection, `type` is "
            "what CATS called the event inside it, and `date_field` names the field the "
            "date was read from, so any row can be traced back to the record behind it.\n\n"
            "Rows are ordered oldest first. A row whose date is missing or unreadable is "
            "still returned - sorted last, with a null date, the original value kept "
            "verbatim in `date_raw`, and a count in `undated`. Losing an event from a "
            "history is a worse failure than returning one out of order, so nothing is "
            "dropped for being awkward.\n\n"
            "Costs one CATS request per candidate per source, so the bill is candidates x "
            "sources; 'pipeline_status' instead costs one request per pipeline, and one "
            "person can hold several. max_requests bounds that, and `sources_skipped` "
            "reports what the budget could not reach rather than passing a short history "
            "off as a complete one.\n\n"
            "CATS has no interview resource. An interview arrives here either as an "
            "activity or as a move into whatever stage the account uses for one, with its "
            "raw type preserved.\n\n"
            "Returns what happened and when. Interpreting them is the caller's job."
        ),
        tags={"ats", "candidate", "activity", "read", "batch"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def get_candidate_timeline(
        candidate_ids: Annotated[
            list[int | str],
            Field(
                description=(
                    f"Candidate ids to build a history for. Maximum {MAX_BATCH} per call. "
                    f"Cost is one request per candidate per source, so a large batch and "
                    f"a small max_requests will return a partial answer that says so."
                )
            ),
        ],
        since: Annotated[
            str | None,
            Field(
                description=(
                    "Keep only events at or after this ISO 8601 date or timestamp, e.g. "
                    "'2026-01-01' or '2026-01-05T09:00:00-00:00'. A bare date is midnight "
                    "UTC. Omit for no lower bound."
                )
            ),
        ] = None,
        until: Annotated[
            str | None,
            Field(
                description=(
                    "Keep only events at or before this ISO 8601 date or timestamp. A bare "
                    "date is midnight UTC, so pass the following day to cover a whole day. "
                    "Omit for no upper bound."
                )
            ),
        ] = None,
        sources: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Event types to merge. "
                    + "; ".join(f"'{k}': {v}" for k, v in SOURCE_OPTIONS.items())
                    + f". Defaults to {list(DEFAULT_SOURCES)}, the three whose cost is "
                    f"exactly one request per candidate."
                )
            ),
        ] = None,
        max_rows: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_ROWS_CEILING,
                description=(
                    f"Rows to return. Ceiling {MAX_ROWS_CEILING}. When it bites, the most "
                    f"recent events are kept and `omitted_by_row_cap` says how many were "
                    f"left out."
                ),
            ),
        ] = DEFAULT_MAX_ROWS,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}. "
                    f"Reading stops here and `sources_skipped` names what was not reached."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
    ) -> dict[str, Any]:
        set_run_id()
        client = client_getter()

        deduplicated = _dedupe(candidate_ids)
        ids = deduplicated[:MAX_BATCH]

        # `sources is None` is "you choose"; `sources=[]` is a call that would
        # read nothing at all and return an empty history that looks like a
        # candidate with no past. Say so instead.
        requested = list(DEFAULT_SOURCES) if sources is None else sources
        wanted = [s.strip().lower() for s in requested if s.strip()]
        unknown = [s for s in wanted if s not in SOURCE_OPTIONS]
        if unknown:
            raise ValueError(
                f"Unknown sources {unknown}. Valid options: {sorted(SOURCE_OPTIONS)}."
            )
        if not wanted:
            raise ValueError(
                "At least one source is required, otherwise there is nothing to read and "
                f"an empty result would be indistinguishable from a candidate with no "
                f"history. Valid options: {sorted(SOURCE_OPTIONS)}."
            )

        # An unreadable bound is raised rather than ignored: a window silently
        # dropped returns the whole history, which reads as "everything happened
        # inside your dates" and is a wrong answer, not a missing one.
        window_start = _parse_iso(since) if since else None
        window_end = _parse_iso(until) if until else None
        for label, raw, parsed in (("since", since, window_start), ("until", until, window_end)):
            if raw and parsed is None:
                raise ValueError(
                    f"{label}={raw!r} is not an ISO 8601 date or timestamp. Use "
                    f"'2026-01-01' or '2026-01-05T09:00:00-00:00'."
                )

        errors: dict[str, str] = {}
        skipped: dict[str, str] = {}
        incomplete: list[dict[str, Any]] = []
        requests_used = 0
        source_requests: dict[str, int] = {}
        events: list[dict[str, Any]] = []

        async def gather(
            source: str, suffix: str, *, paged: bool = True
        ) -> dict[str, Any]:
            """Fetch one per-candidate collection, within whatever budget is left."""
            nonlocal requests_used
            covered = ids[: max(0, max_requests - requests_used)]
            if not covered:
                skipped[source] = (
                    f"request budget of {max_requests} was spent before this source was "
                    f"read; raise max_requests or ask for fewer sources"
                )
                logger.warning("timeline budget exhausted before source %s", source)
                return {}
            if len(covered) < len(ids):
                errors[source] = (
                    f"budget covered {len(covered)} of {len(ids)} candidates; the rest "
                    f"were not read for this source"
                )

            params = {"per_page": SOURCE_PAGE_SIZE} if paged else None

            async def fetch(cid: int | str) -> Any:
                return await client.request("GET", f"/candidates/{cid}{suffix}", params=params)

            payloads, fetch_errors = await _gather_by_id(covered, fetch)
            errors.update({f"{source}:{cid}": msg for cid, msg in fetch_errors.items()})
            requests_used += len(covered)
            source_requests[source] = source_requests.get(source, 0) + len(covered)
            return payloads

        def note_page_limit(candidate_id: Any, source: str, payload: Any) -> None:
            """Record that a candidate has more of this source than one page holds."""
            if _has_next_page(payload):
                incomplete.append(
                    {
                        "candidate_id": candidate_id,
                        "source": source,
                        "reason": (
                            f"more than {SOURCE_PAGE_SIZE} rows exist; only the most "
                            f"recent page was read"
                        ),
                    }
                )

        # --- pipelines first: pipeline_status needs the ids they carry -------
        needs_pipelines = "application" in wanted or "pipeline_status" in wanted
        titles: dict[str, str] = {}
        # Keyed by str(pipeline id) because that is what _gather_by_id keys its
        # results by; the original id is carried alongside so a stage-change row
        # and the application row it belongs to report the same pipeline_id in
        # the same type, which is what lets a caller join them.
        pipeline_owner: dict[str, dict[str, Any]] = {}

        if needs_pipelines and ids:
            # Status ids are account-specific; "6377104" does not say Placed.
            # A failure here loses the labels, not the call - the ids are still
            # returned, just unlabelled.
            titles, status_calls = await _status_titles(client)
            requests_used += status_calls
            source_requests["status_titles"] = status_calls

            payloads = await gather("pipelines", "/pipelines")
            for cid, payload in payloads.items():
                note_page_limit(cid, "pipelines", payload)
                projected = {str(p.get("id")): p for p in _pipeline_rows(payload, titles)}
                for raw in _embedded_rows(payload):
                    pipeline_id = raw.get("id")
                    detail = projected.get(str(pipeline_id), {})
                    if pipeline_id is not None:
                        pipeline_owner[str(pipeline_id)] = {
                            "candidate_id": cid,
                            "pipeline_id": pipeline_id,
                        }
                    if "application" not in wanted:
                        continue
                    events.append(
                        _event(
                            cid,
                            "application",
                            "application",
                            raw.get("date_created"),
                            "date_created",
                            event_id=pipeline_id,
                            pipeline_id=pipeline_id,
                            job_id=detail.get("job_id"),
                            status_id=detail.get("status_id"),
                            status=detail.get("status"),
                        )
                    )

        # --- activities -------------------------------------------------------
        if "activity" in wanted:
            for cid, payload in (await gather("activity", "/activities")).items():
                note_page_limit(cid, "activity", payload)
                for raw in _embedded_rows(payload):
                    row = _project(raw, SUMMARY_FIELDS["activity"])
                    events.append(
                        _event(
                            cid,
                            "activity",
                            row.get("type"),
                            row.get("date_created"),
                            "date_created",
                            event_id=row.get("id"),
                            summary=row.get("notes"),
                            regarding_id=row.get("regarding_id"),
                        )
                    )

        # --- tasks -------------------------------------------------------------
        if "task" in wanted:
            for cid, payload in (await gather("task", "/tasks")).items():
                note_page_limit(cid, "task", payload)
                for raw in _embedded_rows(payload):
                    row = _project(raw, SUMMARY_FIELDS["task"])
                    # A task carries two dates and CATS does not always send
                    # both. date_created is when it happened; due_date is when
                    # it is meant to. date_field says which one this row used
                    # rather than leaving the reader to guess.
                    dated = raw.get("date_created") or row.get("due_date")
                    field = "date_created" if raw.get("date_created") else "due_date"
                    events.append(
                        _event(
                            cid,
                            "task",
                            "task",
                            dated,
                            field,
                            event_id=row.get("id"),
                            summary=row.get("title"),
                            is_completed=row.get("is_completed"),
                            due_date=row.get("due_date"),
                        )
                    )

        # --- the candidate record's own timestamps ----------------------------
        if "record" in wanted:
            for cid, record in (await gather("record", "", paged=False)).items():
                if not isinstance(record, dict):
                    continue
                # Key present is the test, not truthiness. A record carrying
                # date_modified=null is telling you it has never been touched,
                # which is an event worth surfacing as undated; a record with no
                # such key is telling you nothing and gets no row.
                for field, event_type in (
                    ("date_created", "record_created"),
                    ("date_modified", "record_modified"),
                ):
                    if field in record:
                        events.append(
                            _event(cid, "record", event_type, record.get(field), field)
                        )

        # --- stage history last: the one source billed per pipeline -----------
        if "pipeline_status" in wanted:
            pipeline_ids = list(pipeline_owner)
            covered = pipeline_ids[: max(0, max_requests - requests_used)]
            if pipeline_ids and not covered:
                skipped["pipeline_status"] = (
                    f"{len(pipeline_ids)} pipelines were found but the request budget of "
                    f"{max_requests} was already spent; stage history costs one request "
                    f"per pipeline"
                )
            elif len(covered) < len(pipeline_ids):
                errors["pipeline_status"] = (
                    f"budget covered {len(covered)} of {len(pipeline_ids)} pipelines; the "
                    f"rest have no stage history in this result"
                )

            async def fetch_statuses(pipeline_id: int | str) -> Any:
                return await client.request(
                    "GET",
                    f"/pipelines/{pipeline_id}/statuses",
                    params={"per_page": SOURCE_PAGE_SIZE},
                )

            if covered:
                payloads, fetch_errors = await _gather_by_id(covered, fetch_statuses)
                errors.update(
                    {f"pipeline_status:{pid}": msg for pid, msg in fetch_errors.items()}
                )
                requests_used += len(covered)
                source_requests["pipeline_status"] = len(covered)

                for key, payload in payloads.items():
                    owner = pipeline_owner.get(str(key), {})
                    cid = owner.get("candidate_id")
                    note_page_limit(cid, "pipeline_status", payload)
                    for raw in _embedded_rows(payload):
                        parsed = _status_change_row(raw, titles)
                        events.append(
                            _event(
                                cid,
                                "pipeline_status",
                                "status_change",
                                parsed["date_value"],
                                parsed["date_field"],
                                pipeline_id=owner.get("pipeline_id"),
                                **parsed["fields"],
                            )
                        )

        # --- order, window, cap ------------------------------------------------
        undated = [e for e in events if e["_at"] is None]
        dated = sorted((e for e in events if e["_at"] is not None), key=lambda e: e["_at"])

        in_window = [
            e
            for e in dated
            if (window_start is None or e["_at"] >= window_start)
            and (window_end is None or e["_at"] <= window_end)
        ]
        out_of_window = len(dated) - len(in_window)

        # Undated rows are placed before the dated ones are trimmed, so the row
        # cap never silently eats the rows this tool is least able to reconstruct
        # later. They are rare; a caller who sees `undated` climb has a data
        # problem worth knowing about, not a display problem.
        kept_undated = undated[:max_rows]
        room = max(0, max_rows - len(kept_undated))
        kept_dated = in_window[-room:] if room else []
        omitted = (len(in_window) - len(kept_dated)) + (len(undated) - len(kept_undated))

        rows = [{k: v for k, v in e.items() if k != "_at"} for e in kept_dated + kept_undated]

        return {
            "timeline": rows,
            "count": len(rows),
            "candidate_ids": ids,
            "requested": len(candidate_ids),
            "candidate_ids_truncated": len(deduplicated) > MAX_BATCH,
            "sources": wanted,
            "source_requests": source_requests,
            "sources_skipped": skipped,
            "incomplete": incomplete,
            "window": {"since": since, "until": until},
            "out_of_window": out_of_window,
            "undated": len(undated),
            "omitted_by_row_cap": omitted,
            "row_cap_reached": omitted > 0,
            "errors": errors,
            "requests_used": requests_used,
            "rate_limit": client.rate_limit.snapshot(),
            "note": (
                "Ordered oldest first. Every row carries source, type and date_field so it "
                "can be traced back to the CATS collection it came from. Rows whose date "
                "could not be read are last with a null date, the original in `date_raw`, "
                "and a count in `undated`; they are returned whatever the window, because "
                "losing one silently is the worst thing a history can do. A non-empty "
                "`sources_skipped` means the budget "
                "stopped before that source was read, not that it holds nothing."
            ),
        }

    return 1
