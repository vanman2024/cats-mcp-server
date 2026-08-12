# CATS-MCP Audit & Gap Report

Step 1 of the redesign. Every claim below was verified against the code or an
authoritative external source on 2026-08-10. Claims that turned out to be wrong
are marked **FALSE**.

---

## 1. Tool count drift — resolved

**Actual registered tool count: 186 live tools.**

Counted by live (non-commented) `@mcp.tool` decorators. A naive grep returns 187
because `toolsets_default.py` contains one commented-out decorator on the dead
`update_job_list` stub (lines 1394–1405), whose own docstring says "This endpoint
does not exist in the CATS API v3 spec."

| Module | Live | Commented |
| --- | ---: | ---: |
| `toolsets_default.py` | 93 | 1 |
| `toolsets_recruiting.py` | 75 | 0 |
| `toolsets_data.py` | 18 | 0 |
| **Total** | **186** | 1 |

This is corroborated independently: extracting every `make_request(...)` call
site yields exactly **186 unique method+endpoint pairs**. Live tools and API
call sites agree exactly.

Per-toolset actual vs. what `server.py` logs at startup:

| Toolset | Actual | Logged | |
| --- | ---: | ---: | --- |
| candidates | 43 | 43 | ok |
| **jobs** | **30** | **33** | **off by 3** |
| pipelines | 12 | 12 | ok |
| context | 3 | 3 | ok |
| tasks | 5 | 5 | ok |
| companies | 30 | 30 | ok |
| contacts | 28 | 28 | ok |
| activities | 6 | 6 | ok |
| portals | 8 | 8 | ok |
| work_history | 3 | 3 | ok |
| tags / webhooks / users / triggers / attachments / backups / events | 18 | 18 | ok |

The entire 189-vs-186 gap is the `jobs` toolset. Every other per-toolset number
is correct.

Where the other numbers come from:

| Source | Claims | Verdict |
| --- | ---: | --- |
| `README.md` line 7, 10, 89 | 228 | **FALSE** — no basis found in code |
| `README.md` line 153 | 163 | **FALSE** — stale |
| `README.md` line 310 | "expanded from 164 to 228" | **FALSE** |
| `server.py` / `server_all_tools.py` | 189 | off by 3 (jobs) |
| `tests/test_all_tools_comprehensive.py` | `TOTAL_EXPECTED_TOOLS = 186` | **CORRECT** — this is the only accurate count in the repo |
| `CATS-INTELLIGENCE-SYSTEM.md` | 163 | stale |

The test file is the single source in the repository that has the right number.
The new registry must make 186 the generated, asserted value.

## 2. `server.py` registers zero tools when imported — CONFIRMED, severe

`load_toolsets()` is called only inside `if __name__ == "__main__"`
(`server.py:293`). Any import-based entrypoint — `fastmcp run server.py`,
`server:mcp`, or a test importing the module — gets a `FastMCP` instance with
**no tools registered at all**.

This is almost certainly why `server_all_tools.py` exists: it registers at module
import time (lines 147–163) as a workaround rather than a fix.

## 3. `CATS_TOOLSETS` is dead config in cloud deploys — CONFIRMED

`fastmcp.json` sets `source.path = "server_all_tools.py"`, and passes
`CATS_TOOLSETS: "${CATS_TOOLSETS:-candidates,jobs,pipelines}"` in `deployment.env`.

`server_all_tools.py` never reads `CATS_TOOLSETS`. It unconditionally registers
all 17 toolsets. The documented configuration is silently ignored in the
deployed path, and the deployed server always exposes all 187 tools.

`fastmcp.json` also declares `transport: http` / port 3000, while
`server_all_tools.py`'s `__main__` block runs `mcp.run(transport="stdio")`. The
`__main__` block is dead code under the cloud runner, which imports the `mcp`
object directly.

## 4. Duplicated, divergent request handling — CONFIRMED

Two `make_request` implementations with materially different behaviour:

| | `server.py:55` | `server_all_tools.py:31` |
| --- | --- | --- |
| Retries | none | 4 attempts, exponential 1/2/4/8s |
| Logging | `logging` module | `print()` |
| Rate limit | logs remaining header only | retries on 429 |
| `Retry-After` | **not honoured** | **not honoured** |
| Connection pooling | none — new `AsyncClient` per call | none — new `AsyncClient` per call |
| Jitter | n/a | none |

Both construct a fresh `httpx.AsyncClient` on every request, so there is no
connection reuse anywhere in the server. Neither reads the `Retry-After` header
that CATS returns. `print()`-based logging in `server_all_tools.py` is the
production path used by `fastmcp.json`.

## 5. `filter_candidates` pagination "fix" was never applied — **FALSE claim**

`ENDPOINT_COVERAGE_REPORT.md` claims at lines 92, 166, 374 and 433 that
pagination was moved into the JSON body for `filter_candidates` and
`filter_jobs`.

The code does not do this. `toolsets_default.py:213`:

```python
raw = await make_request("POST", "/candidates/search",
                         params={"per_page": per_page, "page": page},
                         json_data=payload)
```

`filter_jobs` (`toolsets_default.py:1125`) is identical. Pagination is still in
query params. Whether that is actually wrong needs confirming against live CATS
behaviour — the report asserts it caused 400s, but the report is demonstrably
unreliable, so its diagnosis is not trustworthy either. **Open item: verify
against the live API before "fixing".**

## 6. FastMCP version pins — four sources, three floors

| Source | Pin |
| --- | --- |
| `pyproject.toml:8` | `fastmcp>=3.0.0` |
| `fastmcp.json` | `fastmcp>=3.0.0` |
| `requirements.txt:5` | `fastmcp>=2.0.0` |
| `uv.lock` | resolved against `fastmcp[all]>=2.0.0` |

All four are unpinned floors, so any install resolves to whatever is newest.
See section 9 for what "upgrade to FastMCP 4" actually costs.

## 7. Response summarization covers only 5 of 17 resource types

`response_helpers.py:11` defines `SUMMARY_FIELDS` for `candidates`, `jobs`,
`companies`, `contacts`, `activities` only.

For every other entity type, `selected_fields` resolves to `[]`, the field-
extraction branch at line 55 is skipped, and **full records are returned
unmodified**. The summarization is also opt-out-by-default only where it was
explicitly wired in; most list tools call `make_request` directly.

Additional issues in the helper:

- `total` falls back to `len(items)` (line 72), which silently reports a page
  size as if it were the full result count.
- `per_page` defaults to a hardcoded guess of 25 (line 80).
- Assumes the HAL collection key always equals `entity_type` (line 48).

## 8. Tool descriptions are endpoint docs, not discovery metadata — CONFIRMED

Representative sample, verbatim from `toolsets_default.py`:

- "List all activities for a candidate."
- "List all attachments for a candidate (resume, cover letter, etc)."
- "Get all custom fields for a candidate."
- "Add tags to a candidate (keeps existing tags)."

None state *when an agent should reach for the tool*, which is what BM25 ranks
on. There are no tags on any tool, and no safety, read/write, or destructive
classification anywhere in the codebase.

## 9. FastMCP 4 is a **beta**, and it replaces httpx with httpx2

This is the finding that most affects the plan.

| Package | Latest stable | Prereleases |
| --- | --- | --- |
| `fastmcp` | **3.4.7** | `4.0.0a1`, `4.0.0a2`, `4.0.0b1`, `4.0.0b2` |

There is no stable 4.x. The official install docs confirm it:
`pip install "fastmcp==4.0.0b1"` is required, because plain `pip install fastmcp`
still resolves to 3.x.

The dependency change is verified at the package level:

| | `fastmcp-slim` 3.4.7 | `fastmcp-slim` 4.0.0b2 |
| --- | --- | --- |
| HTTP library | `httpx<1.0,>=0.28.1` | `httpx2>=2.5.0` |

The v3→v4 migration guide confirms the consequence: "FastMCP now uses `httpx2`
exclusively", and "`except httpx.` handlers become dead code". This repository's
entire CATS client and all of its error handling are built on `httpx`.

**Critically, everything this redesign needs already exists in stable 3.4.7.**
Verified by inspecting the published wheels — both 3.4.7 and 4.0.0b2 contain:

```
transforms/search/          <- BM25SearchTransform, RegexSearchTransform
transforms/code_mode.py     <- Code Mode
transforms/tool_transform.py
transforms/visibility.py
middleware/response_limiting/
```

So the raw / search / code discovery profiles, tool transforms, and response
limiting are all available on the stable release. FastMCP 4 is not required to
build the target architecture.

### Decision: pin `fastmcp==4.0.0b2` — and v4 does buy something

An initial recommendation to stay on stable 3.4.7 was **wrong**, and empirical
testing disproved it. Both versions were installed and the current code was
imported under each, snapshotting all 186 tool schemas.

Result:

| | FastMCP 3.0.0 | FastMCP 4.0.0b2 |
| --- | --- | --- |
| Tools registered | 186 | 186 |
| `@mcp.tool()` (with parens) | works | works |
| `ResponseLimitingMiddleware` | present | present |
| Tool schemas identical | — | **182 of 186 differ** |
| Parameters carrying a `description` | 0 of 454 | **454 of 454** |

**FastMCP 4 parses docstrings.** It strips `Args:`/`Returns:` blocks out of the
tool description and lifts each argument's documentation into that parameter's
`description` field in the JSON schema. The only four tools whose schema is
unchanged — `get_me`, `get_site`, `list_job_statuses`,
`list_pipeline_workflows` — are exactly the four that take no arguments.

This matters directly for the discovery design: **BM25 indexes tool names,
descriptions, parameter names, and parameter descriptions.** Under v3 all
argument documentation is trapped inside one prose blob. Under v4 it is
structured per-parameter and independently indexable. v4 improves search
quality across all 454 parameters for free.

**Decision:** pin `fastmcp==4.0.0b2` exactly, accepting beta risk. Isolate all
HTTP behind one client module.

Two practical traps found while installing:

1. **v4 does not install `httpx` at all** — only `httpx2` 2.10.0. Every
   `import httpx` in this repo fails outright under v4. This is a hard
   migration, not an optional one.
2. **Do not use a blanket `--prerelease=allow`.** Doing so let `httpx` resolve
   to `1.0.dev3`. Pin exact versions for every dependency instead, so only
   `fastmcp` itself resolves to a prerelease. Horizon's uv-based build will need
   the same treatment.

## 10. Other confirmed issues

- **No authentication on the HTTP endpoint.** `fastmcp.json` deploys over HTTP
  on `0.0.0.0:3000` with no auth configured. All 187 tools, including
  destructive ones, are reachable by anyone who can hit the URL.
- **Single global credential.** `CATS_API_KEY` is read once at module import in
  both servers. One CATS account per deployment; no request-scoped resolution.
- **`health_check` likely broken** (`server.py:117`): passes a `dict` as
  `content=` to a Starlette `Response`, which expects `str`/`bytes`. Needs a
  runtime check.
- **`register_all_recruiting_toolsets`** registers 0 tools — it appears to be a
  dead aggregator.
- **`archive/old_servers/`** contains four more server variants, compounding the
  "which file is real" problem.

## 11. CATS API correctness — endpoints that are wrong or missing

Every `make_request` call site was extracted and diffed against the documented
CATS API v3 surface.

### 11a. Tools calling endpoints that do not exist — 2 confirmed

| Tool | Calls | Problem |
| --- | --- | --- |
| `get_me` (`toolsets_default.py:1811`) | `GET /users/current` | **Endpoint does not exist. Returns 404.** CATS v3 has no whoami endpoint. |
| `authorize_user` (`toolsets_default.py:1831`) | `POST /authorization` | Not in the CATS v3 spec. Only `POST /candidates/authorization` exists, which this is not. |

Both docstrings openly admit the endpoint may not exist. `get_me` even claims it
"falls back to user info from the authentication context" — **there is no
fallback in the code**; it makes the call and fails.

This matters beyond the two broken tools: the target architecture pins
"connection status and current-user/context inspection" as always-visible tools
in the `search` profile. **The current-user tool is one of the two that is
permanently broken.** `GET /site` works and is the correct substitute.

A third tool, `update_job_list`, is commented out for the same reason
(`PUT /jobs/lists/{id}` does not exist) — that one was handled correctly.

### 11b. Documented endpoints with no tool — 12 confirmed gaps

| Missing endpoint | Notes |
| --- | --- |
| `GET /candidates/{id}/applications` | **Required by the redesign spec** as a candidate-detail tool ("candidate application history"). Not implemented. |
| `GET /candidates/{id}/tasks` | `jobs` has `/jobs/{id}/tasks`; candidates does not. |
| `GET /contacts/{id}/tasks` | Same asymmetry. |
| `GET /candidates/custom_fields` | Custom field *definitions*. Only per-record values are implemented. |
| `GET /candidates/custom_fields/{id}` | Definition by id. |
| `GET /jobs/custom_fields` | Definitions. |
| `GET /jobs/custom_fields/{id}` | Definition by id. |
| `GET /candidates/{id}/emails/{email_id}` | Has list/create/update/delete, missing the single GET. |
| `GET /candidates/{id}/phones/{phone_id}` | Companies has `GET /companies/{id}/phones/{id}`; candidates does not. |
| `POST /jobs/{id}/attachments` | Upload. Only GET is implemented. |
| `POST /jobs/{id}/tags` | Replace-all-tags. `PUT` (attach) and `DELETE` exist. |
| `PUT /companies/{id}/custom_fields/{id}`, `PUT /contacts/{id}/custom_fields/{id}` | Update custom field value. Candidates and jobs have `PUT`; companies and contacts are read-only. |

Custom-field *definition* endpoints being absent is the most consequential gap:
without them an agent cannot discover which custom fields exist or what their
IDs are, so it cannot use the per-record value endpoints that are implemented.

### 11c. Verified correct

- `GET /*/search` correctly uses `params={"query": ...}`. The documented trap
  here is that `q=` silently returns every record instead of erroring; commit
  `b5be427` fixed this and the fix is present in all five search tools.
- Pipelines coverage is complete: all 12 documented endpoints implemented.
- Work history, portals, tags, triggers, users, webhooks, backups, events,
  attachments, tasks: all complete against the documented surface.

### 11d. Pagination on `POST /*/search` — the report's claim is unsupported

`ENDPOINT_COVERAGE_REPORT.md` asserts pagination must move into the JSON body.
The documented CATS collection response contradicts this — its own `next` link
is a query string:

```json
"_links": { "next": {"href": "...?page=2&per_page=25"} }
```

That is the API telling the client how to paginate, in query params. The
existing code already does this. **Recommendation: leave as-is and verify
against the live API before changing anything.** The report is unreliable and
its diagnosis should not be acted on without evidence.

---

## Summary of what is actually true

| Claim under investigation | Verdict |
| --- | --- |
| README says 228 tools | **FALSE** — actual is 187 |
| Servers reference ~189 | Nearly right; `jobs` is 31 not 33 |
| Tests expect ≥186 | True, and passes only due to `>=` |
| Intelligence doc says 163 | Stale |
| Per-toolset logs differ from README | True |
| `fastmcp.json` ignores `CATS_TOOLSETS` | **CONFIRMED** |
| `filter_candidates` pagination in query params | **CONFIRMED** — the report's "fixed" claim is false |
| Inconsistent FastMCP versions | **CONFIRMED** — 4 sources, 3 floors |
| FastMCP 4 is current | **Beta only**; stable is 3.4.7 |
