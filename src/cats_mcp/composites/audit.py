"""Structural anomalies in a set of candidate records.

Issue #12, item 6. An account accumulates damage that nobody put there on
purpose: the same person entered twice from two inbound channels, a province
typed four ways across four years of data entry, a phone field holding "call
mum", a title column that is empty on a third of the rows, a record still
carrying an active marker while its pipeline sits at a stage the account
considers final.

None of that is visible from the atomic surface. `GET /candidates` hands back a
page of rows; seeing that two of them hold one phone number written differently
means normalizing both and comparing, and seeing that "BC" and "B.C." are one
spelling means holding the whole column in memory at once. So the damage is
found by accident, one record at a time, usually by the person it inconveniences.

This sweeps a bounded set of records and reports what is structurally wrong with
the *fields*, and only with the fields. Everything it returns is a fact about
stored data: this value does not parse as an email address, these two records
fold to the same string, this column holds five spellings that fold to one. It
does not describe, characterise or evaluate any person, and there is no field in
any model below where such a thing could be recorded.

Two rules shape the whole module:

    Evidence, never a measure. A duplicate is reported as the folded string two
    records share and each record's own spelling of it. There is no likelihood,
    no closeness and no ordering, because none of those can be computed from
    CATS and a number attached to a pair of records is read as a decision about
    them however it is labelled.

    Unknown is not clean. A title key the projection never returned, a contact
    sub-collection the budget could not read, a record with no active marker -
    each is counted in `unchecked` rather than passed off as a record with
    nothing wrong. A present-but-null field is an answer; a missing key is not,
    and conflating the two is the bug this repository has already shipped once.

What is account-specific stays with the caller. Which pipeline stages are final,
which field marks a record active, and which spelling of a province is the right
one are all decisions this adapter has no standing to make: the first two are
parameters, and the third is simply not stated - the clusters and their counts
are reported and the account picks.

Cost model:

    Phase A  the pool. One request per page of 100 records, or one per id when
             candidate_ids is given. Bounded by max_records and max_requests.
    Phase B  everything answerable from a row already in hand - regions, titles,
             links, and any contact values the row carried. Free.
    Phase C  contact sub-collections, one request per collection per record, and
             only for records whose row carried no contact key at all. A search
             row never carries emails or phones, and "not carried" is not "none".
    Phase D  pipelines for the stage check, one request per record.

Phases C and D are the expensive ones and both are opt-in through `anomalies`,
which is what makes the cost of this tool something the caller sets rather than
something they discover afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any

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
)
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError

logger = get_logger(__name__)

#: Records per page. CATS honours this, so a 500-record sweep is five requests
#: rather than twenty at the default page size of 25.
POOL_PAGE_SIZE = 100

#: Default and hard ceiling on requests for one call.
#:
#: The CATS standard allowance is 500 requests/hour. An audit can always read
#: one more sub-collection, so it stops at a number the caller chose and reports
#: what it did not reach in `unchecked`.
DEFAULT_MAX_REQUESTS = 25
MAX_REQUESTS_CEILING = 120

#: Default and hard ceiling on records examined in one call - the scan ceiling.
#: Separate from the request budget because paging is cheap and analysis is not:
#: a caller can afford the requests for 2000 rows and still not want 2000 rows
#: of findings.
DEFAULT_MAX_RECORDS = 500
MAX_RECORDS_CEILING = 2000

#: Trailing digits two phone numbers are compared on. Identical to
#: composites/lookup.py's `_normalise_phone`, deliberately: a duplicate found
#: here and a duplicate confirmed by `lookup_candidate` must be the same
#: relation, or one tool would report a pair the other denies. The helpers below
#: are reimplemented rather than imported only to keep this module's edits
#: independent; the semantics are the same to the digit.
PHONE_SIGNIFICANT_DIGITS = 10

#: Fewest digits a value must hold to be treated as a subscriber number at all.
#: Below this a value is reported as unparseable rather than clustered - forty
#: records storing "n/a" in a phone field are forty broken fields, not a
#: forty-way duplicate.
MIN_PHONE_DIGITS = 7

#: Most digits E.164 permits, country code included.
MAX_PHONE_DIGITS = 15

#: Longest stored value quoted verbatim as evidence. A description pasted into a
#: title field runs long, and the caller needs to see the value, not the essay.
VALUE_LIMIT = 200

#: What `anomalies` accepts, and what each costs. The asymmetry is the point:
#: four of these ride along on rows the sweep already paid for, and two spend a
#: request per record. Selecting only the free ones is a real reduction in cost,
#: not a formality.
ANOMALY_OPTIONS: dict[str, str] = {
    "duplicates": (
        "records holding the same email address or phone number once folded - free "
        "for rows that carry contact values, one request per sub-collection per "
        "record whose row carried none"
    ),
    "region_variants": (
        "spellings of a region field that fold to one string, with the count of "
        "each - free, from rows already read"
    ),
    "contact_methods": (
        "records with no contact value stored at all, and stored values that do not "
        "parse as an email address or a phone number - same sub-collection cost as "
        "duplicates"
    ),
    "titles": (
        "title fields that came back null, blank, or holding no letters - free, from "
        "rows already read"
    ),
    "stage_conflicts": (
        "records whose active marker is set while a pipeline sits at one of "
        "terminal_status_ids - one request per record, and needs active_field and "
        "terminal_status_ids, both account-specific"
    ),
    "links": (
        "stored URLs that are not well formed, and _embedded references that came "
        "back empty beside a populated id - free, from rows already read"
    ),
}

#: What `anomalies` covers when the caller names none: everything answerable
#: from rows the sweep already paid for. The two that spend a request per record
#: are opt-in, because a default that quietly costs 500 requests is not a
#: default a caller chose.
DEFAULT_ANOMALIES: tuple[str, ...] = (
    "duplicates",
    "region_variants",
    "contact_methods",
    "titles",
    "links",
)

#: The anomalies that need contact values, and therefore may spend Phase C.
CONTACT_ANOMALIES = frozenset({"duplicates", "contact_methods"})

#: Region fields swept when the caller names none. `state` is where CATS stores
#: a province or state; a caller whose account keeps it elsewhere passes that
#: field instead. There is no list of provinces anywhere in this module - the
#: variants come from the data scanned and nothing else.
DEFAULT_REGION_FIELDS = ("state",)

#: Candidate-record keys an email may be stored under, inline or embedded, and
#: the keys a value may sit under inside a row. Same coverage as
#: composites/lookup.py: reading one shape and reporting "no email" is a silent
#: wrong answer on the exact field a duplicate check turns on.
EMAIL_KEYS = ("emails", "email", "email_address", "email1", "email2")
EMAIL_VALUE_KEYS = ("email", "address", "email_address", "value")

#: The same, for phones. `number` is the shape a live account returned; the
#: create endpoint's body calls it `phone`.
PHONE_KEYS = ("phones", "phone", "phone_cell", "phone_home", "phone_work", "mobile")
PHONE_VALUE_KEYS = ("phone", "number", "phone_number", "value")

#: And for URLs. CATS has no single canonical home for one.
URL_KEYS = ("linkedin_url", "linkedin", "profile_url", "website", "url", "social")
URL_VALUE_KEYS = ("url", "link", "address", "value")

#: Schemes a stored web address may use. Anything else - `mailto:`, a Windows
#: path, a bare `linkedin:` - is reported, not followed.
URL_SCHEMES = ("http", "https")

_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")
_NON_DIGITS = re.compile(r"\D+")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


# --- normalization: spelling only -------------------------------------------
#
# Every function here folds how a value is written and nothing about what it
# means. That line is the difference between an adapter and a guess: "BC" and
# "B.C." are one string written two ways, and deciding that "BC" and "British
# Columbia" are one place requires a gazetteer this module refuses to carry.


def _normalise_email(value: Any) -> str:
    """Lowercase and trim. Nothing else.

    Identical to composites/lookup.py. Deliberately not the provider-specific
    folding that strips dots or a '+tag': pat.lee@gmail.com and patlee@gmail.com
    are one mailbox at one provider and two people at another, so folding them
    together would be a claim about identity rather than about spelling.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _digits(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _NON_DIGITS.sub("", value)


def _normalise_phone(value: Any) -> str:
    """Digits only, and the last ten of them.

    Identical to composites/lookup.py. "250-555-0111", "(250) 555 0111" and
    "+1 250 555 0111" are one number written three ways, which is punctuation
    and a country code. A number carrying an extension runs on past the
    subscriber number and folds to something else.
    """
    digits = _digits(value)
    return digits[-PHONE_SIGNIFICANT_DIGITS:] if digits else ""


def _normalise_region(value: Any) -> str:
    """Lowercase and drop everything that is not a letter or a digit.

    Whitespace goes too, which is what makes "BC", "B.C." and "b c" one string.
    "British Columbia" folds to "britishcolumbia" and stays a different value,
    reported separately with its own count - saying otherwise would need a table
    of place names, and a table of place names is an opinion about an account's
    data that this adapter is in no position to hold.
    """
    if not isinstance(value, str):
        return ""
    return _NON_ALPHANUMERIC.sub("", value.strip().lower())


def _normalise_text(value: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Used for marker values."""
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.lower())).strip()


def _clip(value: str) -> str:
    return value if len(value) <= VALUE_LIMIT else value[:VALUE_LIMIT] + "..."


# --- structural validation --------------------------------------------------
#
# Each of these answers "can this string be read as the kind of thing the field
# is for", and returns the reason when it cannot. None of them contact anything
# or judge whether a value is a good one - an address that parses but bounces is
# not something CATS knows and not something this reports.


def _email_problem(value: str) -> str | None:
    text = value.strip()
    if not text:
        return "the field holds only whitespace"
    if any(character.isspace() for character in text):
        return "the value contains whitespace"
    if text.count("@") != 1:
        return f"the value has {text.count('@')} '@' characters, not one"
    local, _, domain = text.partition("@")
    if not local:
        return "there is nothing before the '@'"
    if not domain:
        return "there is nothing after the '@'"
    if "." not in domain:
        return "the domain has no dot"
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return "the domain has an empty label"
    return None


def _phone_problem(value: str) -> str | None:
    text = value.strip()
    if not text:
        return "the field holds only whitespace"
    digits = _digits(text)
    if not digits:
        return "the value contains no digits"
    if len(digits) < MIN_PHONE_DIGITS:
        return (
            f"the value has {len(digits)} digits, fewer than the "
            f"{MIN_PHONE_DIGITS} a subscriber number needs"
        )
    if len(digits) > MAX_PHONE_DIGITS:
        return (
            f"the value has {len(digits)} digits, more than the "
            f"{MAX_PHONE_DIGITS} E.164 allows"
        )
    return None


def _url_problem(value: str) -> str | None:
    text = value.strip()
    if not text:
        return "the field holds only whitespace"
    if any(character.isspace() for character in text):
        return "the value contains whitespace"
    remainder = text
    match = _SCHEME.match(text.lower())
    if match:
        scheme = text[: match.end() - 3].lower()
        if scheme not in URL_SCHEMES:
            return f"the scheme is '{scheme}', not http or https"
        remainder = text[match.end() :]
    host = remainder.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    if not host:
        return "the value names no host"
    if "." not in host:
        return "the host has no dot"
    if any(not label for label in host.split(".")):
        return "the host has an empty label"
    return None


def _is_set(value: Any) -> bool:
    """Whether a marker field reads as set, when the caller named no values.

    Deliberately blunt and written down rather than left to Python truthiness,
    because a CATS account may store a flag as 0/1, as "false", or as "N", and a
    caller reading this needs to know which of those this call treated as set.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return _normalise_text(value) not in ("", "0", "false", "no", "n")
    return bool(value)


# --- reading values off a record --------------------------------------------


def _flatten(value: Any, value_keys: tuple[str, ...]) -> list[str]:
    """Every non-empty string in a value, whether it is one, a list, or rows."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten(item, value_keys))
        return out
    if isinstance(value, dict):
        return [
            value[key]
            for key in value_keys
            if isinstance(value.get(key), str) and value[key].strip()
        ]
    return []


def _embedded(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    embedded = record.get("_embedded")
    return embedded if isinstance(embedded, dict) else {}


def _collect(
    record: Any, container_keys: tuple[str, ...], value_keys: tuple[str, ...]
) -> list[str]:
    """Stored values out of a record, inline or under _embedded."""
    if not isinstance(record, dict):
        return []
    embedded = _embedded(record)
    out: list[str] = []
    for key in container_keys:
        value = record.get(key)
        if value is None and key in embedded:
            value = embedded.get(key)
        out.extend(_flatten(value, value_keys))
    return out


def _carries(record: Any, container_keys: tuple[str, ...]) -> bool:
    """Whether the record answered the question at all.

    A row holding `emails: []` says this person has no email address. A row with
    no `emails` key says nothing, because the candidate search projection does
    not carry the sub-collection. Reporting the second as the first is the
    "silently answered in the negative" failure the whole module is arranged to
    avoid, and this predicate is where the two are kept apart.
    """
    if not isinstance(record, dict):
        return False
    embedded = _embedded(record)
    return any(key in record or key in embedded for key in container_keys)


def _orphaned_references(record: dict[str, Any]) -> list[tuple[str, str]]:
    """(id field, embedded key) where an id is populated and its target is empty.

    CATS embeds the record an id points at when it can. An `owner_id` of 41
    beside an `_embedded.owner` of null is a reference to something the API could
    not produce - which is a fact about the data, not about whoever 41 is.
    """
    embedded = _embedded(record)
    out: list[tuple[str, str]] = []
    for key, value in record.items():
        if not isinstance(key, str) or not key.endswith("_id") or value in (None, ""):
            continue
        target = key[: -len("_id")]
        if target not in embedded:
            continue
        held = embedded[target]
        if held is None or held == {} or held == []:
            out.append((key, target))
    return out


# --- the result contract ----------------------------------------------------
#
# Issue #17: a composite returning `dict[str, Any]` publishes no output schema,
# so a key could be renamed or quietly stop being emitted with every test still
# green. The models below are the contract written down. Only the rows are
# local; how the call reports on itself lives in composites/models.py, shared
# with every other composite.


class ScanSeed(BaseModel):
    """One narrowing the pool sweep was given.

    Reported back because the pool is the universe the answer was computed over.
    A caller who cannot see that the sweep was seeded to one city cannot tell "no
    duplicate exists" from "no duplicate exists among these rows".
    """

    field: str = Field(description="The CATS candidate field, or 'candidate_ids', or 'all'.")
    value: int | str | None = Field(
        default=None, description="The exact value filtered on, as the caller gave it."
    )


class ClusterMember(BaseModel):
    """One record in a cluster, and the spelling it holds."""

    candidate_id: int | str = Field(description="The CATS candidate id.")
    stored_value: str = Field(description="The value as this record stores it.")
    source: str = Field(description="Where the value was read: the record, or a sub-collection.")


class DuplicateCluster(BaseModel):
    """Records holding one value, and the evidence that they do.

    There is deliberately nowhere here to record how likely it is that these are
    one person. The relation reported is exact and checkable: `normalized` is the
    folded string, and every member quotes its own spelling of it, so a reader
    can see the whole comparison rather than take a number on trust. A likelihood
    would be a decision about these records wearing a statistic's clothes, and
    the decision is the caller's.
    """

    field: str = Field(description="What the records share: 'email' or 'phone'.")
    normalized: str = Field(
        description="The folded string every member holds, e.g. '2505550111'."
    )
    members: list[ClusterMember] = Field(
        default_factory=list, description="Every record holding it, with its own spelling."
    )
    record_count: int = Field(description="How many records are in this cluster.")


class ValueCount(BaseModel):
    """One stored spelling and how many records use it."""

    value: str = Field(description="The value exactly as stored.")
    normalized: str = Field(description="What it folds to.")
    count: int = Field(description="Records holding this exact spelling.")


class VariantCluster(BaseModel):
    """Spellings of one field that fold to one string.

    No spelling here is named as the correct one, and the list is not ordered to
    imply that the first is. Which form an account standardises on is the
    account's decision and is not a fact CATS stores; the counts are given so
    that decision can be made with the numbers in view.
    """

    field: str = Field(description="The candidate field these values were read from.")
    normalized: str = Field(description="The folded string they all share, e.g. 'bc'.")
    variants: list[ValueCount] = Field(
        default_factory=list, description="Each stored spelling and its count."
    )


class FieldValues(BaseModel):
    """Every distinct value one field holds across the records scanned.

    The full inventory, not only the clustered part. It is what lets a caller see
    that an account holds "BC" 412 times and "British Columbia" 6 times - two
    values that do not fold together and never will here, because folding them
    would need a table of place names.
    """

    field: str = Field(description="The candidate field.")
    values: list[ValueCount] = Field(
        default_factory=list, description="Distinct stored values and their counts."
    )


class RecordAnomaly(BaseModel):
    """One structurally wrong field on one record.

    A fact about a stored value: what is in the field, where it was read, and
    what about it does not parse. Nothing here is about the person the record
    describes, and `detail` is a description of the string, never of them.
    """

    candidate_id: int | str = Field(description="The CATS candidate id.")
    anomaly: str = Field(
        description=(
            "The machine-readable kind: 'contact_missing', 'email_unparseable', "
            "'phone_unparseable', 'title_missing', 'title_unparseable', "
            "'url_unparseable' or 'reference_empty'."
        )
    )
    field: str | None = Field(
        default=None, description="The field the value was read from, when there is one."
    )
    value: str | None = Field(
        default=None, description="The stored value, clipped. Null when the field is null."
    )
    source: str | None = Field(
        default=None, description="'candidate record' or the sub-collection it came from."
    )
    detail: str = Field(description="What about the stored value does not parse.")


class StageConflict(BaseModel):
    """A record whose active marker and pipeline stage disagree.

    Both halves are the caller's definition: which field marks a record active
    and which stage ids are final are account-specific, and this module hardcodes
    neither. What it reports is that the two stored facts the caller named are
    both true of one record at the same time.
    """

    candidate_id: int | str = Field(description="The CATS candidate id.")
    active_field: str = Field(description="The field the caller named as the active marker.")
    active_value: str | None = Field(default=None, description="What that field holds.")
    pipeline_id: int | str | None = Field(default=None, description="The pipeline row's id.")
    job_id: int | str | None = Field(default=None, description="The job that pipeline is on.")
    status_id: int | str | None = Field(
        default=None, description="The pipeline's stage - one of terminal_status_ids."
    )


class AuditResult(BaseModel):
    """Everything the sweep found, and what it did not get to look at.

    `counts` and `unchecked` are read together or not at all. An empty findings
    list beside a non-zero `unchecked` for the same anomaly does not mean the
    records are clean; it means the budget or the projection stopped the call
    from knowing.
    """

    duplicates: list[DuplicateCluster] = Field(
        default_factory=list, description="Records sharing a folded email or phone."
    )
    region_variants: list[VariantCluster] = Field(
        default_factory=list, description="Spellings of a region field that fold to one string."
    )
    region_values: list[FieldValues] = Field(
        default_factory=list, description="Every distinct region value seen, with counts."
    )
    contact_methods: list[RecordAnomaly] = Field(
        default_factory=list, description="Missing or unparseable contact values."
    )
    titles: list[RecordAnomaly] = Field(
        default_factory=list, description="Title fields that are null, blank or hold no letters."
    )
    stage_conflicts: list[StageConflict] = Field(
        default_factory=list,
        description="Records active by the caller's marker while at a stage the caller named.",
    )
    links: list[RecordAnomaly] = Field(
        default_factory=list, description="Unparseable URLs and empty embedded references."
    )
    counts: dict[str, int] = Field(
        default_factory=dict, description="Findings per anomaly kind, for the kinds checked."
    )
    anomalies_checked: list[str] = Field(
        default_factory=list, description="The anomaly kinds this call actually looked for."
    )
    unchecked: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per anomaly kind, records the budget or a missing field kept this call "
            "from evaluating. Unknown, not clean."
        ),
    )
    scanned: int = Field(description="Candidate records the sweep actually read.")
    seeds_used: list[ScanSeed] = Field(
        default_factory=list, description="How the pool was narrowed, if it was."
    )
    execution: ExecutionFacts = Field(
        description="What the call spent and what it could not finish."
    )
    note: str = Field(description="How to read this result.")


#: Returned with every result. Kept out of the display content, which stays a
#: counts line - see `_content_line`.
RESULT_NOTE = (
    "Every finding is a structural fact about a stored field, quoting the value "
    "that produced it. A duplicate cluster says that two or more records hold one "
    "value once folded, and names both the folded string and each record's own "
    "spelling - that is the whole of the evidence, and no likelihood is attached "
    "because none can be computed from CATS. `region_variants` groups spellings "
    "that fold to one string and counts each; which of them this account treats as "
    "correct is not stated, because that is the account's decision. `region_values` "
    "is the full inventory, including values that fold apart - there is no table of "
    "place names here, so 'BC' and 'British Columbia' are two values. `unchecked` "
    "counts records a missing field or the request budget kept this call from "
    "evaluating: those are unknown, not clean. A title field that came back null is "
    "an answer - no title is recorded - while a title key the projection never "
    "returned is counted in `unchecked` instead. Interpreting any of it is the "
    "caller's job; nothing here has been changed."
)


def _seed_plan(
    seed_field: str | None, seed_values: list[str] | None
) -> list[tuple[str, Any]]:
    """(field, value) pairs to seed the pool from, one `exactly` filter each.

    One filter per value, always exact. The CATS `contains` filter tokenises the
    value and matches ANY token, so a single contains="Logan Lake" also returns
    Williams Lake and Slave Lake - no error, just extra rows that look entirely
    plausible and would be audited as though the caller had asked for them.
    """
    if not seed_field:
        return []
    field = seed_field.strip()
    return [(field, str(v).strip()) for v in _dedupe(list(seed_values or [])) if str(v).strip()]


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the data-quality audit primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="audit_candidate_data",
        # Passed explicitly, and it has to be. Annotating the return as
        # ToolResult publishes no output schema at all; returning the model
        # itself publishes one but repeats the whole payload as display text,
        # which is the duplication issue #17 is about. This gives the caller an
        # object-rooted schema and a one-line summary.
        output_schema=AuditResult.model_json_schema(),
        description=(
            "Report structural data-quality anomalies across a bounded set of "
            "candidate records: records holding the same email address or phone "
            "number once punctuation and case are folded away, region spellings that "
            "fold to one string, contact fields that are absent or do not parse, "
            "title fields that came back null or blank, records whose active marker "
            "is set while a pipeline sits at a stage you name, and stored URLs that "
            "are not well formed.\n\n"
            "Everything returned is a fact about a stored field, with the value "
            "quoted as evidence. Nothing here describes a person: a title finding "
            "means the field is empty or holds no letters, never anything about the "
            "work someone does, and no result carries an age, an identity or any "
            "other protected characteristic. Interpreting them is the caller's job.\n\n"
            "Duplicates are reported as evidence rather than as a measure. A cluster "
            "names the folded string and every record's own spelling of it, so the "
            "comparison is visible and checkable. There is no likelihood, no "
            "closeness and no ordering, because none of those can be computed from "
            "CATS and a number attached to a pair of records reads as a decision "
            "about them.\n\n"
            "Region variants come from the data scanned, not from a built-in list of "
            "places. Values that fold to one string are grouped with the count of "
            "each spelling, and no spelling is named as the correct one - that is "
            "the account's decision. 'BC' and 'British Columbia' fold apart and stay "
            "two values; both appear in region_values with their counts.\n\n"
            "Anomaly kinds are selectable through `anomalies`, and that is the cost "
            "control. "
            + " ".join(f"'{k}': {v}." for k, v in ANOMALY_OPTIONS.items())
            + "\n\n"
            "Which pipeline stages are final and which field marks a record active "
            "are account-specific, so stage_conflicts needs terminal_status_ids and "
            "active_field from you; nothing is inferred from a stage's title.\n\n"
            "Budgeted twice: max_records caps how many rows are examined and "
            "max_requests caps what the call may spend. `execution.requests_used` "
            "reports the spend and `unchecked` counts records a missing field or the "
            "budget kept this call from evaluating - unknown, not clean."
        ),
        tags={"ats", "candidate", "read", "audit"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def audit_candidate_data(
        anomalies: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Which anomaly kinds to look for, and therefore what this call "
                    "costs. "
                    + "; ".join(f"'{k}': {v}" for k, v in ANOMALY_OPTIONS.items())
                    + ". Defaults to the free ones - "
                    + ", ".join(repr(k) for k in DEFAULT_ANOMALIES)
                    + " - because the other two spend a request per record."
                )
            ),
        ] = None,
        candidate_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    f"Audit these records only, one request each. Ceiling {MAX_BATCH}. "
                    "Takes precedence over the sweep. Note that duplicate and region "
                    "findings are relations within the set given: two records outside "
                    "it that share an address are not visible from here."
                )
            ),
        ] = None,
        seed_field: Annotated[
            str | None,
            Field(
                description=(
                    "Narrow the sweep to records matching seed_values on this CATS "
                    "candidate field - one exact filter per value. Field names are "
                    "account-specific; find them with "
                    "list_candidate_custom_field_definitions or filter_candidates."
                )
            ),
        ] = None,
        seed_values: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Exact values for seed_field, one CATS request each. Exact rather "
                    "than contains: the CATS contains filter tokenises, so one "
                    "'Logan Lake' would also pull in Williams Lake."
                )
            ),
        ] = None,
        region_fields: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Fields to gather region spellings from. Defaults to "
                    + ", ".join(repr(f) for f in DEFAULT_REGION_FIELDS)
                    + ". Add 'city' or an account-specific field to audit those too."
                )
            ),
        ] = None,
        terminal_status_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Pipeline status ids this account treats as final - placed, "
                    "accepted, or whatever it calls them. Required for "
                    "'stage_conflicts'. These are account-specific and nothing here "
                    "guesses them from a stage title; find them with "
                    "list_pipeline_workflows."
                )
            ),
        ] = None,
        active_field: Annotated[
            str | None,
            Field(
                description=(
                    "The candidate field this account uses to mark a record active. "
                    "Required for 'stage_conflicts'. There is no default because CATS "
                    "does not define one - a wrong guess here would report every "
                    "record in the account."
                )
            ),
        ] = None,
        active_values: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Values of active_field that mean active, compared with case and "
                    "punctuation folded. Omit to treat any set value as active: true, "
                    "a non-zero number, or a string that is not '', '0', 'false', "
                    "'no' or 'n'."
                )
            ),
        ] = None,
        max_records: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_RECORDS_CEILING,
                description=(
                    f"The scan ceiling: records examined in one call. Ceiling "
                    f"{MAX_RECORDS_CEILING}. `scanned` reports how many were read."
                ),
            ),
        ] = DEFAULT_MAX_RECORDS,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}. "
                    "The CATS allowance is 500 requests/hour. Work stops here and the "
                    "response says what was left undone."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
    ) -> ToolResult:
        set_run_id()
        client = client_getter()

        # `anomalies is None` is "you choose"; `anomalies=[]` is "look for
        # nothing", which is a legitimate way to ask only what a sweep costs.
        # Collapsing the two with `or` would silently bill a caller who asked
        # for none.
        requested = list(DEFAULT_ANOMALIES) if anomalies is None else anomalies
        wanted = [a.strip().lower() for a in requested if a.strip()]
        unknown = [a for a in wanted if a not in ANOMALY_OPTIONS]
        if unknown:
            raise ValueError(
                f"Unknown anomalies {unknown}. Valid options: {sorted(ANOMALY_OPTIONS)}."
            )

        if "stage_conflicts" in wanted:
            missing = [
                name
                for name, value in (
                    ("terminal_status_ids", terminal_status_ids),
                    ("active_field", active_field),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"'stage_conflicts' needs {missing}. Which stages are final and "
                    f"which field marks a record active are specific to your account, "
                    f"so this tool will not guess either: find stage ids with "
                    f"list_pipeline_workflows and name the marker field yourself."
                )

        region_keys = [
            str(f).strip()
            for f in (region_fields if region_fields is not None else DEFAULT_REGION_FIELDS)
            if str(f).strip()
        ]

        wanted_ids = _dedupe(candidate_ids or [])
        if len(wanted_ids) > MAX_BATCH:
            raise ValueError(
                f"candidate_ids holds {len(wanted_ids)} ids; the ceiling is {MAX_BATCH} "
                f"because each one costs a CATS request. Sweep with seed_field instead."
            )

        errors: dict[str, str] = {}
        unchecked: dict[str, int] = {}
        requests_used = 0
        truncated = False

        def note_unchecked(kind: str, count: int = 1) -> None:
            if kind in wanted and count:
                unchecked[kind] = unchecked.get(kind, 0) + count

        # --- Phase A: the pool ------------------------------------------------
        pool: dict[str, dict[str, Any]] = {}
        seeds: list[ScanSeed] = []

        if wanted_ids:
            seeds = [ScanSeed(field="candidate_ids", value=v) for v in wanted_ids]
            affordable = wanted_ids[: max(0, min(max_requests, max_records))]
            if len(affordable) < len(wanted_ids):
                truncated = True
                errors["candidate_ids"] = (
                    f"budget covered {len(affordable)} of {len(wanted_ids)} ids; the "
                    f"rest were not read at all"
                )

            async def fetch_record(cid: int | str) -> Any:
                return await client.request("GET", f"/candidates/{cid}")

            records, id_errors = await _gather_by_id(list(affordable), fetch_record)
            requests_used += len(affordable)
            for key, message in id_errors.items():
                errors[f"candidate:{key}"] = message
            for key, record in records.items():
                if isinstance(record, dict) and record:
                    pool[key] = record
        else:
            plan = _seed_plan(seed_field, seed_values)
            seeds = (
                [ScanSeed(field=f, value=v) for f, v in plan]
                if plan
                else [ScanSeed(field="all", value=None)]
            )
            # No seed means the whole candidate list. That is deliberate for an
            # audit - a province typed four ways is only visible across the
            # column - and it is what max_records exists to bound.
            streams: list[tuple[str, Any] | None] = list(plan) or [None]

            for stream in streams:
                page = 1
                while True:
                    if len(pool) >= max_records:
                        logger.info("scan ceiling of %s records reached", max_records)
                        truncated = True
                        break
                    if requests_used >= max_requests:
                        truncated = True
                        errors["scan"] = (
                            "the request budget stopped the sweep before the pool was "
                            "complete; findings cover the rows read so far only"
                        )
                        break
                    label = "candidates" if stream is None else f"seed:{stream[0]}"
                    try:
                        if stream is None:
                            payload = await client.request(
                                "GET",
                                "/candidates",
                                params={"per_page": POOL_PAGE_SIZE, "page": page},
                            )
                        else:
                            # `exactly`, one filter per value: CATS `contains`
                            # tokenises and matches any token.
                            payload = await client.request(
                                "POST",
                                "/candidates/search",
                                json={
                                    "field": stream[0],
                                    "filter": "exactly",
                                    "value": stream[1],
                                },
                                params={"per_page": POOL_PAGE_SIZE, "page": page},
                            )
                        requests_used += 1
                        # Counts and a page number only. Issue #21: no name,
                        # address or number goes anywhere but the payload.
                        await _progress(
                            requests_used, max_requests, f"scanning candidate records, page {page}"
                        )
                    except CATSAPIError as exc:
                        errors[f"{label}:page:{page}"] = str(exc)
                        break

                    page_rows = _embedded_rows(payload)
                    for record in page_rows:
                        identifier = record.get("id")
                        if identifier is None:
                            continue
                        key = str(identifier)
                        if key not in pool and len(pool) >= max_records:
                            # A row the ceiling dropped is a row nothing was
                            # checked against, so the answer is short whatever
                            # the request budget said. Setting the flag here
                            # rather than only on re-entering the loop is what
                            # covers the case where one page overflows the
                            # ceiling and CATS reports no next page.
                            truncated = True
                            continue
                        pool.setdefault(key, record)

                    if not page_rows or not _has_next_page(payload):
                        break
                    page += 1
                if truncated:
                    break

        scanned = len(pool)

        # --- Phase B: everything a row already in hand can answer -------------
        title_rows: list[RecordAnomaly] = []
        link_rows: list[RecordAnomaly] = []
        region_seen: dict[str, dict[str, dict[str, Any]]] = {f: {} for f in region_keys}

        for key, row in pool.items():
            cid = row.get("id", key)

            if "titles" in wanted:
                # `_project` keeps only keys the record actually carried, which
                # is exactly the distinction that matters: a null title is an
                # answer, an absent key is not.
                projected = _project(row, ["title"])
                if "title" not in projected:
                    note_unchecked("titles")
                else:
                    title = projected["title"]
                    if title is None:
                        title_rows.append(
                            RecordAnomaly(
                                candidate_id=cid,
                                anomaly="title_missing",
                                field="title",
                                value=None,
                                source="candidate record",
                                detail="the field is present and null - no title is recorded",
                            )
                        )
                    elif not str(title).strip():
                        title_rows.append(
                            RecordAnomaly(
                                candidate_id=cid,
                                anomaly="title_missing",
                                field="title",
                                value=_clip(str(title)),
                                source="candidate record",
                                detail="the field holds only whitespace",
                            )
                        )
                    elif not any(character.isalpha() for character in str(title)):
                        title_rows.append(
                            RecordAnomaly(
                                candidate_id=cid,
                                anomaly="title_unparseable",
                                field="title",
                                value=_clip(str(title)),
                                source="candidate record",
                                detail="the field holds no letters, so it reads as no title",
                            )
                        )

            if "region_variants" in wanted and region_keys:
                region_projection = _project(row, region_keys)
                for field in region_keys:
                    if field not in region_projection:
                        continue
                    value = region_projection[field]
                    if not isinstance(value, str) or not value.strip():
                        continue
                    folded = _normalise_region(value)
                    if not folded:
                        continue
                    bucket = region_seen[field].setdefault(value, {"n": folded, "c": 0})
                    bucket["c"] += 1

            if "links" in wanted:
                for value in _collect(row, URL_KEYS, URL_VALUE_KEYS):
                    problem = _url_problem(value)
                    if problem:
                        link_rows.append(
                            RecordAnomaly(
                                candidate_id=cid,
                                anomaly="url_unparseable",
                                field="url",
                                value=_clip(value),
                                source="candidate record",
                                detail=problem,
                            )
                        )
                for id_field, target in _orphaned_references(row):
                    link_rows.append(
                        RecordAnomaly(
                            candidate_id=cid,
                            anomaly="reference_empty",
                            field=id_field,
                            value=_clip(str(row.get(id_field))),
                            source="candidate record",
                            detail=(
                                f"the id is set but _embedded.{target} came back empty, "
                                f"so the reference points at nothing this call could read"
                            ),
                        )
                    )

        # --- Phase C: contact sub-collections, only where the row said nothing -
        contact_wanted = bool(CONTACT_ANOMALIES & set(wanted))
        stored: dict[str, dict[str, list[tuple[str, str]]]] = {}
        known: dict[str, dict[str, bool]] = {}

        if contact_wanted:
            for key, row in pool.items():
                stored[key] = {
                    "email": [
                        (v, "candidate record") for v in _collect(row, EMAIL_KEYS, EMAIL_VALUE_KEYS)
                    ],
                    "phone": [
                        (v, "candidate record") for v in _collect(row, PHONE_KEYS, PHONE_VALUE_KEYS)
                    ],
                }
                known[key] = {
                    "email": _carries(row, EMAIL_KEYS),
                    "phone": _carries(row, PHONE_KEYS),
                }

            for kind, endpoint, value_keys in (
                ("email", "emails", EMAIL_VALUE_KEYS),
                ("phone", "phones", PHONE_VALUE_KEYS),
            ):
                pending = [key for key in pool if not known[key][kind]]
                if not pending:
                    continue
                affordable = pending[: max(0, max_requests - requests_used)]
                if len(affordable) < len(pending):
                    truncated = True
                    errors[endpoint] = (
                        f"budget covered {len(affordable)} of {len(pending)} {endpoint} "
                        f"sub-collections; the rest hold no {kind} this call could read, "
                        f"which is not the same as holding none"
                    )
                if affordable:
                    await _progress(
                        requests_used,
                        max_requests,
                        f"reading {endpoint} for {len(affordable)} records",
                    )

                async def fetch_sub(cid: int | str, path: str = endpoint) -> Any:
                    return await client.request(
                        "GET", f"/candidates/{cid}/{path}", params={"per_page": POOL_PAGE_SIZE}
                    )

                payloads, sub_errors = await _gather_by_id(list(affordable), fetch_sub)
                requests_used += len(affordable)
                for cid, message in sub_errors.items():
                    errors[f"{endpoint}:{cid}"] = message
                for cid, payload in payloads.items():
                    known[cid][kind] = True
                    stored[cid][kind].extend(
                        (v, f"{endpoint} sub-collection")
                        for v in _flatten(_embedded_rows(payload), value_keys)
                    )

        # --- contact findings and duplicate clusters --------------------------
        contact_rows: list[RecordAnomaly] = []
        clusters: dict[tuple[str, str], list[ClusterMember]] = {}

        if contact_wanted:
            for key in pool:
                cid = pool[key].get("id", key)
                unknown_here = [k for k in ("email", "phone") if not known[key][k]]

                if "contact_methods" in wanted:
                    if unknown_here:
                        note_unchecked("contact_methods")
                    elif not stored[key]["email"] and not stored[key]["phone"]:
                        contact_rows.append(
                            RecordAnomaly(
                                candidate_id=cid,
                                anomaly="contact_missing",
                                field=None,
                                value=None,
                                source="candidate record",
                                detail=(
                                    "the record stores no email address and no phone "
                                    "number; both sub-collections were read"
                                ),
                            )
                        )

                if "duplicates" in wanted and unknown_here:
                    note_unchecked("duplicates")

                for kind, checker in (("email", _email_problem), ("phone", _phone_problem)):
                    for value, source in stored[key][kind]:
                        problem = checker(value)
                        if problem:
                            if "contact_methods" in wanted:
                                contact_rows.append(
                                    RecordAnomaly(
                                        candidate_id=cid,
                                        anomaly=f"{kind}_unparseable",
                                        field=kind,
                                        value=_clip(value),
                                        source=source,
                                        detail=problem,
                                    )
                                )
                            # A value that does not parse takes no part in
                            # clustering. Forty records storing "n/a" in a phone
                            # field are forty broken fields, not a forty-way
                            # relation between the people on them.
                            continue
                        if "duplicates" not in wanted:
                            continue
                        folded = (
                            _normalise_email(value)
                            if kind == "email"
                            else _normalise_phone(value)
                        )
                        if not folded:
                            continue
                        members = clusters.setdefault((kind, folded), [])
                        if not any(str(m.candidate_id) == str(cid) for m in members):
                            members.append(
                                ClusterMember(
                                    candidate_id=cid, stored_value=_clip(value), source=source
                                )
                            )

        duplicates = [
            DuplicateCluster(
                field=kind, normalized=folded, members=members, record_count=len(members)
            )
            for (kind, folded), members in clusters.items()
            if len(members) > 1
        ]

        # --- Phase D: the stage check -----------------------------------------
        stage_rows: list[StageConflict] = []
        if "stage_conflicts" in wanted and active_field:
            terminal = {str(v) for v in (terminal_status_ids or [])}
            folded_active = {_normalise_text(v) for v in (active_values or []) if str(v).strip()}

            marked: list[str] = []
            for key, row in pool.items():
                projected = _project(row, [active_field])
                if active_field not in projected:
                    note_unchecked("stage_conflicts")
                    continue
                value = projected[active_field]
                is_active = (
                    _normalise_text(str(value)) in folded_active
                    if folded_active
                    else _is_set(value)
                )
                if is_active:
                    marked.append(key)

            affordable = marked[: max(0, max_requests - requests_used)]
            if len(affordable) < len(marked):
                truncated = True
                note_unchecked("stage_conflicts", len(marked) - len(affordable))
                errors["pipelines"] = (
                    f"budget covered {len(affordable)} of {len(marked)} records carrying "
                    f"the active marker; the rest were not checked against "
                    f"terminal_status_ids"
                )
            if affordable:
                await _progress(
                    requests_used,
                    max_requests,
                    f"reading pipelines for {len(affordable)} records",
                )

            async def fetch_pipelines(cid: int | str) -> Any:
                return await client.request(
                    "GET", f"/candidates/{cid}/pipelines", params={"per_page": POOL_PAGE_SIZE}
                )

            payloads, pipeline_errors = await _gather_by_id(list(affordable), fetch_pipelines)
            requests_used += len(affordable)
            for cid, message in pipeline_errors.items():
                errors[f"pipelines:{cid}"] = message
                note_unchecked("stage_conflicts")
            for cid, payload in payloads.items():
                row = pool[cid]
                for pipeline in _embedded_rows(payload):
                    if str(pipeline.get("status_id")) not in terminal:
                        continue
                    stage_rows.append(
                        StageConflict(
                            candidate_id=row.get("id", cid),
                            active_field=active_field,
                            active_value=_clip(str(row.get(active_field))),
                            pipeline_id=pipeline.get("id"),
                            job_id=pipeline.get("job_id"),
                            status_id=pipeline.get("status_id"),
                        )
                    )

        # --- region clusters and the inventory --------------------------------
        region_variants: list[VariantCluster] = []
        region_values: list[FieldValues] = []
        if "region_variants" in wanted:
            for field in region_keys:
                seen = region_seen.get(field, {})
                if not seen:
                    continue
                region_values.append(
                    FieldValues(
                        field=field,
                        values=[
                            ValueCount(value=value, normalized=meta["n"], count=meta["c"])
                            for value, meta in seen.items()
                        ],
                    )
                )
                by_fold: dict[str, list[ValueCount]] = {}
                for value, meta in seen.items():
                    by_fold.setdefault(meta["n"], []).append(
                        ValueCount(value=value, normalized=meta["n"], count=meta["c"])
                    )
                for folded, variants in by_fold.items():
                    if len(variants) > 1:
                        region_variants.append(
                            VariantCluster(field=field, normalized=folded, variants=variants)
                        )

        findings: dict[str, int] = {
            "duplicates": len(duplicates),
            "region_variants": len(region_variants),
            "contact_methods": len(contact_rows),
            "titles": len(title_rows),
            "stage_conflicts": len(stage_rows),
            "links": len(link_rows),
        }

        result = AuditResult(
            duplicates=duplicates,
            region_variants=region_variants,
            region_values=region_values,
            contact_methods=contact_rows,
            titles=title_rows,
            stage_conflicts=stage_rows,
            links=link_rows,
            counts={k: v for k, v in findings.items() if k in wanted},
            anomalies_checked=wanted,
            unchecked=unchecked,
            scanned=scanned,
            seeds_used=seeds,
            execution=ExecutionFacts(
                requests_used=requests_used,
                rate_limit=RateLimit.model_validate(client.rate_limit.snapshot()),
                truncated=truncated,
                # No resumable cursor: the pool is rebuilt from the seeds each
                # call, so there is no position to hand back. Null means "no
                # continuation exists"; `unchecked` and `truncated` are what say
                # the answer may be short a finding.
                next_cursor=None,
                errors=errors,
            ),
            note=RESULT_NOTE,
        )
        return ToolResult(content=_content_line(result), structured_content=result.model_dump())

    return 1


def _content_line(result: AuditResult) -> str:
    """The one line a human sees. Counts and flags, never record data.

    The structured payload is the answer; repeating it here would double the
    tokens for nothing, and issue #21 keeps names, addresses and phone numbers
    out of every channel that is not it - which is exactly what a duplicate
    finding would otherwise put on display.
    """
    parts = [f"scanned {result.scanned}", f"requests {result.execution.requests_used}"]
    parts.extend(f"{kind} {n}" for kind, n in result.counts.items() if n)
    if result.execution.truncated:
        parts.append("truncated")
    if result.unchecked:
        parts.append(f"unchecked {sum(result.unchecked.values())}")
    if result.execution.errors:
        parts.append(f"errors {len(result.execution.errors)}")
    return ", ".join(parts)
