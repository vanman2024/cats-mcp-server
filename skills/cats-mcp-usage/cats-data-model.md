# CATS data model

Supporting reference for `cats-mcp-usage`. Fetched only when needed, so it can
afford to be thorough.

## How records relate

```
company
  |
  +-- contact            a person at the company
  +-- job                a role being hired for
        |
        +-- pipeline     one candidate's application to one job
              |
              +-- status  the stage it is currently at
```

```
candidate
  +-- emails            sub-collection, own ids
  +-- phones            sub-collection, own ids
  +-- attachments       sub-collection, own ids; one may be flagged is_resume
  +-- work_history      sub-collection, own ids
  +-- activities        sub-collection, own ids; unbounded, grows forever
  +-- custom_fields     account-defined values
  +-- tags              labels
  +-- pipelines         applications to jobs
```

A candidate is **not** contained by a job. The pipeline is the join between
them, and it carries the stage.

## Three things that look alike and are not

**Lists, tags and custom fields** all attach extra meaning to a candidate and
work completely differently.

| | What it is | Membership answered by |
| --- | --- | --- |
| **saved list** | a named collection of records | sweeping the list; there is no reverse lookup |
| **tag** | a label on the record | reading the record's own tags |
| **custom field** | an account-defined value on the record | reading the record |

A "Do Not Contact" list is a saved list, not a tag, which is why checking it
means fetching the list rather than reading the person.

## Pipelines and status

`status_id` on a pipeline is its **current** stage only. A candidate who passed
through Placed and later fell out is no longer Placed, and the current status
alone will not tell you they ever were. Read the history with
`get_pipeline_statuses` if "did they ever reach X" is the question.

One candidate can hold several pipelines, one per job. **Counting pipelines is
not counting people.** Deduplicate by `candidate_id` before reporting a number
of candidates.

Status ids are defined per workflow and per account. `cats://reference/workflows`
lists every workflow with the id and title of each stage.

## Custom fields

Two different things share the name:

- **definitions** - `/candidates/custom_fields`. The account's schema: what
  fields exist, their ids, their types. Changes when an administrator edits a
  setting.
- **values** - `/candidates/{id}/custom_fields`. One person's data.

You need a definition's id before you can read or write a value, and those ids
differ per account. For dropdown and checkbox fields, read the definition to see
which values are accepted; writing outside the configured options is rejected.

## Attachments and resumes

An attachment row carries `filename`, `content_type`, `date_created` and
`is_resume`. More than one attachment may exist and more than one may look like
a resume; `is_resume` is the reliable signal.

`download_attachment` returns the document as content a model can read, not raw
bytes and not a link.

## HAL

Collection responses follow HAL:

```json
{
  "count": 25,
  "total": 106,
  "_links": {"next": {"href": ".../jobs?page=2&per_page=25"}},
  "_embedded": {"jobs": [...]}
}
```

`count` is this page. `total` is the whole result set, and is sometimes null.
The presence of `_links.next` is what says there is more; do not compute it from
`page * per_page`.

This adapter strips HAL plumbing before returning rows, but harvests any record
ids out of it first, because some rows reference their record only through
`_links`.

## Dates

RFC 3339, e.g. `2015-12-27T09:14:22-00:00`. Note that responses mix offsets:
`date_created` on one endpoint may be `+00:00` and `-05:00` on another for the
same moment. Compare instants, not strings.
