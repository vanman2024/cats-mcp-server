"""Canonical entrypoint.

`fastmcp.json` points here. Tools are registered at import time by
`create_server()`, so importing this module always yields a fully populated
server - unlike the previous `server.py`, which registered nothing unless run
as `__main__`.
"""

from __future__ import annotations

from cats_mcp.config import Transport, load_settings
from cats_mcp.server import create_server

settings = load_settings()
mcp = create_server(settings)


def main() -> None:
    """Run the server on the configured transport."""
    if settings.transport is Transport.HTTP:
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
