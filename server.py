"""
CATS MCP Server - FastMCP server for CATS API v3

Supports toolset-based loading for 189 endpoints across 17 toolsets.
Use --toolsets flag or CATS_TOOLSETS env var to control which tools load.
"""

import os
import sys
import argparse
import logging
from typing import Any, Optional, Set
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

try:
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
    HAS_RESPONSE_LIMITING = True
except ImportError:
    HAS_RESPONSE_LIMITING = False

load_dotenv()

# Configure structured logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("cats-mcp-server")

mcp = FastMCP("CATS API v3")
if HAS_RESPONSE_LIMITING:
    mcp.add_middleware(ResponseLimitingMiddleware(max_size=100_000))

# Configuration
CATS_API_BASE_URL = os.getenv("CATS_API_BASE_URL", "https://api.catsone.com/v3")
CATS_API_KEY = os.getenv("CATS_API_KEY", "")

# Toolset definitions
DEFAULT_TOOLSETS = ['candidates', 'jobs', 'pipelines', 'context', 'tasks']
ALL_TOOLSETS = DEFAULT_TOOLSETS + [
    'companies', 'contacts', 'activities', 'portals', 'work_history',
    'tags', 'webhooks', 'users', 'triggers', 'attachments', 'backups', 'events'
]


class CATSAPIError(Exception):
    """CATS API error"""
    pass


async def make_request(
    method: str,
    endpoint: str,
    params: dict = None,
    json_data: dict = None
) -> dict:
    """Make authenticated request to CATS API with enhanced error handling and logging"""
    if not CATS_API_KEY:
        logger.error("CATS_API_KEY not configured")
        raise CATSAPIError("CATS_API_KEY not configured")

    url = f"{CATS_API_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Token {CATS_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            logger.debug(f"Making {method} request to {endpoint}")
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data
            )
            response.raise_for_status()

            # Log rate limit information if available
            if "X-Rate-Limit-Remaining" in response.headers:
                remaining = response.headers["X-Rate-Limit-Remaining"]
                logger.debug(f"Rate limit remaining: {remaining}")

            # Handle empty responses (204 No Content, etc.)
            if response.status_code == 204 or not response.content:
                return {"status": "success", "status_code": response.status_code}

            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error {e.response.status_code} for {endpoint}: {e.response.text}")
            raise CATSAPIError(f"API HTTP error {e.response.status_code}: {e.response.text}")
        except httpx.TimeoutException as e:
            logger.error(f"Timeout error for {endpoint}: {e}")
            raise CATSAPIError(f"API timeout error: {e}")
        except httpx.HTTPError as e:
            logger.error(f"HTTP error for {endpoint}: {e}")
            raise CATSAPIError(f"API error: {e}")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for monitoring and load balancers"""
    from fastmcp.server.http import Response

    health_status = {
        "status": "healthy",
        "service": "CATS MCP Server",
        "api_configured": bool(CATS_API_KEY),
        "api_base_url": CATS_API_BASE_URL,
    }

    return Response(
        status_code=200,
        content=health_status,
        headers={"Content-Type": "application/json"}
    )


def load_toolsets(toolsets: Set[str]):
    """Load specified toolsets with structured logging"""
    from toolsets_default import (
        register_candidates_tools,
        register_jobs_tools,
        register_pipelines_tools,
        register_context_tools,
        register_tasks_tools
    )
    from toolsets_recruiting import (
        register_companies_tools,
        register_contacts_tools,
        register_activities_tools,
        register_portals_tools,
        register_work_history_tools
    )
    from toolsets_data import (
        register_tags_tools,
        register_webhooks_tools,
        register_users_tools,
        register_triggers_tools,
        register_attachments_tools,
        register_backups_tools,
        register_events_tools
    )

    logger.info(f"Loading toolsets: {', '.join(sorted(toolsets))}")

    # DEFAULT TOOLSETS (96 tools)
    if 'candidates' in toolsets or 'all' in toolsets:
        register_candidates_tools(mcp, make_request)
        logger.info("  ✓ candidates (43 tools)")

    if 'jobs' in toolsets or 'all' in toolsets:
        register_jobs_tools(mcp, make_request)
        logger.info("  ✓ jobs (33 tools)")

    if 'pipelines' in toolsets or 'all' in toolsets:
        register_pipelines_tools(mcp, make_request)
        logger.info("  ✓ pipelines (12 tools)")

    if 'context' in toolsets or 'all' in toolsets:
        register_context_tools(mcp, make_request)
        logger.info("  ✓ context (3 tools)")

    if 'tasks' in toolsets or 'all' in toolsets:
        register_tasks_tools(mcp, make_request)
        logger.info("  ✓ tasks (5 tools)")

    # RECRUITING TOOLSETS (75 tools)
    if 'companies' in toolsets or 'all' in toolsets:
        register_companies_tools(mcp, make_request)
        logger.info("  ✓ companies (30 tools)")

    if 'contacts' in toolsets or 'all' in toolsets:
        register_contacts_tools(mcp, make_request)
        logger.info("  ✓ contacts (28 tools)")

    if 'activities' in toolsets or 'all' in toolsets:
        register_activities_tools(mcp, make_request)
        logger.info("  ✓ activities (6 tools)")

    if 'portals' in toolsets or 'all' in toolsets:
        register_portals_tools(mcp, make_request)
        logger.info("  ✓ portals (8 tools)")

    if 'work_history' in toolsets or 'all' in toolsets:
        register_work_history_tools(mcp, make_request)
        logger.info("  ✓ work_history (3 tools)")

    # DATA & CONFIG TOOLSETS (18 tools)
    if 'tags' in toolsets or 'all' in toolsets:
        register_tags_tools(mcp, make_request)
        logger.info("  ✓ tags (2 tools)")

    if 'webhooks' in toolsets or 'all' in toolsets:
        register_webhooks_tools(mcp, make_request)
        logger.info("  ✓ webhooks (4 tools)")

    if 'users' in toolsets or 'all' in toolsets:
        register_users_tools(mcp, make_request)
        logger.info("  ✓ users (2 tools)")

    if 'triggers' in toolsets or 'all' in toolsets:
        register_triggers_tools(mcp, make_request)
        logger.info("  ✓ triggers (2 tools)")

    if 'attachments' in toolsets or 'all' in toolsets:
        register_attachments_tools(mcp, make_request)
        logger.info("  ✓ attachments (4 tools)")

    if 'backups' in toolsets or 'all' in toolsets:
        register_backups_tools(mcp, make_request)
        logger.info("  ✓ backups (3 tools)")

    if 'events' in toolsets or 'all' in toolsets:
        register_events_tools(mcp, make_request)
        logger.info("  ✓ events (1 tool)")

    # Calculate and display total
    loaded_count = len(toolsets) if 'all' not in toolsets else len(ALL_TOOLSETS)
    logger.info(f"\nTotal toolsets loaded: {loaded_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CATS API v3 MCP Server")
    parser.add_argument(
        "--toolsets",
        type=str,
        default=None,
        help="Comma-separated list of toolsets to load (default: core set)"
    )
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="List available toolsets and exit"
    )

    args = parser.parse_args()

    if args.list_toolsets:
        print("CATS API v3 MCP Server - Available Toolsets\n")
        print("DEFAULT toolsets (loaded by default, 96 tools):")
        print("  - candidates (43 tools) - Core recruiting")
        print("  - jobs (33 tools) - Job management")
        print("  - pipelines (12 tools) - Workflow management")
        print("  - context (3 tools) - Site/user info")
        print("  - tasks (5 tools) - Task management")
        print("\nRECRUITING toolsets (75 tools):")
        print("  - companies (30 tools)")
        print("  - contacts (28 tools)")
        print("  - activities (6 tools)")
        print("  - portals (8 tools)")
        print("  - work_history (3 tools)")
        print("\nDATA & CONFIG toolsets (18 tools):")
        print("  - tags (2 tools)")
        print("  - webhooks (4 tools)")
        print("  - users (2 tools)")
        print("  - triggers (2 tools)")
        print("  - attachments (4 tools)")
        print("  - backups (3 tools)")
        print("  - events (1 tool)")
        print("\nUsage:")
        print("  python server.py                                    # Default toolsets")
        print("  python server.py --toolsets candidates,jobs          # Specific toolsets")
        print("  python server.py --toolsets all                      # All 189 tools")
        print("  export CATS_TOOLSETS='candidates,companies' && python server.py")
        exit(0)

    # Determine which toolsets to load
    if args.toolsets:
        requested = {t.strip() for t in args.toolsets.split(',')}
    else:
        env_toolsets = os.getenv('CATS_TOOLSETS', '')
        if env_toolsets:
            requested = {t.strip() for t in env_toolsets.split(',')}
        else:
            requested = set(DEFAULT_TOOLSETS)

    # Validate toolsets
    if 'all' not in requested:
        invalid_toolsets = requested - set(ALL_TOOLSETS)
        if invalid_toolsets:
            logger.error(f"Invalid toolsets specified: {', '.join(invalid_toolsets)}")
            logger.error(f"Valid toolsets: {', '.join(ALL_TOOLSETS)}")
            logger.error("Use --list-toolsets to see all available toolsets")
            exit(1)

    # Load toolsets
    load_toolsets(requested)

    # Determine transport mode
    transport = os.getenv('CATS_TRANSPORT', 'stdio').lower()

    logger.info("\nStarting CATS MCP Server...")
    logger.info(f"Transport: {transport.upper()}")
    logger.info(f"API Base URL: {CATS_API_BASE_URL}")
    logger.info(f"API Key configured: {'Yes' if CATS_API_KEY else 'No'}")

    if transport == 'stdio':
        # STDIO transport for Claude Desktop, Claude Code, Cursor
        # Configured via .mcp.json or IDE config files
        logger.info("Server ready for STDIO connections")
        mcp.run()
    elif transport == 'http':
        # HTTP transport for remote services, web applications
        port = int(os.getenv('CATS_PORT', '8000'))
        host = os.getenv('CATS_HOST', '0.0.0.0')
        logger.info(f"HTTP Server starting at http://{host}:{port}/mcp")
        logger.info(f"Health check endpoint: http://{host}:{port}/health")
        mcp.run(transport="http", host=host, port=port)
    else:
        logger.error(f"Invalid transport '{transport}'. Use 'stdio' or 'http'")
        logger.error("Set CATS_TRANSPORT environment variable to 'stdio' or 'http'")
        exit(1)
