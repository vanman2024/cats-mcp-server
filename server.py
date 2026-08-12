"""Backward-compatible entrypoint.

The implementation lives in `src/cats_mcp/`. This module exists so that existing
MCP client configurations pointing at `server.py` keep working.

Unlike the previous version of this file, importing it registers every tool:
tool registration used to happen only inside `if __name__ == "__main__"`, so
`fastmcp run server.py` and `server:mcp` both produced an empty server.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Work whether or not the project was installed as a package. A deployment that
# only installs a dependency file leaves `src/` off the path, and this file
# would then fail at import with a bare ModuleNotFoundError.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cats_mcp.app import main, mcp, settings  # noqa: E402

__all__ = ["main", "mcp", "settings"]


if __name__ == "__main__":
    main()
