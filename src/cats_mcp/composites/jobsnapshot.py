"""What a job's recruiting record actually says, in one call.

Issue #12, item 1. "What is happening on this job?" is the first question
anybody asks of an ATS and the most expensive one to answer through the atomic
surface. The facts live in five places: the job record, its pipelines, the
account's workflow vocabulary, its custom fields and its tasks. A caller
assembling that by hand spends a request per endpoint per job, pages a pipeline
it only wants counts from, and pulls every intermediate record through model
context to throw nearly all of it away.

Worse, the part that matters most does not survive the trip. A pipeline row
carries `status_id`, and a status id is account-specific: `6377104` is a number
until `/pipelines/workflows` says it is "Interview". A snapshot that reports
raw ids has reported nothing a human can read, and a caller who guesses at them
guesses wrong on the next account. Resolving them is `_status_titles`' job and
it costs one shared request for the whole call - so it is not optional here.

Three things this tool refuses to do:

  * It does not invent openings. CATS has no field for how many positions a job
    is hiring for, so there is nowhere in this output to put one and no count
    is derived from the pipeline. `total_candidates` counts pipeline entries.
  * It does not label a status id it could not resolve. An unknown id comes
    back with `status` null, because a plausible-looking guess at a stage name
    is worse information than an honest gap.
  * It does not carry a pipeline's `rating`. It is a stored CATS field, but it
    is the one number on a pipeline row shaped like a verdict, and a snapshot
    is not where a caller should meet it. (This is also why
    `reads._pipeline_rows` is not reused below: it projects `rating` and drops
    `candidate_id`, which is the wrong half of the row for this tool.)

Cost model, and why the phases are ordered the way they are:

    Phase A  the job records. One request per job.
    Phase B  the stage vocabulary. One shared request for the whole call,
             served from the reference-data cache for ten minutes after.
    Phase C  the pipelines. One request per page of 100 per job. This is what
             produces the counts, so it runs before anything optional.
    Phase D  custom fields, one request per job, on request.
    Phase E  tasks, one request per job, on request.
    Phase F  per-candidate enrichment - identity and latest activity - at one
             CATS request EACH. Opt-in, applied only to the candidates at the
             stages the caller named, and bounded twice over by
             max_candidate_reads and max_requests. Whatever the budget did not
             reach is counted in `unenriched` rather than reported as absent.

Phase F is the one that can eat an hourly allowance, which is why it is last,
why it defaults to off, and why what it skipped is reported alongside what it
found.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field

from cats_mcp.composites.models import ExecutionFacts, MatchEvidence, RateLimit
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

logger = get_logger(__name__)

#: Jobs per call. Every job is at least two requests before anything optional,
#: so an unbounded list could spend an hourly allowance in one tool call.
MAX_JOBS_PER_CALL = 20

#: Pipeline rows per page. CATS honours this, so a 240-candidate job is three
#: requests rather than ten at the default of 25.
PIPELINE_PAGE_SIZE = 100

#: Pages per job. A stop, so one pathological pipeline cannot consume a budget
#: the other jobs in the same call still need.
MAX_PIPELINE_PAGES = 20

#: Default and hard ceiling on requests for one call.
#:
#: The CATS standard allowance is 500 requests/hour. A snapshot of five jobs
#: with activity enrichment can ask for eighty of them, so the stop is a number
#: the caller chose and the result says what it did not reach.
DEFAULT_MAX_REQUESTS = 25
MAX_REQUESTS_CEILING = 150

#: Default and hard ceiling on candidate rows returned per job. The rows are
#: free - they come off pipeline pages already fetched - so this bounds the
#: size of the answer, not its cost.
DEFAULT_MAX_CANDIDATES_PER_JOB = 25
MAX_CANDIDATES_PER_JOB_CEILING = 200

#: Default and hard ceiling on candidates any per-candidate phase will read.
#: Ceiling is MAX_BATCH, the same limit every other per-candidate composite
#: uses, because this phase is the same O(N) shape they are.
DEFAULT_MAX_CANDIDATE_READS = 25

#: What `include` accepts, and what each costs.
#:
#: The asymmetry is the whole design: the job-level extras are one request per
#: *job* and the candidate-level ones are one request per *candidate*.
INCLUDE_OPTIONS: dict[str, str] = {
    "custom_fields": "the job's account-specific fields - one request per job",
    "tasks": "tasks filed against the job, with due dates - one request per job",
    "identity": "name, title and location for the candidates listed - one request per candidate",
    "latest_activity": (
        "the most recent recorded activity for the candidates listed - one request per candidate"
    ),
}

#: Fetched by default. Both are per-job, so the default stays cheap.
DEFAULT_INCLUDE: tuple[str, ...] = ("custom_fields",)

#: The `include` values that cost a request per candidate rather than per job.
PER_CANDIDATE_INCLUDES: tuple[str, ...] = ("identity", "latest_activity")

#: Projected off the job record. Whatever an account omits simply does not
#: appear - `_project` keeps only the keys the record actually carried.
JOB_FIELDS = [
    "id",
    "title",
    "status_id",
    "city",
    "state",
    "company_id",
    "owner_id",
    "date_created",
    "date_modified",
]

#: Row keys consulted for a display name, in order. Several per concept on
#: purpose: accounts differ on whether the client is `company_name` on the row
#: or an `_embedded.company` object, and reading only one shape returns null on
#: accounts built the other way.
COMPANY_KEYS = ("company", "company_name", "client", "client_name")
OWNER_KEYS = ("owner", "owner_name", "recruiter", "recruiter_name")
STATUS_KEYS = ("status", "status_title", "status_name")


class JobCustomField(BaseModel):
    """One account-specific field stored on the job."""

    name: str | None = Field(
        default=None, description="The field's label, as the account named it."
    )
    value: str | None = Field(default=None, description="The stored value.")


class StageCount(BaseModel):
    """How many candidates sit at one pipeline stage.

    `status` is null when the account's workflow vocabulary did not name this
    id. That is a gap, not a stage called nothing - the id is still returned so
    the caller can resolve it themselves.
    """

    status_id: int | str | None = Field(default=None, description="The CATS pipeline status id.")
    status: str | None = Field(
        default=None,
        description="The account's own title for that status, or null if it could not be resolved.",
    )
    candidates: int = Field(description="Pipeline entries currently at this status.")


class ActivityFact(BaseModel):
    """The most recent activity CATS has recorded against a candidate."""

    activity_id: int | str | None = Field(default=None, description="The CATS activity id.")
    type: str | None = Field(default=None, description="The activity type as CATS stores it.")
    date: str | None = Field(default=None, description="When the activity was created.")


class CandidateAtStage(BaseModel):
    """One candidate in the job's pipeline, at the stage CATS currently has them.

    `status_id` is the pipeline's CURRENT status and carries no history: how
    the candidate arrived there needs GET /pipelines/{id}/statuses, which is one
    request per pipeline and deliberately not spent here.

    There is nowhere in this model to record how well the candidate matched, and
    the pipeline's `rating` is not carried through. Ordering follows CATS and
    means nothing.
    """

    candidate_id: int | str | None = Field(default=None, description="The CATS candidate id.")
    pipeline_id: int | str | None = Field(default=None, description="The CATS pipeline id.")
    status_id: int | str | None = Field(default=None, description="Current pipeline status id.")
    status: str | None = Field(
        default=None, description="The account's title for that status, or null if unresolved."
    )
    name: str | None = Field(
        default=None,
        description=(
            "The candidate's name, when the pipeline row embedded it or 'identity' was "
            "included. Null otherwise - it is not inferred."
        ),
    )
    title: str | None = Field(default=None, description="Current title, when 'identity' was read.")
    city: str | None = Field(default=None, description="City, when 'identity' was read.")
    state: str | None = Field(default=None, description="State or province, when read.")
    date_modified: str | None = Field(
        default=None, description="When CATS last changed the pipeline entry."
    )
    latest_activity: ActivityFact | None = Field(
        default=None,
        description=(
            "The most recent recorded activity, when 'latest_activity' was included and "
            "the budget reached this candidate. Null means not read or none recorded - "
            "check `unenriched`."
        ),
    )


class JobTask(BaseModel):
    """One task filed against the job.

    A CATS task has no title field; its text is `description`, and it
    associates through `data_item`, reported here as regarding_id/type.

    `urgency_value` is CATS' own `priority` column, renamed on the way out. The
    stored number is kept because it is a fact, but the key is not: a response
    field called `priority` reads as this adapter ordering the caller's work,
    and tests/test_boundary.py fails the build on the name for exactly that
    reason. CATS does not document the scale, so no meaning is attached to it.
    """

    task_id: int | str | None = Field(default=None, description="The CATS task id.")
    description: str | None = Field(default=None, description="The task's text.")
    due_date: str | None = Field(default=None, description="Due date, from CATS' date_due.")
    overdue: bool = Field(
        default=False, description="True when due_date is in the past. Arithmetic, not judgement."
    )
    days_overdue: int | None = Field(
        default=None, description="Whole days past due_date, or null when not overdue."
    )
    is_completed: bool | None = Field(default=None, description="CATS' completion flag.")
    assigned_to_id: int | str | None = Field(default=None, description="The assigned user id.")
    regarding_id: int | str | None = Field(
        default=None, description="Id of the record the task hangs off, from data_item."
    )
    regarding_type: str | None = Field(
        default=None, description="That record's type, e.g. 'candidate'."
    )
    urgency_value: int | str | None = Field(
        default=None,
        description=(
            "CATS' stored priority number, renamed. The scale is undocumented; no "
            "ordering is implied."
        ),
    )


class JobSnapshot(BaseModel):
    """The recorded state of one job.

    Note what is absent: there is no opening count, headcount, or slot figure,
    because CATS stores none and deriving one from the pipeline would be an
    invention that reads exactly like a fact.
    """

    job_id: int | str | None = Field(default=None, description="The CATS job id.")
    title: str | None = Field(default=None, description="The job title as CATS stores it.")
    status_id: int | str | None = Field(default=None, description="The job's own status id.")
    status: str | None = Field(
        default=None, description="The job status as text, when the record carries it."
    )
    company_id: int | str | None = Field(default=None, description="The client company's id.")
    company: str | None = Field(default=None, description="The client company name.")
    owner_id: int | str | None = Field(default=None, description="The owning user's id.")
    owner: str | None = Field(default=None, description="The owner's name, when the record has it.")
    city: str | None = Field(default=None, description="City on the job record.")
    state: str | None = Field(default=None, description="State or province on the job record.")
    date_created: str | None = Field(default=None, description="When the job was created.")
    date_modified: str | None = Field(default=None, description="When CATS last changed the job.")
    custom_fields: list[JobCustomField] = Field(
        default_factory=list,
        description="Account-specific fields, when 'custom_fields' was included.",
    )
    total_candidates: int | None = Field(
        default=None,
        description=(
            "Candidates in this job's pipeline, as CATS reports the collection total. "
            "This is a pipeline count and nothing else - it is not an opening count."
        ),
    )
    candidates_counted: int = Field(
        default=0,
        description=(
            "Pipeline rows this call actually read. Below total_candidates means the "
            "page cap or the budget stopped the sweep, so the stage counts are partial."
        ),
    )
    stage_counts: list[StageCount] = Field(
        default_factory=list,
        description=(
            "Candidates per pipeline stage across the rows read, sorted by status id. "
            "That order means nothing."
        ),
    )
    candidates_selected: int = Field(
        default=0,
        description=(
            "Pipeline rows matching the stage selection, before max_candidates_per_job "
            "capped the list below."
        ),
    )
    candidates: list[CandidateAtStage] = Field(
        default_factory=list,
        description="The rows themselves, in the order CATS returned them.",
    )
    tasks: list[JobTask] = Field(
        default_factory=list, description="Tasks on the job, when 'tasks' was included."
    )
    task_count: int = Field(default=0, description="Tasks read for this job.")
    overdue_task_count: int = Field(default=0, description="How many of them are past due.")


class JobSnapshotResult(BaseModel):
    """Snapshots for the jobs asked about, and what the call actually covered.

    The counts are not decoration. `candidates_counted` against
    `total_candidates` says whether the stage counts are complete;
    `stages_matched` says which account statuses a stage term actually resolved
    to; `unenriched` says how many candidates the budget never read. A caller
    that cannot read those cannot tell a complete answer from a partial one.
    """

    jobs: list[JobSnapshot] = Field(
        default_factory=list, description="One snapshot per job that could be read."
    )
    count: int = Field(description="Snapshots in `jobs`.")
    requested: int = Field(description="Job ids the caller passed, before any cap.")
    stages_requested: list[str] = Field(
        default_factory=list,
        description="The stage terms and ids the caller asked to list candidates for.",
    )
    stages_matched: list[MatchEvidence] = Field(
        default_factory=list,
        description=(
            "Which account statuses each stage term resolved to, with the stored title. "
            "A term absent here matched no stage in this account's vocabulary."
        ),
    )
    included: list[str] = Field(
        default_factory=list, description="The optional data this call actually asked CATS for."
    )
    unenriched: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per per-candidate include, how many listed candidates the budget never "
            "read. These are unknown, not absent."
        ),
    )
    execution: ExecutionFacts = Field(
        description="What the call spent and what it could not finish."
    )
    note: str = Field(description="How to read this result.")


#: Prose returned with every result. Kept out of the display content, which
#: stays a counts-only line - see `_content_line`.
RESULT_NOTE = (
    "Counts, ids, stage titles and dates are what CATS records; interpreting them "
    "is the caller's job. `stage_counts` covers every stage the rows actually "
    "used, with the account's own title resolved from the status id - an id the "
    "workflow vocabulary did not name comes back with `status` null rather than "
    "guessed. A pipeline's status is its current one only; how a candidate got "
    "there needs GET /pipelines/{id}/statuses, one request per pipeline, which "
    "this call does not spend. CATS stores no opening or headcount figure for a "
    "job, so none is returned or derived: `total_candidates` counts pipeline "
    "entries. `candidates_counted` below `total_candidates` means the sweep "
    "stopped early and the stage counts are partial. `unenriched` counts "
    "candidates the budget never read, which are unknown rather than absent."
)


def _fold(value: Any) -> str:
    """Lowercase and collapse whitespace. Nothing else.

    This is the only transformation applied to a stage name, so "Interview"
    and "  interview " are the same string. Deciding that "Interview" and
    "Client Meeting" are the same stage is not normalisation and belongs to
    the caller, who can pass stage_status_ids instead.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).lower()


def _text(value: Any) -> str | None:
    """A stored name or label as display text, or None. Never raises on an odd shape.

    CATS is account-shaped: a field this adapter expects to be a string can
    arrive as a nested object on somebody's account, so objects are unwrapped.

    Numbers are deliberately not stringified. An owner id is not an owner name
    and a job's numeric status is not a status title; rendering either as text
    would put a number where the caller reads a label and make it look like the
    account had named it that.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        full = " ".join(
            str(value.get(key) or "").strip() for key in ("first_name", "last_name")
        ).strip()
        if full:
            return full
        for key in ("name", "title", "full_name", "label", "value"):
            text = _text(value.get(key))
            if text:
                return text
    return None


def _value_text(value: Any) -> str | None:
    """A stored *value* as text, numbers included.

    Unlike `_text`, this keeps numerics: a custom field holding a rate or a
    count is genuinely that value, and dropping it would report the field as
    empty when the account has filled it in.
    """
    text = _text(value)
    if text is not None:
        return text
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _first_text(record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first of `keys` that yields text, checked on the row and its _embedded."""
    embedded = record.get("_embedded") if isinstance(record.get("_embedded"), dict) else {}
    for key in keys:
        value = record.get(key)
        if value is None and isinstance(embedded, dict):
            value = embedded.get(key)
        text = _text(value)
        if text:
            return text
    return None


def _embedded_candidate_name(row: dict[str, Any]) -> str | None:
    """A candidate name off a pipeline row, when CATS embedded one.

    Free when present. When it is not, the name stays null rather than being
    fetched implicitly - a name is one request per candidate, and a tool that
    spends those without being asked is the cost surprise this module's budget
    exists to prevent.
    """
    embedded = row.get("_embedded") if isinstance(row.get("_embedded"), dict) else {}
    if isinstance(embedded, dict):
        text = _text(embedded.get("candidate"))
        if text:
            return text
    return _text(row.get("candidate"))


def _custom_fields(payload: Any) -> list[JobCustomField]:
    out: list[JobCustomField] = []
    for row in _embedded_rows(payload):
        label = row.get("name") or row.get("title") or row.get("field_name")
        out.append(JobCustomField(name=_text(label), value=_value_text(row.get("value"))))
    return out


def _tasks(payload: Any, now: datetime) -> list[JobTask]:
    """Task rows, with overdue derived from the stored due date.

    CATS names the due date `date_due`; `due_date` is the name that gets
    accepted on writes and silently dropped (issue #15), so it is read second
    only as a courtesy to accounts that echo it back.
    """
    out: list[JobTask] = []
    for row in _embedded_rows(payload):
        item = row.get("data_item") if isinstance(row.get("data_item"), dict) else {}
        raw_due = row.get("date_due") or row.get("due_date")
        due = _parse_iso(raw_due)
        overdue = bool(due and due < now and not row.get("is_completed"))
        out.append(
            JobTask(
                task_id=row.get("id"),
                description=_text(row.get("description")),
                due_date=_text(raw_due),
                overdue=overdue,
                days_overdue=(now - due).days if overdue and due else None,
                is_completed=row.get("is_completed"),
                assigned_to_id=row.get("assigned_to_id") or row.get("assigned_to"),
                regarding_id=item.get("id") if isinstance(item, dict) else None,
                regarding_type=_text(item.get("type")) if isinstance(item, dict) else None,
                # CATS' `priority`, renamed - see JobTask's docstring.
                urgency_value=row.get("priority"),
            )
        )
    return out


def _latest_activity(payload: Any) -> ActivityFact | None:
    """The most recent activity row, by parsed date rather than string order.

    String ordering is nearly right and wrong at the edges: a bare "2023-05-27"
    sorts after "2023-05-27T09:00:00-00:00" as text.
    """
    rows = [row for row in _embedded_rows(payload) if row.get("date_created")]
    if not rows:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    newest = max(rows, key=lambda row: _parse_iso(row.get("date_created")) or epoch)
    return ActivityFact(
        activity_id=newest.get("id"),
        type=_text(newest.get("type")),
        date=_text(newest.get("date_created")),
    )


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the job snapshot primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="get_job_recruiting_snapshot",
        # Passed explicitly because the function returns ToolResult, which on
        # its own leaves the tool with no output schema at all. Declaring it
        # here is what gives the caller a typed, object-rooted contract while
        # the display content stays a single counts line rather than a second
        # copy of the payload.
        output_schema=JobSnapshotResult.model_json_schema(),
        description=(
            "Report the currently recorded state of one or more CATS jobs as one compact "
            "structure: job identity, client company, status, owner and custom fields; how "
            "many candidates are in the pipeline; and how many sit at each pipeline stage, "
            "with the account's own stage title resolved from its id.\n\n"
            "CATS spreads those facts over five endpoints - the job record, its pipelines, "
            "the workflow vocabulary, its custom fields and its tasks - so assembling them "
            "by hand costs a chain of calls and pulls every intermediate record through "
            "context. This is that chain as one call returning counts and ids.\n\n"
            "A raw status id such as 6377104 means nothing on its own and is different on "
            "every account, so each stage carries the title CATS has for it. An id the "
            "workflow vocabulary does not name comes back with a null title rather than a "
            "guessed one.\n\n"
            "Facts only. Stage counts, stage titles, task due dates and activity dates are "
            "what CATS stores. Interpreting them is the caller's job. CATS holds no figure "
            "for how many positions a job is hiring for, so none is returned or derived - "
            "`total_candidates` counts pipeline entries and nothing else.\n\n"
            "Cost: one request for each job record, one shared request for the stage "
            "vocabulary, and one per page of 100 pipeline entries per job. 'custom_fields' "
            "and 'tasks' add one request per job. 'identity' and 'latest_activity' are one "
            "CATS request PER CANDIDATE, so they are opt-in, apply only to the candidates "
            "listed for the stages in `stages`, and stop at max_candidate_reads or "
            "max_requests, whichever comes first; `unenriched` counts the candidates the "
            "budget never reached, which are unknown rather than absent. The CATS allowance "
            "is 500 requests/hour."
        ),
        tags={"ats", "job", "pipeline", "read", "batch"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def get_job_recruiting_snapshot(
        job_ids: Annotated[
            list[int | str],
            Field(
                description=(
                    f"The jobs to snapshot. Maximum {MAX_JOBS_PER_CALL} per call, since "
                    f"each is at least two CATS requests before any option."
                )
            ),
        ],
        stages: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Stage titles to list candidates for, e.g. ['interview', 'offer']. "
                    "Matched case-insensitively as a substring of the account's own stage "
                    "titles, so 'interview' also selects 'Phone Interview'. "
                    "`stages_matched` reports exactly which statuses each term resolved "
                    "to. Omit to list candidates from every stage."
                )
            ),
        ] = None,
        stage_status_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Pipeline status ids to list candidates for, when you already know "
                    "them. Exact, and combined with anything `stages` matched. Find ids "
                    "with list_pipeline_workflows."
                )
            ),
        ] = None,
        include: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional data to fetch. "
                    + "; ".join(f"'{k}': {v}" for k, v in INCLUDE_OPTIONS.items())
                    + f". Defaults to {list(DEFAULT_INCLUDE)}. Pass [] for the cheapest "
                    "call - identity, counts and stages only."
                )
            ),
        ] = None,
        max_candidates_per_job: Annotated[
            int,
            Field(
                ge=0,
                le=MAX_CANDIDATES_PER_JOB_CEILING,
                description=(
                    f"Candidate rows listed per job. Ceiling "
                    f"{MAX_CANDIDATES_PER_JOB_CEILING}. These rows are free - "
                    f"`candidates_selected` always reports how many there really were."
                ),
            ),
        ] = DEFAULT_MAX_CANDIDATES_PER_JOB,
        max_candidate_reads: Annotated[
            int,
            Field(
                ge=0,
                le=MAX_BATCH,
                description=(
                    f"Candidates any per-candidate include may read, across all jobs. "
                    f"Ceiling {MAX_BATCH}. One CATS request each, per include."
                ),
            ),
        ] = DEFAULT_MAX_CANDIDATE_READS,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}. "
                    f"The CATS allowance is 500 requests/hour."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
    ) -> ToolResult:
        set_run_id()
        client = client_getter()
        now = datetime.now(timezone.utc)

        requested = _dedupe(list(job_ids or []))
        if not requested:
            raise ValueError(
                "Pass at least one job id in job_ids. There is no account-wide snapshot: "
                "sweeping every job would spend an hourly allowance to answer a question "
                "nobody asked."
            )

        wanted_include = list(DEFAULT_INCLUDE) if include is None else include
        wanted = [i.strip().lower() for i in wanted_include if str(i).strip()]
        unknown = [i for i in wanted if i not in INCLUDE_OPTIONS]
        if unknown:
            raise ValueError(
                f"Unknown include values {unknown}. Valid options: {sorted(INCLUDE_OPTIONS)}."
            )

        stage_terms = [str(s).strip() for s in (stages or []) if str(s).strip()]
        exact_stage_ids = _dedupe(list(stage_status_ids or []))

        errors: dict[str, str] = {}
        requests_used = 0
        truncated = False

        def afford(count: int) -> int:
            """How many of `count` requests the remaining budget covers."""
            return max(0, min(count, max_requests - requests_used))

        ids = requested[:MAX_JOBS_PER_CALL]
        if len(requested) > len(ids):
            truncated = True
            errors["job_ids"] = (
                f"{len(requested)} job ids given; this call covers the first "
                f"{MAX_JOBS_PER_CALL}. Split the rest into another call."
            )

        # --- Phase A: the job records ---------------------------------------
        affordable = afford(len(ids))
        if affordable < len(ids):
            truncated = True
            errors["jobs"] = (
                f"budget covered {affordable} of {len(ids)} job records; the rest were "
                f"not read. Raise max_requests or pass fewer job ids."
            )
        target = ids[:affordable]

        async def fetch_job(jid: int | str) -> Any:
            return await client.request("GET", f"/jobs/{jid}")

        records, job_errors = await _gather_by_id(list(target), fetch_job)
        requests_used += len(target)
        errors.update({f"job:{key}": value for key, value in job_errors.items()})
        # Ordered by the caller's ids, not by whatever _gather_by_id finished
        # first, so the answer does not reshuffle between identical calls.
        readable = [str(jid) for jid in target if str(jid) in records]
        await _progress(requests_used, max_requests, f"read {len(readable)} job records")

        # --- Phase B: the stage vocabulary ----------------------------------
        #
        # One shared request for the whole call. Without it every stage below
        # is an account-specific integer, which is not a snapshot of anything.
        titles: dict[str, str] = {}
        if readable and afford(1):
            titles, status_calls = await _status_titles(client)
            requests_used += status_calls
        if stage_terms and not titles:
            errors["stages"] = (
                "the workflow vocabulary could not be read, so stage titles could not be "
                "matched. Pass stage_status_ids to select stages by id instead."
            )

        # --- Phase C: the pipelines -----------------------------------------
        pipelines: dict[str, list[dict[str, Any]]] = {}
        totals: dict[str, int | None] = {}
        for key in readable:
            rows: list[dict[str, Any]] = []
            total: int | None = None
            page = 1
            while page <= MAX_PIPELINE_PAGES:
                if not afford(1):
                    truncated = True
                    errors.setdefault(
                        "pipelines",
                        "the budget stopped the pipeline sweep; stage counts are partial "
                        "for at least one job. Raise max_requests.",
                    )
                    break
                try:
                    payload = await client.request(
                        "GET",
                        f"/jobs/{key}/pipelines",
                        params={"per_page": PIPELINE_PAGE_SIZE, "page": page},
                    )
                except CATSAPIError as exc:
                    errors[f"pipelines:{key}:page:{page}"] = str(exc)
                    break
                requests_used += 1
                if total is None and isinstance(payload, dict):
                    total = payload.get("total")
                page_rows = _embedded_rows(payload)
                rows.extend(page_rows)
                if not page_rows or not _has_next_page(payload):
                    break
                page += 1
            else:
                logger.warning("pipeline sweep hit the %s-page cap", MAX_PIPELINE_PAGES)
                truncated = True
                errors.setdefault(
                    "pipelines",
                    f"a pipeline exceeded the {MAX_PIPELINE_PAGES}-page cap; its stage "
                    f"counts are partial.",
                )
            pipelines[key] = rows
            totals[key] = total
        await _progress(requests_used, max_requests, f"read pipelines for {len(pipelines)} jobs")

        # --- which stages the caller asked to see people at ------------------
        selected_status_ids: set[str] = set()
        stages_matched: list[MatchEvidence] = []
        for status_id in exact_stage_ids:
            selected_status_ids.add(str(status_id))
            stages_matched.append(
                MatchEvidence(
                    field="pipeline_status",
                    source=str(status_id),
                    value=titles.get(str(status_id)) or None,
                    matched=str(status_id),
                    mode="exact",
                )
            )
        for term in stage_terms:
            folded = _fold(term)
            for status_id, title in titles.items():
                if folded and folded in _fold(title):
                    selected_status_ids.add(str(status_id))
                    stages_matched.append(
                        MatchEvidence(
                            field="pipeline_status",
                            source=str(status_id),
                            value=title,
                            matched=term,
                            mode="contains",
                        )
                    )
        # No selector means every stage. Listing nobody would make the common
        # single-job snapshot answer "there are 12 candidates" and refuse to say
        # who they are.
        select_all = not selected_status_ids and not stage_terms and not exact_stage_ids

        # --- the rows, and the stage counts they produce ----------------------
        listed: dict[str, list[CandidateAtStage]] = {}
        counts_by_job: dict[str, list[StageCount]] = {}
        selected_totals: dict[str, int] = {}
        for key in readable:
            tally: dict[str, int] = {}
            first_seen: dict[str, Any] = {}
            chosen: list[dict[str, Any]] = []
            for row in pipelines.get(key, []):
                status_id = row.get("status_id")
                status_key = str(status_id) if status_id is not None else ""
                tally[status_key] = tally.get(status_key, 0) + 1
                first_seen.setdefault(status_key, status_id)
                if select_all or status_key in selected_status_ids:
                    chosen.append(row)

            counts_by_job[key] = [
                StageCount(
                    status_id=first_seen[status_key],
                    # Null, never invented: an id the workflow vocabulary did
                    # not name is a gap the caller can resolve, and a plausible
                    # guess at a stage name is worse than an honest one.
                    status=titles.get(status_key) or None,
                    candidates=tally[status_key],
                )
                for status_key in sorted(tally)
            ]
            selected_totals[key] = len(chosen)
            if len(chosen) > max_candidates_per_job:
                truncated = True
            listed[key] = [
                CandidateAtStage(
                    candidate_id=row.get("candidate_id"),
                    pipeline_id=row.get("id"),
                    status_id=row.get("status_id"),
                    status=titles.get(str(row.get("status_id"))) or None,
                    name=_embedded_candidate_name(row),
                    date_modified=_text(row.get("date_modified")),
                )
                for row in chosen[:max_candidates_per_job]
            ]

        # --- Phase D: job custom fields --------------------------------------
        custom: dict[str, list[JobCustomField]] = {}
        if "custom_fields" in wanted and readable:
            custom, used = await _job_extra(
                client, readable, "custom_fields", afford, errors, _custom_fields
            )
            requests_used += used

        # --- Phase E: job tasks ----------------------------------------------
        tasks: dict[str, list[JobTask]] = {}
        if "tasks" in wanted and readable:
            tasks, used = await _job_extra(
                client, readable, "tasks", afford, errors, lambda payload: _tasks(payload, now)
            )
            requests_used += used
        if custom or tasks:
            await _progress(requests_used, max_requests, "read job-level extras")

        # --- Phase F: per-candidate enrichment, one request each -------------
        per_candidate = [option for option in PER_CANDIDATE_INCLUDES if option in wanted]
        unenriched: dict[str, int] = {}
        if per_candidate:
            everyone = _dedupe(
                [
                    row.candidate_id
                    for key in readable
                    for row in listed.get(key, [])
                    if row.candidate_id is not None
                ]
            )
            pool = everyone[:max_candidate_reads]
            capped_out = len(everyone) - len(pool)

            for option in per_candidate:
                budget = afford(len(pool))
                affordable_ids = pool[:budget]
                skipped = capped_out + (len(pool) - len(affordable_ids))
                if skipped:
                    unenriched[option] = skipped
                    truncated = True
                    errors[option] = (
                        f"read {len(affordable_ids)} of {len(everyone)} listed candidates "
                        f"for {option}; {skipped} were never read and are counted in "
                        f"`unenriched`, not reported as having nothing. Raise "
                        f"max_candidate_reads and max_requests, or narrow `stages`."
                    )
                if not affordable_ids:
                    continue

                tail = "" if option == "identity" else "/activities"
                params = None if option == "identity" else {"per_page": 100}

                async def fetch(cid: int | str, tail: str = tail, params: Any = params) -> Any:
                    return await client.request("GET", f"/candidates/{cid}{tail}", params=params)

                found, fetch_errors = await _gather_by_id(list(affordable_ids), fetch)
                requests_used += len(affordable_ids)
                errors.update({f"{option}:{k}": v for k, v in fetch_errors.items()})

                for key in readable:
                    for row in listed.get(key, []):
                        payload = found.get(str(row.candidate_id))
                        if payload is None:
                            continue
                        if option == "identity":
                            record = _project(payload, ["title", "city", "state"])
                            row.name = _text(payload) or row.name
                            row.title = _text(record.get("title"))
                            row.city = _text(record.get("city"))
                            row.state = _text(record.get("state"))
                        else:
                            row.latest_activity = _latest_activity(payload)
                await _progress(
                    requests_used, max_requests, f"read {option} for {len(affordable_ids)} people"
                )

        # --- assemble ---------------------------------------------------------
        snapshots: list[JobSnapshot] = []
        for key in readable:
            record = records[key]
            projected = _project(record, JOB_FIELDS)
            job_tasks = tasks.get(key, [])
            snapshots.append(
                JobSnapshot(
                    job_id=record.get("id", key),
                    title=_text(projected.get("title")),
                    status_id=projected.get("status_id"),
                    status=_first_text(record, STATUS_KEYS),
                    company_id=projected.get("company_id"),
                    company=_first_text(record, COMPANY_KEYS),
                    owner_id=projected.get("owner_id"),
                    owner=_first_text(record, OWNER_KEYS),
                    city=_text(projected.get("city")),
                    state=_text(projected.get("state")),
                    date_created=_text(projected.get("date_created")),
                    date_modified=_text(projected.get("date_modified")),
                    custom_fields=custom.get(key, []),
                    total_candidates=(
                        totals.get(key)
                        if totals.get(key) is not None
                        else len(pipelines.get(key, []))
                    ),
                    candidates_counted=len(pipelines.get(key, [])),
                    stage_counts=counts_by_job.get(key, []),
                    candidates_selected=selected_totals.get(key, 0),
                    candidates=listed.get(key, []),
                    tasks=job_tasks,
                    task_count=len(job_tasks),
                    overdue_task_count=sum(1 for task in job_tasks if task.overdue),
                )
            )

        result = JobSnapshotResult(
            jobs=snapshots,
            count=len(snapshots),
            requested=len(job_ids or []),
            stages_requested=stage_terms + [str(i) for i in exact_stage_ids],
            stages_matched=stages_matched,
            included=wanted,
            unenriched=unenriched,
            execution=ExecutionFacts(
                requests_used=requests_used,
                rate_limit=RateLimit.model_validate(client.rate_limit.snapshot()),
                truncated=truncated,
                # Nothing resumable: the snapshot is rebuilt from the job ids
                # each call, so there is no position to hand back. Null means
                # "no continuation exists"; `unenriched` and
                # `candidates_counted` are what report the gaps.
                next_cursor=None,
                errors=errors,
            ),
            note=RESULT_NOTE,
        )
        return ToolResult(content=_content_line(result), structured_content=result.model_dump())

    return 1


async def _job_extra(
    client: Any,
    keys: list[str],
    tail: str,
    afford: Callable[[int], int],
    errors: dict[str, str],
    parse: Callable[[Any], Any],
) -> tuple[dict[str, Any], int]:
    """One per-job sub-collection read, bounded by the remaining budget.

    Shared by custom fields and tasks because they differ only in the endpoint
    tail and how the rows are parsed - and because the budget arithmetic is the
    part worth having in one place rather than two.
    """
    budget = afford(len(keys))
    affordable = keys[:budget]
    if len(affordable) < len(keys):
        errors[tail] = (
            f"budget covered {len(affordable)} of {len(keys)} jobs for {tail}; the rest "
            f"were not read. Raise max_requests."
        )

    async def fetch(jid: int | str) -> Any:
        return await client.request("GET", f"/jobs/{jid}/{tail}")

    found, fetch_errors = await _gather_by_id(list(affordable), fetch)
    errors.update({f"{tail}:{key}": value for key, value in fetch_errors.items()})
    return {key: parse(payload) for key, payload in found.items()}, len(affordable)


def _content_line(result: JobSnapshotResult) -> str:
    """The one line a human sees. Counts and flags, never record data.

    The structured payload is the answer; repeating it here would double the
    tokens for nothing, and putting a job title, a client name or a candidate
    name in it would hand a reader a sample of the data as though it were the
    point.
    """
    parts = [
        f"jobs {result.count}",
        f"candidates {sum(len(job.candidates) for job in result.jobs)}",
        f"stages {sum(len(job.stage_counts) for job in result.jobs)}",
        f"requests {result.execution.requests_used}",
    ]
    overdue = sum(job.overdue_task_count for job in result.jobs)
    if overdue:
        parts.append(f"overdue tasks {overdue}")
    if result.execution.truncated:
        parts.append("truncated")
    if result.unenriched:
        parts.append(f"unenriched {sum(result.unenriched.values())}")
    if result.execution.errors:
        parts.append(f"errors {len(result.execution.errors)}")
    return ", ".join(parts)
