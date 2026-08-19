# cats-mcp-server — baseline

**Profile:** integration-project (Integration / MCP Server)
**Baselined:** 2026-08-17 at commit `3df42da`
**Facts live in the Product Graph** (`.project-cache/project.db`). This document is
the human-readable agreement about them, not a second copy.

---

## What this system is

A universal MCP adapter exposing the CATS (CatsOne) applicant tracking system API v3
as typed tools, resources and prompts, built on FastMCP 4.

The load-bearing architectural decision is a **facts/judgment boundary**: the adapter
returns what CATS would confirm and deliberately refuses to decide anything. No
ranking, no `should_contact`, no outreach, no customer-specific taxonomy. That
boundary is enforced by tests, not just documented — `tests/test_boundary.py`
police tool vocabulary against a policy wordlist with per-tool justified allowlist
entries, and the composite list is derived from the registry rather than hardcoded
so the check cannot go stale.

CATS is treated as *one possible ATS*, not the product. That framing is why the
adapter is worth baselining separately from anything consuming it.

## Runtimes

Python 3.10+ (ruff target `py310`), FastMCP 4, httpx. Single process, stateless.
Deployed as a FastMCP Cloud connector.

## Domains and ownership

| Domain | Path | Owns | Does not own |
| :--- | :--- | :--- | :--- |
| D1 Endpoint Registry | `registry/` | ToolSpec catalog, params, scopes, tags | composition, recruiting semantics |
| D2 Composite Reads | `composites/` | batching, projection, partial failure | ranking, contact decisions |
| D3 HTTP & Rate Limiting | `http/` | retry, backoff, HAL pagination, budget | business retries |
| D4 Discovery & Profiles | `discovery/` | visibility and pinning | tool callability (never restricted) |
| D5 Auth & Credentials | `auth/`, `credentials/` | scope enforcement, credential resolution | identity issuance |
| D6 Resources & Responses | `resources/`, `responses/` | self-description, size limits, links | — |
| D7 Observability | `observability.py` | timing, structured logging, correlation | external APM |

No ownership conflicts: single product, single writer per domain. The multi-product
ownership analysis that dominates a platform baseline does not apply here.

## Capability state

14 of 57 capabilities apply under the integration-project profile.

**Complete (3):** S08 Application/API Service Layer · S16 Integration Framework ·
S40 Reliability & Resilience

**Not applicable (2):** S01 Product Surfaces (no UI) · S09 Database & Persistence
(stateless by design; mirroring CATS is an explicit non-goal)

**Partial or missing (9):** S06 Domain Model · S15 Event & Webhook · S17 Developer
Platform · S38 Security · S41 Observability · S42 Infrastructure · S43 Delivery ·
S44 Testing · S45 Releases

Full evidence per capability is in `project_capabilities`.

> The `capability-scan.py` heuristics are tuned for web applications and mis-serve
> this project type. Run with an inferred profile it reported S16 Integration
> Framework as `NONE` on a project that *is* an integration framework, and matched
> S50 Business Model on the substrings `cac` in "caching" and `arr` in "arrives".
> Statuses above are judged from reading the code, recorded as `observed`.

## Architecture state

197 endpoint tools across 18 resource families, 6 composite reads on `main`
(8 in the working tree), 1 status tool. Declarative `ToolSpec` drives registration,
so endpoint coverage grows without touching the executor.

Test suite is substantial: **292 test functions across 20 files** (~367 with
parametrization), including registry parity, docs drift, boundary enforcement and
write-path regression tests written after live-account defects.

## Delivery foundation

**This is the weakest area and it gates the rest.**

- The only CI workflow is `security-scan.yml`. **Nothing runs the test suite in CI.**
- No staging environment and no promotion path.
- `version` has never moved off `0.1.0`. No CHANGELOG, no tagging.
- The suite runs clean in `.venv` (**367 passed**). It fails only when invoked with
  the user-global interpreter, which drags in an unrelated OpenTelemetry conflict
  (`ReadableLogRecord`); the repo has no OpenTelemetry dependency at all. Nothing to
  fix in the repo — but it is one more reason CI must pin the interpreter.

The S43 contract is not met.

## Target state

Close the delivery foundation (S43, S44, S45) before expanding tool surface. Bring
S17 to complete via build identity, which is what makes deployment drift visible.
S06 reaches complete when composite outputs are typed rather than raw dicts.

S15 stays deliberately `partial`: polling via `get_changed_records` is the correct
choice under a 500 req/hour budget, not a gap to close.

## Gap → migration

Dependency-ordered. The foundation items are not optional prerequisites invented
for tidiness — without F015 and F020 the drift in #11 cannot be diagnosed at all.

```
F019 CI pipeline (S43/S44)
  └── F020 Release versioning & build provenance (S45)
        └── F015 Build identity & inventory contract (S17)
              └── CR2 diagnose deployment drift  [/investigate]
                    ├── F014 Compound candidate fact query   (issue #11)
                    ├── F016 Question-shaped recruiting reads (issue #12 Ph1)
                    ├── F017 Account vocabulary resources     (issue #12 Ph2)
                    └── F018 Typed response contract          (issue #12 Ph3)
```

F018 must land **after** F014 and F016 or it will be redone across tools that do
not exist yet.

## Findings

1. **The Product Graph had never been built.** `project.db` was 0 bytes. Thirteen
   shipped features across five merged PRs existed only as code and git history.
   Schema initialized and features registered as part of this baseline.

2. **Two composites are finished but uncommitted.** `get_candidate_activity` and
   `find_candidate_resume` are implemented, tested (22 tests) and documented in the
   working tree, never committed. Recorded as F013 `IN_PROGRESS`. They cannot be
   deployed, which partly explains the drift in #11 — and issue #12 proposes
   building `get_candidate_timeline`, which overlaps `get_candidate_activity`.

3. **Deployed inventory does not match the repository.** Connector exposed 186 CATS
   tools and no composites; repo documents 204 on `main`. Three live hypotheses
   (stale deploy, profile filtering, different commit). Undiagnosed — this is why
   CR2 is DISCOVERY rather than a fix.

4. **No build is traceable to a commit.** Root cause behind finding 3 being
   undiagnosable from outside: `cats://server/capabilities` reports version,
   discovery mode, tool count, transport and auth, but no git SHA, no FastMCP
   version, no composite names.

5. **A strong test suite that CI never runs** is a false sense of safety. 367 tests
   pass locally in `.venv`, and nothing enforces that on any change. The write-path
   defects fixed in `21d1abe` were found by running against a live account, and
   explicitly could not have been caught by mocked unit tests.

6. **Three FastMCP version floors disagree** across `pyproject.toml`,
   `requirements.txt` and `uv.lock` while v4 is current.

## Open questions

- Is the deployed connector meant to track `main`, or a pinned release? The answer
  changes whether F020 is a prerequisite for CR2 or merely useful.
- Should F016's seven composites be one feature or seven? Registered as an umbrella
  pending the `get_candidate_timeline` / `get_candidate_activity` reconciliation.
- Is there a second consumer of this adapter beyond StaffHive? The "one integration
  among several" framing implies yes, but nothing in the repo names one.
