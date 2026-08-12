"""Backward-compatible entrypoint.

The implementation lives in `src/cats_mcp/`. This module exists so that existing
MCP client configurations pointing at `server.py` keep working.

Unlike the previous version of this file, importing it registers every tool:
tool registration used to happen only inside `if __name__ == "__main__"`, so
`fastmcp run server.py` and `server:mcp` both produced an empty server.
"""

from __future__ import annotations

from cats_mcp.app import main, mcp, settings

__all__ = ["main", "mcp", "settings"]


if __name__ == "__main__":
    main()
