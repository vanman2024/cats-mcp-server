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
orchestrator. See [docs/architecture/README.md](docs/architecture/README.md).

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
| `CATS_DISCOVERY_MODE` | `search` | `raw`, `search` or `code` |
| `CATS_TOOLSETS` | all | e.g. `candidates,jobs,pipelines` |
| `CATS_TRANSPORT` | `stdio` | `stdio` or `http` |
| `CATS_HOST` / `CATS_PORT` | `0.0.0.0` / `8000` | HTTP bind |
| `CATS_AUTH_JWKS_URI` | - | JWKS endpoint; **required for HTTP** |
| `CATS_AUTH_ISSUER` / `CATS_AUTH_AUDIENCE` | - | JWT claims to verify |
| `CATS_ALLOW_UNAUTHENTICATED_HTTP` | `false` | local development escape hatch |
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

An HTTP deployment **refuses to start without authentication**. It exposes
destructive tools; over a network it must verify who is calling.

```bash
CATS_AUTH_JWKS_URI=https://your-issuer/.well-known/jwks.json
CATS_AUTH_ISSUER=https://your-issuer/
CATS_AUTH_AUDIENCE=cats-mcp
```

For local development only: `CATS_ALLOW_UNAUTHENTICATED_HTTP=true`.

stdio needs no MCP-layer auth - the transport is a pipe to a process you
started.

Scopes follow the safety class: `cats:read`, `cats:write`, `cats:destructive`,
`cats:bulk`, `cats:admin`. Authorization filters **discovery as well as
execution**: a read-only caller cannot see destructive tools in a listing, in
search results, or reach them through `call_tool`.

## Working with candidate data

List and search tools return compact summaries by default - ids plus a small
field projection, with `count`, `total`, `has_more` and `next_page`. Resumes,
attachments, activities, pipelines and custom fields are never included in a
list result; each has a dedicated tool.

Widen deliberately with `summary_level='full'`, `fields='a,b,c'`, `page` and
`per_page`.

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

- [docs/architecture/README.md](docs/architecture/README.md) - architecture and boundaries
- [docs/TOOLS.md](docs/TOOLS.md) - generated tool inventory
- [docs/architecture/00-audit-gap-report.md](docs/architecture/00-audit-gap-report.md) - pre-refactor audit
- [docs/CREDENTIAL-SAFETY.md](docs/CREDENTIAL-SAFETY.md) - credential handling
- [DEPLOYMENT.md](DEPLOYMENT.md) - deployment guide
