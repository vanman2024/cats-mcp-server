# How to check whether people are on a saved list

You'll resolve every member of a CATS saved list (a Do Not Contact list, a
follow-up list, a talent pool) and check candidates against it. A 299-member list
takes **three requests**.

The naive approach takes 299 requests and forty minutes. The difference is one
parameter and one field name.

## Prerequisites

- A working connection. Check with `get_connection_status`.
- Enough request budget. The CATS standard is 500 requests/hour; read the real
  ceiling from `cats://account/rate-limit`.

## Steps

### 1. Find the list id

```json
list_candidate_lists {"per_page": 100}
```

```json
{
  "items": [
    {"id": 1600001, "name": "Do Not Contact", "date_created": "2023-05-18T17:43:25+00:00"},
    {"id": 1600002, "name": "Apprentices", "date_created": "2023-05-18T17:43:25+00:00"}
  ],
  "count": 34,
  "total": 34,
  "has_more": false
}
```

Match by name and take the `id`. List ids are account-specific.

### 2. Pull the memberships, 100 at a time

```json
list_candidate_list_items {"list_id": 1600001, "per_page": 100, "fields": "candidate_id"}
```

```json
{
  "items": [
    {"candidate_id": 400000002, "id": 390000001},
    {"candidate_id": 400000003, "id": 390000004}
  ],
  "count": 100,
  "total": 299,
  "has_more": true,
  "next_page": 2
}
```

Repeat with `"page": 2` and `"page": 3`. `has_more` tells you when to stop.

**`per_page` defaults to 25.** Leaving it at the default triples your request
count for no reason.

### 3. Compare on `candidate_id`, never on `id`

```
member_ids = {row["candidate_id"] for row in all_rows}
is_blocked = candidate_id in member_ids
```

> **This is the step that goes wrong.** Each row's `id` is the membership row, not
> the person. Both are real ids on the account, so comparing against `id` does not
> error. It just never matches, and reports the list as clear.
>
> On a Do Not Contact list, that means contacting someone who asked not to be.

See [RECORD-IDENTITY.md](RECORD-IDENTITY.md) for why.

## Verification

Two checks that the result is real rather than empty-by-accident:

1. `total` from step 2 matches the member count shown in the CATS UI.
2. The number of ids you collected equals `total`. If it is short, you stopped
   paginating early.

If every candidate you test comes back "not on the list", check you are reading
`candidate_id` and not `id` before you trust it.

## Troubleshooting

**Every row looks like a candidate but nothing matches.**
You are comparing against `id`. Use `candidate_id`.

**Rows come back with only an `id` and a date.**
The response is being over-projected. Drop `fields` and retry, or pass
`"summary_level": "standard"`. Also check the server version with
`get_connection_status` (0.2.0 or later).

**It is taking hundreds of calls.**
You are calling `get_candidate_list_item` per row. You do not need to. That tool
exists for reading a single known membership; the list route returns the same
data in bulk.

**You need names, not just ids.**
Resolve them in bulk with `get_candidate_summaries`, not one `get_candidate` per
person.

## Why this document exists

A Do Not Contact check on a 299-member list ran for forty minutes and made 299
requests. The consumer concluded the endpoint was "inherently one-item-at-a-time".

It was not. The adapter was projecting membership rows through the *candidate*
summary, which dropped `candidate_id` and left the row id sitting in `id`. The
rows genuinely could not identify anyone, so opening each one individually was
the only remaining option. Correct reasoning, corrupted input.

The fix shipped in 0.2.0. The endpoint always supported `per_page=100`.

## Related

- [RECORD-IDENTITY.md](RECORD-IDENTITY.md) - the underlying trap
- [RESPONSE-SHAPING.md](RESPONSE-SHAPING.md) - `fields` and `summary_level`
