# Proposed Module Structure

Step 2 of the redesign. Depends on the findings in `00-audit-gap-report.md`.

## Guiding constraints

1. **One of everything.** One server factory, one HTTP client, one registry, one
   place tools are registered. The audit found two divergent `make_request`
   implementations and two server modules; that class of bug should become
   structurally impossible.
2. **HTTP isolated to one module.** FastMCP 4 replaces `httpx` with `httpx2`.
   Exactly one file imports the HTTP library so that swap stays a one-file change.
3. **Registration must not depend on `__main__`.** The current `server.py`
   registers zero tools when imported. The factory is called at module scope.
4. **Budget for 500 req/hr.** 500/hour is the CATS standard; this account is
   raised to 1,500. A universal adapter cannot assume the raise.
5. **No StaffHive business logic.** Data-access primitives only.
6. **No paid Horizon tier required.** Service accounts are a paid feature and
   nothing here depends on one.

## Layout

```
src/cats_mcp/
├── __init__.py
├── config.py                 # Settings: discovery mode, limits, base URL
├── server.py                 # create_server() — the only place a FastMCP is built
├── app.py                    # module-level `mcp` object for import entrypoints
│
├── credentials/
│   ├── base.py               # CredentialProvider protocol
│   ├── env.py                # env-backed, single tenant (today)
│   └── request_scoped.py     # per-request resolution from caller identity (stub)
│
├── http/
│   ├── client.py             # CATSClient — ONLY module importing httpx/httpx2
│   ├── errors.py             # CATS error -> ToolError normalization
│   ├── ratelimit.py          # header-driven budget, Retry-After, retry + jitter
│   └── correlation.py        # correlation IDs, structured logging
│
├── registry/
│   ├── models.py             # ToolSpec
│   ├── catalog.py            # assembles all specs, exposes counts
│   ├── build.py              # ToolSpec -> registered FastMCP tool
│   └── specs/
│       ├── candidates.py     jobs.py        pipelines.py
│       ├── companies.py      contacts.py    activities.py
│       ├── portals.py        tasks.py       attachments.py
│       └── admin.py          # tags, users, triggers, webhooks, backups, events, site
│
├── responses/
│   ├── shaping.py            # summary_level, fields, include
│   ├── pagination.py         # API page size decoupled from model page size
│   └── schemas.py            # typed compact models per resource
│
├── composites/
│   └── reads.py              # batch summaries, changed-since, compact job pool
│
├── discovery/
│   └── profiles.py           # raw / search / code wiring
│
└── auth/
    ├── verifier.py           # JWTVerifier / StaticTokenVerifier selection
    └── policies.py           # scope checks derived from safety class
```

### Entrypoints

| Path | Role |
| --- | --- |
| `src/cats_mcp/app.py` | canonical `mcp` object; what `fastmcp.json` points at |
| `server.py` (repo root) | thin backward-compat shim: `from cats_mcp.app import mcp` |
| `server_all_tools.py` | **deleted** |

## The ToolSpec

One declarative record per tool. Everything else is generated from it.

```python
class ToolSpec(BaseModel):
    name: str                      # stable MCP tool name
    resource: str                  # "candidate", "job", ...
    operation: str                 # "list", "get", "create", ...
    method: HTTPMethod
    endpoint: str                  # "/candidates/{candidate_id}/activities"
    description: str               # when-to-use guidance, BM25-indexed
    input_model: type[BaseModel]
    response: ResponseStrategy     # raw | summary | typed(schema)
    tags: frozenset[str]
    safety: Safety                 # READ | WRITE | DESTRUCTIVE | BULK | ADMIN
    required_scopes: frozenset[str]
    deprecated: bool = False
    replaced_by: str | None = None
```

Generated from this, with no second source of truth:

- the registered FastMCP tool and its JSON schema
- tool counts in README, startup logs, and tests
- `auth=` scope checks (derived from `safety`)
- the endpoint-coverage table

The audit found five different tool counts across the repo. Afterwards the count
is a function call, and a test asserts the docs match it.

## Discovery profiles

`CATS_DISCOVERY_MODE=raw|search|code`

| Mode | Wiring | Intended consumer |
| --- | --- | --- |
| `raw` | full authorized catalog, no transform | Mastra / orchestrators doing their own discovery. **Protected — not the public default.** |
| `search` | `BM25SearchTransform(max_results=5, always_visible=[...])` | direct MCP clients (Claude, ChatGPT) |
| `code` | Code Mode transform | direct clients composing multi-step reads. Marked experimental. |

Default is `search`. `raw` requires authentication.

**Pinned tools for `search`:** the spec calls for "connection status and
current-user/context inspection". The audit found `get_me` is permanently broken
(`GET /users/current` returns 404). Pin `get_site` and a new synthetic
`get_connection_status` instead. Do not pin `get_me`.

## Rate-limit-aware response shaping

At 500 req/hr the naive design fails. Two page sizes, deliberately separate:

| | Source | Default | Purpose |
| --- | --- | --- | --- |
| API page size | `per_page` sent to CATS | up to 100 | minimise request count |
| Model page size | items returned to the caller | 10 | minimise context |

The client fetches wide; the shaping layer trims narrow. Budget is read from
`X-Rate-Limit-Limit` / `X-Rate-Limit-Remaining` rather than assumed, and
`Retry-After` is honoured on 429 — neither current implementation does either.

## Migration risks

| Risk | Mitigation |
| --- | --- |
| **Tool renames break existing consumers.** Claude Desktop configs, Mastra routing, and the deployed Horizon server all reference current names. | Keep all 186 names byte-identical. Improve descriptions only. Any rename goes through an alias map retaining the old name as deprecated. |
| **httpx → httpx2 semantics differ.** Exception types, timeouts, and pool defaults may not map 1:1. | All HTTP in `http/client.py`. Contract tests against recorded fixtures run before and after. |
| **Auth may not filter BM25 search results.** FastMCP docs do not state whether `search_tools` respects component-level `auth=`. If BM25 indexes pre-auth, unauthorized tools leak through search. | Explicit test. If it leaks, wrap the transform with a filtered provider. **Unproven until that test passes.** |
| **Generated schemas may drift from hand-written ones.** 186 tools have hand-written signatures today. | Snapshot every current tool's JSON schema first, diff generated against snapshot, investigate every difference. |
| **`fastmcp.json` currently deploys `server_all_tools.py`.** Repointing changes what production serves. | Repoint and redeploy as one deliberate step after the schema diff is clean. Horizon requires a rebuild for env changes. |
| **Two tools call non-existent endpoints.** | `get_me` and `authorize_user` are removed, not ported. Recorded as a breaking fix. |

## Backward compatibility

Preserved:

- all 186 tool names and their argument names
- `CATS_API_KEY`, `CATS_API_BASE_URL`, `CATS_TRANSPORT`, `CATS_HOST`, `CATS_PORT`
- `CATS_TOOLSETS` — and it will actually be honoured this time
- root-level `server.py` as an importable entrypoint

Broken deliberately:

- `get_me` and `authorize_user` removed — they never worked
- `server_all_tools.py` deleted
- default discovery becomes `search`; set `CATS_DISCOVERY_MODE=raw` to restore
  the current all-tools-visible behaviour
