# CATS-MCP Architecture

## What this is

A universal MCP adapter for the CATS (CatsOne) API v3. One adapter, one tool
server, comprehensive atomic coverage of the vendor API.

It is **one integration among several**. CATS is one possible ATS. StaffHive may
use others, and this repository must not assume otherwise.

## What it owns

- CATS authentication and requests
- CATS-specific resource and endpoint coverage
- Accurate tool schemas and discovery metadata
- Compact, structured responses; pagination
- Rate-limit handling and request validation
- Read/write/destructive safety classification
- Audit and correlation metadata
- Efficient CATS data-access primitives

## What it does not own

Recruitment workflows. Agent orchestration. Agent memory. Outreach campaigns.
Email or SMS delivery. Follow-up sequences. Scheduling. Client-specific
recruiting rules. Candidate-ranking policy. A frontend. A mirrored CATS
database. A separate Supabase application. StaffHive product state.

If a change would encode a decision about *who to contact, when, or why*, it
belongs in the orchestrator, not here.

## Consumers

```
                       +---------------------------+
                       |      CATS-MCP (this)      |
                       |  184 endpoint tools       |
                       |  + 5 composite reads      |
                       |  + connection status      |
                       +-------------+-------------+
                                     |
        +--------------------+-------+--------+--------------------+
        |                    |                |                    |
   StaffHive /            RedAI /          ChatGPT               Claude
    Mastra              Google ADK          Codex            Claude Desktop
   (raw profile)        (raw profile)   (search profile)    (search profile)
```

Mastra is StaffHive's primary orchestrator today and Google ADK is used inside
RedAI, but neither is a dependency of this repository. Nothing here imports or
targets them. Any MCP-compatible client works.

The orchestrator owns: agent reasoning, multi-step workflows, tool routing
across multiple MCP servers, human approval, long-running automation,
scheduling, memory, and cross-system coordination.

## Discovery profiles

`CATS_DISCOVERY_MODE=raw|search|code`

| Mode | Model sees | Intended consumer |
| --- | --- | --- |
| `raw` | the full authorized catalog | orchestrators that do their own tool discovery |
| `search` | `search_tools`, `call_tool`, and pinned tools | direct MCP clients |
| `code` | a Code Mode sandbox | direct clients composing multi-step reads |

Default is `search`.

**Switching modes never removes capability.** All tools remain callable in every
mode; only visibility changes. In `search` mode a hidden tool is reached through
`call_tool`, and authorization is enforced identically either way.

**Do not stack tool search.** StaffHive/Mastra should consume the protected
`raw` profile and run Mastra's own `ToolSearchProcessor` across CATS and every
other connected MCP server. Running this server's BM25 transform underneath
Mastra's means searching an index of an index, and Mastra cannot rank CATS tools
against tools from other servers if it can only see two meta-tools.

`raw` must not be the public default. It exposes every tool, including
destructive ones, to whoever can reach the endpoint.

`code` requires the optional extra: `uv pip install 'cats-mcp-server[code-mode]'`.
It is experimental and unnecessary for Mastra, which already provides
orchestration and code mode of its own.

## Candidate data is retrieved progressively

Tool discovery solves the tool-*schema* context problem. It does nothing about
oversized *results*.

List and search tools return compact summaries: stable CATS ids, a small field
projection, `count`, `total`, `has_more` and `next_page`. Resumes, attachments,
activities, pipelines, applications and custom fields are never included in a
list result at any summary level. Each has a dedicated tool.

Callers can widen deliberately with `fields`, `summary_level`, `page` and
`per_page`.

Pagination metadata is read from the HAL `_links.next` href rather than computed
from page arithmetic, so it stays correct when CATS changes its page size.

## Rate limits shape the design

The CATS standard is **500 requests/hour**, roughly 8 per minute. Some accounts
are raised - this project's is at 1,500 - so the ceiling is read from the
`X-Rate-Limit-Limit` and `X-Rate-Limit-Remaining` headers on every response
rather than assumed. `Retry-After` is honoured on 429. Backoff uses full jitter
so concurrent callers de-correlate.

The composite read primitives exist because of this budget. "Find candidates in
Kamloops, then check when each was last contacted" is one search plus N activity
calls; at 50 candidates that is ten minutes of budget and, done naively, 50 full
activity lists through the model's context. `get_candidate_engagement` makes it
one tool call returning one compact table.

Composites report `requests_used` and the live `rate_limit` snapshot so the
orchestrator can budget. They return facts - last contact date, activity count -
and never a recommendation about who to contact.

## Authentication

Three separate concerns, deliberately not conflated:

| Layer | Mechanism |
| --- | --- |
| Authenticating the MCP caller | `JWTVerifier` via `CATS_AUTH_JWKS_URI` |
| Authorizing discovery and execution | per-tool `require_scopes`, derived from safety class |
| The downstream CATS credential | `CredentialProvider` |

Authorization filters **both** listing and execution, and this is verified by
test: a read-only caller searching "delete a candidate" gets nothing back, and
cannot reach the tool through `call_tool` either.

**An HTTP deployment refuses to start without authentication.** Set
`CATS_AUTH_JWKS_URI`, or `CATS_ALLOW_UNAUTHENTICATED_HTTP=true` for local
development only. stdio needs no MCP-layer auth: the transport is a pipe to a
process the user already started.

Scopes follow the safety classification: `cats:read`, `cats:write`,
`cats:destructive`, `cats:bulk`, `cats:admin`.

## Deployment options

1. **One deployment per customer.** `EnvCredentialProvider` reads
   `CATS_API_KEY`. Simplest, and the current model.
2. **Protected multi-tenant service.** Validate a StaffHive identity token, then
   resolve that tenant's CATS connection. The seam is
   `RequestScopedCredentialProvider`; the resolver is injected so this
   repository never depends on StaffHive.
3. **Customer-hosted**, connected to StaffHive over a protected URL.

CATS credentials are never returned in tool output and never appear in logs,
errors, or any string form of the credential object. That is asserted by test.

## Module layout

```
src/cats_mcp/
  config.py          settings; rejects unresolved ${VAR} placeholders at startup
  server.py          the single factory - the only place a FastMCP is built
  app.py             module-level `mcp`; what fastmcp.json points at
  credentials/       provider protocol, env impl, multi-tenant seam
  http/              the only module importing an HTTP library
  registry/          ToolSpec model, catalog, and spec data per resource
  responses/         compact shaping and pagination
  composites/        batch and change-feed read primitives
  discovery/         raw / search / code wiring
  auth/              caller verification and scope policy
```

The tool catalog is declarative. `ToolSpec` records the endpoint, method,
parameters and their locations, safety class, tags and response strategy; one
executor turns any spec into a working tool. Tool counts are generated from the
registry, so the drift the audit found - five different counts across README,
logs, tests and design docs - cannot recur.

## Out of scope

StaffHive-specific logic. See `docs/archive/CATS-INTELLIGENCE-SYSTEM.md` for the
earlier product exploration and why it does not belong here.

## Further reading

- `00-audit-gap-report.md` - what was wrong before the refactor, with evidence
- `01-module-structure.md` - the structure proposal and migration risks
