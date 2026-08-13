# How to search candidates by location

You'll find people in a town or region without silently matching the wrong
places, and without spending a request per candidate to check their
qualifications.

Two traps make location searches return confident, wrong answers.

## Prerequisites

- The list of municipalities in scope. Decide this before you start.
- The custom field names your account uses for qualifications, from
  `cats://reference/custom-fields/candidates`.

## Trap 1: `contains` tokenizes

`contains` splits the value and matches **any** token, not the whole string.

| Filter | Also returns |
|---|---|
| `contains: "Logan Lake"` | Williams Lake, Slave Lake, Deer Lake |
| `contains: "Cache Creek"` | every Creek |

Nothing errors. The extra results look plausible, which is what makes it
expensive: you notice after acting on them, not before.

**`contains` is reliable only for single words.** For any multi-word value, use
`exactly`.

## Trap 2: one city excludes the commuter belt

People commute. Searching only the posting's city misses the surrounding towns
they would travel from. Establish the list of towns first.

## Steps

### 1. One filter per municipality, matched exactly

```json
filter_candidates {
  "filter_field": "city",
  "filter_type": "exactly",
  "filter_value": "Logan Lake",
  "per_page": 100,
  "summary_level": "standard"
}
```

Repeat per town. Paginate with `"page": 2` while `has_more` is true.

### 2. Pull qualifications with the list, not after it

`"summary_level": "standard"` includes `custom_fields`, where certifications and
trade qualifications live.

Without it you get a compact record, discover it lacks the field you screen on,
and fall back to one `get_candidate` per person. On a 60-candidate result set
against a 500/hour budget, that is the difference between one call and sixty.

Narrow further with `fields` once you know the field names:

```json
{"fields": "id,first_name,last_name,city,phone,custom_fields"}
```

### 3. Deduplicate by candidate id

One person can appear under more than one town query. Deduplicate on `id` before
counting or contacting.

### 4. Load detail only for people who already match

Resumes, attachments and activity history are never included in a list result at
any summary level. Fetch them per person, only after they pass the filter.

## Verification

- Spot-check two or three results: does `city` exactly equal what you asked for?
  If a "Logan Lake" search returned a Williams Lake record, you used `contains`.
- Compare the deduplicated count against the sum of per-town totals. A large gap
  means the towns overlap heavily, which is expected; a gap of zero means
  deduplication is not running.

## Troubleshooting

**Results include towns I did not ask for.**
`filter_type` is `contains`. Switch to `exactly`.

**I need "within 50km" rather than a town list.**
The API's filter list includes `geo_distance`, described as taking postal codes.
**Its field and value format are unconfirmed.** Two attempts against a live
account were both rejected:

```
filter_field="city",        filter_type="geo_distance" -> 400 "That filter cannot be used on this field"
filter_field="postal_code", filter_type="geo_distance" -> 400 "That filter cannot be used on this field"
```

Until the correct field is known, use the explicit town list. It is verified and
it works. If you determine the right field, add it here.

**Searching returns everything.**
`search_candidates` requires `query=`. Passing `q=` or `filter=` returns every
candidate instead of erroring.

**I am only seeing the first 25 results.**
Pass `per_page` and `page`. Every collection tool accepts both. Read `total` to
see the real size of the match: a free-text search for "mechanic" on this
account returns `total: 3336`, so a single default page is under 1% of it.
Follow `next_page` until `has_more` is false.

## Related

- [RESPONSE-SHAPING.md](RESPONSE-SHAPING.md) - `summary_level` and `fields`
- The `search_by_location` MCP prompt carries this guidance to a model directly
