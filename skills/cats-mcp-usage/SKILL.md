---
name: cats-mcp-usage
description: How a recruiting agent should drive the CATS adapter - the tool sequence for sourcing, screening, reviewing, logging and submitting, what each step costs, and the traps that return confident wrong answers.
---

# Driving CATS as a recruiting agent

This server reports what is true in CATS and acts on CATS. **It holds none of
your recruiting rules.** Who counts as unavailable, what makes someone a fit,
who to approach and when: those are yours, and nothing here will decide them for
you.

What follows is the mechanics: for each piece of recruiting work, the cheapest
correct sequence of calls, and the specific ways this API returns a confident
wrong answer.

Requests are the scarce resource. 500/hour is the CATS standard. Read
`cats://account/rate-limit` for this connection's real ceiling.

---

## What a recruiter is actually asking

| The question behind it | Start with | Costs | Needs a model? |
| --- | --- | --- | --- |
| "Is this person already in CATS, maybe more than once?" | `lookup_candidate` | one batched call for the whole set | no |
| "Who have we got for this job?" | `filter_candidates` / `search_candidates` | one call per criterion | no |
| "Is this person off-limits?" | `get_candidate_context` include=`lists` | one pass per list | **no - but the rule is yours** |
| "What actually happened with this person?" | `get_candidate_timeline` | one call per person | no |
| "Have we talked to them before?" | `get_candidate_engagement` | one call per person | no |
| "Who has gone quiet?" | `get_candidate_engagement` | one call per person | no |
| "Are they any good for this?" | `find_candidate_resume`, work history | one call per person | **yes** |
| "Where are they in the process?" | `get_candidate_context` include=`pipelines` | one call per person | no |
| "Which job is 'the Artemis one'?" | `resolve_job` | one batched call | no |
| "Who's overdue for follow-up with nothing since?" | `find_followup_facts` | caller sets the threshold | no |
| "I just spoke to them" | `create_candidate_activity` | one call | no |
| "Put them forward" | `create_pipeline`, `change_pipeline_status` | two calls | no |
| "What changed since yesterday?" | `get_changed_records` | one call | no |

**The last column is the one worth designing around.** Most recruiting work
against CATS is deterministic: given the inputs there is exactly one right
answer, and no reasoning is involved. Those operations can fire from a schedule,
a webhook or a button. They do not need an agent and they will be faster,
cheaper and more reliable without one.

Two rows are different:

- **"Is this person off-limits?"** The lookup is mechanical - this server tells
  you which lists someone is on and what stage their applications are at. What
  those facts *mean* is a rule that lives in your orchestrator, not here. That
  rule can be a plain conditional; it does not need a model either, it just
  needs to be written down somewhere that is not this server.
- **"Are they any good for this?"** Reading a resume against a role is genuine
  judgment. This is where a model earns its cost.

So the shape that works: **automate the retrieval, keep the model for the
reading.** An agent that spends its turns paginating a search is spending them
on the part that never needed it.

---

## Sourcing for a role

**Goal: a set of candidate ids worth looking at, without spending a request per
person.**

```
1. filter_candidates(field, "exactly", value, per_page=100)     one call per criterion
   or search_candidates(query, per_page=100)                    free-text across the profile
2. read `total` before you believe you have seen everything
3. follow `next_page` until has_more is false
4. deduplicate by candidate id across queries
```

`per_page` defaults to **25**. Leaving it there is the single most common reason
a search looks thin: a free-text search for "mechanic" on a mid-size account
returns `total: 3336`.

**The filter trap.** `contains` tokenizes the value and matches any token.
Filtering `city` with `contains: "Logan Lake"` also returns Williams Lake, Slave
Lake and Deer Lake. `contains: "Cache Creek"` returns every Creek. Nothing
errors; the extra people simply look plausible. Use `exactly` for any multi-word
value, one filter per value.

**Commuter belt.** One city excludes the towns people travel from. Decide the
list of municipalities first and run one exact filter per town. A `geo_distance`
filter is listed in the API but the field it applies to is unconfirmed, and it
returns 400 on both `city` and `postal_code`.

**If a qualification decides the match**, pass `summary_level="standard"` on the
search itself. That includes custom fields - certifications, trade
qualifications - with the list. Discovering afterwards that you need one costs a
request per person.

---

## Checking a name against CATS, for a whole batch at once

**Goal: know whether each of N people you're looking at (a LinkedIn list, an
inbound batch) is already in CATS, without one search per person.**

This is the question asked constantly and answered expensively before
`lookup_candidate` existed: "is this person already in here, and under what
name/email/phone." The old way was one `/candidates/search` request *per field
per person* - five people with a name and an email each is ten requests, and
each response still has to be re-checked by eye because CATS's `exactly` only
matches the stored spelling.

```
lookup_candidate(
    names          = ["Pat Lee", "Jordan Smith", ...],
    emails         = ["pat@example.com", ...],
    profile_urls   = ["linkedin.com/in/jordan-smith-42", ...],
)
```

One call, whatever the batch size (bounded by `max_requests`). It probes every
value given, then re-checks every row it gets back by normalized equality - an
email lowercased and trimmed, a phone reduced to its last ten digits, a name or
URL with case and punctuation folded - so a `(250) 555 0111` on file still
matches a `250-555-0111` you were asked to check. Every result reports
`matched_fields` and the evidence on both sides; it does not decide which
record to keep or whether two rows are the same human being.

**Do not call it once per candidate.** Pass the whole set's names/emails/URLs
in the arrays on a single call. That is the entire fix for "one lookup per
person, every turn" - it is not something a client has to parallelize, because
the tool already batches it server-side in one round trip.

---

## Getting one person's whole history in one call

**Goal: everything that happened with this candidate, in order, without four
separate reads.**

CATS keeps a candidate's history in five places that nothing joins: activities,
pipelines, per-pipeline stage history, tasks, and the record's own
`date_modified`. Reading them separately for one person you're vetting is four
or five calls; doing that for a batch is the fastest way to burn the hourly
budget.

```
get_candidate_timeline(candidate_ids=[...], date_from=..., date_to=...)
```

Returns one ordered, source-labelled table per candidate - every row says which
collection it came from (`source`) and which field its date was read from
(`date_field`), so it stays auditable. A row with an unparseable date is
returned last with a null date rather than dropped. Use this instead of
`get_candidate_engagement` + `get_candidate_activity` + a pipeline read when
you need the actual narrative, not just "last contacted when."

---

## Finding the right job when the name isn't the title

**Goal: resolve "the Artemis job" to every record it could mean, not just the
first title match.**

```
resolve_job(query="Artemis")
```

Looks in title, company, location, owner, description, custom fields and tags -
not just the title `search_jobs`/`filter_jobs` are limited to. It never
collapses several plausible jobs into one: three Artemis jobs come back as
three rows with `matched_fields` showing which field named each. Deciding which
one is meant is the caller's call: silently picking the first title match is
how work lands on the wrong req.

---

## Finding who's overdue with nothing since

**Goal: candidates sitting in a stage past a threshold you choose, with no
later activity.**

```
find_followup_facts(
    job_ids=[...],
    pipeline_status_ids=[...],
    older_than_hours=24,
    require_no_later_activity=true,
)
```

There is no built-in idea of what "overdue" means - `older_than_hours` and
`anchor` (`stage_entered` / `activity` / `pipeline_modified`) are always
supplied by the caller. Every returned row carries the stage, the timestamp,
which field it came from, and the elapsed hours, so the comparison can be
redone by hand. A record the request budget never reached comes back in
`unevaluated`, never silently reported as passing.

---

## Screening a set before you spend on them

**Goal: shrink the set using facts, before any per-person request.**

```
get_candidate_context(
    candidate_ids = [ ...everything from sourcing... ],
    include       = ["lists"],
    list_ids      = [ ...the lists your rules care about... ]
)
```

This is the step that changes the economics, and it is the step that gets
skipped.

CATS has **no candidate-to-lists lookup**. It answers only "who is on this
list", so membership is resolved by sweeping each list once and inverting it.
The cost is one pass **per list**, not per person: several hundred candidates
checked against a 299-member list in about three requests.

The result reports which lists each person is on. **It does not say whether that
excludes them** - you decide that. `list_candidate_lists` gives you the ids;
which ones matter is your policy, and passing them in `list_ids` is how that
policy reaches the call without living in this server.

Add `include=["pipelines"]` when current application stage is part of your
screen. That costs one request per candidate, so do it on the survivors, not on
the whole set. The tool refuses per-candidate data on large sets for that reason
and the error names the cheaper path.

Then discard whoever your rules exclude, and carry only the survivors forward.

---

## Reviewing one person properly

**Goal: enough evidence to make a call, without pulling a whole profile into
context.**

```
find_candidate_resume(candidate_id)        the document itself, readable
list_candidate_work_history(candidate_id)  employers, titles, dates
get_candidate(candidate_id, summary_level="standard")   includes custom fields
```

The resume is returned as a document you can read, not raw bytes and not a link.
`is_resume` on an attachment row is the reliable signal for which file it is;
filenames are not.

Prefer the resume over a custom field when they disagree. A custom field records
what somebody typed into CATS once; the resume is the source document.

---

## Checking whether you have approached someone before

**Goal: know the contact history before making contact.**

```
get_candidate_engagement(candidate_ids)    when each was last contacted, and how much
get_candidate_activity(candidate_ids, lookback_days=365)   the actual entries
```

Use engagement for "who has gone cold" across a batch - it returns a date and a
count per person rather than the entries. Use activity when you need to read
what actually happened.

Activity is unbounded and grows forever, so it is never included in a list
result at any summary level. Always bound it by time.

---

## Recording that contact happened

**This server sends nothing.** No email, no SMS, no scheduling. Delivery is your
system's job; this writes the record afterwards.

```
create_candidate_activity(candidate_id, type, notes)
```

Keep it faithful to what actually happened. An activity logged for an
interaction that did not occur will be read later as genuine contact history, by
you or by whoever picks the person up next.

---

## Moving someone through a job

```
1. cats://reference/workflows            status ids, per account, per workflow
2. create_pipeline(candidate_id, job_id) if they are not on the job yet
3. change_pipeline_status(pipeline_id, status_id)
```

**Status ids cannot be guessed or reused from another account.** A
wrong-but-valid id does not error. It silently moves the record to a different
stage.

`status_id` on a pipeline is its **current** stage only. Someone who passed
through Placed and later fell out is no longer Placed, and the current status
will not tell you they ever were. `get_pipeline_statuses` has the history.

One candidate holds one pipeline per job. **Counting pipelines is not counting
people** - deduplicate by `candidate_id` before reporting any number.

---

## Adding someone to a list, and knowing it stuck

```
create_candidate_list_items(list_id, candidate_ids)   -> reports `verified`
```

A 2xx from CATS means the request was accepted, not that the record now says
what you intended. These writes read the list back. If `verified` is false, read
`verification` and `missing` before assuming either outcome.

---

## Staying in sync

```
get_changed_records(since)
```

Cheaper than re-listing whole record sets, and the only sane way to keep an
external system current inside an hourly budget.

---

# The trap that produces wrong answers

**An id in a response is often not the id of the record you think you have.**
Both are real ids on the account, so using the wrong one does not error. It
resolves to a different person.

| You have | It identifies | The record is in |
| --- | --- | --- |
| a saved-list row's `id` | the membership row | `candidate_id` |
| a pipeline row's `id` | the application | `candidate_id`, `job_id` |
| an attachment / email / phone row's `id` | that item | the id you passed in the path |

For any sub-collection - `/candidates/{id}/attachments`, `/emails`,
`/work_history` - **the parent id is the one you sent in the request**, never
one read back out of a row.

On a Do Not Contact list this is the difference between a clean check and
contacting somebody who asked you to stop.

---

# Reference

**Cost.** Identity, pipelines, work history, activity and resumes cost one
request per candidate. Saved-list membership costs one pass per list, whatever
the number of people.

**Pagination.** Every collection tool takes `page` and `per_page`. Read `total`;
follow `next_page` until `has_more` is false. Three endpoints misbehave:
`list_events` and `list_triggers` report `total: null` with no next link, so a
full page may be truncated; `list_webhooks` claims `has_more` on a complete set
and repeats itself on page 2.

**Shaping.** `summary_level` is `compact` (a few fields), `standard` (whole
record including custom fields) or `full`. `fields` takes a projection on list
tools and is ignored on single-record tools.

**Account-specific ids** that must be looked up, never assumed: pipeline status
ids (`cats://reference/workflows`), custom field ids
(`cats://reference/custom-fields/candidates`), saved list ids
(`list_candidate_lists`).

**Writes.** Creates answer with an empty 201; the new id arrives as `created_id`
when CATS sends a Location header, and a new candidate is not immediately
matchable by filter. Email and phone are **not** set by `create_candidate` or
`update_candidate` - CATS keeps them as sub-resources and a flat field in the
body is silently discarded. Use `create_candidate_email` and
`create_candidate_phone`. CATS also normalizes some values on write, so do not
verify a write by exact string comparison on a name.

**Diagnostics.** `get_connection_status` reports configuration and remaining
budget without calling CATS, which separates a configuration problem from a data
problem in one call.

Deeper structure - how records relate, lists versus tags versus custom fields,
HAL - is in `cats-data-model.md`.
