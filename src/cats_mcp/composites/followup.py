"""Records that satisfy a time threshold the caller chose, with the arithmetic.

Issue #12, item 3. The question behind it is always some version of "this stage
has been sitting for a day and nothing has happened since" - and every part of
that sentence belongs to the person asking. Which stage. How long a day is.
What counts as something happening. This module supplies none of those. It
takes them as arguments, applies them to CATS records, and returns the facts
the comparison was made on.

Assembling that by hand is four kinds of call and a lot of arithmetic. The
pipelines on a job are one collection; a pipeline's `status_id` is its *current*
status only, so when it entered that stage lives in a second collection one
request per pipeline away; the candidate's activity is a third, one request per
candidate; and nothing in CATS joins any of them or subtracts two timestamps.

Three properties matter more than anything else here:

  * **Every threshold is an argument.** There is no default number of hours,
    no built-in idea of which stage or activity type means an interview, and
    no notion of what to do about a record that comes back. CATS v3 has no
    interview resource at all - an interview is an activity type or a move
    into an account-named stage - so guessing would be inventing a fact.

  * **Every returned row carries what the comparison used.** The stage, the
    timestamp, the field that timestamp was read from, the elapsed hours, the
    reference instant, and the candidate's most recent activity. The whole
    comparison can be redone by hand from the row, which is the difference
    between a filter and an oracle.

  * **Nothing is dropped for being awkward.** A record whose timestamp cannot
    be read, or that the request budget never reached, comes back in
    `unevaluated` with the raw value and a reason. A record that was never
    assessed is not a record that failed the threshold, and quietly conflating
    the two is how a caller ends up certain about a set that was never
    computed.

Cost model, and why the phases are ordered the way they are:

    Phase A  the pool. One request per page of pipelines per job or candidate
             seed. `pipeline_status_ids` is applied here, for free, on rows
             already in hand - which is what keeps the paid phases off records
             that could not have matched anyway.
    Phase B  the anchor. 'stage_entered' is one request per PIPELINE, because
             stage history is its own collection. 'pipeline_modified' is free
             and approximate. 'activity' is free here and paid in Phase C.
    Phase C  activity, one request per CANDIDATE. Always spent, because every
             returned row reports its last activity date. On any realistic
             call this is the dominant cost.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastmcp.tools import ToolResult
from pydantic import BaseModel, ConfigDict, Field

from cats_mcp.composites.models import ExecutionFacts, RateLimit
from cats_mcp.composites.reads import (
    MAX_BATCH,
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _has_next_page,
    _parse_iso,
    _progress,
    _project,
    _status_titles,
)
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError
from cats_mcp.responses.shaping import SUMMARY_FIELDS

logger = get_logger(__name__)

#: Rows per page. CATS honours this, so a job with 300 applicants is three
#: requests rather than twelve at the default of 25.
POOL_PAGE_SIZE = 100

#: Seconds in an hour. The only unit conversion in this module, named so the
#: elapsed arithmetic reads as arithmetic rather than as a magic number.
SECONDS_PER_HOUR = 3600

#: Decimal places `elapsed_hours` is rounded to - about a third of a second.
#:
#: The comparison is made on the *rounded* value, not on the raw float. That is
#: deliberate: the number in the row is then exactly the number the threshold
#: was tested against, so a caller re-deriving the decision gets the same answer
#: rather than one that disagrees in the seventh decimal place.
ELAPSED_PRECISION = 4

#: Default and hard ceiling on CATS requests for one call.
#:
#: The budget is the point. Phase C is one request per candidate and Phase B can
#: be one per pipeline, so the naive shape of this tool is pipelines + people:
#: a job with eighty applicants is 160 requests against a 500/hour allowance.
#: The ceiling stops that, and `unevaluated` names the records it could not
#: reach instead of presenting a partial answer as a complete one.
DEFAULT_MAX_REQUESTS = 40
MAX_REQUESTS_CEILING = 200

#: Default and hard ceiling on rows returned by one call. `total_matched`
#: always reports how many there really were.
DEFAULT_MAX_ROWS = 100
MAX_ROWS_CEILING = 500

#: Hard stop on pipeline rows held in the pool, whatever the budget allows. Not
#: a caller knob - it exists so a very large job cannot be pulled into memory
#: wholesale by a generous max_requests.
MAX_POOL = 500

#: What `anchor` accepts - the timestamp the elapsed time is measured from -
#: and what each costs. The asymmetry is the thing to read.
ANCHOR_OPTIONS: dict[str, str] = {
    "stage_entered": (
        "when the pipeline last moved into a stage, from its stage history. Narrowed to "
        "pipeline_status_ids when you pass them. One request per PIPELINE, because a "
        "pipeline's status_id is its current status only and the history is a separate "
        "collection"
    ),
    "activity": (
        "the candidate's most recent activity whose type is in anchor_activity_types, or "
        "their most recent activity of any type when that list is empty. Free - the "
        "activity read happens anyway"
    ),
    "pipeline_modified": (
        "date_modified on the pipeline row. Free, and approximate: CATS touches it for "
        "reasons other than a stage change, so it is the cheapest anchor and the least "
        "specific one"
    ),
}

#: Why a record was returned without being assessed. Every one of these is
#: "unknown", never "did not match".
UNEVALUATED_REASONS: dict[str, str] = {
    "budget": "max_requests ran out before this record was read",
    "read_failed": "CATS returned an error for the read this record needed",
    "anchor_missing": "no timestamp of the requested kind exists on this record",
    "anchor_date_unreadable": "the timestamp exists but could not be parsed",
    "candidate_unknown": "the pipeline row carried no candidate_id to read activity for",
}


class Criteria(BaseModel):
    """Exactly what the call tested, echoed back so it can be redone by hand.

    This is not a convenience copy of the arguments. `as_of` in particular is
    resolved here - a call that did not pass one was compared against a clock
    reading nobody recorded, and an elapsed time whose reference instant is
    unknown cannot be checked. The two `comparison` strings are the operators
    themselves, spelled out, because "older than" is ambiguous at the boundary
    and a caller should not have to find out which way by experiment.
    """

    older_than_hours: float = Field(description="The threshold the caller passed, in hours.")
    comparison: str = Field(
        description="The test applied, as an expression: elapsed_hours >= older_than_hours."
    )
    as_of: str = Field(
        description="The instant elapsed time was measured to, ISO 8601. Every "
        "elapsed_hours in this result is relative to it."
    )
    as_of_source: str = Field(
        description="Whether as_of came from the caller or from this server's clock."
    )
    anchor: str = Field(description="Which timestamp elapsed time was measured from.")
    anchor_activity_types: list[str] = Field(
        default_factory=list,
        description="Activity types allowed to serve as the anchor. Empty means any type.",
    )
    require_no_later_activity: bool = Field(
        description="Whether a record with activity after its anchor was kept out."
    )
    later_activity_types: list[str] = Field(
        default_factory=list,
        description="Activity types that count as later activity. Empty means any type.",
    )
    later_comparison: str = Field(
        description="What counts as later, as an expression: activity_date > anchor_date. "
        "An activity at exactly the anchor instant is the anchor, not something after it."
    )
    pipeline_status_ids: list[int | str] = Field(
        default_factory=list,
        description="The stages the pool was narrowed to. Empty means every stage.",
    )


class FollowupRecord(BaseModel):
    """One pipeline that satisfied the caller's thresholds, and why.

    There is deliberately nowhere in this model to record what should happen
    next. It holds the stage, the timestamp, the field that timestamp came from,
    the subtraction, and the candidate's latest activity - the inputs to a
    decision rather than the decision. Order carries no meaning and the first
    row is not a nomination.

    `activities_undated` is the honest part. An activity whose date could not be
    read might have fallen after the anchor and there is no way to tell, so a
    non-zero count here means "no later activity" is a claim about the readable
    ones only.
    """

    #: CATS sends ids and titles interchangeably as numbers or strings on some
    #: accounts. Coercing rather than raising keeps one odd row from failing a
    #: whole result.
    model_config = ConfigDict(coerce_numbers_to_str=True)

    candidate_id: int | str | None = Field(description="The candidate on this pipeline.")
    pipeline_id: int | str | None = Field(description="The CATS pipeline id.")
    job_id: int | str | None = Field(default=None, description="The job the pipeline is on.")
    status_id: int | str | None = Field(
        default=None, description="The pipeline's current stage id, account-specific."
    )
    status: str | None = Field(
        default=None,
        description="Human title for status_id. Null when the workflow does not describe "
        "that id - an unknown id is never given an invented label.",
    )
    anchor: str = Field(description="Which timestamp the elapsed time was measured from.")
    anchor_type: str | None = Field(
        default=None,
        description="What CATS called the thing the anchor came from - an activity type, "
        "or 'status_change' for a stage move.",
    )
    anchor_date: str = Field(
        description="The timestamp itself, verbatim as CATS stored it. Subtracting it "
        "from criteria.as_of reproduces elapsed_hours."
    )
    anchor_date_field: str = Field(
        description="The field the anchor was read from. Sources disagree "
        "(date_created, date_modified), so the row says which it used."
    )
    elapsed_hours: float = Field(
        description="criteria.as_of minus anchor_date, in hours, rounded to "
        f"{ELAPSED_PRECISION} places. This is the value the threshold was tested against."
    )
    last_activity_date: str | None = Field(
        default=None,
        description="The candidate's most recent readable activity, of any type. Null "
        "when they have none.",
    )
    last_activity_type: str | None = Field(
        default=None, description="What CATS called that activity."
    )
    activities_after_anchor: int = Field(
        description="Readable activities strictly after the anchor, counting only the "
        "types in criteria.later_activity_types when that list is non-empty."
    )
    activities_undated: int = Field(
        description="Activities whose date could not be read. Non-zero means any "
        "statement about what came after the anchor covers the readable ones only."
    )
    more_activity_exists: bool = Field(
        default=False,
        description="True when the candidate has more activity than one page holds, so "
        "older rows were not read.",
    )


class UnevaluatedRecord(BaseModel):
    """One record the call could not test, and what stopped it.

    Kept apart from the results and never silently dropped. A record here is
    unknown, not one that failed the threshold, and a caller that cannot tell
    those apart is confident about a set that was never computed.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    candidate_id: int | str | None = Field(default=None, description="The candidate, if known.")
    pipeline_id: int | str | None = Field(default=None, description="The CATS pipeline id.")
    job_id: int | str | None = Field(default=None, description="The job the pipeline is on.")
    status_id: int | str | None = Field(
        default=None, description="The pipeline's current stage id."
    )
    reason: Literal[
        "budget",
        "read_failed",
        "anchor_missing",
        "anchor_date_unreadable",
        "candidate_unknown",
    ] = Field(
        description=(
            "Why no comparison was made. "
            + "; ".join(f"'{k}': {v}" for k, v in UNEVALUATED_REASONS.items())
            + ". Every one of these means unknown, never 'did not match'."
        )
    )
    detail: str = Field(description="The same thing in words the caller can act on.")
    date_raw: str | None = Field(
        default=None,
        description="The value that could not be parsed, verbatim, so the underlying "
        "record can be corrected.",
    )
    date_field: str | None = Field(
        default=None, description="The field that value was read from."
    )


class FollowupResult(BaseModel):
    """The records that satisfied the thresholds, and what the call covered.

    The counts are not decoration. `total_matched` against `count` says whether
    rows were left out; `evaluated`, `below_threshold` and `with_later_activity`
    account for every record that was tested; `unevaluated` accounts for every
    record that was not. A caller that cannot read those cannot tell a complete
    answer from a confident partial one.
    """

    records: list[FollowupRecord] = Field(
        default_factory=list,
        description="Records satisfying the thresholds, in the order CATS returned the "
        "pipelines. That order means nothing.",
    )
    count: int = Field(description="Rows in `records`. Compare with total_matched.")
    total_matched: int = Field(
        description="How many records satisfied the thresholds in total, even when "
        "max_rows capped `records`."
    )
    criteria: Criteria = Field(description="Exactly what was tested, so it can be redone.")
    pipelines_seen: int = Field(description="Pipeline rows the pool sweep read.")
    pipelines_in_scope: int = Field(
        description="Of those, how many survived pipeline_status_ids and were candidates "
        "for the paid reads."
    )
    candidates_read: int = Field(description="Candidates whose activity was actually read.")
    evaluated: int = Field(description="Records a comparison was actually made on.")
    below_threshold: int = Field(
        description="Evaluated records whose elapsed_hours was under older_than_hours."
    )
    with_later_activity: int = Field(
        description="Evaluated records held back only because activity was logged after "
        "their anchor. Zero unless require_no_later_activity was set."
    )
    unevaluated: list[UnevaluatedRecord] = Field(
        default_factory=list,
        description="Records no comparison was made on, with the reason. Unknown, not "
        "misses.",
    )
    execution: ExecutionFacts = Field(
        description="What the call spent and what it could not finish."
    )
    note: str = Field(description="How to read this result.")


#: Prose returned with every result. Kept out of the display content, which
#: stays a counts-only line - see `_content_line`.
RESULT_NOTE = (
    "Every record here satisfied thresholds the caller supplied, and carries the facts "
    "the comparison used: the stage, the anchor timestamp and the field it came from, "
    "the elapsed hours, and the candidate's most recent activity. `criteria` holds the "
    "reference instant and the two comparison operators, so any row can be re-derived "
    "by hand. The order is the order CATS returned the pipelines and means nothing. "
    "`unevaluated` lists records no comparison was made on - a budget that ran out, a "
    "timestamp that could not be parsed, a stage history that was empty - and those are "
    "unknown rather than records that failed the threshold. `activities_undated` on a "
    "row means any statement about what came after the anchor covers the readable "
    "activities only. Interpreting all of it is the caller's job."
)


def _types(values: list[str] | None) -> frozenset[str]:
    """The caller's activity types, normalised for comparison. Empty means any."""
    return frozenset(v.strip().lower() for v in (values or []) if v and v.strip())


def _type_matches(value: Any, wanted: frozenset[str]) -> bool:
    """Whether one CATS activity type is one the caller named.

    An empty `wanted` matches everything, which is what makes "any activity" the
    behaviour of passing no list rather than a separate flag. Matching is on the
    normalised string only - this module has no taxonomy of activity types and
    will not decide that "Phone Screen" and "Interview" are the same thing.
    """
    if not wanted:
        return True
    return str(value or "").strip().lower() in wanted


def _activity_rows(
    payload: Any,
) -> tuple[list[tuple[datetime, str | None, Any]], list[tuple[str | None, Any]]]:
    """(when, type, raw date) for each activity, and the ones whose date failed.

    Returned as two lists rather than one with nulls because they answer
    different questions: the dated ones are compared against the anchor, and the
    undated ones are the reason a "nothing since" statement has a caveat.
    """
    dated: list[tuple[datetime, str | None, Any]] = []
    undated: list[tuple[str | None, Any]] = []
    for raw in _embedded_rows(payload):
        row = _project(raw, SUMMARY_FIELDS["activity"])
        kind = row.get("type")
        label = str(kind) if kind is not None else None
        value = row.get("date_created")
        parsed = _parse_iso(value)
        if parsed is None:
            undated.append((label, value))
        else:
            dated.append((parsed, label, value))
    return dated, undated


def _stage_rows(
    payload: Any,
) -> tuple[list[tuple[datetime, dict[str, Any]]], list[dict[str, Any]]]:
    """Stage changes out of `/pipelines/{id}/statuses`, dated and undated.

    The row shape varies by account age: some carry `status_id`, others record
    the move as `to_status_id`, and the date is `date_created` on newer rows and
    `date_modified` on older ones. All of them are checked, because picking one
    and calling the rest undated would push real stage changes into
    `unevaluated` for no reason at all.
    """
    dated: list[tuple[datetime, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    for raw in _embedded_rows(payload):
        status_id = raw.get("status_id")
        if status_id is None:
            status_id = raw.get("to_status_id")
        field = "date_created" if raw.get("date_created") else "date_modified"
        value = raw.get(field)
        entry = {"status_id": status_id, "date_field": field, "date_value": value}
        parsed = _parse_iso(value)
        if parsed is None:
            undated.append(entry)
        else:
            dated.append((parsed, entry))
    return dated, undated


def _unevaluated(
    entry: dict[str, Any],
    reason: str,
    detail: str,
    *,
    date_raw: Any = None,
    date_field: str | None = None,
) -> UnevaluatedRecord:
    """One record that was not tested, carrying enough to find it again."""
    raw = None
    if date_raw is not None:
        raw = date_raw if isinstance(date_raw, str) else str(date_raw)
    return UnevaluatedRecord(
        candidate_id=entry.get("candidate_id"),
        pipeline_id=entry.get("pipeline_id"),
        job_id=entry.get("job_id"),
        status_id=entry.get("status_id"),
        reason=reason,  # type: ignore[arg-type]
        detail=detail,
        date_raw=raw,
        date_field=date_field,
    )


def _content_line(result: FollowupResult) -> str:
    """The one line a human sees. Counts and flags, never record data.

    The structured payload is the answer; repeating it here would double the
    tokens for nothing, and issue #21 keeps anything read off a person's record
    - names, notes, activity text - inside the structured result where a caller
    has to ask for it.
    """
    parts = [
        f"matched {result.total_matched}",
        f"returned {result.count}",
        f"evaluated {result.evaluated}",
        f"requests {result.execution.requests_used}",
    ]
    if result.unevaluated:
        parts.append(f"unevaluated {len(result.unevaluated)}")
    if result.execution.truncated:
        parts.append("truncated")
    if result.execution.errors:
        parts.append(f"errors {len(result.execution.errors)}")
    return ", ".join(parts)


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the follow-up facts primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="find_followup_facts",
        # Passed explicitly because the function returns ToolResult, which on its
        # own leaves the tool with no output schema at all. Declaring it here is
        # what gives the caller a typed, object-rooted contract while the display
        # content stays a single counts line instead of a second copy of the
        # payload.
        output_schema=FollowupResult.model_json_schema(),
        description=(
            "Return the CATS pipeline records that satisfy deterministic time thresholds "
            "you supply - a stage or an activity older than N hours, optionally with "
            "nothing logged since.\n\n"
            "Every threshold is yours. There is no default number of hours, and this "
            "adapter has no idea which stage or activity type means an interview: CATS v3 "
            "has no interview resource, so an interview is whatever your account calls "
            "it. Name the pipeline status ids and the activity types and they are matched "
            "literally, with no expansion.\n\n"
            "Each returned row carries the facts the comparison used: the pipeline's "
            "current stage, the timestamp elapsed time was measured from, the field that "
            "timestamp was read from, the elapsed hours, and the candidate's most recent "
            "activity. `criteria` holds the reference instant and the operators - the "
            "test is elapsed_hours >= older_than_hours, and an activity counts as later "
            "only when it is strictly after the anchor - so any row can be re-derived by "
            "hand. Pass as_of to pin the reference instant instead of using this server's "
            "clock.\n\n"
            "Cost: one request per candidate for the activity read, always, because every "
            "row reports its last activity date; plus one request per PIPELINE when "
            "anchor is 'stage_entered', since a pipeline's status_id is its current "
            "status only and stage history is a separate collection. Narrow the pool with "
            "pipeline_status_ids first - that filter is free and keeps the paid reads off "
            "records that could not have matched. max_requests bounds the rest.\n\n"
            "Nothing is dropped for being awkward. A record the budget never reached, or "
            "whose timestamp could not be parsed, comes back in `unevaluated` with the "
            "raw value and a reason; it is unknown, not a record that failed the "
            "threshold.\n\n"
            "Returns facts and the arithmetic behind them. Interpreting them is the "
            "caller's job."
        ),
        tags={"ats", "candidate", "pipeline", "activity", "read"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def find_followup_facts(
        older_than_hours: Annotated[
            float,
            Field(
                gt=0,
                description=(
                    "How old the anchor timestamp must be, in hours. Required and "
                    "deliberately undefaulted - there is no number of hours this adapter "
                    "could pick that would not be somebody's policy. The test is "
                    "elapsed_hours >= older_than_hours, so a record sitting at exactly "
                    "this many hours is returned."
                ),
            ),
        ],
        job_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    f"Jobs whose pipelines form the pool. Maximum {MAX_BATCH} per call, "
                    f"one request per page of pipelines each."
                )
            ),
        ] = None,
        candidate_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    f"Candidates whose pipelines form the pool, instead of or as well as "
                    f"job_ids. Maximum {MAX_BATCH} per call."
                )
            ),
        ] = None,
        pipeline_status_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Keep only pipelines currently in one of these stages, and - when "
                    "anchor is 'stage_entered' - measure from the last move into one of "
                    "them. Account-specific ids; find them with list_pipeline_workflows. "
                    "Applied on rows already fetched, so this filter is free and every "
                    "paid read it removes is a request saved. Omit for every stage."
                )
            ),
        ] = None,
        anchor: Annotated[
            Literal["stage_entered", "activity", "pipeline_modified"],
            Field(
                description=(
                    "Which timestamp elapsed time is measured from. "
                    + "; ".join(f"'{k}': {v}" for k, v in ANCHOR_OPTIONS.items())
                    + "."
                )
            ),
        ] = "stage_entered",
        anchor_activity_types: Annotated[
            list[str] | None,
            Field(
                description=(
                    "When anchor is 'activity', only activities of these types may serve "
                    "as the anchor - your account's own type strings, matched literally "
                    "after case and whitespace. Empty means any type."
                )
            ),
        ] = None,
        require_no_later_activity: Annotated[
            bool,
            Field(
                description=(
                    "Keep only records with no activity strictly after their anchor. "
                    "Defaults to false, which applies no such condition; the facts needed "
                    "to apply it yourself are on every row either way."
                )
            ),
        ] = False,
        later_activity_types: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Activity types that count as later activity. Empty means any type. "
                    "`last_activity_date` on each row is the latest activity of any type "
                    "regardless, so a narrowed list stays visible rather than hidden."
                )
            ),
        ] = None,
        as_of: Annotated[
            str | None,
            Field(
                description=(
                    "The instant to measure elapsed time to, ISO 8601, e.g. "
                    "'2026-01-06T12:00:00-00:00'. Defaults to this server's clock at call "
                    "time. Passing it makes the result reproducible, and it is echoed in "
                    "`criteria` either way."
                )
            ),
        ] = None,
        max_rows: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_ROWS_CEILING,
                description=(
                    f"Records to return. Ceiling {MAX_ROWS_CEILING}. `total_matched` "
                    f"always reports how many there really were."
                ),
            ),
        ] = DEFAULT_MAX_ROWS,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}; "
                    f"the CATS allowance is 500 requests/hour. Reading stops here and "
                    f"`unevaluated` names every record that was never assessed."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
    ) -> ToolResult:
        set_run_id()
        client = client_getter()

        if not job_ids and not candidate_ids:
            raise ValueError(
                "Pass job_ids or candidate_ids. Without either there is no pool to apply "
                "the threshold to, and sweeping every pipeline in the account to find one "
                "is not something this tool will do on its own initiative."
            )

        # An unreadable as_of is raised rather than ignored. Falling back to the
        # clock would return elapsed times measured from an instant the caller
        # did not choose and cannot see they did not get.
        if as_of is not None:
            reference = _parse_iso(as_of)
            if reference is None:
                raise ValueError(
                    f"as_of={as_of!r} is not an ISO 8601 date or timestamp. Use "
                    f"'2026-01-06' or '2026-01-06T12:00:00-00:00'."
                )
            as_of_source = "caller"
        else:
            reference = datetime.now(timezone.utc)
            as_of_source = "server clock at call time"

        wanted_status = frozenset(str(s) for s in (pipeline_status_ids or []))
        anchor_types = _types(anchor_activity_types)
        later_types = _types(later_activity_types)

        errors: dict[str, str] = {}
        unevaluated: list[UnevaluatedRecord] = []
        requests_used = 0
        truncated = False

        # --- Phase A: the pool ------------------------------------------------
        pool: list[dict[str, Any]] = []
        seen: set[str] = set()

        async def sweep(path: str, label: str, owner: dict[str, Any]) -> None:
            """Page one pipeline collection into the pool, within the budget."""
            nonlocal requests_used, truncated
            page = 1
            while True:
                if len(pool) >= MAX_POOL:
                    logger.warning("pipeline pool hit the %s-row ceiling", MAX_POOL)
                    truncated = True
                    return
                if requests_used >= max_requests:
                    truncated = True
                    return
                try:
                    payload = await client.request(
                        "GET", path, params={"per_page": POOL_PAGE_SIZE, "page": page}
                    )
                    requests_used += 1
                except CATSAPIError as exc:
                    errors[f"{label}:page:{page}"] = str(exc)
                    return

                rows = _embedded_rows(payload)
                for raw in rows:
                    row = _project(raw, SUMMARY_FIELDS["pipeline"])
                    pipeline_id = row.get("id")
                    if pipeline_id is None:
                        continue
                    key = str(pipeline_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidate_id = row.get("candidate_id")
                    if candidate_id is None:
                        candidate_id = owner.get("candidate_id")
                    job_id = row.get("job_id")
                    if job_id is None:
                        job_id = owner.get("job_id")
                    pool.append(
                        {
                            "pipeline_id": pipeline_id,
                            "candidate_id": candidate_id,
                            "job_id": job_id,
                            "status_id": row.get("status_id"),
                            "date_modified": row.get("date_modified"),
                        }
                    )
                await _progress(requests_used, max_requests, f"reading {label} pipelines")
                if not rows or not _has_next_page(payload):
                    return
                page += 1

        seed_jobs = _dedupe(list(job_ids or []))
        seed_candidates = _dedupe(list(candidate_ids or []))
        for seeds, name in ((seed_jobs, "job_ids"), (seed_candidates, "candidate_ids")):
            if len(seeds) > MAX_BATCH:
                truncated = True
                errors[name] = (
                    f"{len(seeds)} ids were passed and only the first {MAX_BATCH} were "
                    f"read; the rest contributed no pipelines to this result"
                )
        for job_id in seed_jobs[:MAX_BATCH]:
            await sweep(f"/jobs/{job_id}/pipelines", f"job:{job_id}", {"job_id": job_id})
        for candidate_id in seed_candidates[:MAX_BATCH]:
            await sweep(
                f"/candidates/{candidate_id}/pipelines",
                f"candidate:{candidate_id}",
                {"candidate_id": candidate_id},
            )

        pipelines_seen = len(pool)
        # Free, and the most valuable filter there is: every pipeline it removes
        # is a stage read and an activity read never spent.
        scoped = [
            entry
            for entry in pool
            if not wanted_status or str(entry["status_id"]) in wanted_status
        ]

        # Status titles. An id is account-specific and says nothing to a reader,
        # so resolving it is normalization; a failure loses the labels, not the
        # call. Skipped entirely when there is nothing to label.
        titles: dict[str, str] = {}
        if scoped:
            if requests_used < max_requests:
                titles, title_calls = await _status_titles(client)
                requests_used += title_calls
            else:
                errors["status_titles"] = (
                    "the request budget was spent before stage titles could be read; "
                    "status_id is still reported, unlabelled"
                )

        # --- Phase B: the anchor ---------------------------------------------
        #
        # Keyed by str(pipeline id), which is what _gather_by_id keys its results
        # by, so a stage-history payload and the pool row it belongs to line up.
        anchors: dict[str, dict[str, Any]] = {}

        if anchor == "stage_entered":
            budget = max(0, max_requests - requests_used)
            covered = scoped[:budget]
            for entry in scoped[budget:]:
                truncated = True
                unevaluated.append(
                    _unevaluated(
                        entry,
                        "budget",
                        f"the budget of {max_requests} requests was spent before this "
                        f"pipeline's stage history could be read; raise max_requests or "
                        f"narrow the pool with pipeline_status_ids",
                    )
                )

            if covered:

                async def fetch_stages(pipeline_id: int | str) -> Any:
                    return await client.request(
                        "GET",
                        f"/pipelines/{pipeline_id}/statuses",
                        params={"per_page": POOL_PAGE_SIZE},
                    )

                payloads, fetch_errors = await _gather_by_id(
                    [entry["pipeline_id"] for entry in covered], fetch_stages
                )
                requests_used += len(covered)
                errors.update({f"stage:{k}": v for k, v in fetch_errors.items()})
                await _progress(requests_used, max_requests, "reading pipeline stage history")

                for entry in covered:
                    key = str(entry["pipeline_id"])
                    if key not in payloads:
                        unevaluated.append(
                            _unevaluated(
                                entry,
                                "read_failed",
                                "CATS returned an error for this pipeline's stage history",
                            )
                        )
                        continue
                    dated, undated = _stage_rows(payloads[key])
                    if wanted_status:
                        dated = [
                            (at, row)
                            for at, row in dated
                            if str(row["status_id"]) in wanted_status
                        ]
                        undated = [
                            row for row in undated if str(row["status_id"]) in wanted_status
                        ]
                    if dated:
                        at, row = max(dated, key=lambda pair: pair[0])
                        anchors[key] = {
                            "at": at,
                            "raw": row["date_value"],
                            "field": row["date_field"],
                            "type": "status_change",
                        }
                    elif undated:
                        unevaluated.append(
                            _unevaluated(
                                entry,
                                "anchor_date_unreadable",
                                "this pipeline's most relevant stage change carries a "
                                "timestamp that is not ISO 8601, so no elapsed time could "
                                "be computed from it",
                                date_raw=undated[0]["date_value"],
                                date_field=undated[0]["date_field"],
                            )
                        )
                    else:
                        unevaluated.append(
                            _unevaluated(
                                entry,
                                "anchor_missing",
                                "CATS holds no stage change for this pipeline matching "
                                "the stages asked for",
                            )
                        )

        elif anchor == "pipeline_modified":
            for entry in scoped:
                value = entry.get("date_modified")
                parsed = _parse_iso(value)
                if parsed is not None:
                    anchors[str(entry["pipeline_id"])] = {
                        "at": parsed,
                        "raw": value,
                        "field": "date_modified",
                        "type": None,
                    }
                elif value is not None:
                    unevaluated.append(
                        _unevaluated(
                            entry,
                            "anchor_date_unreadable",
                            "date_modified on this pipeline is not ISO 8601",
                            date_raw=value,
                            date_field="date_modified",
                        )
                    )
                else:
                    unevaluated.append(
                        _unevaluated(
                            entry, "anchor_missing", "this pipeline carries no date_modified"
                        )
                    )

        # --- Phase C: activity, one request per candidate ---------------------
        #
        # Always spent, because every returned row reports its last activity
        # date - and only for pipelines still in play, so a record the anchor
        # phase already set aside costs nothing more.
        pending: list[dict[str, Any]] = []
        for entry in scoped:
            if anchor != "activity" and str(entry["pipeline_id"]) not in anchors:
                continue
            if entry["candidate_id"] is None:
                unevaluated.append(
                    _unevaluated(
                        entry,
                        "candidate_unknown",
                        "the pipeline row carried no candidate_id, so no activity could "
                        "be read for it",
                    )
                )
                continue
            pending.append(entry)

        needed = _dedupe([entry["candidate_id"] for entry in pending])
        budget = max(0, max_requests - requests_used)
        covered_ids = needed[:budget]
        unreached = {str(i) for i in needed[budget:]}
        if unreached:
            truncated = True

        activity: dict[str, dict[str, Any]] = {}
        if covered_ids:

            async def fetch_activity(candidate_id: int | str) -> Any:
                return await client.request(
                    "GET",
                    f"/candidates/{candidate_id}/activities",
                    params={"per_page": POOL_PAGE_SIZE},
                )

            payloads, fetch_errors = await _gather_by_id(covered_ids, fetch_activity)
            requests_used += len(covered_ids)
            errors.update({f"activity:{k}": v for k, v in fetch_errors.items()})
            await _progress(requests_used, max_requests, "reading candidate activity")

            for key, payload in payloads.items():
                dated, undated = _activity_rows(payload)
                activity[key] = {
                    "dated": dated,
                    "undated": undated,
                    "more": _has_next_page(payload),
                }

        # --- evaluate ---------------------------------------------------------
        records: list[FollowupRecord] = []
        evaluated = 0
        below_threshold = 0
        with_later_activity = 0

        for entry in pending:
            key = str(entry["pipeline_id"])
            candidate_key = str(entry["candidate_id"])

            if candidate_key in unreached:
                unevaluated.append(
                    _unevaluated(
                        entry,
                        "budget",
                        f"the budget of {max_requests} requests was spent before this "
                        f"candidate's activity could be read; every row reports its last "
                        f"activity date, so no comparison is made without it",
                    )
                )
                continue
            if candidate_key not in activity:
                unevaluated.append(
                    _unevaluated(
                        entry,
                        "read_failed",
                        "CATS returned an error for this candidate's activity",
                    )
                )
                continue

            facts = activity[candidate_key]
            dated_activity: list[tuple[datetime, str | None, Any]] = facts["dated"]
            undated_activity: list[tuple[str | None, Any]] = facts["undated"]

            if anchor == "activity":
                usable = [row for row in dated_activity if _type_matches(row[1], anchor_types)]
                if usable:
                    at, kind, raw = max(usable, key=lambda row: row[0])
                    anchors[key] = {
                        "at": at,
                        "raw": raw,
                        "field": "date_created",
                        "type": kind,
                    }
                else:
                    unreadable = [
                        row for row in undated_activity if _type_matches(row[0], anchor_types)
                    ]
                    if unreadable:
                        unevaluated.append(
                            _unevaluated(
                                entry,
                                "anchor_date_unreadable",
                                "the only activity of the requested type carries a "
                                "timestamp that is not ISO 8601",
                                date_raw=unreadable[0][1],
                                date_field="date_created",
                            )
                        )
                    else:
                        unevaluated.append(
                            _unevaluated(
                                entry,
                                "anchor_missing",
                                "this candidate has no activity of the requested type",
                            )
                        )
                    continue

            resolved = anchors[key]
            at = resolved["at"]
            elapsed = round(
                (reference - at).total_seconds() / SECONDS_PER_HOUR, ELAPSED_PRECISION
            )
            evaluated += 1

            later = [
                row
                for row in dated_activity
                if row[0] > at and _type_matches(row[1], later_types)
            ]
            last_date: Any = None
            last_type: str | None = None
            if dated_activity:
                _, last_type, last_date = max(dated_activity, key=lambda row: row[0])

            # The comparison is made on the rounded value, which is the value the
            # row reports - so a caller redoing the subtraction reaches the same
            # verdict rather than one that disagrees in the seventh decimal.
            if elapsed < older_than_hours:
                below_threshold += 1
                continue
            if require_no_later_activity and later:
                with_later_activity += 1
                continue

            records.append(
                FollowupRecord(
                    candidate_id=entry["candidate_id"],
                    pipeline_id=entry["pipeline_id"],
                    job_id=entry["job_id"],
                    status_id=entry["status_id"],
                    status=titles.get(str(entry["status_id"])) or None,
                    anchor=anchor,
                    anchor_type=resolved["type"],
                    anchor_date=str(resolved["raw"]),
                    anchor_date_field=resolved["field"],
                    elapsed_hours=elapsed,
                    last_activity_date=str(last_date) if last_date is not None else None,
                    last_activity_type=last_type,
                    activities_after_anchor=len(later),
                    activities_undated=len(undated_activity),
                    more_activity_exists=bool(facts["more"]),
                )
            )

        total_matched = len(records)
        if total_matched > max_rows:
            truncated = True

        result = FollowupResult(
            records=records[:max_rows],
            count=len(records[:max_rows]),
            total_matched=total_matched,
            criteria=Criteria(
                older_than_hours=older_than_hours,
                comparison="elapsed_hours >= older_than_hours",
                as_of=reference.isoformat(),
                as_of_source=as_of_source,
                anchor=anchor,
                anchor_activity_types=sorted(anchor_types),
                require_no_later_activity=require_no_later_activity,
                later_activity_types=sorted(later_types),
                later_comparison="activity_date > anchor_date",
                pipeline_status_ids=list(pipeline_status_ids or []),
            ),
            pipelines_seen=pipelines_seen,
            pipelines_in_scope=len(scoped),
            candidates_read=len(activity),
            evaluated=evaluated,
            below_threshold=below_threshold,
            with_later_activity=with_later_activity,
            unevaluated=unevaluated,
            execution=ExecutionFacts(
                requests_used=requests_used,
                rate_limit=RateLimit.model_validate(client.rate_limit.snapshot()),
                # Only a limit counts here - the budget, the row cap, the pool
                # ceiling. A record in `unevaluated` because its stage history is
                # empty is a fact about that record, not a sign the call stopped
                # early, and conflating the two would make `truncated` mean
                # nothing on any real account.
                truncated=truncated,
                # No resumable position exists: the pool is rebuilt from the
                # seeds on every call, so there is nothing to hand back.
                # `unevaluated` is what says the answer may be short a record.
                next_cursor=None,
                errors=errors,
            ),
            note=RESULT_NOTE,
        )

        return ToolResult(content=_content_line(result), structured_content=result.model_dump())

    return 1
