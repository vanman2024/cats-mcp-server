# Deployment

Three ways to run this server. Pick by who is calling it.

| | Transport | Auth | Use for |
| --- | --- | --- | --- |
| [Local client](#local-client-stdio) | stdio | none needed | Claude Desktop, Claude Code, Cursor |
| [Prefect Horizon](#prefect-horizon) | HTTP | Horizon's gateway | shared/remote access, orchestrators |
| [Self-hosted](#self-hosted-http) | HTTP | yours to provide | your own infrastructure |

---

## Local client (stdio)

The transport is a pipe to a process your client starts, so there is no network
surface and no MCP-layer auth is needed.

```json
{
  "mcpServers": {
    "cats": {
      "command": "python",
      "args": ["/absolute/path/to/cats-mcp-server/server.py"],
      "env": {
        "CATS_API_KEY": "your_cats_api_key_here",
        "CATS_DISCOVERY_MODE": "search"
      }
    }
  }
}
```

The path must be absolute. `server.py` is a thin shim over
`src/cats_mcp/app.py` and works whether or not the project is pip-installed.

`CATS_DISCOVERY_MODE=search` is the right choice here: a direct client sees
`search_tools`, `call_tool` and a couple of pinned tools rather than 200 schemas.

---

## Prefect Horizon

### How Horizon actually works

Two things are easy to get wrong, because they differ from what the repository
config implies:

1. **Horizon does not read `fastmcp.json`.** The entrypoint comes from the
   server's configuration in the Horizon UI - it "defaults to `main.py` unless
   the server is configured with another entrypoint." Changing `source.path` in
   the repo has no effect on the deployment.
2. **Environment variables come from the Horizon UI**, not from the repo.
   Settings -> Environment Variables, encrypted, injected into the process.

`fastmcp.json` is still useful - the local `fastmcp` CLI reads it - but it is
not what configures the hosted deployment.

### Setup

| Setting | Value |
| --- | --- |
| Entrypoint | `src/cats_mcp/app.py` |
| Object | `mcp` |
| Dependency file | `requirements.txt` |

Environment variables to set in the UI:

```
CATS_API_KEY        = <your CATS API key>
CATS_AUTH_MODE      = platform
CATS_DISCOVERY_MODE = search        # or raw, for an orchestrator
```

`CATS_AUTH_MODE=platform` is correct on Horizon: its gateway "runs before your
server code" and authentication is enabled by default for hosted endpoints, so
a rejected caller never reaches this process.

### Two things that will bite

- **Production and Preview variables are separate records.** "Updating one does
  not update the other." Set both, or preview deploys fail.
- **Changing a variable does not update a running server.** You must rebuild.
  The same applies to changing the entrypoint.

### Rotating the CATS key

Update the variable in the UI, then trigger a rebuild. There is no way to swap
it on a running server.

---

## Self-hosted HTTP

```bash
export CATS_API_KEY="your_cats_api_key_here"
export CATS_TRANSPORT=http
export CATS_AUTH_MODE=jwt
export CATS_AUTH_JWKS_URI="https://your-issuer/.well-known/jwks.json"
export CATS_AUTH_ISSUER="https://your-issuer/"
export CATS_AUTH_AUDIENCE="cats-mcp"

python server.py
```

Serving HTTP **requires an explicit `CATS_AUTH_MODE`**. There is no default,
because guessing wrong is harmful either way: assume a gateway that is not there
and destructive tools sit on an open URL; assume none and a correctly-fronted
deployment fails to start.

- `platform` - a reverse proxy or gateway authenticates first
- `jwt` - this server verifies bearer tokens itself
- `none` - nobody does; local development only

Only `jwt` gives per-tool scope enforcement, because only then does this server
see verified claims. Under `platform`, authorization is the gateway's job.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY server.py ./

ENV CATS_TRANSPORT=http \
    CATS_HOST=0.0.0.0 \
    CATS_PORT=8000

EXPOSE 8000
CMD ["python", "server.py"]
```

Pass `CATS_API_KEY` and the auth variables at runtime - never bake them into an
image.

---

## Configuration reference

| Variable | Default | Notes |
| --- | --- | --- |
| `CATS_API_KEY` | - | **Required.** |
| `CATS_API_BASE_URL` | `https://api.catsone.com/v3` | |
| `CATS_DISCOVERY_MODE` | `search` | `raw`, `search`, `code` |
| `CATS_TOOLSETS` | all | e.g. `candidates,jobs,pipelines` |
| `CATS_TRANSPORT` | `stdio` | `stdio` or `http` |
| `CATS_HOST` / `CATS_PORT` | `0.0.0.0` / `8000` | HTTP only |
| `CATS_AUTH_MODE` | - | **Required for HTTP.** `platform`, `jwt`, `none` |
| `CATS_AUTH_JWKS_URI` | - | required by `jwt` |
| `CATS_AUTH_ISSUER` / `CATS_AUTH_AUDIENCE` | - | JWT claims to verify |
| `CATS_SEARCH_MAX_RESULTS` | `5` | results per `search_tools` call |
| `CATS_REQUEST_TIMEOUT` | `30.0` | seconds |
| `CATS_MAX_RETRIES` | `4` | attempts per request |
| `LOG_LEVEL` | `INFO` | |

A `${VAR}` placeholder that never got substituted is rejected at startup rather
than being sent to CATS as a literal key - that would otherwise surface as a
confusing 401.

---

## Which discovery mode

| Consumer | Mode | Why |
| --- | --- | --- |
| Mastra, Google ADK, any orchestrator | `raw` | it runs its own tool discovery across every connected server; stacking search means searching an index of an index |
| Claude Desktop, ChatGPT, Cursor | `search` | 200 schemas must not land in the context window |
| Multi-step composition | `code` | needs the `code-mode` extra; experimental |

Every tool stays callable in every mode. Only visibility changes.

---

## Multi-tenancy

One CATS account per deployment today, via `CATS_API_KEY`.

For per-user CATS keys, Horizon offers **delegated authorization**: the gateway
"identifies the Horizon user, exchanges that identity for the saved downstream
credential, and injects the credential before invoking hosted server code."
That is available on Developer and Enterprise plans.

When you get there, `RequestScopedCredentialProvider` in
`src/cats_mcp/credentials/request_scoped.py` is the seam - it reads the injected
credential instead of the environment, and no tool changes.

---

## Verifying a deployment

Call `get_connection_status`, or read the resource `cats://server/capabilities`.
Both report the discovery mode, tool count, credential source and rate-limit
budget, and neither requires CATS to be reachable - so they distinguish a
configuration problem from an API problem.

## Troubleshooting

**`Couldn't find the entrypoint ...`** - Horizon's configured entrypoint does
not match a file in the repo. Set it to `src/cats_mcp/app.py`.

**`ModuleNotFoundError: cats_mcp`** - the project was not installed as a
package. The root shims add `src/` to `sys.path` for this reason; if you wrote
your own entrypoint, do the same or `pip install -e .`.

**`Refusing to serve HTTP without knowing who authenticates callers`** - set
`CATS_AUTH_MODE`. This is deliberate.

**`CATS_API_KEY is not set`** - on Horizon, set it in the UI and rebuild.

**401 from CATS on every call** - the key is wrong, revoked, or an unsubstituted
placeholder. Check `get_connection_status` first.

**429 / running out of requests** - the CATS standard is 500 requests/hour. Use
the batch tools rather than one call per record, and `get_changed_records`
rather than re-listing. `cats://account/rate-limit` shows the live budget.

## Resources

- CATS API: https://docs.catsone.com/api/v3/
- FastMCP: https://gofastmcp.com/
- Horizon: https://docs.horizon.prefect.io/
