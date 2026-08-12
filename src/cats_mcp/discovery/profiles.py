"""Discovery profiles - how the tool catalog is presented to a client.

The catalog is large by design: comprehensive, atomic coverage of the CATS API
is what makes this adapter reusable by any orchestrator. But 184 tool schemas
must not all land in a model's context.

    raw     everything visible. For orchestrators (Mastra, ADK) that run their
            own cross-server tool discovery. Protected; never the public default.
    search  BM25 index in front of the catalog. The model sees two meta-tools
            plus a couple of pinned ones. For direct clients.
    code    Code Mode sandbox. Optional extra, experimental.

Do not stack this server's search transform with an orchestrator's own tool
search. An orchestrator should consume `raw` and index across every connected
server itself; running both means searching an index of an index.
"""

from __future__ import annotations

from typing import Any

from cats_mcp.config import DiscoveryMode, Settings
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)

#: Tools always visible in `search` mode, alongside the search/call meta-tools.
#:
#: Deliberately minimal and deliberately *not* `get_me`: the audit found that
#: tool called `GET /users/current`, which does not exist in CATS v3 and returns
#: 404. Pinning a broken tool as the entry point to the whole server would be
#: the worst possible choice. `get_site` is the working equivalent.
PINNED_TOOLS: tuple[str, ...] = ("get_site",)


class CodeModeUnavailableError(RuntimeError):
    """Code Mode was requested but the optional extra is not installed."""


def _build_search_transform(settings: Settings) -> Any:
    from fastmcp.server.transforms.search import BM25SearchTransform

    return BM25SearchTransform(
        # Conservative on purpose. Returning many candidate tools re-creates the
        # context problem the transform exists to solve.
        max_results=settings.search_max_results,
        always_visible=list(PINNED_TOOLS),
    )


def _build_code_transform() -> Any:
    try:
        from fastmcp.server.transforms.code_mode import CodeModeTransform
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise CodeModeUnavailableError(
            "CATS_DISCOVERY_MODE=code requires the optional `code-mode` extra, "
            "which is not installed. Install it with:\n"
            "    uv pip install 'cats-mcp-server[code-mode]'\n"
            "Code Mode is experimental. If you are driving this server from an "
            "orchestrator that already composes tool calls, use "
            "CATS_DISCOVERY_MODE=raw instead."
        ) from exc
    return CodeModeTransform()


def transforms_for(settings: Settings) -> list[Any]:
    """Build the transform list for the configured discovery mode.

    Failing here at startup is intentional: a misconfigured discovery mode that
    only surfaced on the first tool call would look like a tool bug.
    """
    mode = settings.discovery_mode

    if mode is DiscoveryMode.RAW:
        logger.info(
            "discovery=raw: exposing the full catalog. Ensure this deployment is "
            "authenticated - raw must not be publicly reachable."
        )
        return []

    if mode is DiscoveryMode.SEARCH:
        logger.info(
            "discovery=search: exposing search_tools/call_tool with max_results=%d, pinned=%s",
            settings.search_max_results,
            ", ".join(PINNED_TOOLS),
        )
        return [_build_search_transform(settings)]

    logger.warning("discovery=code: Code Mode is experimental.")
    return [_build_code_transform()]
