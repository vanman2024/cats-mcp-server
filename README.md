# CATS MCP Server

A universal MCP adapter for the [CATS (CatsOne)](https://www.catsone.com/) API v3.

Comprehensive, atomic coverage of the CATS API exposed to any MCP-compatible
client - Claude, ChatGPT, Codex, Mastra, Google ADK, or your own orchestrator.

For the current inventory see **[docs/TOOLS.md](docs/TOOLS.md)**, generated from
the registry.

## What a client sees

This server exposes all three MCP primitives, not just tools:

| Primitive | What it gives you |
| --- | --- |
| **Tools** | every CATS endpoint, plus batch reads and a status check |
| **Resources** | what this adapter is, which account it is attached to, and the account-specific ids nothing else works without |
| **Prompts** | how to operate the CATS API correctly - not what to do with the results |

Start with the resource `cats://server/capabilities`. It states what this server
owns, what it deliberately leaves to the caller, and how it is configured - and
it works even when CATS is unreachable.

## What this is, and is not

This is an **adapter**. It owns CATS authentication, endpoint coverage, tool
schemas, discovery metadata, response shaping, pagination, rate-limit handling
and safety classification.

It does **not** own recruiting workflows, agent orchestration, memory, outreach,
scheduling, candidate ranking, or a frontend. Those belong to the calling
orchestrator. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

```bash
uv venv
uv pip install -r requirements.txt -r requirements-dev.txt
uv pip install -e .
```

FastMCP 4 is a prerelease, so every dependency is pinned exactly - a loose
specifier lets uv resolve that package to a prerelease too.

## Configure

Copy the variables you need into `.env` (gitignored) or set them in your
deployment environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CATS_API_KEY` | - | **Required.** CATS API key |
| `CATS_API_BASE_URL` | `https://api.catsone.com/v3` | API base URL |
| `CATS_UI_BASE_URL` | - | e.g. `https://acme.catsone.com`; adds a `url` to each record |
| `CATS_DISCOVERY_MODE` | `search` | `raw`, `search` or `code` |
| `CATS_TOOLSETS` | all | e.g. `candidates,jobs,pipelines` |
| `CATS_TRANSPORT` | `stdio` | `stdio` or `http` |
| `CATS_HOST` / `CATS_PORT` | `0.0.0.0` / `8000` | HTTP bind |
| `CATS_AUTH_MODE` | - | `platform`, `jwt` or `none`; **required for HTTP** |
| `CATS_AUTH_JWKS_URI` | - | JWKS endpoint, for `jwt` mode |
| `CATS_AUTH_ISSUER` / `CATS_AUTH_AUDIENCE` | - | JWT claims to verify |
| `CATS_SEARCH_MAX_RESULTS` | `5` | results per `search_tools` call |
| `LOG_LEVEL` | `INFO` | logging verbosity |

## Run

```bash
python server.py                    # stdio, for Claude Desktop / Cursor / Claude Code
CATS_TRANSPORT=http python server.py    # HTTP (requires auth, see below)
```

Or via the FastMCP CLI:

```bash
fastmcp run src/cats_mcp/app.py:mcp
```

## Discovery modes

The catalog is large on purpose - atomic coverage is what makes the adapter
reusable. But those schemas must not all land in a model's context.

| Mode | The model sees | Use for |
| --- | --- | --- |
| `raw` | the full authorized catalog | orchestrators doing their own tool discovery |
| `search` | `search_tools`, `call_tool`, pinned tools | direct MCP clients |
| `code` | a Code Mode sandbox | multi-step composition without intermediate results |

**Every tool stays callable in every mode.** Only visibility changes; hidden
tools are reached through `call_tool`, and authorization is enforced the same
either way.

**Mastra and other orchestrators should use `raw`** and run their own tool
search across every connected MCP server. Stacking this server's BM25 transform
under Mastra's means searching an index of an index, and prevents Mastra from
ranking CATS tools against tools from other servers.

`code` needs an optional extra:

```bash
uv pip install -e '.[code-mode]'
```

## Authentication

Serving over HTTP requires saying **who verifies the caller**, via
`CATS_AUTH_MODE`. There is no default, because guessing wrong is harmful in
both directions: assume a gateway that is not there and destructive tools sit
on an open URL; assume none and a correctly-fronted deployment fails to start.

| Mode | Meaning | Use when |
| --- | --- | --- |
| `platform` | something in front authenticates first | hosted on Prefect Horizon, or behind a reverse proxy |
| `jwt` | this server verifies bearer tokens itself | self-hosted with nothing in front |
| `none` | nobody authenticates | local development only |

**On Horizon, use `platform`.** Its gateway "runs before your server code" and
authentication is enabled by default for hosted endpoints, so a rejected caller
never reaches this process.

For `jwt`:

```bash
CATS_AUTH_MODE=jwt
CATS_AUTH_JWKS_URI=https://your-issuer/.well-known/jwks.json
CATS_AUTH_ISSUER=https://your-issuer/
CATS_AUTH_AUDIENCE=cats-mcp
```

stdio needs no mode - the transport is a pipe to a process you started.

**Per-tool scopes** (`cats:read`, `cats:write`, `cats:destructive`,
`cats:bulk`, `cats:admin`) apply in `jwt` mode, where this server sees verified
claims. Authorization then filters **discovery as well as execution**: a
read-only caller cannot see destructive tools in a listing, in search results,
or reach them through `call_tool`. Under `platform`, the gateway authenticates
but this server sees no claims, so authorization is the gateway's to enforce.

## Working with candidate data

List and search tools return compact summaries by default - ids plus a small
field projection, with `count`, `total`, `has_more` and `next_page`.

Widen deliberately:

| Level | Returns |
| --- | --- |
| `compact` (default) | a handful of identifying fields |
| `standard` | the record, including **custom fields** - certifications, trade qualifications, screening answers |
| `full` | the whole record |
| `fields='a,b,c'` | exactly those columns |

Custom fields are where account-specific screening data lives, so reach for
`summary_level='standard'` rather than fetching each candidate individually -
that is the difference between one request and fifty against a 500/hour budget.

### Links back to CATS

Set `CATS_UI_BASE_URL` and candidate and job records carry a `url` field
pointing at them in the CATS web UI. Consumers were otherwise building these by
hand and getting them wrong - CATS uses
`index.php?m=candidates&a=show&candidateID=...`, not a REST-style
`/candidates/{id}` path, so hand-built links look right in a spreadsheet and
404 when clicked.

A link is only emitted where the id genuinely identifies that record. A tool is
tagged with the resource it belongs to, not the shape of the rows it returns, so
`list_candidate_attachments` is a candidate tool returning attachments -
building a candidate link from an attachment id yields a working link to an
unrelated real person, which returns 200 and so is never reported as an error.
Saved-list membership rows are the one exception that still links: the row names
its candidate in `candidate_id`, so the link is built from that, never the row's
own `id`.

Unset, no link is emitted at all. A missing link is recoverable; a wrong one is
not noticed until someone tries to use it.

Resumes, attachments, activities, pipelines and applications are never included
in a list at any level. They are unbounded in size and each has its own tool -
`download_attachment` returns the actual document for the model to read.

## Rate limits

The CATS standard is **500 requests/hour**. Some accounts are raised, so the
real ceiling is read from the response headers rather than assumed;
`Retry-After` is honoured and backoff is jittered.

Call `get_connection_status` to see the remaining budget before a large batch.

The composite read primitives exist for this reason - `get_candidate_engagement`
answers "when was each of these 50 candidates last contacted" in one tool call
instead of 50, and returns a compact table instead of 50 activity lists. See
[docs/TOOLS.md](docs/TOOLS.md).

## Development

```bash
python -m pytest tests/ -q            # test suite
python -m ruff check src/ tests/      # lint
python scripts/generate_tool_docs.py --write   # regenerate docs/TOOLS.md
```

Tool counts are generated from the registry and a test fails if the docs drift.

## Adding a tool

Tools are declarative. Add a `ToolSpec` to the right module in
`src/cats_mcp/registry/specs/` - name, endpoint, method, parameters and their
locations, safety class, tags and response strategy. One executor turns any spec
into a working tool; there is no per-tool request code to write.

## Documentation

| Document | Covers |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | what this owns and does not, consumers, design decisions |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | running it locally, on Horizon, or self-hosted |
| [docs/TOOLS.md](docs/TOOLS.md) | tool, resource and prompt inventory (generated) |
| [docs/CREDENTIAL-SAFETY.md](docs/CREDENTIAL-SAFETY.md) | secret handling and the pre-commit guard |

Superseded documentation is not kept in the working tree; git history has it.
