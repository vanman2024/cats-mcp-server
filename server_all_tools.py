"""
CATS MCP Server - All Tools Loaded at Import Time
"""
import os
import asyncio
from typing import Any, Optional
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

try:
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
    HAS_RESPONSE_LIMITING = True
except ImportError:
    HAS_RESPONSE_LIMITING = False

load_dotenv()

# Configuration
CATS_API_BASE_URL = os.getenv("CATS_API_BASE_URL", "https://api.catsone.com/v3")
CATS_API_KEY = os.getenv("CATS_API_KEY", "")

mcp = FastMCP("CATS API v3")
if HAS_RESPONSE_LIMITING:
    mcp.add_middleware(ResponseLimitingMiddleware(max_size=100_000))

class CATSAPIError(Exception):
    """CATS API error"""
    pass

async def make_request(
    method: str, 
    endpoint: str, 
    params: Optional[dict[str, Any]] = None, 
    json_data: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """
    Make authenticated request to CATS API with exponential backoff retry
    
    Retry strategy:
    - 429 (Rate Limit): Retry with exponential backoff (1s → 2s → 4s → 8s)
    - 5xx (Server Error): Retry up to 3 times
    - Other errors: Fail immediately
    """
    if not CATS_API_KEY:
        raise CATSAPIError("CATS_API_KEY not configured")
    
    url = f"{CATS_API_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Token {CATS_API_KEY}",
        "Content-Type": "application/json",
    }
    
    max_retries = 4
    base_delay = 1.0  # seconds
    
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(method, url, headers=headers, params=params, json=json_data)
                
                # Check for rate limiting
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)  # Exponential backoff: 1s, 2s, 4s, 8s
                        print(f"⚠️  Rate limit hit, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise CATSAPIError("Rate limit exceeded after max retries")
                
                # Check for server errors (5xx)
                if 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"⚠️  Server error {response.status_code}, retrying in {delay}s")
                        await asyncio.sleep(delay)
                        continue
                
                # Raise for other HTTP errors
                response.raise_for_status()

                # Handle empty responses (204 No Content, etc.)
                if response.status_code == 204 or not response.content:
                    return {"status": "success", "status_code": response.status_code}

                return response.json()
                
        except httpx.TimeoutException as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️  Timeout, retrying in {delay}s")
                await asyncio.sleep(delay)
                continue
            raise CATSAPIError(f"Request timeout after {max_retries} attempts: {e}")
        except httpx.HTTPStatusError as e:
            # Don't retry on client errors (4xx except 429)
            if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                raise CATSAPIError(f"API error: {e}")
            # Retry on network errors
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️  Network error, retrying in {delay}s")
                await asyncio.sleep(delay)
                continue
            raise CATSAPIError(f"API error after {max_retries} attempts: {e}")
        except httpx.HTTPError as e:
            # Retry on other HTTP errors (network issues, etc.)
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️  HTTP error, retrying in {delay}s")
                await asyncio.sleep(delay)
                continue
            raise CATSAPIError(f"HTTP error after {max_retries} attempts: {e}")
    
    # Should never reach here, but satisfy type checker
    raise CATSAPIError(f"Request failed after {max_retries} attempts")

# LOAD ALL TOOLSETS AT MODULE IMPORT TIME
print("Loading all CATS toolsets...")

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

# Register all toolsets immediately
register_candidates_tools(mcp, make_request)
register_jobs_tools(mcp, make_request)
register_pipelines_tools(mcp, make_request)
register_context_tools(mcp, make_request)
register_tasks_tools(mcp, make_request)
register_companies_tools(mcp, make_request)
register_contacts_tools(mcp, make_request)
register_activities_tools(mcp, make_request)
register_portals_tools(mcp, make_request)
register_work_history_tools(mcp, make_request)
register_tags_tools(mcp, make_request)
register_webhooks_tools(mcp, make_request)
register_users_tools(mcp, make_request)
register_triggers_tools(mcp, make_request)
register_attachments_tools(mcp, make_request)
register_backups_tools(mcp, make_request)
register_events_tools(mcp, make_request)

print("✅ All 189 CATS API tools loaded")

if __name__ == "__main__":
    # Just run the server - tools already registered
    mcp.run(transport="stdio")
