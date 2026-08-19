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

## Facts versus judgment: a test for new work

The list above only settles arguments if it is quick to apply, so here is the
test the codebase already uses. For anything new - a tool, a prompt, a
composite - ask what CATS itself would confirm versus what one customer
decided.

**A fact is something CATS would confirm if asked. A judgment is a rule a
customer chose, and a different customer could choose differently.**

- *"Is this person on list 1610515?"* is a fact: CATS holds the list, CATS
  holds the membership, this adapter reports it. *"Placed people are
  excluded"* is one customer's rule about what that membership should mean -
  another account might run the identical list as a marketing suppression
  list and want placed people included, not excluded.
- *"Don't fetch a resume for someone you haven't screened"* is sequencing: an
  order of operations true for every customer no matter what their screen
  concludes. *"Placed, fired and current employees are excluded"* is policy: a
  conclusion specific to one customer's hiring rules, and the server has no
  way to know it is even true for the next one.

Sequencing is legitimately this server's business; policy is not, and the
difference is what each one needs to know to be enforced. `get_candidate_context`
enforces "screen first, act second" - it refuses a per-candidate lookup over
50 ids until the caller screens with `include=['lists']` first - and it does
that without ever learning what the screen decided. The moment a tool needs to
know *which* lists mean exclusion, or renames a status into "qualified" or
"not a fit", it has stopped enforcing an order and started encoding one
customer's answer for everyone else on the same adapter.

There is a technical floor under this, not just a design preference. This
version of FastMCP's `Context` exposes no `sample` or `create_message` - the
server cannot hand a decision to a model. Any judgment written here would have
to be hand-coded Python (`if status_id in (X, Y, Z): exclude`), which is not
reasoning, it is a snapshot of what one customer wanted on the day someone
wrote the `if`. It rots the first time that customer changes their mind, and
it is simply wrong for the next customer on the same server.

Judgment belongs one layer up, where it can actually reason and can change
without a deploy: a custom GPT's instructions, or an orchestrator's system
prompt (Mastra, today). Put "placed people are excluded for this client" there
as text a human can edit and a model can apply - not here as code only a
developer can change. The two layers also change at very different rates,
which is the practical reason to keep them apart even where it would be
technically possible not to: adapter semantics move when CATS moves - a new
endpoint, a renamed field, a status id that shifts - and policy moves whenever
a customer changes their mind about who counts as a good fit, which happens
far more often than CATS ships a change.

Existing tests enforce parts of this already, so a violation fails the build
rather than waiting for review. `tests/test_prompts.py` checks every prompt's
text and description against a vocabulary of recruiting judgment.
`tests/test_boundary.py` extends that same vocabulary check across tool names
and descriptions, and confirms no spec or composite hardcodes an
account-specific list id, status id or company name.
`tests/test_candidate_context.py` confirms `get_candidate_context` never
returns a key like `excluded` or `fit` - only facts the caller can inspect and
disagree with.

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

**Serving HTTP requires an explicit `CATS_AUTH_MODE`** - `platform` (a
gateway in front authenticates, which is how Horizon works), `jwt` (this server
verifies tokens itself), or `none` (local development). There is no default:
assuming a gateway that is not there exposes destructive tools, and assuming
none breaks a correctly-fronted deployment. stdio needs no mode.

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

StaffHive-specific logic.

An earlier design document proposed building an entire agentic recruiting
application in this repository - FastAPI services, a Supabase mirror of CATS
data, an agent hierarchy, a memory system, a communications layer and a
frontend. That is the StaffHive product, not a vendor adapter. Building it here
would tie a CATS adapter to one product's business rules and make it unusable
for ChatGPT, Claude, Codex, or a future non-CATS ATS.

Four ideas from it were genuinely adapter-specific and were kept: safety
classification of mutations, correlation metadata on every request, explicit
marking of operations that cannot be undone, and efficient bulk reads in place
of a data-mirroring layer.

## Further reading

- [DEPLOYMENT.md](DEPLOYMENT.md) - running it locally, on Horizon, or self-hosted
- [TOOLS.md](TOOLS.md) - generated tool, resource and prompt inventory
The pre-refactor audit that motivated this design is in git history, at
`docs/architecture/00-audit-gap-report.md` before commit 536aba6.
