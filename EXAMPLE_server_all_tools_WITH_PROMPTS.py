"""
EXAMPLE: How to modify server_all_tools.py to add prompts/resources/templates
This shows the changes needed - DON'T use this file directly, just copy the pattern
"""
from __future__ import annotations

import os
import asyncio
from typing import Any, Optional, Callable, Awaitable
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# Configuration
CATS_API_BASE_URL = os.getenv("CATS_API_BASE_URL", "https://api.catsone.com/v3")
CATS_API_KEY = os.getenv("CATS_API_KEY", "")

mcp = FastMCP("CATS API v3")

class CATSAPIError(Exception):
    """CATS API error"""
    pass

async def make_request(
    method: str,
    endpoint: str,
    params: Optional[dict[str, Any]] = None,
    json_data: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Make authenticated request to CATS API with exponential backoff retry"""
    # ... (keep existing implementation)
    pass

# LOAD ALL TOOLSETS AT MODULE IMPORT TIME
print("Loading all CATS toolsets...")

# ==========================================
# EXISTING IMPORTS - Keep these
# ==========================================
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

# ==========================================
# NEW IMPORTS - Add these for prompts/resources/templates
# ==========================================
from prompts_recruiting import (
    register_recruiting_prompts,
    register_interview_prompts
)
from resources_candidates import (
    register_candidate_resources,
    register_job_resources
)
from templates_emails import (
    register_email_templates
)

# ==========================================
# EXISTING TOOL REGISTRATIONS - Keep all these
# ==========================================
register_candidates_tools(mcp, make_request)
print("  ✓ candidates (28 tools)")

register_jobs_tools(mcp, make_request)
print("  ✓ jobs (40 tools)")

register_pipelines_tools(mcp, make_request)
print("  ✓ pipelines (13 tools)")

register_context_tools(mcp, make_request)
print("  ✓ context (3 tools)")

register_tasks_tools(mcp, make_request)
print("  ✓ tasks (5 tools)")

register_companies_tools(mcp, make_request)
print("  ✓ companies (18 tools)")

register_contacts_tools(mcp, make_request)
print("  ✓ contacts (18 tools)")

register_activities_tools(mcp, make_request)
print("  ✓ activities (6 tools)")

register_portals_tools(mcp, make_request)
print("  ✓ portals (8 tools)")

register_work_history_tools(mcp, make_request)
print("  ✓ work_history (3 tools)")

register_tags_tools(mcp)
print("  ✓ tags (2 tools)")

register_webhooks_tools(mcp)
print("  ✓ webhooks (4 tools)")

register_users_tools(mcp)
print("  ✓ users (2 tools)")

register_triggers_tools(mcp)
print("  ✓ triggers (2 tools)")

register_attachments_tools(mcp)
print("  ✓ attachments (4 tools)")

register_backups_tools(mcp)
print("  ✓ backups (3 tools)")

register_events_tools(mcp)
print("  ✓ events (5 tools)")

# ==========================================
# NEW REGISTRATIONS - Add prompts/resources/templates here
# ==========================================
print("\nLoading prompts and resources...")

# Prompts
register_recruiting_prompts(mcp, make_request)
print("  ✓ recruiting prompts (3 prompts)")

register_interview_prompts(mcp, make_request)
print("  ✓ interview prompts (1 prompt)")

# Resources
register_candidate_resources(mcp, make_request)
print("  ✓ candidate resources (2 resources)")

register_job_resources(mcp, make_request)
print("  ✓ job resources (2 resources)")

# Templates (static content - no make_request needed)
register_email_templates(mcp)
print("  ✓ email templates (5 templates)")

# ==========================================
# UPDATED SUCCESS MESSAGE
# ==========================================
print("\n✅ All loaded:")
print("   - 164 tools")
print("   - 4 prompts")
print("   - 4 resources")
print("   - 5 templates\n")

if __name__ == "__main__":
    # Just run the server - everything already registered at import time
    mcp.run(transport="stdio")
