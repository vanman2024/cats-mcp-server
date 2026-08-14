---
name: cats-api
description: CATS ATS API v3 reference. Use when working with CATS/CatsOne API, recruiting data sync, candidates, jobs, pipelines, or webhook integrations.
---

# CATS ATS API v3 Skill

Complete reference for the CATS (CatsOne) Applicant Tracking System REST API v3.

## Overview

- **Base URL:** `https://api.catsone.com/v3`
- **Authentication:** Token-based header `Authorization: Token <Your API Key>`
- **Content-Type:** `application/json`
- **Specification:** HAL (Hypertext Application Language) with `_links` and `_embedded` objects
- **Rate Limit:** **500** requests/hour is the CATS standard (rolling basis). This
  account is raised to 1,500 — do not assume that ceiling in any code meant to run
  against another CATS account. Read `X-Rate-Limit-Limit` / `X-Rate-Limit-Remaining`
  from responses instead of hardcoding either number.
- **Date Format:** RFC 3339 (e.g., `2015-12-27T09:14:22-00:00`)
- **Country Codes:** ISO 3166 Alpha-2 (e.g., `US`, `CA`)

## Pagination

- `page` - Page number (default: 1)
- `per_page` - Results per page (default: 25, max: 100)

## Filtering (POST /search endpoints)

Filters use boolean operators with field/filter/value structure:

```json
{
  "field": "status_id",
  "filter": "exactly",
  "value": 247
}
```

**Available filters:**

- `contains` - String contains value
- `exactly` - Exact match
- `is_empty` - Field is empty
- `greater_than` - Greater than value
- `less_than` - Less than value
- `between` - Between two values (array)
- `geo_distance` - Geographic distance (postal codes)

**Boolean operators for complex queries:**

```json
{
  "AND": [
    { "field": "status_id", "filter": "exactly", "value": 247 },
    { "field": "is_hot", "filter": "exactly", "value": true }
  ]
}
```

---

## API Endpoints Reference

### Activities

| Method | Endpoint                    | Description         |
| ------ | --------------------------- | ------------------- |
| GET    | `/activities`               | List all activities |
| GET    | `/activities/{id}`          | Get an activity     |
| PUT    | `/activities/{id}`          | Update an activity  |
| DELETE | `/activities/{id}`          | Delete an activity  |
| GET    | `/activities/search?query=` | Search activities   |
| POST   | `/activities/search`        | Filter activities   |

### Attachments

| Method | Endpoint                     | Description                           |
| ------ | ---------------------------- | ------------------------------------- |
| GET    | `/attachments/{id}`          | Get attachment metadata               |
| DELETE | `/attachments/{id}`          | Delete an attachment                  |
| GET    | `/attachments/{id}/download` | Download attachment file              |
| POST   | `/attachments/parse`         | Parse resume (returns candidate data) |

### Backups

| Method | Endpoint        | Description      |
| ------ | --------------- | ---------------- |
| GET    | `/backups`      | List all backups |
| GET    | `/backups/{id}` | Get a backup     |
| POST   | `/backups`      | Create a backup  |

### Candidates

| Method | Endpoint                     | Description                 |
| ------ | ---------------------------- | --------------------------- |
| GET    | `/candidates`                | List all candidates         |
| GET    | `/candidates/{id}`           | Get a candidate             |
| POST   | `/candidates`                | Create a candidate          |
| PUT    | `/candidates/{id}`           | Update a candidate          |
| DELETE | `/candidates/{id}`           | Delete a candidate          |
| POST   | `/candidates/authorization`  | Authorize a candidate       |
| GET    | `/candidates/{id}/pipelines` | List pipelines by candidate |
| GET    | `/candidates/{id}/tasks`     | List candidate tasks        |
| GET    | `/candidates/search?query=`  | Search candidates           |
| POST   | `/candidates/search`         | Filter candidates           |

**Candidate Phones:**

| Method | Endpoint                                       | Description           |
| ------ | ---------------------------------------------- | --------------------- |
| GET    | `/candidates/{id}/phones`                      | List candidate phones |
| GET    | `/candidates/{candidate_id}/phones/{phone_id}` | Get a phone           |
| POST   | `/candidates/{id}/phones`                      | Create phone          |
| PUT    | `/candidates/{candidate_id}/phones/{phone_id}` | Update phone          |
| DELETE | `/candidates/{candidate_id}/phones/{phone_id}` | Delete phone          |

**Candidate Emails:**

| Method | Endpoint                                       | Description           |
| ------ | ---------------------------------------------- | --------------------- |
| GET    | `/candidates/{id}/emails`                      | List candidate emails |
| GET    | `/candidates/{candidate_id}/emails/{email_id}` | Get an email          |
| POST   | `/candidates/{id}/emails`                      | Create email          |
| PUT    | `/candidates/{candidate_id}/emails/{email_id}` | Update email          |
| DELETE | `/candidates/{candidate_id}/emails/{email_id}` | Delete email          |

**Candidate Custom Fields:**

| Method | Endpoint                                                     | Description                        |
| ------ | ------------------------------------------------------------ | ---------------------------------- |
| GET    | `/candidates/custom_fields`                                  | List custom field definitions      |
| GET    | `/candidates/custom_fields/{id}`                             | Get custom field definition        |
| GET    | `/candidates/{id}/custom_fields`                             | List candidate custom field values |
| GET    | `/candidates/{candidate_id}/custom_fields/{custom_field_id}` | Get custom field value             |
| PUT    | `/candidates/{candidate_id}/custom_fields/{custom_field_id}` | Update custom field value          |

**Candidate Activities:**

| Method | Endpoint                      | Description               |
| ------ | ----------------------------- | ------------------------- |
| GET    | `/candidates/{id}/activities` | List candidate activities |
| POST   | `/candidates/{id}/activities` | Create candidate activity |

**Candidate Attachments:**

| Method | Endpoint                                 | Description                |
| ------ | ---------------------------------------- | -------------------------- |
| GET    | `/candidates/{id}/attachments`           | List candidate attachments |
| POST   | `/candidates/{id}/attachments?filename=` | Upload attachment          |
| POST   | `/candidates/{id}/resumes?filename=`     | Upload resume              |

**Candidate Work History:**

| Method | Endpoint                                                    | Description         |
| ------ | ----------------------------------------------------------- | ------------------- |
| GET    | `/candidates/{id}/work_history`                             | List work history   |
| GET    | `/candidates/{candidate_id}/work_history/{work_history_id}` | Get work history    |
| POST   | `/candidates/{id}/work_history`                             | Create work history |
| PUT    | `/candidates/{candidate_id}/work_history/{work_history_id}` | Update work history |
| DELETE | `/candidates/{candidate_id}/work_history/{work_history_id}` | Delete work history |

**Candidate Lists:**

| Method | Endpoint                                      | Description              |
| ------ | --------------------------------------------- | ------------------------ |
| GET    | `/candidates/lists`                           | List all candidate lists |
| GET    | `/candidates/lists/{id}`                      | Get a list               |
| POST   | `/candidates/lists`                           | Create a list            |
| DELETE | `/candidates/lists/{id}`                      | Delete a list            |
| GET    | `/candidates/lists/{id}/items`                | List items in list       |
| GET    | `/candidates/lists/{list_id}/items/{item_id}` | Get list item            |
| POST   | `/candidates/lists/{id}/items`                | Add candidates to list   |
| DELETE | `/candidates/lists/{list_id}/items/{item_id}` | Remove from list         |

**Candidate Applications:**

| Method | Endpoint                        | Description                 |
| ------ | ------------------------------- | --------------------------- |
| GET    | `/candidates/{id}/applications` | List candidate applications |

**Candidate Tags:**

| Method | Endpoint                                   | Description            |
| ------ | ------------------------------------------ | ---------------------- |
| GET    | `/candidates/{id}/tags`                    | List candidate tags    |
| POST   | `/candidates/{id}/tags`                    | Replace all tags       |
| PUT    | `/candidates/{id}/tags`                    | Attach tags (additive) |
| DELETE | `/candidates/{candidate_id}/tags/{tag_id}` | Detach a tag           |

**Candidate Thumbnail:**

| Method | Endpoint                     | Description      |
| ------ | ---------------------------- | ---------------- |
| GET    | `/candidates/{id}/thumbnail` | Get thumbnail    |
| PUT    | `/candidates/{id}/thumbnail` | Change thumbnail |

### Companies

| Method | Endpoint                   | Description        |
| ------ | -------------------------- | ------------------ |
| GET    | `/companies`               | List all companies |
| GET    | `/companies/{id}`          | Get a company      |
| POST   | `/companies`               | Create a company   |
| PUT    | `/companies/{id}`          | Update a company   |
| DELETE | `/companies/{id}`          | Delete a company   |
| GET    | `/companies/search?query=` | Search companies   |
| POST   | `/companies/search`        | Filter companies   |

**Company sub-endpoints follow same pattern as Candidates:**

- Phones, Emails, Custom Fields, Activities, Attachments, Tags
- Plus: Departments (`/companies/{id}/departments`)
- Plus: Thumbnail

### Contacts

| Method | Endpoint                  | Description        |
| ------ | ------------------------- | ------------------ |
| GET    | `/contacts`               | List all contacts  |
| GET    | `/contacts/{id}`          | Get a contact      |
| POST   | `/contacts`               | Create a contact   |
| PUT    | `/contacts/{id}`          | Update a contact   |
| DELETE | `/contacts/{id}`          | Delete a contact   |
| GET    | `/contacts/{id}/tasks`    | List contact tasks |
| GET    | `/contacts/search?query=` | Search contacts    |
| POST   | `/contacts/search`        | Filter contacts    |

**Contact sub-endpoints follow same pattern as Candidates:**

- Phones, Emails, Custom Fields, Activities, Attachments, Tags, Thumbnail

### Events

| Method | Endpoint                            | Description                 |
| ------ | ----------------------------------- | --------------------------- |
| GET    | `/events?starting_after_id=`        | List events after ID        |
| GET    | `/events?starting_after_timestamp=` | List events after timestamp |

Event names include: `candidate.created`, `candidate.status_changed`, `job.created`, `pipeline.status_changed`, etc.

### Jobs

| Method | Endpoint               | Description           |
| ------ | ---------------------- | --------------------- |
| GET    | `/jobs`                | List all jobs         |
| GET    | `/jobs/{id}`           | Get a job             |
| POST   | `/jobs`                | Create a job          |
| PUT    | `/jobs/{id}`           | Update a job          |
| DELETE | `/jobs/{id}`           | Delete a job          |
| GET    | `/jobs/{id}/pipelines` | List pipelines by job |
| GET    | `/jobs/{id}/tasks`     | List job tasks        |
| GET    | `/jobs/search?query=`  | Search jobs           |
| POST   | `/jobs/search`         | Filter jobs           |

**Job Status:**

| Method | Endpoint              | Description       |
| ------ | --------------------- | ----------------- |
| GET    | `/jobs/statuses`      | List job statuses |
| GET    | `/jobs/statuses/{id}` | Get a status      |
| POST   | `/jobs/{id}/status`   | Change job status |

**Job Custom Fields:**

| Method | Endpoint                                         | Description                   |
| ------ | ------------------------------------------------ | ----------------------------- |
| GET    | `/jobs/custom_fields`                            | List custom field definitions |
| GET    | `/jobs/custom_fields/{id}`                       | Get definition                |
| GET    | `/jobs/{id}/custom_fields`                       | List job custom field values  |
| GET    | `/jobs/{job_id}/custom_fields/{custom_field_id}` | Get value                     |
| PUT    | `/jobs/{job_id}/custom_fields/{custom_field_id}` | Update value                  |

**Job Attachments:**

| Method | Endpoint                           | Description          |
| ------ | ---------------------------------- | -------------------- |
| GET    | `/jobs/{id}/attachments`           | List job attachments |
| POST   | `/jobs/{id}/attachments?filename=` | Upload attachment    |

**Job Lists:**

| Method | Endpoint                                | Description        |
| ------ | --------------------------------------- | ------------------ |
| GET    | `/jobs/lists`                           | List all job lists |
| GET    | `/jobs/lists/{id}`                      | Get a list         |
| POST   | `/jobs/lists`                           | Create a list      |
| DELETE | `/jobs/lists/{id}`                      | Delete a list      |
| GET    | `/jobs/lists/{id}/items`                | List items         |
| GET    | `/jobs/lists/{list_id}/items/{item_id}` | Get item           |
| POST   | `/jobs/lists/{id}/items`                | Add jobs to list   |
| DELETE | `/jobs/lists/{list_id}/items/{item_id}` | Remove from list   |

**Job Applications:**

| Method | Endpoint                                     | Description              |
| ------ | -------------------------------------------- | ------------------------ |
| GET    | `/jobs/{job_id}/applications`                | List applications by job |
| GET    | `/jobs/applications/{application_id}`        | Get application          |
| GET    | `/jobs/applications/{application_id}/fields` | List application fields  |

**Job Tags:**

| Method | Endpoint                       | Description      |
| ------ | ------------------------------ | ---------------- |
| GET    | `/jobs/{job_id}/tags`          | List job tags    |
| POST   | `/jobs/{job_id}/tags`          | Replace all tags |
| PUT    | `/jobs/{job_id}/tags`          | Attach tags      |
| DELETE | `/jobs/{job_id}/tags/{tag_id}` | Detach tag       |

### Pipelines

Pipelines represent candidate-to-job relationships (submissions).

| Method | Endpoint            | Description                |
| ------ | ------------------- | -------------------------- |
| GET    | `/pipelines`        | List all pipelines         |
| GET    | `/pipelines/{id}`   | Get a pipeline             |
| POST   | `/pipelines`        | Create a pipeline          |
| PUT    | `/pipelines/{id}`   | Update a pipeline (rating) |
| DELETE | `/pipelines/{id}`   | Delete a pipeline          |
| POST   | `/pipelines/search` | Filter pipelines           |

**Pipeline Status:**

| Method | Endpoint                   | Description             |
| ------ | -------------------------- | ----------------------- |
| GET    | `/pipelines/{id}/statuses` | Get historical statuses |
| POST   | `/pipelines/{id}/status`   | Change pipeline status  |

**Workflows:**

| Method | Endpoint                                                  | Description            |
| ------ | --------------------------------------------------------- | ---------------------- |
| GET    | `/pipelines/workflows`                                    | List workflows         |
| GET    | `/pipelines/workflows/{id}`                               | Get a workflow         |
| GET    | `/pipelines/workflows/{workflow_id}/statuses`             | List workflow statuses |
| GET    | `/pipelines/workflows/{workflow_id}/statuses/{status_id}` | Get workflow status    |

### Portals

| Method | Endpoint                             | Description                  |
| ------ | ------------------------------------ | ---------------------------- |
| GET    | `/portals`                           | List all portals             |
| GET    | `/portals/{id}`                      | Get a portal                 |
| GET    | `/portals/{id}/jobs`                 | List portal jobs             |
| POST   | `/portals/{portal_id}/jobs/{job_id}` | Submit application           |
| PUT    | `/portals/{portal_id}/jobs/{job_id}` | Publish job to portal        |
| DELETE | `/portals/{portal_id}/jobs/{job_id}` | Unpublish job                |
| GET    | `/portals/{id}/registration`         | Get registration application |
| POST   | `/portals/{id}/registration`         | Submit registration          |

### Site

| Method | Endpoint | Description   |
| ------ | -------- | ------------- |
| GET    | `/site`  | Get site info |

### Tags

| Method | Endpoint     | Description   |
| ------ | ------------ | ------------- |
| GET    | `/tags`      | List all tags |
| GET    | `/tags/{id}` | Get a tag     |

### Tasks

| Method | Endpoint      | Description    |
| ------ | ------------- | -------------- |
| GET    | `/tasks`      | List all tasks |
| GET    | `/tasks/{id}` | Get a task     |
| POST   | `/tasks`      | Create a task  |
| PUT    | `/tasks/{id}` | Update a task  |
| DELETE | `/tasks/{id}` | Delete a task  |

### Triggers

| Method | Endpoint         | Description       |
| ------ | ---------------- | ----------------- |
| GET    | `/triggers`      | List all triggers |
| GET    | `/triggers/{id}` | Get a trigger     |

### Users

| Method | Endpoint      | Description    |
| ------ | ------------- | -------------- |
| GET    | `/users`      | List all users |
| GET    | `/users/{id}` | Get a user     |

### Webhooks

| Method | Endpoint         | Description       |
| ------ | ---------------- | ----------------- |
| GET    | `/webhooks`      | List all webhooks |
| GET    | `/webhooks/{id}` | Get a webhook     |
| POST   | `/webhooks`      | Create a webhook  |
| DELETE | `/webhooks/{id}` | Delete a webhook  |

**Available webhook events:**

- `candidate.created`, `candidate.updated`, `candidate.deleted`
- `job.created`, `job.updated`, `job.deleted`, `job.status_changed`
- `contact.created`, `contact.updated`, `contact.deleted`, `contact.status_changed`
- `company.created`, `company.updated`, `company.deleted`, `company.status_changed`
- `activity.created`, `activity.updated`, `activity.deleted`
- `user.created`, `user.updated`, `user.deleted`
- `pipeline.created`, `pipeline.deleted`, `pipeline.status_changed`

### Work History

| Method | Endpoint                          | Description         |
| ------ | --------------------------------- | ------------------- |
| GET    | `/work_history/{work_history_id}` | Get work history    |
| PUT    | `/work_history/{work_history_id}` | Update work history |
| DELETE | `/work_history/{work_history_id}` | Delete work history |

---

## Common Request Examples

### Get all jobs with pagination

```bash
curl -X GET \
  "https://api.catsone.com/v3/jobs?page=1&per_page=100" \
  -H "Authorization: Token YOUR_API_KEY"
```

### Create a pipeline (submit candidate to job)

```bash
curl -X POST \
  "https://api.catsone.com/v3/pipelines" \
  -H "Authorization: Token YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"candidate_id": 1367, "job_id": 3882, "rating": 3}'
```

### Change pipeline status

```bash
curl -X POST \
  "https://api.catsone.com/v3/pipelines/8459/status" \
  -H "Authorization: Token YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status_id": 3537}'
```

### Filter pipelines by job_id

```bash
curl -X POST \
  "https://api.catsone.com/v3/pipelines/search" \
  -H "Authorization: Token YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"field": "job_id", "filter": "exactly", "value": 1198}'
```

### Get candidate custom field value

```bash
curl -X GET \
  "https://api.catsone.com/v3/candidates/1234/custom_fields/357946" \
  -H "Authorization: Token YOUR_API_KEY"
```

### Update candidate custom field

```bash
curl -X PUT \
  "https://api.catsone.com/v3/candidates/1234/custom_fields/357946" \
  -H "Authorization: Token YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"value": "Green Hills"}'
```

### Create a webhook

`POST /webhooks` takes three body fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | array | Event names to subscribe to, e.g. `pipeline.status_changed`, `candidate.created` |
| `target_url` | string | Your HTTPS endpoint. CATS POSTs each event to it |
| `secret` | string | A signing key **you** choose. CATS signs each delivery with it so your endpoint can verify the request came from CATS |

Read the signing key from the environment rather than writing it into a
command, so it does not end up in shell history or a committed file:

```bash
curl -X POST "https://api.catsone.com/v3/webhooks" \
  -H "Authorization: Token ${CATS_API_KEY}" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{
  "events": ["pipeline.status_changed", "candidate.created"],
  "target_url": "https://your-server.example/webhook"
}
JSON
```

Add the signing field to that payload from `${CATS_WEBHOOK_SIGNING_KEY}` before
sending. Treat it like an API key: it is what proves a delivery is genuine.

---

## Response Structure (HAL)

All responses follow HAL specification:

```json
{
  "id": 1621,
  "title": "Software Engineer",
  "status_id": 4635,
  "_links": {
    "self": { "href": "https://api.catsone.com/v3/jobs/1621" },
    "company": { "href": "https://api.catsone.com/v3/companies/8209" }
  },
  "_embedded": {
    "company": {
      "id": 8209,
      "name": "Acme Corp"
    }
  }
}
```

## Collection Response

```json
{
  "count": 25,
  "total": 106,
  "_links": {
    "self": {"href": "...?page=1&per_page=25"},
    "next": {"href": "...?page=2&per_page=25"}
  },
  "_embedded": {
    "jobs": [...]
  }
}
```

---

## Project-Specific IDs (Collars/Big Country)

### Workflows and statuses

| Resource | ID | Notes |
| --- | --- | --- |
| Workflow: General | `5691190` | the default, 29 statuses |
| Workflow: Previous Application | `5691292` | 10 statuses, currently unused |
| Workflow: Engagement & Conversion | `5698347` | 5 statuses, no placed stage |
| Job Workflow | `5691189` | 5 job statuses |

Key statuses in workflow `5691190`:

| Status | ID | Mapping |
| --- | --- | --- |
| New Candidate | `6377094` | applicant |
| Booked meeting | `6378757` | interviewing |
| Submitted to Hiring Manager | `6377098` | submitted |
| Hiring Manager Approved | `6377099` | submitted |
| Hiring Manager Rejected | `6377100` | declined |
| Rejected by Recruiter | `6378760` | declined |
| Wrong Timing | `6437864` | declined |
| Send Offer | `6377102` | offered |
| Offer Accepted | `6378986` | offered |
| **Placed** | **`6377103`** | placed |
| Onboarding | `6447222` | (none) |

In `5691292`, the placed status is **Hired** `6377971`.

### Candidate custom fields

| Field | ID | Type |
| --- | --- | --- |
| Has Red Seal | `351005` | dropdown: Yes / No / In Progress / Not Sure |
| Start Date | `351169` | date |
| Site Locations | `354065` | dropdown |
| Do not Hire | `355884` | checkbox |
| Position Type - Trades | `359361` | checkboxes, 53 Red Seal trades |
| Checked SignalHire | `359939` | dropdown |
| Connection Request Status | `359946` | dropdown |
| LinkedIn Messaging Stage | `359950` | dropdown |
| LinkedIn Outreach Type | `359952` | dropdown |
| Mining Industry Experience Types | `360002` | checkboxes |
| **Working for Our Customer** | **`360003`** | dropdown, the client's customer list |
| Years of Experience | `360004` | dropdown |
| Notes on Qualifications | `360016` | text |
| Interest State | `360022` | dropdown: Interested Now / Open To New Opportunities / Not Interested / Unresponsive |
| Offer Declined Reason | `360805` | dropdown |
| Hiring Manager Summary | `360827` | textarea |
| Withdrew Reason | `360885` | text |
| Follow Up Date | `360976` | date |
| **Invoiced For** | **`361122`** | dropdown: Yes / No / Not Needed |
| Mine Sites | `357946` | |
| Job Order Link | `352718` | |
| Rate Breakdown | `359926` | |

`Working for Our Customer` holds the client's own customers. Contacting anyone at one of
these employers breaches the non-solicitation clause in the CEG client agreement. Current
values: Artemis, B2 Gold, Copper Mountain, EVR, Foran, Greenstone, HVC, Magino, New Afton,
Orano, Osisko Development, Rainy River (New Gold, Coeur), Valentine Gold.

`Invoiced For` is how you find placements that were never billed. Filter placed pipelines
where this is `No`.

### Gotchas found the hard way

- `/users/current` does **not** exist. It 404s. There is no whoami endpoint.
- `GET /candidates/search` needs **`query=`**. Using `q=` or `filter=` silently returns
  every candidate instead of erroring, which looks like a match when it is not.
- `status_id` on a pipeline is the **current** status only. A candidate who moved past
  Placed no longer counts as placed. Use `/pipelines/{id}/statuses` for history.
- One candidate can hold several pipelines. Counting pipelines overstates unique people.
- **Saved-list items are not the records they point at.** `GET /candidates/lists/{id}/items`
  returns membership rows whose `id` is the row, with the person in `candidate_id`.
  Comparing candidate ids against those `id` values silently finds nothing — which on a
  Do Not Contact list means contacting someone who asked not to be. Same for
  `/jobs/lists/{id}/items` and `job_id`. Retrieve a whole list with `per_page=100`
  (299 rows is 3 calls) rather than opening each row.

  Confirmed against a live account — the row shape is flat, not HAL-nested:

  ```json
  {"count": 2, "total": 299,
   "_embedded": {"items": [
     {"id": 390055557, "candidate_id": 397943414, "date_created": "2023-05-27T12:34:36-05:00"}
   ]}}
  ```

  Both ids are valid ids on the account, so using the wrong one does not error — it
  resolves to a different person. There is no failure to notice.
- **A sub-collection row is not the parent record.** `/candidates/{id}/attachments`,
  `/tags`, `/work_history`, `/custom_fields` all return rows with their *own* `id`.
  Building a candidate link or lookup from one gives a working reference to an
  unrelated real person. The parent id is the one in the request path, not in the row.
- The **`contains` filter tokenizes** the value and matches *any* token, not the whole
  string. Filtering `city` with `contains: "Logan Lake"` also returns Williams Lake,
  Slave Lake and Deer Lake; `contains: "Cache Creek"` returns every Creek. Nothing
  errors — the extra results just look plausible. Use `exactly` for any multi-word
  value, one filter per municipality, or `geo_distance` from a postal code for a
  radius. `contains` is only reliable on single words.

### Searching by location

Two mistakes compound: `contains` tokenizing (above), and searching only the target
city, which excludes the surrounding communities people commute from.

The pattern that works:

1. One paginated `POST /candidates/search` per town, `city` matched `exactly` — or a
   single `geo_distance` filter from a postal code, which beats any hand-written list.
2. Return **core fields only**. Requesting everything produces enormous records for
   people you are about to discard.
3. Where certifications or other custom fields decide the match, get them with the
   list rather than fetching each candidate individually.
4. Load the full record, resume or activity history only for someone who already matches.
5. Deduplicate across towns by candidate id — one person appears in several queries.

---

## Rate Limiting Best Practices

- **Limit:** 500 requests/hour standard (rolling); this account is raised to 1,500.
  Budget against 500 unless you have confirmed otherwise for the account in question.
- Read the real budget from `X-Rate-Limit-Limit` / `X-Rate-Limit-Remaining` headers
  rather than assuming — the ceiling varies per account.
- Honour `Retry-After` on 429 rather than using a fixed backoff.
- Cache responses when possible
- Use pagination efficiently (max 100 per page). At 500 req/hr, **fetch wide and
  summarize narrow**: request a large `per_page` and trim fields client-side. Small
  page sizes look context-friendly but multiply request count against a tight budget.
- Batch operations where available
- Use `/events` endpoint to poll for changes instead of repeatedly fetching all records
