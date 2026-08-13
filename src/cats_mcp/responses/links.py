"""Links back to records in the CATS web UI.

Consumers kept building these by hand and getting them wrong - a REST-looking
`/candidates/{id}` path that does not exist in CATS, producing links that look
right in a spreadsheet and 404 when someone clicks one.

The adapter knows the account and the id, so it should emit the link rather
than leaving every consumer to guess the format.

Three rules:

* No `CATS_UI_BASE_URL` configured means no link is emitted. A missing link is
  recoverable; a wrong one gets pasted into a spreadsheet and quietly wastes
  someone's afternoon.
* Only formats confirmed against a real account appear here. A guessed pattern
  would reproduce exactly the bug this module exists to fix.
* A link is only emitted when the id being linked is genuinely the id of that
  record. `/candidates/{id}/attachments` returns attachments, whose `id` is an
  attachment - building a candidate link from it points at an unrelated real
  person, which is a 200 OK and therefore worse than a 404.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _UIRecord:
    #: First path segment of the CATS API collection for this resource.
    api_root: str
    #: Query-string pattern for the CATS web UI.
    #:
    #: CATS uses `index.php?m=<module>&a=show&<key>=<id>`, not REST-style paths.
    #: The module and key names do not follow from the API's own vocabulary - a
    #: job is `m=joborders` with `jobOrderID`, not `jobs`/`jobId` - so each one
    #: has to be confirmed rather than inferred.
    ui_path: str


#: Every entry here was taken from a real URL in a live account. Anything not
#: listed gets no link at all, which is the point: a guessed pattern would
#: reproduce exactly the bug this module exists to prevent.
_UI_RECORDS: dict[str, _UIRecord] = {
    "candidate": _UIRecord(
        api_root="candidates",
        ui_path="index.php?m=candidates&a=show&candidateID={id}",
    ),
    "job": _UIRecord(
        api_root="jobs",
        ui_path="index.php?m=joborders&a=show&jobOrderID={id}",
    ),
}

#: Terminal segments that still return the collection's own records, rather than
#: a sub-collection. `/candidates/search` returns candidates.
_SEARCH_VERBS = frozenset({"search", "filter"})

_PLACEHOLDER = re.compile(r"^\{[^}]+\}$")


def record_url(ui_base_url: str, resource: str, record_id: object) -> str | None:
    """Build a UI link for one record, or None if it cannot be built correctly."""
    if not ui_base_url or record_id in (None, ""):
        return None
    record = _UI_RECORDS.get(resource)
    if record is None:
        return None
    return f"{ui_base_url.rstrip('/')}/{record.ui_path.format(id=record_id)}"


def endpoint_returns_the_record(resource: str, endpoint: str) -> bool:
    """Whether rows from this endpoint carry the resource's *own* id.

    A tool is tagged with the resource it belongs to, not the shape of the rows
    it returns, so `list_candidate_attachments` is `resource="candidate"` while
    each row is an attachment. Linking on `spec.resource` alone therefore built
    a candidate URL out of an attachment id - a valid link to the wrong person,
    for 29 of the 37 tools that emit one.

    The rule: strip a trailing id placeholder (it addresses one member of
    whatever precedes it) and a trailing search verb, and what remains must be
    the resource's own collection root.
    """
    record = _UI_RECORDS.get(resource)
    if record is None:
        return False

    segments = [s for s in endpoint.strip("/").split("/") if s]
    if segments and _PLACEHOLDER.match(segments[-1]):
        segments = segments[:-1]
    if len(segments) > 1 and segments[-1] in _SEARCH_VERBS:
        segments = segments[:-1]
    return segments == [record.api_root]


def has_url_format(resource: str) -> bool:
    return resource in _UI_RECORDS
