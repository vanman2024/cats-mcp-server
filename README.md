# CATS MCP Server

FastMCP server for CATS (Complete Applicant Tracking System) API v3.

## Overview

This MCP server provides **164 tools** across **17 toolsets** covering the complete CATS API v3.

**Key Features:**
- 🎯 **164 MCP tools** covering all CATS API v3 endpoints
- 📦 **17 toolsets** organized by resource type
- 🔐 **Secure** - token-based authentication
- 📝 **Type-safe** - full type hints on all tools
- 🚀 **Production-ready** - error handling, async/await, retry logic
- ☁️ **FastMCP Cloud ready** - automatic deployments from GitHub

## Documentation

- **[Deployment Guide](docs/deployment/DEPLOYMENT.md)** - FastMCP Cloud, STDIO, HTTP, Auth
- **[Integration Guide](docs/guides/INTEGRATION_GUIDE.md)** - Claude Desktop, Claude Code, web apps
- **[How to Add Prompts/Resources](docs/guides/HOW_TO_ADD_PROMPTS_RESOURCES.md)** - Extend the server
- **[API Quick Reference](docs/reference/QUICK_REFERENCE.md)** - All 164 tools

## Quick Start

### Option 1: FastMCP Cloud (Recommended)

1. Push to GitHub
2. Connect at https://fastmcp.app
3. Set `CATS_API_KEY` environment variable
4. Deploy

See [Deployment Guide](docs/deployment/DEPLOYMENT.md) for details.

### Option 2: Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your CATS_API_KEY

# Run
python server_all_tools.py
```

## Toolsets

**Core (89 tools)**
- Candidates (28), Jobs (40), Pipelines (13), Context (3), Tasks (5)

**Recruiting (53 tools)**
- Companies (18), Contacts (18), Activities (6), Portals (8), Work History (3)

**Data & Config (22 tools)**
- Tags (2), Webhooks (4), Users (2), Triggers (2), Attachments (4), Backups (3), Events (5)

See [API Reference](docs/reference/QUICK_REFERENCE.md) for complete tool list.

## Architecture

```
server_all_tools.py          # Main entry - loads all 164 tools
├── toolsets_default.py      # Core tools
├── toolsets_recruiting.py   # Recruiting tools
├── toolsets_data.py         # Data & config tools
├── prompts_*.py            # (Optional) Prompts
├── resources_*.py          # (Optional) Resources
└── templates_*.py          # (Optional) Templates
```

See [How to Add Prompts/Resources](docs/guides/HOW_TO_ADD_PROMPTS_RESOURCES.md) to extend.

## Resources

- **CATS API:** https://docs.catsone.com/api/v3/
- **FastMCP:** https://gofastmcp.com/
- **MCP Protocol:** https://modelcontextprotocol.io/
