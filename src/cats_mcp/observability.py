"""Cross-cutting middleware: timing, caching, and rate limiting.

Three problems this addresses, all observed rather than imagined:

* **"Everything is slow."** Nobody could say whether the time went to CATS, to
  this server, or to the model deciding what to call next. `TimingMiddleware`
  logs per-call duration, which turns that from an argument into a number.
* **Repeated reference lookups.** Workflow definitions and custom field ids get
  re-fetched constantly because every account-specific id has to be resolved
  before it can be used. Those answers change when an administrator edits a
  setting, not during a recruiting session.
Why there is no rate limiting here
----------------------------------
Parallel agents sharing one CATS account is a real problem - ten agents fan out,
nothing coordinates them, and the first symptom is 429s halfway through a run.
`RateLimitingMiddleware` is the wrong instrument for it, twice over.

It counts *MCP messages*, not CATS requests. Those do not correspond: a
`get_connection_status` call makes zero CATS requests and a paginated sweep makes
three. And because it counts every message, pacing at the CATS rate (500/hour is
0.14/s) throttles the protocol itself - the `initialize` handshake is refused and
the connection never opens. That is not a hypothetical; it is what the test suite
did when this was tried.

Pacing belongs in `CATSClient.request`, where the outbound calls actually happen.
Until that exists, this adapter continues to observe the budget and report it
through `get_connection_status` and `cats://account/rate-limit`, leaving the
decision to slow down with the caller - which is the documented contract.

What is deliberately *not* cached
---------------------------------
Record data. Never candidates, pipelines, activities or list membership.

A recruiter screens a list, someone is added to Do Not Contact, the screen runs
again a minute later and returns the stale answer. On a DNC list that means
contacting somebody who asked not to be - the exact failure the rest of this
codebase spends its effort preventing. A faster wrong answer is not an
improvement.

Only *reference* data is cacheable: workflow definitions, custom field
definitions, job statuses. Those describe how the account is configured rather
than who is in it, and they change when an administrator edits a setting, not
when a recruiter does their job.

`ResponseLimitingMiddleware` is also deliberately unused. It truncates oversized
tool results, which for `download_attachment` would mean silently returning a
corrupt base64 document. Response size is already controlled where it can be
done safely: compact projections by default, and a hard byte cap on attachments.
"""

from __future__ import annotations

from typing import Any

from cats_mcp.config import Settings
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)

#: Reference-data tools whose results are safe to cache.
#:
#: Each describes account *configuration* rather than account *records*. Nothing
#: here changes as a result of recruiting activity, so a stale answer misleads
#: nobody. Adding a tool that returns records to this list would reintroduce the
#: staleness bug this module's docstring exists to warn about; a test enforces
#: that every entry is classified READ and returns configuration.
CACHEABLE_REFERENCE_TOOLS: tuple[str, ...] = (
    "list_pipeline_workflows",
    "list_pipeline_workflow_statuses",
    "list_job_statuses",
    "list_candidate_custom_field_definitions",
    "list_job_custom_field_definitions",
    "get_candidate_custom_field_definition",
    "get_job_custom_field_definition",
)

#: Reference data changes when an administrator edits a setting. Ten minutes is
#: long enough to help a batch of lookups and short enough that a real edit
#: shows up within one coffee.
REFERENCE_TTL_SECONDS = 600

#: Why component listings are not cached
#: -------------------------------------
#: It looks like free money - the catalog is built once at startup - and it is
#: not.
#:
#: The search transform builds its BM25 index from the tool listing. Cache the
#: listing and the index gets built from the already-transformed set, so
#: searching returns `search_tools` itself instead of the tool asked for.
#: Discovery stops working, silently. The authorization suite caught this.
#:
#: Per-session visibility has the same problem in a worse form: progressive
#: disclosure depends on `list_tools` reflecting what this session can currently
#: see, and a cache serves another session's answer.
#:
#: The saving was imaginary anyway. The ~0.9s belongs to `create_server()`,
#: which runs once; `list_tools` after that measures 0.02s.


def build_middleware(settings: Settings) -> list[Any]:
    """Build the middleware stack for the configured settings.

    Order matters: middleware runs in registration order inbound and reverse
    order outbound, so timing wraps everything and reports the true total
    including any time spent waiting on the rate limiter.
    """
    stack: list[Any] = []

    if settings.log_timing:
        from fastmcp.server.middleware.timing import TimingMiddleware

        stack.append(TimingMiddleware(logger=get_logger("cats_mcp.timing")))

    if settings.cache_reference_data:
        from fastmcp.server.middleware.caching import ResponseCachingMiddleware

        stack.append(
            ResponseCachingMiddleware(
                # Component listings are deliberately NOT cached. See below.
                list_tools_settings={"enabled": False},
                list_resources_settings={"enabled": False},
                list_prompts_settings={"enabled": False},
                # Allowlist, never a blocklist: a new tool must be considered
                # deliberately before its results are ever served from cache.
                call_tool_settings={
                    "enabled": True,
                    "ttl": REFERENCE_TTL_SECONDS,
                    "included_tools": list(CACHEABLE_REFERENCE_TOOLS),
                },
            )
        )

    return stack
