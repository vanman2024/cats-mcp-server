# How to Add Prompts, Resources, and Templates to CATS MCP Server

## Current Architecture (DON'T BREAK IT!)

```
server_all_tools.py  <-- Main entry point (loads everything at import time)
├── toolsets_default.py
├── toolsets_recruiting.py
└── toolsets_data.py
```

## To Add Prompts/Resources/Templates

### 1. Create new files following the same pattern:

```
prompts_recruiting.py      <-- Prompts (email drafts, screening questions)
resources_candidates.py    <-- Resources (candidate profiles, job listings)
templates_emails.py        <-- Templates (email templates, static content)
```

**Pattern:** Each file has registration functions that take `mcp` and optionally `make_request`

### 2. Edit `server_all_tools.py` to import and register them

Add imports after the existing toolset imports (around line 134):

```python
# EXISTING IMPORTS (keep these)
from toolsets_default import (...)
from toolsets_recruiting import (...)
from toolsets_data import (...)

# NEW IMPORTS - Add these:
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
```

Then add registration calls after the existing tool registrations (around line 185):

```python
# EXISTING REGISTRATIONS (keep these)
register_events_tools(mcp)
print("  ✓ events (5 tools)")

print("\n✅ All 164 tools loaded!\n")

# NEW REGISTRATIONS - Add these BEFORE the success message:
print("Loading prompts and resources...")

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

# Templates
register_email_templates(mcp)
print("  ✓ email templates (5 templates)")

print("\n✅ All 164 tools + 4 prompts + 4 resources + 5 templates loaded!\n")
```

## Example Files Created

I've created 3 example files showing the pattern:

1. **`prompts_recruiting.py`** - Shows how to create prompts that:
   - Fetch live data from CATS API
   - Format prompts for LLMs
   - Use `@mcp.prompt()` decorator

2. **`resources_candidates.py`** - Shows how to create resources that:
   - Expose candidate/job data as readable resources
   - Format data as markdown/text for LLMs
   - Use `@mcp.resource("uri://pattern")` decorator

3. **`templates_emails.py`** - Shows how to create static templates that:
   - Provide reusable email templates
   - Can be used by prompts and tools
   - Use `@mcp.resource("template://category/name")` decorator

## Key Points

✅ **DO:**
- Follow the same pattern as toolsets (registration functions)
- Import and call registrations in `server_all_tools.py` at module load time
- Use type hints and `from __future__ import annotations`
- Add print statements so you see what's loaded

❌ **DON'T:**
- Break the existing toolset structure
- Register things conditionally (everything loads at import time)
- Forget to import the registration functions in `server_all_tools.py`

## Testing

After adding new prompts/resources:

1. Run locally: `python server_all_tools.py`
2. Check console output - should see your new registrations
3. Test with MCP inspector or Claude Desktop
4. Deploy to FastMCP Cloud (push to GitHub, redeploy via UI)

## Future: Single File Architecture

For NEW servers, consider putting everything in one `server.py` file:
- All tools inline (no imports)
- All prompts inline
- All resources inline
- All templates inline

Easier to understand, version control, and maintain. But don't change CATS now - it's already working!
