"""Links back to records in the CATS web UI.

Consumers kept building these by hand and getting them wrong - a REST-looking
`/candidates/{id}` path that does not exist in CATS, producing links that look
right in a spreadsheet and 404 when someone clicks one.

The adapter knows the account and the id, so it should emit the link rather
than leaving every consumer to guess the format.

Two rules:

* No `CATS_UI_BASE_URL` configured means no link is emitted. A missing link is
  recoverable; a wrong one gets pasted into a spreadsheet and quietly wastes
  someone's afternoon.
* Only formats confirmed against a real account appear here. A guessed pattern
  would reproduce exactly the bug this module exists to fix.
"""

from __future__ import annotations

#: Query-string patterns for the CATS web UI, keyed by resource.
#:
#: CATS uses `index.php?m=<module>&a=show&<key>=<id>`, not REST-style paths.
#: The module and key names do not follow from the API's own vocabulary - a job
#: is `m=joborders` with `jobOrderID`, not `jobs`/`jobId` - so each one has to
#: be confirmed rather than inferred.
#:
#: Every entry here was taken from a real URL in a live account. Anything not
#: listed gets no link at all, which is the point: a guessed pattern would
#: reproduce exactly the bug this module exists to prevent.
_UI_PATHS: dict[str, str] = {
    "candidate": "index.php?m=candidates&a=show&candidateID={id}",
    "job": "index.php?m=joborders&a=show&jobOrderID={id}",
}


def record_url(ui_base_url: str, resource: str, record_id: object) -> str | None:
    """Build a UI link for one record, or None if it cannot be built correctly."""
    if not ui_base_url or record_id in (None, ""):
        return None
    path = _UI_PATHS.get(resource)
    if path is None:
        return None
    return f"{ui_base_url.rstrip('/')}/{path.format(id=record_id)}"


def has_url_format(resource: str) -> bool:
    return resource in _UI_PATHS
