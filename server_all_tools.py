"""Backward-compatible entrypoint for deployments configured against this path.

The implementation lives in `src/cats_mcp/`. This file exists only so that a
deployment whose entrypoint is still set to `server_all_tools.py` keeps
building - Horizon reads its entrypoint from its own server configuration, not
from `fastmcp.json`, so moving the file broke the build even though the repo's
own config was updated.

The original 169-line implementation is in git history. It registered every
toolset at import time as a workaround for `server.py` registering none, and
diverged into a second request implementation with `print()`-based logging.
Both problems are gone; this is a re-export, not a second server.

Preferred entrypoint: `src/cats_mcp/app.py` (object `mcp`). Update the
entrypoint in the Horizon UI when convenient - it takes effect on the next
build - and this file can then be deleted.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Work whether or not the project was installed as a package. A build that only
# installs a dependency file leaves `src/` off the path, and this file would
# then fail at import with a bare ModuleNotFoundError.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cats_mcp.app import main, mcp, settings  # noqa: E402

__all__ = ["main", "mcp", "settings"]


if __name__ == "__main__":
    main()
