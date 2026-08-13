# Links into the CATS web UI

Candidate and job results carry a `url` field pointing at the record in the CATS
web UI. The adapter emits it so that consumers stop hand-building links and
getting them wrong.

```json
{
  "id": 407813885,
  "first_name": "Dana",
  "url": "https://bigcountryequipmentrepair.catsone.com/index.php?m=candidates&a=show&candidateID=407813885"
}
```

## The URL format

CATS uses query-string routes, not REST-style paths. The module and key names do
not follow from the API's vocabulary, so each is confirmed against a real account
rather than inferred.

| Resource | Format |
|---|---|
| Candidate | `index.php?m=candidates&a=show&candidateID={id}` |
| Job | `index.php?m=joborders&a=show&jobOrderID={id}` |

A job is `m=joborders` with `jobOrderID`, not `jobs` with `jobId`. Company,
contact and pipeline formats are **not implemented**, because they have not been
confirmed. `record_url()` returns `None` for them rather than guessing.

The REST-looking `https://{account}.catsone.com/candidates/{id}` path is the form
consumers kept inventing. It does not exist. Links in that shape look correct in
a spreadsheet and 404 when clicked.

## Which tools emit a link

Exactly eight. A link is only emitted where the row's `id` is genuinely that
record's id.

| Tool | Endpoint |
|---|---|
| `list_candidates` | `/candidates` |
| `get_candidate` | `/candidates/{candidate_id}` |
| `search_candidates` | `/candidates/search` |
| `filter_candidates` | `/candidates/search` |
| `list_jobs` | `/jobs` |
| `get_job` | `/jobs/{job_id}` |
| `search_jobs` | `/jobs/search` |
| `filter_jobs` | `/jobs/search` |

Plus one deliberate exception: **saved-list membership rows** link to the person
they name, built from `candidate_id` and never from the row's own `id`.

29 other tools are tagged `resource="candidate"` or `resource="job"` but return
sub-records (attachments, tags, work history, pipelines, statuses, custom field
definitions). They emit no link. Reasoning in
[RECORD-IDENTITY.md](RECORD-IDENTITY.md).

### The rule

`endpoint_returns_the_record(resource, endpoint)` strips a trailing id
placeholder and a trailing search verb (`search`, `filter`), then requires what
remains to equal the resource's own collection root.

```python
"/candidates"                             -> ["candidates"]                 -> link
"/candidates/{candidate_id}"              -> ["candidates"]                 -> link
"/candidates/search"                      -> ["candidates"]                 -> link
"/candidates/{candidate_id}/attachments"  -> [..., "attachments"]           -> no link
"/candidates/custom_fields"               -> ["candidates","custom_fields"] -> no link
```

The permitted set is pinned **by name** in `MAY_EMIT_A_LINK`
(`tests/test_links.py`). A new tool that starts linking fails that test with its
own name in the message, rather than a count quietly drifting.

## Where the domain comes from

**It is derived, not configured.** `GET /site` returns the account's subdomain,
and which account that resolves to follows from the API key:

```json
{"id": 91508, "mode": "hr", "subdomain": "bigcountryequipmentrepair", "default_company_id": 22668230}
```

So the adapter reads it from the credential already in use:

```
https://{subdomain}.catsone.com
```

This matters beyond saving a setting. A single configured value applies to the
whole process, so the moment two callers bring different CATS accounts, one of
them receives links into the other company's CATS. Those links resolve. Deriving
per credential means the question cannot arise.

### Behaviour

| Property | Behaviour |
|---|---|
| Cache key | SHA-256 of the credential, truncated. Not `account_label`, which defaults to `"default"` and would collide two tenants onto one domain. |
| Cache scope | Process lifetime, per account. |
| Failure | Cached as "no domain". A restricted account does not re-probe on every response. |
| Retries | None (`max_attempts=1`). A link is optional; backing off would add seconds to an answer that already succeeded. |
| Cost | One request per account, and only from a tool that can emit a link. |

`UIDomainResolver.resolve()` never raises. If `/site` is unavailable, no link is
emitted and the tool result is otherwise unchanged.

See `src/cats_mcp/http/site.py`.

## Configuration

`CATS_UI_BASE_URL` is an **override only**. Leave it unset unless you have a
vanity domain or want to skip the one lookup.

```bash
# Not needed. The domain is read from GET /site.
# CATS_UI_BASE_URL=https://your-account.catsone.com
```

When set, it wins and no lookup happens.

## Why a missing link beats a wrong one

Both failure modes are silent to the adapter. Only one is silent to the person
reading the result.

- **No link**: the reader notices immediately and looks the record up.
- **Wrong link**: the reader clicks through to a real record belonging to someone
  else, and nothing anywhere reports a problem.

Every default here resolves toward the first.

## Related

- [RECORD-IDENTITY.md](RECORD-IDENTITY.md) - why the id has to be checked
- [RESPONSE-SHAPING.md](RESPONSE-SHAPING.md) - where `url` is attached
