"""The job facts a candidate query gets built from.

Issue #12, item 7. Before anyone can look for people for a req, they have to
know what the req actually says: the title, the description, the client, the
site, the account's own custom fields, the tags, the workflow it runs on. In
CATS those facts are spread over a record and two sub-resources, and the two
sub-resources are one request each per job. A caller assembling them by hand
spends three requests a job and still ends up holding `{"41": "Red Seal"}` -
values under account-specific field ids that mean nothing without a second trip
to the definitions endpoint.

So this tool gathers them in one call and resolves the ids on the way out. Every
custom field comes back carrying the account's own name for it alongside its id
and its stored value, because that pairing is the part a caller cannot
reconstruct and the part this adapter exists to supply.

What it will not do is turn any of that into a query. The issue is explicit:
"Do not convert these facts into a hidden candidate score." There is no measure
of fit here, nothing ordered, nothing filtered on the caller's behalf. The
sharpest edge of that rule is certifications. A description that says "must hold
a valid Red Seal" is prose, and pulling a requirement out of prose is inference
dressed as a fact - so certifications are returned only when the account
literally stored them in a custom field, with the name and id of the field they
came from, and `certifications.state` says which case the caller is in. A job
whose account has no certifications field reports `no_matching_field`; a job
whose field exists and is blank reports `field_empty`. Those are different
facts, and collapsing them into an empty list would tell a caller "this job
needs no certifications" on the strength of the account never having been asked.

Screening questions are the one item on the issue's list this adapter cannot
supply. CATS API v3 has no per-job screening-question endpoint - the question
labels surface only on an application somebody already submitted, through
`GET /jobs/applications/{id}/fields`, which is per-application rather than
per-job. Rather than invent an endpoint or infer questions from application
rows, the result names the gap in `unavailable` and says where the labels do
live.

Cost model:

    Phase A  one request per job for the record itself - title, description,
             company, site, status, workflow.
    Phase B  custom fields, one request per job.
    Phase C  tags, one request per job.
    Phase D  workflow status titles, one request for the whole call, served
             afterwards from the reference-data cache.

Three job ids with everything included is ten requests, which is why `include`
narrows the sub-resources and `max_requests` bounds the lot. What the budget
never reached is counted in `unread` - an unread sub-resource is unknown, not
absent, and reporting it as absent is how a caller ends up certain a job has no
tags.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field

from cats_mcp.composites.models import ExecutionFacts, RateLimit
from cats_mcp.composites.reads import (
    MAX_BATCH,
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _has_next_page,
    _progress,
    _project,
    _status_titles,
)
from cats_mcp.http.correlation import get_logger, set_run_id

logger = get_logger(__name__)

#: What `include` accepts, and what each costs.
#:
#: The asymmetry is worth reading before setting it: the sub-resources scale
#: with the number of jobs and the workflow titles do not, so trimming
#: `custom_fields` off a ten-job call saves ten requests while trimming
#: `workflow` saves one.
INCLUDE_OPTIONS: dict[str, str] = {
    "custom_fields": (
        "the job's custom fields, each with the account's own field name and id - "
        "one CATS request per job"
    ),
    "tags": "the job's tags - one CATS request per job",
    "workflow": (
        "titles for the account's pipeline status ids - one request per call, not per job"
    ),
}

#: The sub-resources that cost a request per job, and the endpoint tail of each.
PER_JOB_SUB_RESOURCES: dict[str, str] = {"custom_fields": "custom_fields", "tags": "tags"}

#: Default and hard ceiling on requests for one call.
#:
#: The default covers eight jobs with everything included. The ceiling covers a
#: full MAX_BATCH of jobs (3 per job plus one for the workflow titles) so a
#: caller who genuinely wants fifty reqs can have them without the sweep
#: stopping halfway and reporting a gap it did not have to have.
DEFAULT_MAX_REQUESTS = 25
MAX_REQUESTS_CEILING = 200

#: Rows per page when reading a job's custom fields or tags. CATS honours this,
#: so one page covers any realistic job; a job that somehow has more is reported
#: as truncated rather than paged, since a second page is another request.
SUB_RESOURCE_PAGE_SIZE = 100

#: Default and hard ceiling on description characters returned per job.
#:
#: A job description runs to kilobytes of HTML. Ten of them would be most of a
#: context window, and the caller asked for the facts a query is built from, so
#: the text is bounded and `description_truncated` says when the bound bit.
DEFAULT_DESCRIPTION_CHARS = 2000
DESCRIPTION_CHARS_CEILING = 8000

#: Field-name fragments consulted when looking for stored certifications,
#: matched case-insensitively as substrings of the account's own field label.
#:
#: Vocabulary, not a taxonomy: this list decides which *field* is read, never
#: what a value means. Accounts name the field differently - Certifications,
#: Tickets, Licences - and the caller can replace the list outright. Whatever
#: matched is reported back with its name and id so the choice is inspectable.
DEFAULT_CERTIFICATION_FIELD_NAMES: tuple[str, ...] = (
    "certification",
    "certificate",
    "credential",
    "ticket",
    "licence",
    "license",
)

#: Row keys probed for each site slot, in order.
#:
#: Several per slot on purpose. Accounts differ on whether a province is `state`
#: or `province`, and reading only one of them reports an empty site for every
#: account shaped the other way.
SITE_SOURCES: dict[str, tuple[str, ...]] = {
    "city": ("city",),
    "state": ("state", "province", "region"),
    "postal_code": ("postal_code", "zip", "zip_code"),
    "country": ("country", "country_code"),
    "department": ("department",),
    "location": ("location",),
}

#: Row keys probed for the client company name, in order. Same reasoning as
#: SITE_SOURCES: the client may be a string on the row or an `_embedded` object.
COMPANY_SOURCES: tuple[str, ...] = ("company", "company_name", "client", "client_name")

#: Row keys probed for the job description, in order.
DESCRIPTION_SOURCES: tuple[str, ...] = ("description", "job_description", "summary")

#: Row keys probed for the workflow the job's pipeline runs on.
WORKFLOW_SOURCES: tuple[str, ...] = ("workflow_id", "pipeline_workflow_id")

#: Projected off the job record. Whatever an account omits simply does not
#: appear - `_project` keeps only the keys the record actually carried.
OUTPUT_FIELDS = ["id", "title", "status_id", "company_id", "date_modified"]

#: Facts item 7 asks for that CATS API v3 does not expose, and where they do
#: live. Returned as data rather than left out, because a caller who cannot see
#: that screening questions were never available will read their absence as a
#: job having none.
UNAVAILABLE_FACTS: dict[str, str] = {
    "screening_questions": (
        "CATS API v3 exposes no per-job screening-question endpoint. Question labels "
        "appear only on an application somebody already submitted, through "
        "GET /jobs/applications/{application_id}/fields, which is per-application "
        "rather than per-job. If this account keeps its questions in a job custom "
        "field instead, that field is already in `custom_fields` with its name and id."
    )
}

#: Prose returned with every result. Kept out of the display content, which
#: stays a counts-only line - see `_content_line`.
RESULT_NOTE = (
    "The job's stored facts, and nothing derived from them. Every custom field "
    "carries the account's own name and id alongside its value, so a value can be "
    "read back or written back without a second lookup. `certifications.state` "
    "separates the cases an empty list would hide: 'stored', 'field_empty' (the "
    "field exists on this job and holds nothing), 'no_matching_field' (no field "
    "here carries one of the consulted names) and 'not_read' (custom fields were "
    "not read this call). Nothing is parsed out of the description - a requirement "
    "written there stays there. `unavailable` names the facts CATS API v3 does not "
    "expose. `unread` counts sub-resources the budget never reached: those are "
    "unknown, not absent. Interpreting all of it is the caller's job."
)


class CustomFieldValue(BaseModel):
    """One custom field on a job: the account's label, its id, its value.

    The id and the name travel together because neither is usable alone. A field
    id is account-specific - `41` means nothing to a reader and nothing to
    another account - while a label alone cannot be used to read the field back.
    Pairing them is the part a caller cannot reconstruct from the value, and
    supplying it is what makes this an adapter rather than a proxy.
    """

    field_id: int | str | None = Field(default=None, description="The CATS custom field id.")
    name: str | None = Field(default=None, description="The account's own label for the field.")
    values: list[str] = Field(
        default_factory=list,
        description=(
            "The stored value(s), verbatim. Empty means the field exists on this job "
            "and holds nothing - never that the field is absent."
        ),
    )


class TagRef(BaseModel):
    """One tag on a job, with the id needed to filter by it."""

    tag_id: int | str | None = Field(default=None, description="The CATS tag id.")
    title: str | None = Field(default=None, description="The tag as the account wrote it.")


class JobSite(BaseModel):
    """Where the work is, as the job record stores it.

    Every slot is nullable and several are probed from more than one key,
    because accounts differ on where they put a province or a site name. A site
    held in a custom field instead shows up in `custom_fields`, under whatever
    the account called it.
    """

    city: str | None = Field(default=None, description="City on the job record.")
    state: str | None = Field(default=None, description="State or province.")
    postal_code: str | None = Field(default=None, description="Postal or ZIP code.")
    country: str | None = Field(default=None, description="Country, when the account stores it.")
    department: str | None = Field(default=None, description="Department on the job record.")
    location: str | None = Field(
        default=None, description="A free-text location, when the account keeps one."
    )


class CertificationFacts(BaseModel):
    """Certifications, only where the account literally stored them.

    `state` exists because an empty list is ambiguous in the one direction that
    matters. "This job has no certifications field" and "this job's
    certifications field is blank" are different facts, and the third case -
    "custom fields were not read this call" - is different again. A caller
    building a query needs to know which of those it is holding, so the model
    says outright instead of leaving it to be inferred from an empty list.

    Nothing here is derived from the description text. A requirement written in
    prose stays in the description, where the caller can read it and decide.
    """

    state: Literal["stored", "field_empty", "no_matching_field", "not_read"] = Field(
        description=(
            "'stored': at least one matching field holds a value. 'field_empty': the "
            "field exists on this job and holds nothing. 'no_matching_field': custom "
            "fields were read and none carried a consulted name. 'not_read': custom "
            "fields were not read, so this is unknown."
        )
    )
    fields: list[CustomFieldValue] = Field(
        default_factory=list,
        description=(
            "Every custom field whose label matched, with its name, id and stored "
            "values. Empty unless state is 'stored' or 'field_empty'."
        ),
    )


class JobRequirements(BaseModel):
    """One job's stored facts.

    There is deliberately nowhere here to record what the job needs *overall*.
    The fields are the record's own; assembling them into a candidate query is
    the caller's work, and a field summarising them would be this adapter doing
    that work invisibly and for every account at once.
    """

    job_id: int | str | None = Field(default=None, description="The CATS job id.")
    title: str | None = Field(default=None, description="The job title as CATS stores it.")
    description: str | None = Field(
        default=None,
        description=(
            "The job description, verbatim and bounded by max_description_chars. Read "
            "it yourself - nothing here interprets it."
        ),
    )
    description_truncated: bool = Field(
        default=False, description="True when the description was cut at the character bound."
    )
    company: str | None = Field(
        default=None, description="The client company name, from the row or its _embedded company."
    )
    company_id: int | str | None = Field(default=None, description="The client company's id.")
    site: JobSite = Field(
        default_factory=JobSite, description="Where the work is, per the job record."
    )
    status_id: int | str | None = Field(default=None, description="The job's status id.")
    workflow_id: int | str | None = Field(
        default=None,
        description=(
            "The pipeline workflow this job runs on, when the record names one. "
            "Resolve stage ids through `workflow_status_titles` on the result."
        ),
    )
    date_modified: str | None = Field(default=None, description="When CATS last changed the job.")
    custom_fields: list[CustomFieldValue] | None = Field(
        default=None,
        description=(
            "Every custom field on the job, each with its name and id. Null means they "
            "were not read - not that the job has none."
        ),
    )
    certifications: CertificationFacts = Field(
        description="Certifications as stored in a custom field, and which case this is."
    )
    tags: list[TagRef] | None = Field(
        default=None,
        description="The job's tags. Null means they were not read - not that there are none.",
    )


class JobRequirementsResult(BaseModel):
    """The stored facts for every job asked about, and what the call covered.

    The counts are load-bearing. `count` against `requested` says whether a job
    is missing from the answer, `unread` says which sub-resources the budget
    never reached, and `unavailable` says which of the facts asked for CATS does
    not expose at all. A caller that cannot read those cannot tell a complete
    answer from a partial one.
    """

    jobs: list[JobRequirements] = Field(
        default_factory=list,
        description="One entry per job that could be read, in the order the ids were given.",
    )
    count: int = Field(description="Jobs in `jobs`. Compare with `requested`.")
    requested: int = Field(description="Distinct job ids asked about.")
    unread: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per stage, how many jobs the budget never read. Unknown, not absent - a "
            "non-zero count means facts are missing from this answer."
        ),
    )
    unavailable: dict[str, str] = Field(
        default_factory=dict,
        description="Facts item 7 asks for that CATS API v3 does not expose, and why.",
    )
    certification_fields_consulted: list[str] = Field(
        default_factory=list,
        description=(
            "The field-name fragments matched against custom field labels this call. "
            "Replace them with certification_field_names if the account uses others."
        ),
    )
    workflow_status_titles: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Account pipeline status id -> its title, for reading a job's workflow. "
            "Empty when 'workflow' was not included or the account embeds no statuses."
        ),
    )
    execution: ExecutionFacts = Field(
        description="What the call spent and what it could not finish."
    )
    note: str = Field(description="How to read this result.")


def _texts(value: Any) -> list[str]:
    """Every stored value hiding in a custom field, as text, verbatim.

    Numbers are kept, unlike the matching helpers elsewhere in this package. A
    certificate number, a licence class and a required count are all legitimate
    stored values, and dropping them because they are not strings would report a
    populated field as empty - the one error this module is built to avoid.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, bool):
        return [str(value)]
    if isinstance(value, int | float):
        return [str(value)]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_texts(item))
        return out
    if isinstance(value, dict):
        out = []
        for key in ("value", "title", "name", "label", "text"):
            if key in value:
                out.extend(_texts(value[key]))
        return out
    return []


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first non-empty value among several candidate keys, row or _embedded.

    Probing several keys rather than picking one is not defensiveness. CATS row
    shape varies by account, and a single-key read returns a confident null for
    every account shaped the other way.
    """
    embedded = row.get("_embedded") if isinstance(row.get("_embedded"), dict) else {}
    for key in keys:
        value = row.get(key)
        if value is None and isinstance(embedded, dict):
            value = embedded.get(key)
        found = _texts(value)
        if found:
            return found[0]
    return None


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> int | str | None:
    """The first present id among several candidate keys. Ids are not text."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, int | str) and str(value).strip():
            return value
    return None


def _selection_labels(definition: Any) -> dict[str, str]:
    """Map a picklist field's option ids to their labels.

    A choice field stores the id, never the text. `Site` on a live job reads
    `[1186703]`, and the label "Blackwater" lives on the *definition* under
    `field.selections`. Without this the tool returns a number, which is worse
    than returning nothing: it looks like data.
    """
    field = definition.get("field") if isinstance(definition, dict) else None
    selections = field.get("selections") if isinstance(field, dict) else None
    labels: dict[str, str] = {}
    for option in selections or []:
        if isinstance(option, dict) and option.get("id") is not None:
            labels[str(option["id"])] = str(option.get("label") or option.get("name") or "")
    return labels


def _custom_fields(payload: Any) -> list[CustomFieldValue]:
    """A job's custom fields, each keeping the account's label and id.

    Two things CATS does here that the first version missed, both found by
    running this against a live job:

    * The field's name is not on the row. The row is `{"id", "value"}` and the
      name sits in `_embedded.definition.name`. Reading a top-level `name`
      returned None for all 22 fields, which then broke the certification
      lookup downstream - it cannot match a field name it never resolved.
    * A choice field's `value` is an option *id*. `Site` reads `[1186703]`;
      the label "Blackwater" is on the definition under `field.selections`.
      Returning the id is worse than returning nothing, because a number looks
      like an answer.
    """
    out: list[CustomFieldValue] = []
    for row in _embedded_rows(payload):
        definition = (row.get("_embedded") or {}).get("definition") or {}
        label = (
            definition.get("name")
            or row.get("name")
            or row.get("title")
            or row.get("field_name")
        )
        labels = _selection_labels(definition)
        values = _texts(row.get("value"))
        if labels:
            # Resolve what we can; an id with no matching selection is kept as
            # it stands rather than dropped, so a stale option is visible.
            values = [labels.get(v, v) for v in values]
        out.append(
            CustomFieldValue(
                field_id=row.get("id") if isinstance(row.get("id"), int | str) else None,
                name=str(label) if label is not None else None,
                values=values,
            )
        )
    return out


def _tags(payload: Any) -> list[TagRef]:
    out: list[TagRef] = []
    for row in _embedded_rows(payload):
        title = row.get("title") or row.get("name")
        out.append(
            TagRef(
                tag_id=row.get("id") if isinstance(row.get("id"), int | str) else None,
                title=str(title) if title is not None else None,
            )
        )
    return out


def _certifications(
    fields: list[CustomFieldValue] | None, names: tuple[str, ...]
) -> CertificationFacts:
    """Which certifications case this job is in, and the fields behind it.

    Matching is on the field *label* only. The values are never inspected to
    decide whether a field is about certifications - that would be this module
    reading meaning into an account's data, which is the caller's job.
    """
    if fields is None:
        return CertificationFacts(state="not_read")

    matched = [
        field
        for field in fields
        if field.name and any(needle in field.name.lower() for needle in names)
    ]
    if not matched:
        return CertificationFacts(state="no_matching_field")
    if any(field.values for field in matched):
        return CertificationFacts(state="stored", fields=matched)
    return CertificationFacts(state="field_empty", fields=matched)


def _site(row: dict[str, Any]) -> JobSite:
    return JobSite(**{slot: _first_text(row, keys) for slot, keys in SITE_SOURCES.items()})


def _description(row: dict[str, Any], limit: int) -> tuple[str | None, bool]:
    text = _first_text(row, DESCRIPTION_SOURCES)
    if text is None or len(text) <= limit:
        return text, False
    return text[:limit], True


def _job_row(
    record: dict[str, Any],
    *,
    custom_fields: list[CustomFieldValue] | None,
    tags: list[TagRef] | None,
    certification_names: tuple[str, ...],
    description_limit: int,
) -> JobRequirements:
    projected = _project(record, OUTPUT_FIELDS)
    description, cut = _description(record, description_limit)
    title = projected.get("title")
    modified = projected.get("date_modified")
    return JobRequirements(
        job_id=projected.get("id"),
        title=str(title) if title is not None else None,
        description=description,
        description_truncated=cut,
        company=_first_text(record, COMPANY_SOURCES),
        company_id=projected.get("company_id"),
        site=_site(record),
        status_id=projected.get("status_id"),
        workflow_id=_first_value(record, WORKFLOW_SOURCES),
        date_modified=str(modified) if modified is not None else None,
        custom_fields=custom_fields,
        certifications=_certifications(custom_fields, certification_names),
        tags=tags,
    )


def _content_line(result: JobRequirementsResult) -> str:
    """The one line a human sees. Counts and flags, never record data.

    No job title and no client name: issue #21. The structured payload is the
    answer, and a title in the summary is both a second copy of it and a way for
    a reader to act on one job without having read the rest.
    """
    fields = sum(len(job.custom_fields or []) for job in result.jobs)
    tags = sum(len(job.tags or []) for job in result.jobs)
    stored = sum(1 for job in result.jobs if job.certifications.state == "stored")
    parts = [
        f"jobs {result.count}/{result.requested}",
        f"custom fields {fields}",
        f"tags {tags}",
        f"certifications stored {stored}",
        f"requests {result.execution.requests_used}",
    ]
    if result.execution.truncated:
        parts.append("truncated")
    if result.unread:
        parts.append(f"unread {sum(result.unread.values())}")
    if result.execution.errors:
        parts.append(f"errors {len(result.execution.errors)}")
    return ", ".join(parts)


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the job requirements primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="get_job_requirements",
        # Passed explicitly because the function returns ToolResult, which on its
        # own leaves the tool with no output schema at all. Declaring it here is
        # what gives the caller a typed, object-rooted contract while the display
        # content stays a single counts line instead of a second copy of the
        # payload.
        output_schema=JobRequirementsResult.model_json_schema(),
        description=(
            "Return the facts CATS stores about one or more jobs: title, description, "
            "client company, site, the job's custom fields, its tags and the workflow "
            "it runs on. These are the source facts a candidate query gets built "
            "from. Interpreting them is the caller's job - nothing here weighs them, "
            "orders them or turns them into a query.\n\n"
            "Every custom field comes back with the account's own name for it AND its "
            "id. A bare field id is account-specific and unusable on its own, and "
            "resolving it is this adapter's work, not a second call you should have "
            "to make.\n\n"
            "Certifications are returned only where the account literally stored them "
            "in a custom field. `certifications.state` says which case each job is "
            "in: 'stored', 'field_empty' (the field exists here and holds nothing), "
            "'no_matching_field' (no field here carries one of the consulted names) "
            "or 'not_read'. Those are different facts and an empty list would hide "
            "the difference. Nothing is pulled out of the description text - a "
            "requirement written in prose stays in the description, where you can "
            "read it and decide what it means.\n\n"
            "Screening questions: CATS API v3 has no per-job screening-question "
            "endpoint, so they are named in `unavailable` with where the labels do "
            "live, rather than guessed at.\n\n"
            "Cost: one request per job for the record, one per job for custom fields, "
            "one per job for tags, and one per call - not per job - for workflow "
            "status titles. Three job ids with everything included is ten requests, "
            "so several ids add up fast. Narrow `include` to spend less. "
            "`max_requests` bounds the call, `execution.requests_used` says what went, "
            "and `unread` counts the sub-resources the budget never reached - those "
            "are unknown, not absent."
        ),
        tags={"ats", "job", "read"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def get_job_requirements(
        job_ids: Annotated[
            list[int | str],
            Field(
                min_length=1,
                max_length=MAX_BATCH,
                description=(
                    f"The jobs to read. Up to {MAX_BATCH} per call, and each one costs "
                    "up to three CATS requests, so a long list needs max_requests "
                    "raised to match."
                ),
            ),
        ],
        include: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Which sub-resources to read. "
                    + "; ".join(f"'{k}': {v}" for k, v in INCLUDE_OPTIONS.items())
                    + ". Defaults to all of them."
                )
            ),
        ] = None,
        certification_field_names: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Field-name fragments to match against custom field labels when "
                    "looking for stored certifications, case-insensitive. Defaults to "
                    f"{list(DEFAULT_CERTIFICATION_FIELD_NAMES)}. Replace them if this "
                    "account names the field something else; whatever matched is "
                    "reported with its name and id."
                )
            ),
        ] = None,
        max_description_chars: Annotated[
            int,
            Field(
                ge=0,
                le=DESCRIPTION_CHARS_CEILING,
                description=(
                    "Characters of description returned per job. Descriptions run to "
                    f"kilobytes; ceiling {DESCRIPTION_CHARS_CEILING}, and "
                    "`description_truncated` says when the bound bit."
                ),
            ),
        ] = DEFAULT_DESCRIPTION_CHARS,
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

        ids = _dedupe(list(job_ids))
        requested = list(INCLUDE_OPTIONS) if include is None else include
        wanted = [item.strip().lower() for item in requested if item.strip()]
        unknown = [item for item in wanted if item not in INCLUDE_OPTIONS]
        if unknown:
            raise ValueError(
                f"Unknown include {unknown}. Valid options: {sorted(INCLUDE_OPTIONS)}."
            )

        names = tuple(
            name.strip().lower()
            for name in (certification_field_names or DEFAULT_CERTIFICATION_FIELD_NAMES)
            if name.strip()
        )
        if not names:
            raise ValueError(
                "certification_field_names was given but held no usable name. Omit it to "
                f"use the defaults: {list(DEFAULT_CERTIFICATION_FIELD_NAMES)}."
            )

        errors: dict[str, str] = {}
        unread: dict[str, int] = {}
        requests_used = 0
        truncated = False

        # --- Phase A: the job records ---------------------------------------
        affordable = ids[: max(0, max_requests - requests_used)]
        skipped = ids[len(affordable) :]
        if skipped:
            logger.warning(
                "job budget covered %s of %s records; %s were never read",
                len(affordable),
                len(ids),
                len(skipped),
            )
            unread["job_records"] = len(skipped)
            truncated = True
            errors["job_records"] = (
                f"budget covered {len(affordable)} of {len(ids)} jobs; {len(skipped)} were "
                f"never read. Raise max_requests or ask about fewer jobs."
            )

        async def fetch_job(jid: int | str) -> Any:
            return await client.request("GET", f"/jobs/{jid}")

        records, record_errors = await _gather_by_id(list(affordable), fetch_job)
        requests_used += len(affordable)
        errors.update({f"job:{key}": value for key, value in record_errors.items()})
        await _progress(
            requests_used, max_requests, f"read {len(records)} of {len(ids)} job records"
        )

        # Only jobs that actually came back are worth spending sub-resource
        # requests on: a per-job read against an id CATS just 404'd would buy a
        # second copy of the same error.
        found = [jid for jid in affordable if str(jid) in records]

        # --- Phases B and C: the per-job sub-resources -----------------------
        collected: dict[str, dict[str, Any]] = {}
        for sub_resource, tail in PER_JOB_SUB_RESOURCES.items():
            if sub_resource not in wanted:
                continue
            budget = max(0, max_requests - requests_used)
            payable = found[:budget]
            unreached = found[budget:]
            if unreached:
                # Counted rather than reported as empty. A tag list nobody read
                # is unknown, and returning [] for it would tell the caller this
                # job carries no tags.
                unread[sub_resource] = len(unreached)
                truncated = True
                errors[sub_resource] = (
                    f"budget covered {len(payable)} of {len(found)} jobs; {len(unreached)} "
                    f"were never read for {sub_resource} and are counted in `unread`, not "
                    f"reported as empty. Raise max_requests or narrow `include`."
                )
            if not payable:
                continue

            async def fetch(jid: int | str, tail: str = tail) -> Any:
                return await client.request(
                    "GET",
                    f"/jobs/{jid}/{tail}",
                    params={"per_page": SUB_RESOURCE_PAGE_SIZE},
                )

            payloads, fetch_errors = await _gather_by_id(list(payable), fetch)
            requests_used += len(payable)
            errors.update(
                {f"{sub_resource}:{key}": value for key, value in fetch_errors.items()}
            )
            collected[sub_resource] = payloads
            await _progress(
                requests_used, max_requests, f"read {sub_resource} for {len(payloads)} jobs"
            )

            for key, payload in payloads.items():
                if _has_next_page(payload):
                    truncated = True
                    errors[f"{sub_resource}:{key}:pages"] = (
                        f"more than {SUB_RESOURCE_PAGE_SIZE} rows; only the first page "
                        f"was read."
                    )

        # --- Phase D: workflow status titles, once for the whole call --------
        titles: dict[str, str] = {}
        if "workflow" in wanted:
            if requests_used < max_requests:
                titles, spent = await _status_titles(client)
                requests_used += spent
            else:
                unread["workflow"] = 1
                truncated = True
                errors["workflow"] = (
                    "budget spent before workflow status titles could be read; job "
                    "`workflow_id` and pipeline status ids are returned unresolved."
                )

        # --- assemble, in the order the ids were given -----------------------
        rows: list[JobRequirements] = []
        for jid in ids:
            record = records.get(str(jid))
            if not isinstance(record, dict) or not record:
                continue
            fields = (
                _custom_fields(collected["custom_fields"][str(jid)])
                if str(jid) in collected.get("custom_fields", {})
                else None
            )
            tags = (
                _tags(collected["tags"][str(jid)])
                if str(jid) in collected.get("tags", {})
                else None
            )
            rows.append(
                _job_row(
                    record,
                    custom_fields=fields,
                    tags=tags,
                    certification_names=names,
                    description_limit=max_description_chars,
                )
            )

        result = JobRequirementsResult(
            jobs=rows,
            count=len(rows),
            requested=len(ids),
            unread=unread,
            unavailable=dict(UNAVAILABLE_FACTS),
            certification_fields_consulted=list(names),
            workflow_status_titles=titles,
            execution=ExecutionFacts(
                requests_used=requests_used,
                rate_limit=RateLimit.model_validate(client.rate_limit.snapshot()),
                truncated=truncated,
                # Nothing to resume: the caller holds the job ids, so continuing
                # is the same call with the ids that are missing from `jobs`.
                # `unread` is what says which those are.
                next_cursor=None,
                errors=errors,
            ),
            note=RESULT_NOTE,
        )
        return ToolResult(content=_content_line(result), structured_content=result.model_dump())

    return 1


__all__ = [
    "CertificationFacts",
    "CustomFieldValue",
    "JobRequirements",
    "JobRequirementsResult",
    "JobSite",
    "TagRef",
    "register",
]
