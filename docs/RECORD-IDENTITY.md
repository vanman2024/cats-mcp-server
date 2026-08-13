# Why an id is not the record

Every bug this adapter has shipped in the id-handling area is the same bug wearing
a different hat: **an id in a CATS response is frequently not the id of the record
you think you are looking at.**

This is not a CATS defect. It is a normal consequence of a REST API where every
row is a first-class resource. It is dangerous here because of one property:

> Both ids are real ids on the account. Using the wrong one does not error. It
> resolves to a different record.

There is no 404, no validation failure, no warning in a log. The wrong answer
looks exactly like the right answer. That is what makes it worth a document.

## The problem

Four separate incidents, one root cause.

### 1. A saved-list row is not the person on the list

`GET /candidates/lists/{id}/items` returns membership rows:

```json
{"id": 390055557, "candidate_id": 397943414, "date_created": "2023-05-27T12:34:36-05:00"}
```

`390055557` is the membership row. `397943414` is the person. Both are valid ids
on this account.

The adapter classified these rows as `resource="candidate"` and applied the
candidate summary projection. That projection keeps `id` and drops unknown fields,
so `candidate_id` disappeared and the row id was left sitting in `id`, where it
reads as a candidate id and is not one.

The consequence was a Do Not Contact check that compared candidate ids against
membership ids, found no overlap, and reported the list as clear. On a DNC list,
a false negative means contacting someone who asked not to be.

### 2. HAL plumbing was stripped before it was read

Some CATS responses carry the id of the record they reference only inside
`_links` or `_embedded`. The response shaper dropped both unconditionally as
noise, which for those rows removed the last path back to the record.

Rows came back identifying nobody. That made the endpoint look like it could only
be resolved one item at a time, so a consumer opened all 299 memberships
individually. It took forty minutes and 299 requests against a 500/hour budget.
The endpoint supports `per_page=100`. The real cost is three requests.

The reasoning was correct. The data it reasoned from was wrong.

### 3. A sub-collection row is not its parent

`list_candidate_attachments` is tagged `resource="candidate"`, because it belongs
to the candidate toolset. Each row it returns is an attachment, with an
attachment's id.

The UI-link builder combined `spec.resource` with `item["id"]` and produced
`...candidateID=<attachment id>`. That is a working link to an unrelated real
person.

29 of the 37 link-emitting tools had this shape: attachments, tags, work history,
pipelines, activities, job statuses, custom field definitions. It never reached
production only because the UI domain happened to be unset at the time.

### 4. The id you want is often in the request, not the response

For every sub-collection route, the parent id is the one you passed in the path.
`/candidates/{candidate_id}/tags` already tells you the candidate. Reading it back
out of a row is both unnecessary and wrong.

## The approach

Three rules, each enforced by a test.

### Rule 1: classify rows by what they are, not by what they belong to

`resource` on a `ToolSpec` says which toolset a tool belongs to. It does not say
what shape its rows have. Where those differ, the rows get their own resource:

```python
SUMMARY_FIELDS = {
    "candidate":   ["id", "first_name", "last_name", ...],
    "list_item":   ["id", "candidate_id", "job_id", "date_created"],
    "record_list": ["id", "name", "description", "total", "date_created"],
}
```

`list_item` keeps `candidate_id` because that is the field the row exists to
carry.

See `src/cats_mcp/responses/shaping.py`.

### Rule 2: harvest ids out of HAL before discarding it

`_harvest_related_ids()` runs before `_strip_hal()`. It reads `_links` hrefs and
`_embedded` records, skips navigation relations (`self`, `next`, `prev`, `first`,
`last`) because those point at pages rather than records, and never overwrites a
real top-level field.

```python
def _strip_hal(item):
    recovered = _harvest_related_ids(item)
    kept = {k: v for k, v in item.items() if k not in _HAL_KEYS}
    kept.update(recovered)
    return kept
```

The live account turned out to return `candidate_id` as a flat top-level field,
so this path is not what rescues the DNC list. It is kept because it costs
nothing and covers the shapes that are not flat.

### Rule 3: only link an id that is genuinely that record's id

`endpoint_returns_the_record()` decides from the endpoint rather than the tag.
Strip a trailing id placeholder and a trailing search verb; what remains must be
the resource's own collection root.

| Endpoint | Reduces to | Links? |
|---|---|---|
| `/candidates` | `/candidates` | yes |
| `/candidates/{candidate_id}` | `/candidates` | yes |
| `/candidates/search` | `/candidates` | yes |
| `/candidates/{candidate_id}/attachments` | no | no |
| `/candidates/custom_fields` | no | no |
| `/jobs/statuses` | no | no |

Full detail in [LINKS.md](LINKS.md).

## Trade-offs

**Suppressing 29 links loses real convenience.** An attachment row could
reasonably link to its parent candidate, and now does not. That is deliberate:
the parent id is in the request path, so the caller already has it, and the cost
of guessing wrong here is a link to a stranger's file.

**Rule 3 is endpoint-shape inference, not a declaration.** A future CATS route
that returns candidates from an unusual path would be silently suppressed rather
than silently wrong. That is the correct direction to fail, but it does mean a
new linking tool needs a deliberate addition to `MAY_EMIT_A_LINK` in
`tests/test_links.py`, which is pinned by name so the omission fails loudly.

**Harvested ids are named by relation, not verified.** `_links.candidate.href`
becomes `candidate_id`. If CATS ever used a relation name that collides with a
real field, the real field wins, but a novel relation would produce a field that
looks native and is inferred.

## Alternatives considered

**Annotate every spec with `rows_are_resource: bool`.** Explicit and unguessable,
but 103 shaped specs would need hand-annotation and a wrong annotation fails
silently in exactly the original way. The endpoint rule derives the same answer
from data already present, and a test asserts the resulting set by name.

**Keep the wrong links and document the trap.** Rejected. The whole reason
`links.py` exists is that consumers were hand-building links and getting them
wrong. Emitting a wrong link from the adapter is that same bug with more
authority behind it.

**Never emit links at all.** Rejected for the opposite reason: consumers then go
back to guessing, and the REST-looking `/candidates/{id}` path they invent does
not exist in CATS.

## Related

- [LINKS.md](LINKS.md) - which tools link, and how the UI domain is derived
- [RESPONSE-SHAPING.md](RESPONSE-SHAPING.md) - how rows are projected
- [howto-check-a-list.md](howto-check-a-list.md) - the DNC workflow this came from
