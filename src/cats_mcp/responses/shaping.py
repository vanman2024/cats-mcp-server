"""Response shaping - keep CATS payloads out of the model's context window.

Tool discovery solves the tool-schema context problem. It does nothing about
oversized *results*: a single CATS candidate record carries custom fields, HAL
`_links`, and `_embedded` sub-objects, and a 25-record page of those will swamp
a context window on its own.

The rule here: list and search results are always compact by default, and full
detail is reached through explicit, separate tools.

Shaping is driven by the real CATS HAL collection shape:

    {
      "count": 25,
      "total": 106,
      "_links": {"next": {"href": ".../jobs?page=2&per_page=25"}},
      "_embedded": {"jobs": [...]}
    }

`has_more` is derived from the presence of `_links.next`, not from arithmetic
over page/per_page. The previous helper guessed a page size of 25 and fell back
to `len(items)` for `total`, which silently reported a page size as if it were
the full result count.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from cats_mcp.registry.models import ResponseStrategy, ToolSpec
from cats_mcp.responses.links import endpoint_returns_the_record, record_url

#: Compact projections per resource. Every one includes `id`, because an id is
#: what lets the model fetch detail later.
SUMMARY_FIELDS: dict[str, list[str]] = {
    "candidate": [
        "id",
        "first_name",
        "last_name",
        "title",
        "city",
        "state",
        "is_hot",
        "date_modified",
    ],
    "job": ["id", "title", "status_id", "city", "state", "company_id", "date_modified"],
    "company": ["id", "name", "city", "state", "phone", "date_modified"],
    "contact": ["id", "first_name", "last_name", "title", "company_id", "date_modified"],
    "activity": ["id", "type", "notes", "date_created", "regarding_id"],
    "pipeline": ["id", "candidate_id", "job_id", "status_id", "rating", "date_modified"],
    "task": ["id", "title", "due_date", "is_completed"],
    "tag": ["id", "title"],
    "user": ["id", "first_name", "last_name", "email_address"],
    "attachment": ["id", "filename", "content_type", "date_created", "is_resume"],
    "work_history": ["id", "company_name", "title", "start_date", "end_date"],
    # A list *item* is not the record it points at. Its `id` is the membership
    # row; the record is in candidate_id / job_id. Projecting these through the
    # candidate summary dropped candidate_id entirely and left the item id
    # sitting in `id`, where it reads as a candidate id and is not one.
    "list_item": ["id", "candidate_id", "job_id", "date_created"],
    "record_list": ["id", "name", "description", "total", "date_created"],
}

#: Unbounded sub-collections. Excluded from list results at every summary level
#: because one of them can be tens of kilobytes per record, and each has a
#: dedicated tool for retrieving it deliberately.
_NEVER_IN_LISTS = frozenset(
    {
        "resume",
        "resume_text",
        "attachments",
        "activities",
        "pipelines",
        "applications",
        "work_history",
        "notes_html",
    }
)

#: Bounded but noisy: excluded from `compact`, included from `standard` up.
#:
#: Custom fields hold the data an account actually screens on - certifications,
#: trade qualifications, availability - so making them unreachable in a list
#: forces one request per candidate to answer "who has a Red Seal". At 500
#: requests/hour that is the difference between one call and fifty.
_EXCLUDED_FROM_COMPACT = frozenset({"custom_fields"})

#: HAL plumbing. Useful to the API, noise to a model.
_HAL_KEYS = frozenset({"_links", "_embedded"})

_SUMMARY_LEVELS = ("compact", "standard", "full")


#: Navigation relations. These point at pages, not at records.
_NAVIGATION_RELS = frozenset({"self", "next", "prev", "previous", "first", "last"})

#: Trailing numeric id in a HAL href, e.g. ".../candidates/407813885".
_HREF_ID = re.compile(r"/(\d+)/?$")


def _harvest_related_ids(item: dict[str, Any]) -> dict[str, Any]:
    """Recover record ids from HAL `_links` and `_embedded` before they are stripped.

    Some responses carry the id of the record they refer to *only* in the HAL
    plumbing. A saved-list membership row is the clearest case: its own `id` is
    the row, and the person it points at may appear solely as
    `_links.candidate.href`. Stripping HAL then leaves a row that identifies
    nobody, which makes the endpoint look like it can only be resolved one item
    at a time - it cannot, and doing so costs 299 requests against a 500/hour
    budget for a single list.

    Existing top-level fields always win; this only fills gaps.
    """
    found: dict[str, Any] = {}

    links = item.get("_links")
    if isinstance(links, dict):
        for rel, target in links.items():
            if rel in _NAVIGATION_RELS:
                continue
            href = target.get("href") if isinstance(target, dict) else target
            if not isinstance(href, str):
                continue
            match = _HREF_ID.search(href)
            if match:
                found.setdefault(f"{rel}_id", int(match.group(1)))

    embedded = item.get("_embedded")
    if isinstance(embedded, dict):
        for rel, target in embedded.items():
            if isinstance(target, dict) and "id" in target:
                found.setdefault(f"{rel}_id", target["id"])

    return {k: v for k, v in found.items() if k not in item}


def _strip_hal(item: dict[str, Any]) -> dict[str, Any]:
    """Drop HAL plumbing, keeping any record ids it was carrying."""
    recovered = _harvest_related_ids(item)
    kept = {k: v for k, v in item.items() if k not in _HAL_KEYS}
    kept.update(recovered)
    return kept


def _project(item: Any, fields: list[str] | None, *, drop_custom_fields: bool = False) -> Any:
    if not isinstance(item, dict):
        return item
    cleaned = _strip_hal(item)
    if fields is None:
        excluded = _NEVER_IN_LISTS
        if drop_custom_fields:
            excluded = excluded | _EXCLUDED_FROM_COMPACT
        return {k: v for k, v in cleaned.items() if k not in excluded}
    # An explicit field list is an explicit choice; honour it exactly.
    projected = {k: cleaned.get(k) for k in fields if k in cleaned}
    if "id" in cleaned:
        projected["id"] = cleaned["id"]
    return projected


def _extract_items(raw: dict[str, Any], collection_key: str | None) -> list[Any]:
    embedded = raw.get("_embedded")
    if isinstance(embedded, dict):
        if collection_key and collection_key in embedded:
            value = embedded[collection_key]
            if isinstance(value, list):
                return value
        # Fall back to the only list present, rather than assuming the key
        # equals the resource name - CATS does not always pluralise predictably.
        lists = [v for v in embedded.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    if isinstance(raw, list):
        return raw
    if collection_key and isinstance(raw.get(collection_key), list):
        return raw[collection_key]
    return []


def _next_page(raw: dict[str, Any]) -> int | None:
    """Read the next page number straight from HAL, rather than computing it."""
    links = raw.get("_links")
    if not isinstance(links, dict):
        return None
    nxt = links.get("next")
    href = nxt.get("href") if isinstance(nxt, dict) else nxt
    if not isinstance(href, str):
        return None
    values = parse_qs(urlparse(href).query).get("page")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def _fields_for(spec: ToolSpec, summary_level: str, explicit: str | None) -> list[str] | None:
    if explicit:
        requested = [f.strip() for f in explicit.split(",") if f.strip()]
        # `fields="all"` is handled by the caller, which promotes it to
        # summary_level="full". Reaching here with it would silently return a
        # compact record to someone who asked for everything.
        if requested and requested != ["all"]:
            return requested
    if summary_level in ("full", "standard"):
        return None
    return SUMMARY_FIELDS.get(spec.resource)


def shape_list(
    spec: ToolSpec, raw: Any, call_args: dict[str, Any], ui_base_url: str = ""
) -> dict[str, Any]:
    """Compact a CATS collection response."""
    if not isinstance(raw, dict):
        return {"items": raw, "count": len(raw) if isinstance(raw, list) else 0}

    summary_level = str(call_args.get("summary_level") or "compact").lower()
    if summary_level not in _SUMMARY_LEVELS:
        summary_level = "compact"

    explicit_fields = call_args.get("fields")
    # The pre-refactor tools accepted fields="all" to mean "the whole record".
    # Honour it rather than silently returning a compact result to a caller who
    # explicitly asked for everything.
    if explicit_fields and explicit_fields.strip().lower() == "all":
        summary_level = "full"

    fields = _fields_for(spec, summary_level, explicit_fields)
    items = [
        _with_url(
            _project(item, fields, drop_custom_fields=summary_level == "compact"),
            spec,
            ui_base_url,
        )
        for item in _extract_items(raw, spec.collection_key)
    ]

    total = raw.get("total")
    next_page = _next_page(raw)

    result: dict[str, Any] = {
        "items": items,
        "count": raw.get("count", len(items)),
        "total": total,
        "has_more": next_page is not None,
        "summary_level": summary_level,
    }
    if next_page is not None:
        result["next_page"] = next_page

    if summary_level == "compact" and fields:
        result["note"] = (
            f"Compact view: {', '.join(fields)}. "
            f"Pass summary_level='standard' to include custom fields such as "
            f"certifications and trade qualifications, summary_level='full' for "
            f"the whole record, or fields='a,b,c' to choose columns. Resumes, "
            f"attachments, activities and pipelines are never included in a list "
            f"- each has its own tool."
        )
    return result


def shape_detail(
    spec: ToolSpec, raw: Any, call_args: dict[str, Any], ui_base_url: str = ""
) -> Any:
    """Trim a single record: drop HAL plumbing, keep the record itself."""
    if not isinstance(raw, dict):
        return raw
    summary_level = str(call_args.get("summary_level") or "standard").lower()
    cleaned = _strip_hal(raw)
    if summary_level == "full":
        return _with_url(cleaned, spec, ui_base_url)
    if summary_level == "compact":
        fields = SUMMARY_FIELDS.get(spec.resource)
        if fields:
            return _with_url(_project(raw, fields), spec, ui_base_url)
    # `standard`: the whole record minus the sub-collections that have their
    # own tools. Those are what make a candidate record enormous.
    return _with_url(
        {k: v for k, v in cleaned.items() if k not in _NEVER_IN_LISTS}, spec, ui_base_url
    )


#: Resources whose rows *reference* a linkable record rather than being one.
#: A saved-list membership row has its own `id`; the person is `candidate_id`.
_REFERENCED_RECORD = {
    "list_item": (("candidate_id", "candidate"), ("job_id", "job")),
}


def _with_url(item: Any, spec: ToolSpec, ui_base_url: str) -> Any:
    """Attach a link to the record in the CATS web UI, when one can be built.

    Emitted by the adapter rather than left to the caller, because consumers
    were constructing a REST-looking `/candidates/{id}` path that does not
    exist - links that look right in a spreadsheet and 404 when clicked.

    The id has to be the *right* id. A tool is tagged with the resource it
    belongs to, not the shape of the rows it returns, so `list_candidate_tags`
    is `resource="candidate"` while each row is a tag. Linking on the resource
    alone built a candidate URL out of a tag id: a working link to an unrelated
    real person, which is a 200 OK and so worse than a dead one.
    """
    if not isinstance(item, dict):
        return item

    for field, resource in _REFERENCED_RECORD.get(spec.resource, ()):
        url = record_url(ui_base_url, resource, item.get(field))
        if url:
            item["url"] = url
            return item

    if not endpoint_returns_the_record(spec.resource, spec.endpoint):
        return item
    url = record_url(ui_base_url, spec.resource, item.get("id"))
    if url:
        item["url"] = url
    return item


def shape_response(
    spec: ToolSpec, raw: Any, call_args: dict[str, Any], ui_base_url: str = ""
) -> Any:
    if spec.response is ResponseStrategy.SUMMARY:
        return shape_list(spec, raw, call_args, ui_base_url)
    if spec.response is ResponseStrategy.DETAIL:
        return shape_detail(spec, raw, call_args, ui_base_url)
    return raw
