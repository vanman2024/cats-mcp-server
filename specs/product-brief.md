# CATS MCP Adapter — product brief

**Status:** reconstructed from code at commit `3df42da`, 2026-08-17.
Sections marked **OPEN** are genuinely undecided in the repository and must be
answered by a human. They are deliberately left blank rather than inferred — an
invented answer here is worse than an absent one, because it gets planned against.

Companion to `baseline.md`, which answers what this system *is*. This answers what
it is *for*.

---

## Problem

Agents and orchestrators that need CATS data face an API-shaped surface: 197
endpoints, a 500 req/hour budget, HAL pagination, account-specific identifiers, and
single-criterion filters. Answering one ordinary recruiting question costs many
calls, local joins, and a large share of the request budget.

Issue #11 is the canonical demonstration: "BC heavy-equipment mechanics with contact
info" required 17 paginated calls, client-side dedup and local title matching — and
still could not honestly be called a correct answer.

## Who it is for

Calling agents and orchestrators, not end users. The consumer is software.

**OPEN** — which orchestrators, beyond StaffHive? The adapter's own framing ("one
integration among several; CATS is one possible ATS") implies a multi-ATS strategy
with more than one consumer, but nothing in the repository names a second one. This
answer determines how much generality the adapter owes: a single-consumer adapter
can bend toward its caller, a multi-consumer one cannot.

## What it does

Exposes CATS API v3 as typed MCP tools, resources and prompts:

- 197 endpoint tools across 18 resource families, from one declarative spec
- composite read primitives that batch expensive reads into one compact result
- writes confirmed by reading the change back before reporting success
- HAL pagination, retry, backoff and live rate-limit accounting
- three discovery modes (raw / search / code) that change visibility, never callability
- attachments returned as documents a model can actually read

## Differentiation

The **facts/judgment boundary**, enforced by tests rather than convention. The
adapter returns what CATS would confirm and match evidence for why a record
appeared; it never returns a recommendation. Recruiting policy, ranking and outreach
belong to the caller.

This is the reason the adapter can be shared across consumers with different
policies — and the reason both open issues repeat "no `should_contact`" unprompted.
It is the product's actual moat, and it is the thing most likely to erode under
pressure from a caller who wants one convenient exception.

## Domains

See `baseline.md`. Seven: Endpoint Registry, Composite Reads, HTTP & Rate Limiting,
Discovery & Profiles, Auth & Credentials, Resources & Responses, Observability.

## Commercial model

**OPEN.** Nothing in the repository indicates pricing, tiers, metering or whether
this is an internal component or a sold product. The S50 signals the scanner
reported were false positives (`cac` inside "caching", `arr` inside "arrives").

## AI requirements

No LLM inside the adapter — an explicit non-goal in issue #12. The adapter's job is
to make an external model's job cheap and deterministic: precise schemas, compact
responses, match evidence, and honest `unknown` rather than inference.

The design constraint that follows: every output must be machine-checkable. That is
what makes the typed response contract (F018) a product requirement rather than a
tidiness exercise.

## Integrations

CATS (CatsOne) API v3 — the sole upstream. Auth via JWT with per-tool scopes;
credentials resolved per request.

**OPEN** — is a second ATS actually planned? The abstraction is written as if yes.
If no, some generality currently being paid for is dead weight.

## Constraints

- **500 requests/hour.** The binding constraint on every design decision. It is why
  composites exist, why polling beats webhooks here, and why any new primitive must
  report `requests_used`.
- Stateless. No mirroring of CATS into a second database.
- No recruiting policy, ranking, outreach, or customer-specific taxonomy.
- Responses must stay small enough to be model-readable.

## Environment and delivery

Deployed as a FastMCP Cloud connector. **The S43 contract is not met** — no CI test
run, no staging, no promotion path, no release versioning. See `baseline.md`.

**OPEN** — should the connector track `main` or a pinned release?

## Go to market

**OPEN.** Not a code artifact and not answerable from the repository.

## Open questions

1. Who consumes this besides StaffHive, and is a second ATS real?
2. Is this an internal component or a product with a commercial model?
3. Does the connector track `main` or a release?
4. Who is the decision-maker when a caller asks the adapter to cross the
   facts/judgment boundary? That boundary is the differentiation; it needs a
   named owner, not just a test.
