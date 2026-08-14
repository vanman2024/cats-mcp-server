# Response shaping

A single CATS candidate record carries custom fields, HAL plumbing and embedded
sub-objects. A 25-record page of them will fill a context window on its own. So
list and search results are compact by default, and detail is reached
deliberately.

Every shaped tool accepts two optional parameters: `summary_level` and `fields`.

## summary_level

| Level | Lists | Detail | Contents |
|---|---|---|---|
| `compact` | default | opt-in | The identifying fields for that resource, plus `id`. |
| `standard` | opt-in | default | The whole record minus unbounded sub-collections. **Includes custom fields.** |
| `full` | opt-in | opt-in | The whole record, HAL stripped. |

An unrecognised value falls back to `compact` rather than erroring.

```json
{"summary_level": "standard"}
```

**`standard` is the one to reach for when account-specific data decides the
match.** Certifications, trade qualifications and availability live in custom
fields. Without this, answering "who has a Red Seal" costs one request per
candidate. At 500 requests/hour that is the difference between one call and
fifty.

## fields

A comma-separated projection. Overrides `summary_level`.

```json
{"fields": "id,first_name,city,custom_fields"}
```

`id` is always included even if omitted, because an id is what makes a follow-up
lookup possible.

`fields: "all"` is honoured as a synonym for `summary_level: "full"`. It was
accepted by the pre-refactor tools, and silently returning a compact record to a
caller who asked for everything would be worse than not supporting it.

## Compact projections per resource

```python
candidate    id, first_name, last_name, title, city, state, is_hot, date_modified
job          id, title, status_id, city, state, company_id, date_modified
company      id, name, city, state, phone, date_modified
contact      id, first_name, last_name, title, company_id, date_modified
activity     id, type, notes, date_created, regarding_id
pipeline     id, candidate_id, job_id, status_id, rating, date_modified
task         id, title, due_date, is_completed
tag          id, title
user         id, first_name, last_name, email_address
attachment   id, filename, content_type, date_created, is_resume
work_history id, company_name, title, start_date, end_date
list_item    id, candidate_id, job_id, date_created
record_list  id, name, description, total, date_created
```

`list_item` is not the record it points at. Its `id` is the membership row; the
person is `candidate_id`. See [RECORD-IDENTITY.md](RECORD-IDENTITY.md).

## Never included in a list

These are excluded at **every** summary level, including `full`:

```
resume, resume_text, attachments, activities, pipelines,
applications, work_history, notes_html
```

Each is unbounded (one resume can be tens of kilobytes) and each has a dedicated
tool. `download_attachment` returns the actual document as an embedded file the
model can read, rather than raw bytes.

`custom_fields` is excluded from `compact` only. It is bounded but noisy, and it
is the field most often needed, so `standard` includes it.

## Collection envelope

Shaped list results are wrapped:

```json
{
  "items": [...],
  "count": 100,
  "total": 299,
  "has_more": true,
  "next_page": 2,
  "summary_level": "compact",
  "note": "Compact view: ..."
}
```

`has_more` and `next_page` are read from HAL's `_links.next` href, not computed
from `page * per_page`. The previous helper guessed a page size of 25 and fell
back to `len(items)` for `total`, which reported a page size as if it were the
full result count.

`note` appears only on compact results and states how to widen them.

### Two endpoints report `total: null`

`list_events` and `list_triggers` return neither `total` nor `_links.next`, so
`has_more` is false and the real size is unknown. Treat a full-looking page from
those two as possibly truncated.

`list_webhooks` does the opposite: it advertises `has_more: true` on a complete
result set, and page 2 returns the same records again. Deduplicate by `id` if you
follow its pagination.

## Pagination

Every collection tool accepts `page` (1-based) and `per_page`. Both are injected
at registration time for the same reason as the shaping parameters, and for a
worse original defect: **34 of 66 collection tools had no `page` parameter**, so
page 2 was unreachable even where CATS advertised it in its own `_links.next`.

`search_candidates` matched 3,336 records on this account and could return 25 of
them. `list_candidate_custom_field_definitions` returned 25 of 41, hiding the
field ids an account screens on.

`build_signature` and `split_arguments` now read one shared parameter list
(`all_params_for`). Using `spec.params` in the request builder while the
signature included injected parameters is how a tool could advertise `page` and
silently drop it on the way out.

## Availability

`summary_level` and `fields` are injected at registration time into every tool
whose response strategy is `SUMMARY` or `DETAIL` (103 tools).

This was a real gap: only 12 of those 103 declared `fields`, and **none** declared
`summary_level`. On 91 tools there was no way to widen a response at all, so an
agent needing custom fields from a list had no option but one request per record.
That is exactly what happened in production.

Both are optional, so the injection cannot break an existing caller.
`tests/test_registry_parity.py` asserts every shaped tool has them and that
neither is ever required.

## Related

- [RECORD-IDENTITY.md](RECORD-IDENTITY.md) - why row classification matters here
- [LINKS.md](LINKS.md) - the `url` field attached during shaping
- [TOOLS.md](TOOLS.md) - the generated tool catalog
