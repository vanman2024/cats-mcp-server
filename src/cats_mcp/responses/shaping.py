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

from typing import Any
from urllib.parse import parse_qs, urlparse

from cats_mcp.registry.models import ResponseStrategy, ToolSpec

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
}

#: Never returned in a list result at any summary level. These are the fields
#: that make a candidate record enormous, and each has a dedicated tool.
_NEVER_IN_LISTS = frozenset(
    {
        "resume",
        "resume_text",
        "attachments",
        "activities",
        "pipelines",
        "applications",
        "custom_fields",
        "work_history",
        "notes_html",
    }
)

#: HAL plumbing. Useful to the API, noise to a model.
_HAL_KEYS = frozenset({"_links", "_embedded"})

_SUMMARY_LEVELS = ("compact", "standard", "full")


def _strip_hal(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _HAL_KEYS}


def _project(item: Any, fields: list[str] | None) -> Any:
    if not isinstance(item, dict):
        return item
    cleaned = _strip_hal(item)
    if fields is None:
        return {k: v for k, v in cleaned.items() if k not in _NEVER_IN_LISTS}
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
        if requested and requested != ["all"]:
            return requested
    if summary_level == "full":
        return None
    if summary_level == "standard":
        return None
    return SUMMARY_FIELDS.get(spec.resource)


def shape_list(spec: ToolSpec, raw: Any, call_args: dict[str, Any]) -> dict[str, Any]:
    """Compact a CATS collection response."""
    if not isinstance(raw, dict):
        return {"items": raw, "count": len(raw) if isinstance(raw, list) else 0}

    summary_level = str(call_args.get("summary_level") or "compact").lower()
    if summary_level not in _SUMMARY_LEVELS:
        summary_level = "compact"

    fields = _fields_for(spec, summary_level, call_args.get("fields"))
    items = [_project(item, fields) for item in _extract_items(raw, spec.collection_key)]

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
            f"Pass summary_level='full' for complete records, fields='a,b,c' to "
            f"choose columns, or use the dedicated get/detail tool for one record."
        )
    return result


def shape_detail(spec: ToolSpec, raw: Any, call_args: dict[str, Any]) -> Any:
    """Trim a single record: drop HAL plumbing, keep the record itself."""
    if not isinstance(raw, dict):
        return raw
    summary_level = str(call_args.get("summary_level") or "standard").lower()
    cleaned = _strip_hal(raw)
    if summary_level == "full":
        return cleaned
    if summary_level == "compact":
        fields = SUMMARY_FIELDS.get(spec.resource)
        if fields:
            return _project(raw, fields)
    # `standard`: the whole record minus the sub-collections that have their
    # own tools. Those are what make a candidate record enormous.
    return {k: v for k, v in cleaned.items() if k not in _NEVER_IN_LISTS}


def shape_response(spec: ToolSpec, raw: Any, call_args: dict[str, Any]) -> Any:
    if spec.response is ResponseStrategy.SUMMARY:
        return shape_list(spec, raw, call_args)
    if spec.response is ResponseStrategy.DETAIL:
        return shape_detail(spec, raw, call_args)
    return raw
