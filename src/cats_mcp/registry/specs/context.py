"""Context tool specifications.

Generated from the pre-refactor hand-written toolsets, then reviewed. Edit this
file directly; it is the source of truth for these tools.
"""

from __future__ import annotations

from typing import Any, Optional  # noqa: F401  (used in parameter annotations)

from cats_mcp.registry.models import (
    ResponseStrategy,
    Safety,
    ToolSpec,
    Transform,  # noqa: F401  (used by some specs)
)

SPECS: list[ToolSpec] = [
    ToolSpec(
        name="get_site",
        resource="site",
        operation="get",
        method="GET",
        endpoint="/site",
        description="Retrieve information about the connected CATS account. Use this to confirm which account the server is talking to and that the connection works.\n\nWraps: GET /site",
        safety=Safety.READ,
        response=ResponseStrategy.RAW,
        toolset="context",
    ),
]
