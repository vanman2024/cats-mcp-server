# Pre-v4 hand-written toolsets

Superseded by the declarative registry in `src/cats_mcp/registry/`.

These are the 186 hand-written tool functions the server used before the FastMCP 4
refactor. They are kept for reference only and are **not importable** any more:
they `import httpx`, which FastMCP 4 does not ship (it uses `httpx2`).

Every endpoint they covered is preserved. `tests/fixtures/tool_schema_baseline.json`
is a snapshot of the schemas these produced, and `tests/test_registry_parity.py`
asserts the registry still reproduces it byte for byte.

Two tools here were **not** carried across, because their endpoints do not exist
in the CATS API v3 and fail at runtime:

- `get_me` -> `GET /users/current` returns 404. Use `get_site`.
- `authorize_user` -> `POST /authorization` is not a CATS endpoint.

See `docs/architecture/00-audit-gap-report.md` for the full analysis.
