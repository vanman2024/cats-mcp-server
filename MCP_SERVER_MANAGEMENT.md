# MCP Server Management Playbook

> General operating guide for MCP servers we build and maintain.  
> Copy this file into each MCP repository so Claude, Codex, ChatGPT, Cursor, or another coding agent can follow the same release and schema-management process.

## Purpose

MCP servers are live API contracts. The server code can change at any time, but connected clients may cache or snapshot the exposed tool schema. Treat tool names, descriptions, parameters, annotations, and return shapes like a public API.

The goal of this playbook is to prevent:

- stale tool schemas in clients;
- breaking changes that silently disrupt agents;
- uncertainty about which server version a client is using;
- accidental production changes without verification;
- repeated remove/re-add cycles when a simple refresh is enough;
- schema drift between code, docs, tests, and deployed MCP instances.

---

## 1. Every MCP server must expose version identity

Every server should make its deployed identity easy to verify.

Preferred metadata:

- application version;
- MCP schema version;
- git commit SHA;
- build/deploy timestamp;
- environment: local / staging / production;
- server name;
- transport/profile if relevant.

Recommended implementation:

- expose a lightweight read-only tool such as `get_server_info`; or
- expose an MCP resource such as `mcp://server/info`; and
- include the same metadata in startup logs.

Example response:

```json
{
  "server": "example-mcp-server",
  "app_version": "0.8.2",
  "schema_version": "2026-09-19.1",
  "git_sha": "abc1234",
  "environment": "production",
  "deployed_at": "2026-09-19T15:30:00Z"
}
```

Do not rely on package version alone. Tool schemas can change without a package-version bump unless we enforce it.

---

## 2. Treat the tool schema as a public contract

### Safe / additive changes

Usually lower risk:

- add a new optional parameter;
- add a new tool;
- expand a description;
- add non-breaking response fields;
- add a new enum value when existing clients can safely ignore it;
- add new read-only resources or prompts.

### Breaking changes

Require deliberate migration:

- rename a tool;
- remove a tool;
- rename or remove a parameter;
- change a required parameter;
- change a field type;
- change parameter semantics;
- change a write tool into a different action;
- change a response shape that callers depend on;
- change authentication assumptions.

Prefer additive evolution over destructive replacement.

If a tool must be replaced:

1. add the new tool first;
2. keep the old tool working temporarily;
3. mark the old tool deprecated in its description;
4. update clients and tests;
5. remove the old tool only after the migration window.

---

## 3. Schema versioning

Maintain a separate `schema_version` in addition to the application version.

Suggested format:

```text
YYYY-MM-DD.N
```

Example:

```text
2026-09-19.2
```

Bump the schema version whenever any exposed MCP contract changes:

- tool added or removed;
- tool renamed;
- parameters changed;
- descriptions materially changed in a way that affects tool selection;
- annotations/safety metadata changed;
- response contract changed;
- resources/prompts exposed to the client changed.

Code-only fixes that do not alter the external MCP contract do not require a schema bump.

---

## 4. Standard development workflow

For every MCP change:

1. **Change the code locally.**
2. **Run unit/integration tests.**
3. **Regenerate any generated tool documentation.**
4. **Compare the exposed tool schema with the previous version.**
5. **Bump `schema_version` when the contract changed.**
6. **Update the changelog/release notes.**
7. **Deploy or restart the target server.**
8. **Verify `get_server_info` / version resource against the expected git SHA and schema version.**
9. **Refresh/reload the MCP connection in each client that caches tool definitions.**
10. **Start a fresh chat/session when testing schema changes.**
11. **Run smoke tests against the changed tools.**
12. **Only then consider the release complete.**

A successful deployment is not enough. The client must also be confirmed to have loaded the expected schema.

---

## 5. Client refresh workflow

After a tool-schema change, assume clients may still have the previous contract loaded.

### Generic client procedure

1. deploy/restart the MCP server;
2. confirm the server reports the expected version;
3. refresh/reload the MCP connection in the client;
4. inspect the visible tool list/schema if the client exposes it;
5. start a new conversation/session;
6. call `get_server_info`;
7. test at least one changed tool;
8. confirm the result matches the expected schema version.

If a client still behaves as though the old schema exists:

- disconnect/reconnect the MCP;
- restart the client;
- open a new conversation/session;
- verify the remote endpoint is the intended deployment;
- confirm there is not a second stale MCP registration with the same display name.

Deleting and re-adding the MCP should be a last resort during normal development, not the default workflow.

---

## 6. ChatGPT-specific notes

ChatGPT may cache or snapshot MCP tool definitions depending on how the MCP/app is connected and published.

During active development:

- use the development/private connection;
- deploy schema changes;
- refresh/reload the connection;
- test in a fresh chat;
- verify the server version explicitly.

Do not assume an existing conversation automatically sees a newly deployed schema.

For workspace or public publication, treat the published contract as more stable than a development connection. Avoid frequent breaking changes. Product-specific refresh, approval, or re-publication requirements can change over time, so verify the current OpenAI publishing workflow before a release.

The server should remain backward-compatible wherever practical so old cached schemas fail gracefully rather than performing the wrong action.

---

## 7. Claude / coding-agent instructions

When Claude, Codex, Cursor, or another coding agent modifies an MCP server, it should:

1. read this file before changing exposed tools;
2. determine whether the request changes the external MCP contract;
3. preserve existing tool names and parameters unless a breaking migration is explicitly approved;
4. prefer adding optional fields/new tools;
5. update `schema_version` for exposed contract changes;
6. update generated docs and tests;
7. add or update regression tests for the changed schema;
8. report exactly which tools changed;
9. provide the required client-refresh steps after deployment;
10. never claim the client is using the new schema until version verification has been performed.

When fixing implementation logic without changing the tool contract, do not unnecessarily rename tools or parameters.

---

## 8. Tool design rules

### Keep tools focused

Prefer tools with clear, narrow contracts over giant tools with ambiguous behavior.

### Prefer deterministic server-side work

Do filtering, joins, pagination, normalization, validation, and rate-limit protection inside the MCP server when practical. Do not force the LLM to ingest large raw datasets when the server can safely reduce them first.

### Separate reads from writes

Where practical:

- keep read operations clearly read-only;
- make write intent obvious in tool names and descriptions;
- annotate destructive or high-impact actions;
- validate IDs and state before writes;
- return a compact confirmation after writes.

### Use stable identifiers

Prefer stable IDs over display names for mutations. Names may change or collide.

### Make errors actionable

Errors should identify:

- what failed;
- which resource/tool was involved;
- whether retry is safe;
- whether the failure is validation, authentication, rate limit, permission, conflict, or upstream API failure.

---

## 9. Schema drift protection

Each MCP repo should automate as much of the following as possible:

- generate tool documentation from the live registry;
- test that generated docs match committed docs;
- snapshot or hash the exposed JSON schema;
- compare schema changes in CI;
- fail CI on unapproved breaking changes;
- verify declared tool count matches runtime tool count;
- verify tool annotations match expected safety class;
- verify write tools are not accidentally exposed as reads or vice versa.

Recommended CI output:

```text
Schema unchanged
```

or

```text
Schema changed:
+ create_candidate_note
~ update_candidate: added optional source
- legacy_candidate_lookup
BREAKING CHANGE DETECTED
```

---

## 10. Recommended repository files

A mature MCP repo should normally contain:

```text
README.md
MCP_SERVER_MANAGEMENT.md
CHANGELOG.md
docs/
  ARCHITECTURE.md
  DEPLOYMENT.md
  TOOLS.md
tests/
```

Optional:

```text
docs/SCHEMA_CHANGELOG.md
scripts/generate_tool_docs.*
scripts/schema_diff.*
```

Generated inventories such as tool counts should have one source of truth. Avoid manually maintaining the same count or schema details in several files.

---

## 11. Environment strategy

Use distinct environments when the MCP is important enough to support them:

### Local

For development with Claude Code, Cursor, or another local client.

### Staging

For testing:

- authentication;
- remote transport;
- tool schema changes;
- client refresh behavior;
- write safety;
- rate limiting;
- deployment packaging.

### Production

Only deployed after:

- tests pass;
- schema diff is reviewed;
- version metadata is correct;
- secrets are supplied through the deployment environment;
- smoke tests pass.

Never commit real credentials to the repository.

---

## 12. Release checklist

Before release:

- [ ] Tests pass.
- [ ] No secrets are committed.
- [ ] Tool schema diff reviewed.
- [ ] Breaking changes explicitly approved.
- [ ] Schema version bumped if needed.
- [ ] App/package version bumped if needed.
- [ ] Tool docs regenerated.
- [ ] Changelog updated.
- [ ] Deployment target confirmed.
- [ ] Production version metadata verified.
- [ ] MCP connection refreshed in test client.
- [ ] Fresh session started.
- [ ] `get_server_info` returns expected version.
- [ ] Changed tools smoke-tested.
- [ ] Writes tested only against a safe/test record when possible.
- [ ] Rollback path known.

---

## 13. Rollback procedure

If a release causes client failures:

1. stop further writes if safety is uncertain;
2. identify whether the issue is server code or stale client schema;
3. verify the client's loaded version/schema;
4. if server-side, roll back to the last known-good deployment;
5. if schema-related, restore the previous compatible contract where possible;
6. refresh/reconnect affected clients;
7. start a fresh session and verify;
8. document the incident and add a regression test.

Never solve a schema problem by repeatedly deleting and re-adding clients without first identifying the version mismatch.

---

## 14. Troubleshooting stale tool schemas

Symptoms:

- the client calls a parameter that no longer exists;
- a new tool is missing;
- the client keeps using an old tool name;
- validation errors appear immediately before server logic runs;
- the server logs show no request for a tool the client claims to call.

Check in this order:

1. server deployment/version;
2. endpoint URL;
3. `schema_version`;
4. client connection refresh;
5. duplicate MCP registrations;
6. fresh conversation/session;
7. auth/workspace context;
8. publication/approval state if applicable.

Always distinguish:

**server is running the wrong version**

from

**client is using an old cached tool contract**.

They require different fixes.

---

## 15. Publication discipline

Development MCPs can evolve quickly.

Published/shared MCPs should evolve slowly and compatibly.

Before publishing broadly:

- stabilize naming;
- stabilize authentication;
- establish tenant isolation;
- classify read/write/destructive actions;
- add version metadata;
- add audit logging;
- add rate-limit protection;
- document support and rollback expectations;
- minimize breaking schema changes.

The more users and agents depend on the server, the more the tool schema should be treated like a long-lived public API.

---

## Core rule

> **Deploying new code does not prove a client is using the new MCP schema. Always verify the server version, refresh the client connection, start a fresh session when needed, and test the changed contract.**
