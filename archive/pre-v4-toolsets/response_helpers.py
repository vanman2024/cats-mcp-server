"""
Response helpers for CATS MCP Server - Lazy loading and response optimization.

Reduces context window usage by returning summaries for list operations
while keeping full detail available via get_* endpoints.
"""
from typing import Any, Optional


# Summary field definitions per entity type
SUMMARY_FIELDS = {
    "candidates": ["id", "first_name", "last_name", "email", "status", "created_date"],
    "jobs": ["id", "title", "status", "department", "location", "created_date"],
    "companies": ["id", "name", "website", "city", "state", "phone"],
    "contacts": ["id", "first_name", "last_name", "email", "title", "company_id"],
    "activities": ["id", "type", "subject", "date", "created_by"],
}


def summarize_list_response(
    raw_response: dict[str, Any],
    entity_type: str,
    fields: Optional[str] = None,
) -> dict[str, Any]:
    """
    Transform a raw API list response into a summarized version.

    Args:
        raw_response: Raw response from CATS API
        entity_type: Type of entity (candidates, jobs, companies, contacts)
        fields: Comma-separated field names to include, or None for default summary fields

    Returns:
        Optimized response with summary fields and pagination hints
    """
    if raw_response is None:
        return {"error": "No response from API", "items": [], "total": 0}

    # Determine which fields to extract
    if fields:
        selected_fields = [f.strip() for f in fields.split(",")]
    else:
        selected_fields = SUMMARY_FIELDS.get(entity_type, [])

    # The CATS API returns items in _embedded or directly
    items = []
    if "_embedded" in raw_response:
        items = raw_response["_embedded"].get(entity_type, [])
    elif isinstance(raw_response, list):
        items = raw_response
    elif entity_type in raw_response:
        items = raw_response[entity_type]

    # Extract only selected fields if we have a field list
    if selected_fields and items:
        summarized = []
        for item in items:
            if isinstance(item, dict):
                summary = {k: item.get(k) for k in selected_fields if k in item}
                # Always include id if available
                if "id" in item and "id" not in summary:
                    summary["id"] = item["id"]
                summarized.append(summary)
            else:
                summarized.append(item)
        items = summarized

    # Build optimized response
    result = {
        entity_type: items,
        "count": len(items),
        "total": raw_response.get("total", raw_response.get("total_count", len(items))),
        "page": raw_response.get("page"),
        "per_page": raw_response.get("per_page"),
    }

    # Add pagination hint
    total = result["total"]
    page = result.get("page", 1) or 1
    per_page = result.get("per_page", 25) or 25
    if total and total > page * per_page:
        result["has_more"] = True
        result["next_page"] = page + 1
        result["hint"] = (
            f"Showing {len(items)} of {total} {entity_type}. "
            f"Use page={page + 1} to fetch more. "
            f"Use get_{entity_type[:-1]}(id) for full details."
        )
    else:
        result["has_more"] = False

    return result
