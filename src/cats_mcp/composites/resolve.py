"""Resolving a job reference to every record it could mean.

Issue #12, item 4. Nobody names a job the way CATS files it. They say "the
Artemis job", "the Logan Lake posting", "Kim's req" - a client name, a site held
in a custom field, a tag, an owner. The atomic surface answers a different
question: `search_jobs` takes one free-text query the API applies where it
chooses, and `filter_jobs` takes one field, one filter, one value. Neither can
be pointed at "wherever this reference happens to live".

The failure that follows is quiet and expensive. The caller sweeps titles, finds
the one job whose title contains the word, and proceeds - while two more Artemis
jobs sit in the account under a client name and a site custom field. Nothing
errors. The work simply lands on the wrong req, and the first sign of it is a
person submitted to a posting in the wrong town.

So this tool looks in every field a job reference can live in, and returns
*every* record that matched with the evidence for each. It has no notion of a
closer match, it does not order the result, and it never collapses several
plausible records into one. Three Artemis jobs come back as three rows saying
which field named Artemis; deciding which one the caller meant is the caller's
job, and being told there are three is the entire point.

Cost model, and why the phases are ordered the way they are:

    Phase A  the pool. One request per page of 100 jobs, or one per exact seed
             value per page when the caller narrows with status_ids or
             seed_field. Bounded by max_requests.
    Phase B  the free fields - title, company, location, owner, description.
             They ride along on rows already fetched, so they cost nothing.
    Phase C  custom fields and tags, one CATS request per job each. Spent only
             on the jobs no free field matched, because a job already found does
             not need finding again; and jobs the budget could not reach are
             counted in `unsearched` rather than passed off as misses.

Phase C is where the tool earns its keep - it is how the second and third
Artemis job get found - which is why it runs by default rather than on request,
and why the budget that bounds it is reported alongside the answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field

from cats_mcp.composites.models import ExecutionFacts, MatchEvidence, RateLimit
from cats_mcp.composites.reads import (
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _has_next_page,
    _progress,
    _project,
)
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError

logger = get_logger(__name__)

#: Jobs per page. CATS honours this, so an account of 400 openings is four
#: requests rather than sixteen at the default of 25.
POOL_PAGE_SIZE = 100

#: Default and hard ceiling on requests for one call.
#:
#: The CATS standard allowance is 500 requests/hour, and Phase C can always find
#: another job to read, so the sweep stops at a number the caller chose and says
#: what it did not reach. A silent stop is the one outcome this tool cannot
#: have: a reference that resolved against half the account is not resolved.
DEFAULT_MAX_REQUESTS = 25
MAX_REQUESTS_CEILING = 100

#: Default and hard ceiling on matched jobs returned by one call.
DEFAULT_MAX_JOBS = 25
MAX_JOBS_CEILING = 200

#: Hard stop on job rows held in the pool, whatever the budget allows. Not a
#: caller knob - it exists so a very large account cannot be pulled into memory
#: wholesale by a generous max_requests.
MAX_POOL = 500

#: Longest stored value returned verbatim as evidence. A job description runs to
#: kilobytes and the caller needs to see the hit, not the document, so anything
#: longer comes back as a window around the match.
SNIPPET_LIMIT = 200
SNIPPET_RADIUS = 60

#: Where a job reference can live, and what looking there costs.
#:
#: The asymmetry drives the phase order: the free fields are already on the rows
#: the pool sweep fetched, and the paid ones are a request each.
SEARCHABLE_FIELDS: dict[str, str] = {
    "title": "the job title - free, carried by every row",
    "company": "the client company name - free, from the row or its _embedded company",
    "location": "city, state, postal code, department - free",
    "owner": "the owner, recruiter or contact named on the row - free",
    "description": "the job description and notes - free when the row carries them",
    "custom_fields": "account-specific fields such as a site or region - one request per job",
    "tags": "the job's tags - one request per job",
}

#: Fields answerable from a row already in hand.
FREE_FIELDS = ("title", "company", "location", "owner", "description")

#: Fields that cost one CATS request per job, and the endpoint tail each needs.
PAID_FIELDS: dict[str, str] = {"custom_fields": "custom_fields", "tags": "tags"}

#: Row keys consulted for each free field, in order. Several are listed per
#: field on purpose: accounts differ on whether the client is `company_name` on
#: the row or a `_embedded.company` object, and reading only one of them
#: reports "no company match" for accounts shaped the other way - a silent wrong
#: answer on the field this tool exists to search.
FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    "title": ("title", "name"),
    "company": ("company", "company_name", "client", "client_name"),
    "location": ("city", "state", "location", "postal_code", "department", "region"),
    "owner": ("owner", "owner_name", "recruiter", "recruiter_name", "contact", "contact_name"),
    "description": ("description", "notes", "requirements", "summary"),
}

#: Projected onto every returned row. Whatever a given account omits simply does
#: not appear - `_project` keeps only the keys the record actually carried.
OUTPUT_FIELDS = [
    "id",
    "title",
    "status_id",
    "city",
    "state",
    "company_id",
    "owner_id",
    "date_modified",
]

_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")


class TextPredicate(BaseModel):
    """The caller's reference to a job, as a condition rather than a string.

    Modelled so the AND/OR semantics live in the JSON schema instead of prose a
    caller has to guess at. The values are the caller's vocabulary: this module
    has no gazetteer, no client-name list and no taxonomy, so "Artemis" and
    "Artemis Mining Ltd" are two strings it will look for and nothing more.
    """

    values: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Terms to look for. These are yours - the adapter will not expand, "
                "stem or synonym-match them."
            ),
        ),
    ]
    match: Annotated[
        Literal["any", "all"],
        Field(description="'any' is OR across values (the usual case); 'all' is AND."),
    ] = "any"
    mode: Annotated[
        Literal["contains", "exact", "prefix"],
        Field(
            description=(
                "How each value is compared, after case, punctuation and whitespace "
                "are normalised. 'contains' is substring, not CATS token matching - "
                "it will not return Williams Lake for 'Logan Lake'."
            )
        ),
    ] = "contains"


class SeedUsed(BaseModel):
    """One `exactly` filter the pool sweep was narrowed by.

    Reported back because the pool is the universe this answer was computed
    over: a caller who cannot see that the sweep was seeded to two statuses
    cannot tell "no other Artemis job exists" from "no other Artemis job has
    those statuses".
    """

    field: str = Field(description="The CATS job field the filter was applied to.")
    value: int | str | None = Field(
        default=None, description="The exact value filtered on, as the caller gave it."
    )


class ResolvedJob(BaseModel):
    """One job the reference could mean, with the evidence for it.

    There is deliberately nowhere in this model to record how *well* the job
    matched. The fields are the record's own identifiers plus `matched_fields`;
    ordering carries no meaning and the first row is not a nomination. Adding a
    closeness measure here would turn every caller's "return them all" into
    "take the top one", which is the failure this tool exists to prevent.
    """

    job_id: int | str | None = Field(default=None, description="The CATS job id.")
    title: str | None = Field(default=None, description="The job title as CATS stores it.")
    status_id: int | str | None = Field(default=None, description="The job's status id.")
    city: str | None = Field(default=None, description="City on the job record.")
    state: str | None = Field(default=None, description="State or province on the job record.")
    company_id: int | str | None = Field(default=None, description="The client company's id.")
    owner_id: int | str | None = Field(default=None, description="The owning user's id.")
    date_modified: str | None = Field(default=None, description="When CATS last changed the job.")
    company: str | None = Field(
        default=None, description="The client company name, from the row or its _embedded company."
    )
    matched_fields: list[MatchEvidence] = Field(
        default_factory=list,
        description=(
            "Why this job is here: at least one field that matched, with the stored "
            "value. Not necessarily every field that would have matched - once a free "
            "field finds a job, no per-job request is spent collecting more."
        ),
    )


class ResolveResult(BaseModel):
    """Every job the reference could mean, and what the sweep actually covered.


    The counts are not decoration. `total_matched` against `count` says whether
    rows were left out; `ambiguous` says the reference named more than one
    record; `scanned` and `unsearched` say how much of the account the answer
    was computed over. A caller that cannot read those cannot tell a complete
    answer from a confident partial one.
    """

    jobs: list[ResolvedJob] = Field(
        default_factory=list,
        description="Every job that matched, in the order CATS returned it. That "
        "order means nothing.",
    )
    count: int = Field(description="Jobs in `jobs`. Compare with total_matched.")
    ambiguous: bool = Field(
        description=(
            "True when more than one record answered to the reference. A derived "
            "fact, not an opinion: the caller asked for 'the Artemis job' and there "
            "are three."
        )
    )
    total_matched: int = Field(
        description="How many jobs matched in total, even when max_jobs capped `jobs`."
    )
    scanned: int = Field(description="Job rows the pool sweep actually read.")
    fields_searched: list[str] = Field(
        default_factory=list, description="The fields this call looked in."
    )
    seeds_used: list[SeedUsed] = Field(
        default_factory=list, description="The exact filters the pool was narrowed by, if any."
    )
    unsearched: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per field, how many unmatched jobs the budget never looked at. These are "
            "unknown, not misses - a non-zero count means this answer may be short a "
            "record."
        ),
    )
    execution: ExecutionFacts = Field(
        description="What the call spent and what it could not finish."
    )
    note: str = Field(description="How to read this result.")


#: Prose returned with every result. Kept out of the display content, which
#: stays a counts-only line - see `_content_line`.
RESULT_NOTE = (
    "Every record that matched is returned, in the order CATS returned them - "
    "that order means nothing and the first row is not a nomination. "
    "`matched_fields` is the evidence for each; interpreting it is the "
    "caller's job. It names at least one field that matched, not necessarily "
    "every one: once a job is found by a free field, no per-job request is "
    "spent on it to collect more. `unsearched` counts jobs the budget never "
    "looked at for a given field - those are unknown, not misses, and a "
    "non-zero count means this answer may be short a record."
)


def _normalise(value: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Nothing else.

    This is the only transformation applied to job text. It makes "Artemis
    Mining Ltd." and "artemis mining ltd" the same string, which is
    normalisation. Deciding that "Artemis" and "ARTM-2024" are the same project
    is not, and belongs to the caller.
    """
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.lower())).strip()


def _evaluate(text: Any, predicate: TextPredicate) -> tuple[bool, list[str]]:
    """Apply the predicate to one field's text. Returns (satisfied, which values hit).

    'all' is evaluated within a single field: every value must appear in the
    same piece of text. Spreading it across fields would make "all" mean
    something a caller cannot see the shape of from the evidence returned.
    """
    haystack = _normalise(text)
    hits: list[str] = []
    needles = [(raw, _normalise(raw)) for raw in predicate.values]
    usable = [(raw, n) for raw, n in needles if n]

    if not haystack:
        return False, []

    for raw, needle in usable:
        if predicate.mode == "exact":
            found = haystack == needle
        elif predicate.mode == "prefix":
            found = haystack.startswith(needle)
        else:
            found = needle in haystack
        if found:
            hits.append(raw)

    if not usable:
        return False, []
    satisfied = len(hits) == len(usable) if predicate.match == "all" else bool(hits)
    return satisfied, hits


def _snippet(text: str, needle: str) -> str:
    """A bounded window of the stored value around the hit.

    Evidence has to show what actually matched. Returning a whole job
    description to prove one word appeared in it would push the thing the caller
    is reading out of view, and truncating from the front would frequently cut
    off the hit itself.
    """
    if len(text) <= SNIPPET_LIMIT:
        return text
    position = text.lower().find(needle.lower())
    if position < 0:
        return text[:SNIPPET_LIMIT] + "..."
    start = max(0, position - SNIPPET_RADIUS)
    end = min(len(text), position + len(needle) + SNIPPET_RADIUS)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def _text_values(value: Any) -> list[str]:
    """Every string hiding in a job field, whatever shape CATS returned it in.

    Numbers are deliberately not stringified. An owner id or a salary is not a
    name, and substring-matching a caller's "12" against an id would manufacture
    matches that read exactly like real ones.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        out: list[str] = []
        full = " ".join(
            str(value.get(key) or "").strip() for key in ("first_name", "last_name")
        ).strip()
        if full:
            out.append(full)
        for key in ("name", "title", "value", "label", "text", "email_address"):
            out.extend(_text_values(value.get(key)))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_text_values(item))
        return out
    return []


def _row_texts(row: dict[str, Any], field: str) -> list[tuple[str, str]]:
    """(source key, text) for one free field of one job row."""
    embedded = row.get("_embedded") if isinstance(row.get("_embedded"), dict) else {}
    out: list[tuple[str, str]] = []
    for key in FIELD_SOURCES[field]:
        value = row.get(key)
        if value is None and isinstance(embedded, dict):
            value = embedded.get(key)
        for text in _text_values(value):
            out.append((key, text))
    return out


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Rows from a HAL collection, or from a bare list some endpoints return."""
    rows = _embedded_rows(payload)
    if rows:
        return rows
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _custom_field_texts(payload: Any) -> list[tuple[str, str]]:
    """(field label, stored value) for a job's custom fields.

    Only the value is matched. Searching the labels too would match every job
    that merely *has* a field called Site against a caller looking for a site,
    which is a false positive on the exact field this tool was added to search.
    """
    out: list[tuple[str, str]] = []
    for row in _rows(payload):
        label = row.get("name") or row.get("title") or row.get("field_name")
        source = str(label) if label else "custom_field"
        for text in _text_values(row.get("value")):
            out.append((source, text))
    return out


def _tag_texts(payload: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in _rows(payload):
        for text in _text_values(row.get("title") or row.get("name")):
            out.append(("tag", text))
    return out


def _company_name(row: dict[str, Any]) -> str | None:
    embedded = row.get("_embedded") if isinstance(row.get("_embedded"), dict) else {}
    for key in ("company", "company_name", "client", "client_name"):
        value = row.get(key)
        if value is None and isinstance(embedded, dict):
            value = embedded.get(key)
        names = _text_values(value)
        if names:
            return names[0]
    return None


def _seed_plan(
    status_ids: list[int | str] | None,
    seed_field: str | None,
    seed_values: list[str] | None,
) -> list[tuple[str, Any]]:
    """(field, value) pairs to seed from, one `exactly` filter each.

    One filter per value, always exact. The CATS `contains` filter tokenises the
    value and matches ANY token, so a single contains="Logan Lake" also returns
    Williams Lake, Slave Lake and Deer Lake - no error, just extra rows that
    look entirely plausible. Every multi-word value therefore gets its own
    `exactly` filter and its own request.
    """
    plan: list[tuple[str, Any]] = []
    for value in _dedupe(list(status_ids or [])):
        plan.append(("status_id", value))
    if seed_field:
        field = seed_field.strip()
        for value in _dedupe(list(seed_values or [])):
            if str(value).strip():
                plan.append((field, str(value).strip()))
    return plan


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the job resolution primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="resolve_job",
        # Passed explicitly because the function returns ToolResult, which on its
        # own leaves the tool with no output schema at all. Declaring it here is
        # what gives the caller a typed, object-rooted contract while the display
        # content stays a single counts line instead of a second copy of the
        # payload.
        output_schema=ResolveResult.model_json_schema(),
        description=(
            "Resolve a job reference - 'the Artemis job', a site name, a client, an "
            "id - to every CATS job it could plausibly mean, with the evidence for "
            "each.\n\n"
            "A reference usually names something that is not in the title: the client "
            "company, a site or region held in a custom field, a tag, the owner, or a "
            "phrase from the description. CATS has no cross-field job search, so this "
            "sweeps the job list once and matches locally across all of those.\n\n"
            "Returns every record that matched, never one. Each row carries "
            "`matched_fields` saying which field matched and what the stored value "
            "was. If three jobs mention Artemis you get three rows: this tool does not "
            "choose between them, publishes no ordering, and offers no measure of "
            "closeness, because silently picking one of three is the failure it exists "
            "to prevent. Interpreting them is the caller's job.\n\n"
            "Cost: title, company, location, owner and description are free - they "
            "ride along on rows already fetched. Custom fields and tags are one CATS "
            "request per job, so they are read only for jobs no free field matched, "
            "and only as far as max_requests allows. `execution.requests_used` says "
            "what was spent and `unsearched` counts the jobs the budget never looked "
            "at - an unreached job is unknown, not a job that failed to match.\n\n"
            "Pass job_id instead when you already have one; that is a single direct "
            "read and no sweep happens."
        ),
        tags={"ats", "job", "read", "search"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def resolve_job(
        query: Annotated[
            TextPredicate | None,
            Field(
                description=(
                    "The reference to resolve, applied to every field in `fields`. "
                    "Pass the wordings you mean - ['Artemis', 'Artemis Mining'] - "
                    "because nothing here knows they are one client."
                )
            ),
        ] = None,
        job_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Resolve a known id directly. One request, one row, no sweep. "
                    "Takes precedence over query."
                )
            ),
        ] = None,
        fields: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Where to look. "
                    + "; ".join(f"'{k}': {v}" for k, v in SEARCHABLE_FIELDS.items())
                    + ". Defaults to all of them; narrow it to the free ones to spend "
                    "no per-job requests."
                )
            ),
        ] = None,
        status_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Narrow the pool to these job statuses before matching - one exact "
                    "CATS filter per value. Find ids with list_job_statuses."
                )
            ),
        ] = None,
        seed_field: Annotated[
            str | None,
            Field(
                description=(
                    "Any other CATS job field to narrow the pool by, used with "
                    "seed_values. Field names are account-specific; discover them with "
                    "list_job_custom_field_definitions or filter_jobs."
                )
            ),
        ] = None,
        seed_values: Annotated[
            list[str] | None,
            Field(description="Exact values for seed_field, one filter each."),
        ] = None,
        max_jobs: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_JOBS_CEILING,
                description=(
                    f"Matched jobs to return. Ceiling {MAX_JOBS_CEILING}. "
                    "`total_matched` always reports how many there really were."
                ),
            ),
        ] = DEFAULT_MAX_JOBS,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}. "
                    "The CATS allowance is 500 requests/hour."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
    ) -> ToolResult:
        set_run_id()
        client = client_getter()

        if query is None and job_id is None:
            raise ValueError(
                "Pass query (the reference to resolve) or job_id (a known id). "
                "Without either this would sweep every job in the account and match "
                "nothing against them."
            )
        if seed_values and not seed_field:
            raise ValueError("seed_values needs seed_field: name the CATS job field to filter.")

        requested = list(SEARCHABLE_FIELDS) if fields is None else fields
        wanted = [f.strip().lower() for f in requested if f.strip()]
        unknown = [f for f in wanted if f not in SEARCHABLE_FIELDS]
        if unknown:
            raise ValueError(
                f"Unknown fields {unknown}. Valid options: {sorted(SEARCHABLE_FIELDS)}."
            )

        errors: dict[str, str] = {}
        requests_used = 0

        # --- the direct id read ---------------------------------------------
        #
        # An id is the one unambiguous reference there is, so it short-circuits
        # the sweep entirely. The result keeps the same shape as a resolved
        # query - a list of rows with evidence - so a caller never has to branch
        # on which way it was asked.
        if job_id is not None:
            rows: list[ResolvedJob] = []
            try:
                record = await client.request("GET", f"/jobs/{job_id}")
                requests_used += 1
            except CATSAPIError as exc:
                errors[f"job:{job_id}"] = str(exc)
                record = None

            if isinstance(record, dict) and record:
                rows.append(
                    _output_row(
                        record,
                        [
                            MatchEvidence(
                                field="job_id",
                                source="id",
                                value=str(record.get("id", job_id)),
                                matched=str(job_id),
                                mode="exact",
                            )
                        ],
                    )
                )

            return _result(
                client,
                rows=rows,
                total_matched=len(rows),
                scanned=len(rows),
                fields_searched=["job_id"],
                seeds_used=[SeedUsed(field="id", value=job_id)],
                unsearched={},
                truncated=False,
                errors=errors,
                requests_used=requests_used,
            )

        # --- Phase A: the pool ----------------------------------------------
        plan = _seed_plan(status_ids, seed_field, seed_values)
        # No seed means the whole job list, because the reference may live in a
        # field CATS cannot filter on - which is the case this tool was written
        # for. `None` is that stream.
        streams: list[tuple[str, Any] | None] = list(plan) or [None]

        pool: dict[str, dict[str, Any]] = {}
        scanned = 0
        truncated = False

        for stream in streams:
            page = 1
            while True:
                if len(pool) >= MAX_POOL:
                    logger.warning("job pool hit the %s-row ceiling; narrow the sweep", MAX_POOL)
                    truncated = True
                    break
                if requests_used >= max_requests:
                    truncated = True
                    break
                label = "jobs" if stream is None else f"seed:{stream[0]}={stream[1]}"
                try:
                    if stream is None:
                        payload = await client.request(
                            "GET", "/jobs", params={"per_page": POOL_PAGE_SIZE, "page": page}
                        )
                    else:
                        # `exactly`, one filter per value: CATS `contains`
                        # tokenises and matches any token, so contains="Logan
                        # Lake" also returns Williams Lake.
                        payload = await client.request(
                            "POST",
                            "/jobs/search",
                            json={"field": stream[0], "filter": "exactly", "value": stream[1]},
                            params={"per_page": POOL_PAGE_SIZE, "page": page},
                        )
                    requests_used += 1
                    await _progress(requests_used, max_requests, f"scanning jobs, page {page}")
                except CATSAPIError as exc:
                    errors[f"{label}:page:{page}"] = str(exc)
                    break

                page_rows = _embedded_rows(payload)
                scanned += len(page_rows)
                for row in page_rows:
                    identifier = row.get("id")
                    if identifier is not None:
                        pool.setdefault(str(identifier), row)

                if not page_rows or not _has_next_page(payload):
                    break
                page += 1
            if truncated:
                break

        # --- Phase B: the free fields, on rows already in hand ---------------
        free_wanted = [f for f in FREE_FIELDS if f in wanted]
        evidence: dict[str, list[MatchEvidence]] = {}
        for key, row in pool.items():
            for field in free_wanted:
                for source, text in _row_texts(row, field):
                    satisfied, hits = _evaluate(text, query)
                    if not satisfied:
                        continue
                    for hit in hits:
                        _add_evidence(evidence.setdefault(key, []), field, source, text, hit, query)

        # --- Phase C: the paid fields, on jobs nothing free matched ----------
        #
        # A job already found does not need finding again, so the per-job reads
        # go to the remainder. That is the search: it is how an Artemis job
        # filed under a site custom field turns up at all.
        paid_wanted = [f for f in PAID_FIELDS if f in wanted]
        unsearched: dict[str, int] = {}
        if paid_wanted:
            for field in paid_wanted:
                # Recomputed per field: a job the custom-field pass just matched
                # does not need a tag read to be returned.
                remainder = _dedupe([k for k in pool if k not in evidence])
                if not remainder:
                    break
                budget = max(0, max_requests - requests_used)
                affordable = remainder[:budget]
                skipped = remainder[budget:]
                if skipped:
                    # Counted at the top level, not on a row: a job the budget
                    # never reached cannot appear in the rows, because nothing
                    # matched it. This count is the only thing standing between
                    # the caller and "there is one Artemis job" when there are
                    # three.
                    unsearched[field] = len(skipped)
                    truncated = True
                    errors[field] = (
                        f"budget covered {len(affordable)} of {len(remainder)} unmatched "
                        f"jobs; {len(skipped)} were never searched for {field} and are "
                        f"counted in `unsearched`, not reported as misses. Raise "
                        f"max_requests or narrow the pool with status_ids or seed_field."
                    )

                tail = PAID_FIELDS[field]
                extractor = _custom_field_texts if field == "custom_fields" else _tag_texts

                async def fetch(jid: int | str, tail: str = tail) -> Any:
                    return await client.request("GET", f"/jobs/{jid}/{tail}")

                found, fetch_errors = await _gather_by_id(list(affordable), fetch)
                errors.update({f"{field}:{k}": v for k, v in fetch_errors.items()})
                requests_used += len(affordable)

                for key, payload in found.items():
                    for source, text in extractor(payload):
                        satisfied, hits = _evaluate(text, query)
                        if not satisfied:
                            continue
                        for hit in hits:
                            _add_evidence(
                                evidence.setdefault(key, []), field, source, text, hit, query
                            )

        # --- assemble, in the order CATS returned the rows -------------------
        matched = [key for key in pool if key in evidence]
        total_matched = len(matched)
        if total_matched > max_jobs:
            truncated = True
        rows = [_output_row(pool[key], evidence[key]) for key in matched[:max_jobs]]

        return _result(
            client,
            rows=rows,
            total_matched=total_matched,
            scanned=scanned,
            fields_searched=wanted,
            seeds_used=[SeedUsed(field=f, value=v) for f, v in plan],
            unsearched=unsearched,
            truncated=truncated,
            errors=errors,
            requests_used=requests_used,
        )

    return 1


def _add_evidence(
    bucket: list[MatchEvidence],
    field: str,
    source: str,
    text: str,
    hit: str,
    predicate: TextPredicate,
) -> None:
    """Record one match, without repeating an identical one."""
    entry = MatchEvidence(
        field=field,
        source=source,
        value=_snippet(text, hit),
        matched=hit,
        mode=predicate.mode,
    )
    if entry not in bucket:
        bucket.append(entry)


def _as_text(value: Any) -> str | None:
    """A stored value as display text, or None. Never raises on an odd shape.

    CATS is account-shaped: a field this adapter expects to be a string can
    arrive as a number on somebody's account. Coercing here keeps that from
    turning a resolvable reference into a validation error on the way out.
    """
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _output_row(record: dict[str, Any], evidence: list[MatchEvidence]) -> ResolvedJob:
    projected = _project(record, OUTPUT_FIELDS)
    return ResolvedJob(
        job_id=record.get("id"),
        title=_as_text(projected.get("title")),
        status_id=projected.get("status_id"),
        city=_as_text(projected.get("city")),
        state=_as_text(projected.get("state")),
        company_id=projected.get("company_id"),
        owner_id=projected.get("owner_id"),
        date_modified=_as_text(projected.get("date_modified")),
        company=_company_name(record),
        matched_fields=evidence,
    )


def _content_line(result: ResolveResult) -> str:
    """The one line a human sees. Counts and flags, never record data.

    The structured payload is the answer; repeating it here would double the
    tokens for nothing, and putting a job title or a client name in it would
    hand a reader the first row as though it were the pick.
    """
    parts = [
        f"matched {result.total_matched}",
        f"returned {result.count}",
        f"scanned {result.scanned}",
        f"requests {result.execution.requests_used}",
    ]
    if result.ambiguous:
        parts.append("ambiguous")
    if result.execution.truncated:
        parts.append("truncated")
    if result.unsearched:
        parts.append(f"unsearched {sum(result.unsearched.values())}")
    if result.execution.errors:
        parts.append(f"errors {len(result.execution.errors)}")
    return ", ".join(parts)


def _result(
    client: Any,
    *,
    rows: list[ResolvedJob],
    total_matched: int,
    scanned: int,
    fields_searched: list[str],
    seeds_used: list[SeedUsed],
    unsearched: dict[str, int],
    truncated: bool,
    errors: dict[str, str],
    requests_used: int,
) -> ToolResult:
    """One shape for both paths, so a caller never branches on how it asked."""
    result = ResolveResult(
        jobs=rows,
        count=len(rows),
        # A derived fact, not an opinion: more than one record answered to the
        # reference. The caller asked for "the Artemis job" and there are three.
        ambiguous=total_matched > 1,
        total_matched=total_matched,
        scanned=scanned,
        fields_searched=fields_searched,
        seeds_used=seeds_used,
        unsearched=unsearched,
        execution=ExecutionFacts(
            requests_used=requests_used,
            rate_limit=RateLimit.model_validate(client.rate_limit.snapshot()),
            truncated=truncated,
            # This sweep has no resumable cursor: the pool is rebuilt from the
            # seeds each call, so there is no position to hand back. Null here
            # means "no continuation exists", and `unsearched` is what says the
            # answer may be short a record.
            next_cursor=None,
            errors=errors,
        ),
        note=RESULT_NOTE,
    )
    return ToolResult(content=_content_line(result), structured_content=result.model_dump())
