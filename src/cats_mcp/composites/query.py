"""Compound candidate query.

The atomic surface answers one criterion per request. That is faithful to CATS
- `POST /candidates/search` really does take a single field/filter/value - but
it means a question with three conditions in it has no server-side answer, and
the caller ends up sweeping the account and matching locally.

Issue #11 is the worked example: finding heavy-equipment mechanics in British
Columbia who have a contact method took 17 paginated state calls, client-side
deduplication and local title matching, and the result still could not honestly
be described as the answer, because list membership and pipeline state were
never consulted.

`query_candidate_facts` composes that sweep server-side. It is a data-access
primitive, not a screening policy: the occupational vocabulary comes from the
caller, and the result reports which predicate matched which field so the
caller can see what the set is made of. It returns facts and evidence. It does
not rank anyone, and it does not say who should be contacted - see
tests/test_boundary.py, which enforces that in both directions.

Cost model, and why the phases are ordered the way they are:

    Phase A  one request per seed value per page. Bounded by max_requests.
    Phase B  text predicates and saved-list membership. Text is free; a list
             costs one pass per *list* rather than one request per person.
    Phase C  per-candidate enrichment, one request each, spent only on the
             candidates that survived A and B.

Doing C before B is the mistake this exists to prevent: it spends the whole
hourly budget describing people the caller was about to discard.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from cats_mcp.composites.reads import (
    _dedupe,
    _embedded_rows,
    _gather_by_id,
    _has_next_page,
    _list_memberships,
    _pipeline_rows,
    _progress,
    _status_titles,
)
from cats_mcp.http.correlation import get_logger, set_run_id
from cats_mcp.http.errors import CATSAPIError

logger = get_logger(__name__)

#: Rows per seed page. CATS honours this, so a 1,094-candidate province is
#: eleven requests rather than forty-four at the default of 25.
SEED_PAGE_SIZE = 100

#: Default and hard ceiling on requests for one call.
#:
#: The budget is the whole point of the tool. A compound query over a large
#: account can always find more work to do, so it stops at a number the caller
#: chose and hands back a cursor rather than quietly spending an hour's worth
#: of a 500/hour allowance.
DEFAULT_MAX_REQUESTS = 25
MAX_REQUESTS_CEILING = 120

#: Default and hard ceiling on candidates returned from one call.
DEFAULT_MAX_CANDIDATES = 100
MAX_CANDIDATES_CEILING = 500

#: What `include` accepts, and what each costs. Same asymmetry as
#: get_candidate_context: membership is nearly free, per-person detail is not.
INCLUDE_OPTIONS: dict[str, str] = {
    "identity": "name, title, city, state, current employer - one request per candidate",
    "contact_methods": "whether an email and/or phone exists - free alongside identity",
    "custom_fields": "account-specific fields such as certifications - free alongside identity",
    "lists": "saved-list membership - one request per list, not per person",
    "pipelines": "job applications and their current stage - one request per candidate",
    "engagement": "last activity date and total activity count - one request per candidate",
}

#: Includes that cost one request per candidate.
PER_CANDIDATE_INCLUDES = frozenset({"identity", "contact_methods", "custom_fields"})
PIPELINE_INCLUDES = frozenset({"pipelines"})
ENGAGEMENT_INCLUDES = frozenset({"engagement"})

_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")


class TextPredicate(BaseModel):
    """A compound text condition over one candidate field.

    Modelled rather than passed as a bare string so the AND/OR semantics live
    in the JSON schema instead of in prose a caller has to guess at. The values
    are the caller's vocabulary: 'Field Service Technician', 'HD Tech' and
    'Mobile Equipment Technician' are all the same job to a recruiter and
    nothing in CATS knows that, so the adapter must not pretend to.
    """

    values: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Terms to look for. These are yours - the adapter has no occupational "
                "taxonomy and will not expand, stem or synonym-match them."
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


def _normalise(value: Any) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Nothing else.

    This is the only transformation applied to candidate text. It makes
    'Heavy-Duty Mechanic' and 'heavy duty mechanic' the same string, which is
    normalisation. Deciding that 'Millwright' is also that job is not, and
    belongs to the caller.
    """
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.lower())).strip()


def _evaluate(text: Any, predicate: TextPredicate) -> tuple[bool, list[str]]:
    """Apply a predicate to one field. Returns (satisfied, which values hit)."""
    haystack = _normalise(text)
    hits: list[str] = []
    needles = [(raw, _normalise(raw)) for raw in predicate.values]
    usable = [(raw, n) for raw, n in needles if n]

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
        return True, []
    satisfied = len(hits) == len(usable) if predicate.match == "all" else bool(hits)
    return satisfied, hits


def _contact_methods(record: Any) -> dict[str, bool]:
    """Whether a record carries an email and/or a phone.

    CATS exposes emails and phones as sub-collections, but a full candidate
    read returns them inline or under _embedded depending on the account's
    configuration. All three shapes are checked because guessing one and
    reporting has_email=false for the other two would be a silent wrong answer
    on the exact field a caller filters outreach on.
    """
    if not isinstance(record, dict):
        return {"has_email": False, "has_phone": False}

    embedded = record.get("_embedded") if isinstance(record.get("_embedded"), dict) else {}

    def present(*keys: str) -> bool:
        for key in keys:
            value = record.get(key)
            if value is None and isinstance(embedded, dict):
                value = embedded.get(key)
            if isinstance(value, list) and any(v for v in value):
                return True
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, dict) and value:
                return True
        return False

    return {
        "has_email": present("emails", "email", "email_address", "email1"),
        "has_phone": present("phones", "phone", "phone_cell", "phone_home", "phone_work"),
    }


def _encode_cursor(seed_index: int, page: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"s": seed_index, "p": page}, separators=(",", ":")).encode()
    ).decode()


def _decode_cursor(cursor: str | None) -> tuple[int, int]:
    """Resume position as (seed index, page). Anything unreadable starts over.

    A cursor is opaque to the caller, so a malformed one is far more likely to
    be a truncated round trip than an attack. Restarting the sweep costs
    requests but returns correct data; raising would strand the caller with no
    way to continue.
    """
    if not cursor:
        return 0, 1
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return max(0, int(decoded["s"])), max(1, int(decoded["p"]))
    except (ValueError, KeyError, TypeError, binascii.Error):
        logger.warning("unreadable cursor, restarting the sweep")
        return 0, 1


def _seed_plan(
    states: list[str] | None,
    cities: list[str] | None,
    seed_field: str | None,
    seed_values: list[str] | None,
) -> list[tuple[str, str]]:
    """(field, value) pairs to sweep, one exact filter each.

    One filter per value on purpose: `contains` tokenises, so a single
    contains='British Columbia' also matches every record containing
    'Columbia'. Province spelling varies per account - 'BC', 'British
    Columbia', 'B.C.' are three different stored values, not three spellings of
    one - so the caller passes the variants it wants and each is exact.
    """
    plan: list[tuple[str, str]] = []
    for field, values in (("state", states), ("city", cities)):
        for value in values or []:
            if str(value).strip():
                plan.append((field, str(value).strip()))
    if seed_field and seed_values:
        for value in seed_values:
            if str(value).strip():
                plan.append((seed_field.strip(), str(value).strip()))
    return plan


def register(mcp: Any, client_getter: Callable[[], Any], *, enforce_auth: bool) -> int:
    """Register the compound query primitive. Returns how many were added."""
    tool_kwargs: dict[str, Any] = {}
    if enforce_auth:
        from fastmcp.server.auth import require_scopes

        tool_kwargs["auth"] = require_scopes("cats:read")

    @mcp.tool(
        name="query_candidate_facts",
        # Kept deliberately tight. Every pinned schema is resident in every
        # request, and this description alone was 7.2KB of the 45KB budget -
        # more than the next two pinned tools combined. The detail that used to
        # live here is in the module docstring, where it costs nothing to skip.
        description=(
            "Answer a compound candidate question in one call: seed from exact CATS "
            "filters (state, city, or any field you name), then narrow locally by "
            "title, employer, saved-list membership, pipeline stage and whether a "
            "contact method exists. Use this instead of paging the account yourself - "
            "the atomic search takes one criterion per request.\n\n"
            "The occupational vocabulary is yours: pass the titles you mean, because "
            "CATS does not know that 'HD Tech' and 'Field Service Technician' are one "
            "job, and this tool will not invent that.\n\n"
            "Returns facts and match evidence - every row names which predicate "
            "matched which field, and `dropped_by` counts what each predicate removed. "
            "Interpreting them is the caller's job. Budgeted and resumable: on reaching "
            "max_requests or max_candidates it stops and returns next_cursor."
        ),
        tags={"ats", "candidate", "read", "batch", "search", "screening"},
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        **tool_kwargs,
    )
    async def query_candidate_facts(
        states: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Exact state/province values to seed from, one filter each. Accounts "
                    "store variants separately, so pass every one you mean: "
                    "['BC', 'British Columbia', 'B.C.']."
                )
            ),
        ] = None,
        cities: Annotated[
            list[str] | None,
            Field(description="Exact city values to seed from, one filter each."),
        ] = None,
        seed_field: Annotated[
            str | None,
            Field(
                description=(
                    "Any other CATS candidate field to seed from, for example "
                    "'status_id'. Used with seed_values."
                )
            ),
        ] = None,
        seed_values: Annotated[
            list[str] | None,
            Field(description="Exact values for seed_field, one filter each."),
        ] = None,
        title: Annotated[
            TextPredicate | None,
            Field(description="Compound condition on the candidate's title."),
        ] = None,
        current_employer: Annotated[
            TextPredicate | None,
            Field(description="Compound condition on the candidate's current employer."),
        ] = None,
        require_email: Annotated[
            bool, Field(description="Keep only candidates with at least one email.")
        ] = False,
        require_phone: Annotated[
            bool, Field(description="Keep only candidates with at least one phone.")
        ] = False,
        require_any_contact_method: Annotated[
            bool, Field(description="Keep only candidates with an email or a phone.")
        ] = False,
        include_list_ids: Annotated[
            list[int | str] | None,
            Field(description="Keep only candidates on at least one of these saved lists."),
        ] = None,
        exclude_list_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Drop candidates on any of these saved lists - a Do Not Contact "
                    "list, for example. Find ids with list_candidate_lists."
                )
            ),
        ] = None,
        include_pipeline_status_ids: Annotated[
            list[int | str] | None,
            Field(description="Keep only candidates with a pipeline at one of these statuses."),
        ] = None,
        exclude_pipeline_status_ids: Annotated[
            list[int | str] | None,
            Field(description="Drop candidates with a pipeline at any of these statuses."),
        ] = None,
        include: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Facts to return for surviving candidates. "
                    + "; ".join(f"'{k}': {v}" for k, v in INCLUDE_OPTIONS.items())
                    + ". Defaults to ['identity', 'contact_methods']."
                )
            ),
        ] = None,
        max_candidates: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_CANDIDATES_CEILING,
                description=f"Rows to return. Ceiling {MAX_CANDIDATES_CEILING}.",
            ),
        ] = DEFAULT_MAX_CANDIDATES,
        max_requests: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_REQUESTS_CEILING,
                description=(
                    f"CATS requests this call may spend. Ceiling {MAX_REQUESTS_CEILING}. "
                    "The sweep stops here and returns next_cursor."
                ),
            ),
        ] = DEFAULT_MAX_REQUESTS,
        cursor: Annotated[
            str | None,
            Field(description="next_cursor from a previous call, to continue that sweep."),
        ] = None,
    ) -> dict[str, Any]:
        set_run_id()
        client = client_getter()

        plan = _seed_plan(states, cities, seed_field, seed_values)
        if not plan:
            raise ValueError(
                "A seed is required: pass states, cities, or seed_field with "
                "seed_values. Without one this would sweep every candidate in the "
                "account, which the request budget exists to prevent. Use "
                "list_candidate_custom_field_definitions or filter_candidates to find "
                "a field to seed from."
            )

        # `include is None` is "you choose"; `include=[]` is "nothing extra".
        # Collapsing the two with `or` silently bills the caller for a profile
        # read per candidate when they explicitly asked for none.
        requested = ["identity", "contact_methods"] if include is None else include
        wanted = [i.strip().lower() for i in requested if i.strip()]
        unknown = [i for i in wanted if i not in INCLUDE_OPTIONS]
        if unknown:
            raise ValueError(
                f"Unknown include values {unknown}. Valid options: {sorted(INCLUDE_OPTIONS)}."
            )

        contact_required = require_email or require_phone or require_any_contact_method
        status_filtered = bool(include_pipeline_status_ids or exclude_pipeline_status_ids)

        # A filter implies the read that answers it. Asking to drop everyone at
        # a given pipeline status while not fetching pipelines would silently
        # drop nobody, which reads as "none matched" rather than "not checked".
        if contact_required and "contact_methods" not in wanted:
            wanted.append("contact_methods")
        if status_filtered and "pipelines" not in wanted:
            wanted.append("pipelines")

        errors: dict[str, str] = {}
        requests_used = 0
        dropped_by: dict[str, int] = {}

        def drop(reason: str, count: int = 1) -> None:
            if count:
                dropped_by[reason] = dropped_by.get(reason, 0) + count

        # --- Phase A: bounded seed ------------------------------------------
        seed_index, page = _decode_cursor(cursor)
        seeded: dict[str, dict[str, Any]] = {}
        scanned = 0
        exhausted = True
        next_cursor: str | None = None

        while seed_index < len(plan):
            field, value = plan[seed_index]
            if requests_used >= max_requests:
                exhausted = False
                next_cursor = _encode_cursor(seed_index, page)
                break

            try:
                payload = await client.request(
                    "POST",
                    "/candidates/search",
                    json={"field": field, "filter": "exactly", "value": value},
                    params={"per_page": SEED_PAGE_SIZE, "page": page},
                )
                requests_used += 1
                # The sweep is the slow part and the caller cannot see it. Its
                # budget is the only honest denominator - the account's true
                # size is exactly what this is trying to find out.
                await _progress(
                    requests_used, max_requests, f"sweeping {field}={value}, page {page}"
                )
            except CATSAPIError as exc:
                errors[f"seed:{field}={value}:page:{page}"] = str(exc)
                seed_index += 1
                page = 1
                continue

            rows = _embedded_rows(payload)
            scanned += len(rows)
            for row in rows:
                identifier = row.get("id")
                if identifier is None:
                    continue
                key = str(identifier)
                existing = seeded.get(key)
                if existing is None:
                    row = dict(row)
                    row["_matched"] = [{"field": field, "predicate": "exact", "value": value}]
                    seeded[key] = row
                else:
                    existing["_matched"].append(
                        {"field": field, "predicate": "exact", "value": value}
                    )

            if rows and _has_next_page(payload):
                page += 1
            else:
                seed_index += 1
                page = 1

        deduplicated = scanned - len(seeded)

        # --- Phase B: local predicates, then cheap membership ---------------
        survivors = list(seeded.values())

        for label, predicate, source in (
            ("title", title, "title"),
            ("current_employer", current_employer, "current_employer"),
        ):
            if predicate is None:
                continue
            kept = []
            unevaluated = 0
            for row in survivors:
                if source not in row:
                    # The projection did not carry the field at all, so the
                    # predicate is unanswerable rather than unsatisfied.
                    # Dropping the whole set over a projection detail would be
                    # worse than reporting the gap, so the row survives.
                    #
                    # A *present* field holding null is a different thing and is
                    # handled below: it means the record has no value, which is
                    # a real answer - the predicate is not satisfied. Conflating
                    # the two kept 549 candidates with no title at all in a
                    # search for heavy-equipment mechanics, against a live
                    # account, which is exactly the false-positive flood this
                    # tool exists to prevent.
                    row.setdefault("_unevaluated", []).append(label)
                    unevaluated += 1
                    kept.append(row)
                    continue
                satisfied, hits = _evaluate(row.get(source), predicate)
                if satisfied:
                    for hit in hits:
                        row["_matched"].append(
                            {"field": label, "predicate": predicate.mode, "value": hit}
                        )
                    kept.append(row)
            drop(label, len(survivors) - len(kept))
            if unevaluated:
                errors[f"unevaluated:{label}"] = (
                    f"{unevaluated} seed rows did not carry '{source}'; the predicate "
                    f"could not be applied to them and they were kept, not dropped. "
                    f"Re-run with include=['identity'] to evaluate it."
                )
            survivors = kept

        # Membership is one pass per list regardless of how many people are
        # checked, so it runs before anything per-candidate.
        memberships: dict[str, list[dict[str, Any]]] = {}
        wanted_lists = _dedupe([*(include_list_ids or []), *(exclude_list_ids or [])])
        if wanted_lists and requests_used < max_requests:
            memberships, list_errors, used = await _list_memberships(client, wanted_lists)
            errors.update(list_errors)
            requests_used += used

            if include_list_ids:
                allowed = {str(i) for i in include_list_ids}
                kept = [
                    r
                    for r in survivors
                    if allowed & {str(m["id"]) for m in memberships.get(str(r.get("id")), [])}
                ]
                drop("include_list_ids", len(survivors) - len(kept))
                survivors = kept

            if exclude_list_ids:
                banned = {str(i) for i in exclude_list_ids}
                kept = [
                    r
                    for r in survivors
                    if not banned & {str(m["id"]) for m in memberships.get(str(r.get("id")), [])}
                ]
                drop("exclude_list_ids", len(survivors) - len(kept))
                survivors = kept
        elif wanted_lists:
            errors["lists"] = "request budget exhausted before list membership was resolved"

        truncated = len(survivors) > max_candidates
        survivors = survivors[:max_candidates]
        if truncated and next_cursor is None:
            next_cursor = _encode_cursor(seed_index, page)
            exhausted = False

        ids = [r.get("id") for r in survivors if r.get("id") is not None]

        # --- Phase C: per-candidate enrichment, on survivors only -----------
        records: dict[str, Any] = {}
        if ids and set(wanted) & PER_CANDIDATE_INCLUDES:
            affordable = ids[: max(0, max_requests - requests_used)]
            if len(affordable) < len(ids):
                errors["identity"] = (
                    f"budget covered {len(affordable)} of {len(ids)} candidates; "
                    f"raise max_requests or lower max_candidates"
                )

            async def fetch_record(cid):
                return await client.request("GET", f"/candidates/{cid}")

            records, record_errors = await _gather_by_id(affordable, fetch_record)
            errors.update(record_errors)
            requests_used += len(affordable)

        pipelines: dict[str, Any] = {}
        statuses: dict[str, str] = {}
        if ids and set(wanted) & PIPELINE_INCLUDES and requests_used < max_requests:
            statuses, status_calls = await _status_titles(client)
            requests_used += status_calls
            affordable = ids[: max(0, max_requests - requests_used)]

            async def fetch_pipelines(cid):
                return await client.request("GET", f"/candidates/{cid}/pipelines")

            pipelines, pipeline_errors = await _gather_by_id(affordable, fetch_pipelines)
            errors.update(pipeline_errors)
            requests_used += len(affordable)

        engagement: dict[str, Any] = {}
        if ids and set(wanted) & ENGAGEMENT_INCLUDES and requests_used < max_requests:
            affordable = ids[: max(0, max_requests - requests_used)]

            async def fetch_activities(cid):
                return await client.request(
                    "GET", f"/candidates/{cid}/activities", params={"per_page": 100}
                )

            engagement, activity_errors = await _gather_by_id(affordable, fetch_activities)
            errors.update(activity_errors)
            requests_used += len(affordable)

        # --- Assemble, then apply the filters that needed Phase C -----------
        rows: list[dict[str, Any]] = []
        for row in survivors:
            key = str(row.get("id"))
            record = records.get(key) if isinstance(records.get(key), dict) else None
            merged = {**row, **(record or {})}

            out: dict[str, Any] = {"candidate_id": row.get("id")}

            if "identity" in wanted:
                name = " ".join(
                    str(merged.get(part) or "").strip()
                    for part in ("first_name", "last_name")
                ).strip()
                out["name"] = name or None
                out["title"] = merged.get("title")
                out["current_employer"] = merged.get("current_employer")
                out["location"] = {"city": merged.get("city"), "state": merged.get("state")}

            if "contact_methods" in wanted:
                out["contact_methods"] = _contact_methods(merged)

            if "custom_fields" in wanted:
                out["custom_fields"] = merged.get("custom_fields")

            if "lists" in wanted or wanted_lists:
                out["list_memberships"] = memberships.get(key, [])

            if "pipelines" in wanted:
                out["pipelines"] = _pipeline_rows(pipelines.get(key), statuses)

            if "engagement" in wanted:
                payload = engagement.get(key)
                activities = _embedded_rows(payload)
                dated = [a for a in activities if a.get("date_created")]
                latest = max(dated, key=lambda a: a["date_created"], default=None)
                out["engagement"] = {
                    "activity_count": (payload or {}).get("total", len(activities)),
                    "last_activity_date": (latest or {}).get("date_created"),
                    "last_activity_type": (latest or {}).get("type"),
                }

            out["matched"] = row.get("_matched", [])
            if row.get("_unevaluated"):
                out["unevaluated"] = row["_unevaluated"]
            rows.append(out)

        if contact_required:
            def has_contact(row: dict[str, Any]) -> bool:
                methods = row.get("contact_methods") or {}
                if require_email and not methods.get("has_email"):
                    return False
                if require_phone and not methods.get("has_phone"):
                    return False
                if require_any_contact_method and not (
                    methods.get("has_email") or methods.get("has_phone")
                ):
                    return False
                return True

            kept = [r for r in rows if has_contact(r)]
            drop("contact_requirements", len(rows) - len(kept))
            rows = kept

        if status_filtered:
            if include_pipeline_status_ids:
                allowed = {str(i) for i in include_pipeline_status_ids}
                kept = [
                    r
                    for r in rows
                    if allowed & {str(p.get("status_id")) for p in (r.get("pipelines") or [])}
                ]
                drop("include_pipeline_status_ids", len(rows) - len(kept))
                rows = kept
            if exclude_pipeline_status_ids:
                banned = {str(i) for i in exclude_pipeline_status_ids}
                kept = [
                    r
                    for r in rows
                    if not banned & {str(p.get("status_id")) for p in (r.get("pipelines") or [])}
                ]
                drop("exclude_pipeline_status_ids", len(rows) - len(kept))
                rows = kept

        return {
            "candidates": rows,
            "count": len(rows),
            "scanned": scanned,
            "deduplicated": deduplicated,
            "dropped_by": dropped_by,
            "truncated": truncated or not exhausted,
            "next_cursor": next_cursor,
            "seeds_used": [{"field": f, "value": v} for f, v in plan],
            "included": wanted,
            "errors": errors,
            "requests_used": requests_used,
            "rate_limit": client.rate_limit.snapshot(),
            "note": (
                "Facts and match evidence only; interpreting them is the caller's "
                "job. `dropped_by` counts what each of your "
                "predicates removed; a non-null next_cursor means the sweep stopped on "
                "budget, not that the account holds nothing further."
            ),
        }

    return 1
