# FastMCP Cloud Deployment Guide

This guide covers deploying the CATS MCP Server to FastMCP Cloud, a managed hosting platform for MCP servers.

## Overview

FastMCP Cloud provides automated deployment and hosting for FastMCP servers with:

- **Automatic Deployments**: Push to GitHub main branch to deploy
- **Preview URLs**: Pull requests get unique preview URLs
- **Managed Infrastructure**: No servers to manage
- **Environment Variables**: Secure storage for API keys
- **Custom Domains**: Use your own domain (optional)

## Prerequisites

1. **GitHub Account**: Your code must be in a GitHub repository
2. **CATS API Key**: Get from your CATS account (Administration → API Keys)
3. **FastMCP Cloud Account**: Sign up at https://fastmcp.app

## Deployment Steps

### Step 1: Prepare Your Repository

Ensure your repository contains:

```
cats-mcp-server/
├── server.py                  # Main server file
├── toolsets_default.py        # Default toolsets
├── toolsets_recruiting.py     # Recruiting toolsets
├── toolsets_data.py           # Data toolsets
├── fastmcp.json              # FastMCP Cloud configuration
├── requirements.txt          # Python dependencies
└── .env.example              # Environment template
```

**Important**: Ensure `fastmcp.json` is present in your repository root (it was created automatically).

### Step 2: Create FastMCP Cloud Project

1. Go to https://fastmcp.app and log in
2. Click "New Project"
3. Connect your GitHub repository
4. Configure your project:

   **Project Settings:**
   - **Project Name**: `cats-mcp-server` (generates URL: `https://cats-mcp-server.fastmcp.app`)
   - **Repository**: Select your GitHub repository
   - **Branch**: `main`
   - **Entrypoint**: `server.py` or `server.py:mcp`
   - **Authentication**: Choose based on your needs:
     - Public: Anyone with the URL can access
     - Organization-only: Restrict to your organization

### Step 3: Configure Environment Variables

In the FastMCP Cloud project settings, add these environment variables:

**Required:**
```
CATS_API_KEY=your_actual_cats_api_key_here
```

**Optional (with defaults):**
```
CATS_API_BASE_URL=https://api.catsone.com/v3
CATS_TOOLSETS=candidates,jobs,pipelines
LOG_LEVEL=INFO
```

**Note**: You do NOT need to set `CATS_TRANSPORT`, `CATS_HOST`, or `CATS_PORT` - these are automatically configured by `fastmcp.json`.

### Step 4: Deploy

FastMCP Cloud will automatically:

1. Clone your repository
2. Install dependencies from `requirements.txt`
3. Use `fastmcp.json` configuration
4. Deploy to your unique URL

**Deployment URL**: `https://your-project-name.fastmcp.app/mcp`

**Health Check**: `https://your-project-name.fastmcp.app/health`

### Step 5: Connect Clients

Once deployed, connect from any MCP client:

**Claude Desktop** (`~/.claude/config.json`):
```json
{
  "mcpServers": {
    "cats": {
      "url": "https://your-project-name.fastmcp.app/mcp"
    }
  }
}
```

**Claude Code** (`.mcp.json`):
```json
{
  "mcpServers": {
    "cats": {
      "url": "https://your-project-name.fastmcp.app/mcp"
    }
  }
}
```

**Python Client**:
```python
from fastmcp.client import Client

async with Client("https://your-project-name.fastmcp.app/mcp") as client:
    tools = await client.list_tools()
    print(f"Available tools: {len(tools)}")
```

## Configuration Details

### fastmcp.json Explained

The `fastmcp.json` file tells FastMCP Cloud how to run your server:

```json
{
  "$schema": "https://gofastmcp.com/public/schemas/fastmcp.json/v1.json",
  "source": {
    "type": "filesystem",
    "path": "server.py",
    "entrypoint": "mcp"
  },
  "environment": {
    "type": "uv",
    "python": ">=3.10",
    "dependencies": [
      "fastmcp>=2.0.0",
      "httpx>=0.27.0",
      "pydantic>=2.0.0",
      "python-dotenv>=1.0.0"
    ]
  },
  "deployment": {
    "transport": "http",
    "host": "0.0.0.0",
    "port": 3000,
    "path": "/mcp/",
    "log_level": "INFO",
    "env": {
      "CATS_API_BASE_URL": "https://api.catsone.com/v3",
      "CATS_API_KEY": "${CATS_API_KEY}",
      "CATS_TOOLSETS": "${CATS_TOOLSETS:-candidates,jobs,pipelines}",
      "CATS_TRANSPORT": "http",
      "CATS_HOST": "0.0.0.0",
      "CATS_PORT": "3000"
    }
  }
}
```

**Key Points:**

- **`source.path`**: Points to `server.py`
- **`source.entrypoint`**: The FastMCP instance name (`mcp`)
- **`environment.dependencies`**: Installed automatically
- **`deployment.transport`**: Always `http` for cloud deployment
- **`deployment.env`**: Environment variables with `${VAR}` interpolation
- **Default values**: `${CATS_TOOLSETS:-candidates,jobs,pipelines}` uses default if not set

### Environment Variable Interpolation

The `${VARIABLE_NAME}` syntax allows dynamic configuration:

- `${CATS_API_KEY}` - Required, must be set in FastMCP Cloud
- `${CATS_TOOLSETS:-candidates,jobs,pipelines}` - Optional with default value

This allows the same `fastmcp.json` to work across environments without hardcoding values.

## Continuous Deployment

FastMCP Cloud automatically redeploys when you push to GitHub:

1. **Push to main branch** → Automatic deployment to production
2. **Create pull request** → Automatic preview URL for testing
3. **Merge PR** → Preview URL deleted, production updated

**Example Workflow:**

```bash
# Make changes locally
git add server.py
git commit -m "Add new toolset"
git push origin main

# FastMCP Cloud detects the push and redeploys automatically
# New version live in ~30-60 seconds
```

## Monitoring and Logs

### Health Check Endpoint

The server includes a health check endpoint at `/health`:

```bash
curl https://your-project-name.fastmcp.app/health
```

**Response:**
```json
{
  "status": "healthy",
  "service": "CATS MCP Server",
  "api_configured": true,
  "api_base_url": "https://api.catsone.com/v3"
}
```

Use this for:
- Load balancer health checks
- Uptime monitoring
- Deployment verification

### Viewing Logs

Access logs through the FastMCP Cloud dashboard:

1. Go to your project
2. Click "Logs" tab
3. View real-time server logs

**Log Levels:**
- `DEBUG`: Detailed request/response information
- `INFO`: Normal operations (default)
- `WARNING`: Unexpected but handled situations
- `ERROR`: Errors that need attention
- `CRITICAL`: Severe errors

Set log level via `LOG_LEVEL` environment variable.

### Structured Logging

The server uses structured logging with timestamps:

```
2025-01-26 10:30:15 - cats-mcp-server - INFO - Loading toolsets: candidates, jobs, pipelines
2025-01-26 10:30:15 - cats-mcp-server - INFO - ✓ candidates (28 tools)
2025-01-26 10:30:15 - cats-mcp-server - INFO - ✓ jobs (40 tools)
2025-01-26 10:30:15 - cats-mcp-server - INFO - ✓ pipelines (13 tools)
2025-01-26 10:30:15 - cats-mcp-server - INFO - HTTP Server starting at http://0.0.0.0:3000/mcp
```

## Toolset Configuration

Control which tools load via the `CATS_TOOLSETS` environment variable:

**Default Toolsets (89 tools):**
```
CATS_TOOLSETS=candidates,jobs,pipelines,context,tasks
```

**Minimal Setup (28 tools):**
```
CATS_TOOLSETS=candidates
```

**Full Access (162 tools):**
```
CATS_TOOLSETS=all
```

**Custom Combination:**
```
CATS_TOOLSETS=candidates,jobs,companies,contacts,webhooks
```

**Available Toolsets:**
- DEFAULT: `candidates`, `jobs`, `pipelines`, `context`, `tasks`
- RECRUITING: `companies`, `contacts`, `activities`, `portals`, `work_history`
- DATA: `tags`, `webhooks`, `users`, `triggers`, `attachments`, `backups`, `events`

## Security Best Practices

### API Key Management

1. **Never commit API keys** to Git
2. **Use environment variables** in FastMCP Cloud dashboard
3. **Rotate keys regularly** from CATS administration panel
4. **Use organization-only auth** in FastMCP Cloud for internal tools

### Transport Security

- FastMCP Cloud automatically provides **HTTPS/TLS**
- All traffic is encrypted in transit
- No additional SSL configuration needed

### Rate Limiting

CATS API has rate limits (500 requests/hour):

- The server logs rate limit info at DEBUG level
- Monitor `X-Rate-Limit-Remaining` header
- Implement client-side caching for frequently accessed data
- Consider batching requests when possible

### Network Security

- FastMCP Cloud servers are isolated
- Only your configured environment variables are accessible
- No access to other projects or data
- Automatic security updates

## Troubleshooting

### Deployment Fails

**Check:**
1. `fastmcp.json` is valid JSON (use a validator)
2. All dependencies in `requirements.txt` are available
3. GitHub repository is accessible
4. Branch name is correct

**Common Issues:**
- Missing dependencies → Add to `requirements.txt`
- Invalid JSON → Validate `fastmcp.json`
- Wrong entrypoint → Ensure `server.py` has `mcp = FastMCP(...)`

### "CATS_API_KEY not configured"

**Solution:**
1. Go to FastMCP Cloud dashboard
2. Select your project
3. Add `CATS_API_KEY` environment variable
4. Redeploy (push to main or click "Redeploy")

### Health Check Returns Unhealthy

**Check:**
1. `CATS_API_KEY` is set correctly
2. Server logs for errors
3. CATS API is accessible (not down)
4. API key has correct permissions

### Tools Not Loading

**Check:**
1. `CATS_TOOLSETS` environment variable
2. Toolset names are spelled correctly
3. Server logs show which toolsets loaded
4. Use `/list-toolsets` locally to verify names

### Rate Limiting (429 Errors)

**Solutions:**
1. Reduce request frequency
2. Implement caching
3. Use more specific queries
4. Consider CATS API plan upgrade

### Connection Timeout

**Check:**
1. FastMCP Cloud service status
2. Your internet connection
3. Firewall rules
4. Client timeout settings

## Advanced Configuration

### Custom Domains

Point your own domain to FastMCP Cloud:

1. In FastMCP Cloud, go to project settings
2. Add custom domain (e.g., `cats.yourdomain.com`)
3. Create CNAME record: `cats.yourdomain.com` → `your-project.fastmcp.app`
4. Wait for DNS propagation (up to 48 hours)
5. Access at `https://cats.yourdomain.com/mcp`

### Preview Deployments

Test changes before production:

1. Create a feature branch
2. Make your changes
3. Push to GitHub
4. Open pull request
5. FastMCP Cloud creates preview URL
6. Test at `https://your-project-pr-123.fastmcp.app/mcp`
7. Merge when ready

### Multiple Environments

Run separate deployments for dev/staging/production:

**Approach 1: Separate Projects**
- Create 3 projects: `cats-dev`, `cats-staging`, `cats-prod`
- Use different branches or repositories
- Configure different environment variables

**Approach 2: Branch-Based**
- Use main branch for production
- Use staging branch for staging
- Use PRs for development testing

## Local Testing

Before deploying, test locally with the same configuration:

```bash
# Run with fastmcp.json
fastmcp run

# Or run server directly
export CATS_API_KEY="your_key"
export CATS_TRANSPORT="http"
python server.py

# Test endpoints
curl http://localhost:8000/health
curl http://localhost:8000/mcp
```

## Cost Considerations

FastMCP Cloud pricing varies by plan. Consider:

- **Request volume**: CATS API has rate limits
- **Concurrent connections**: How many users?
- **Compute time**: Server is always running
- **Data transfer**: API requests + responses

Check FastMCP Cloud pricing page for current rates.

## Migration from Other Platforms

### From Railway/Render/Fly.io

1. Create FastMCP Cloud project
2. Copy environment variables
3. Add `fastmcp.json` to repository
4. Push to GitHub
5. Update client configurations with new URL
6. Test thoroughly
7. Delete old deployment

### From Local/STDIO

1. Keep existing `.mcp.json` for local use
2. Deploy to FastMCP Cloud for remote access
3. Update some clients to use cloud URL
4. Others can continue using local STDIO

## Support and Resources

- **FastMCP Cloud Docs**: https://gofastmcp.com/deployment/fastmcp-cloud
- **FastMCP General Docs**: https://gofastmcp.com/
- **CATS API Docs**: https://docs.catsone.com/api/v3/
- **MCP Protocol**: https://modelcontextprotocol.io/
- **GitHub Issues**: Report problems in your repository
- **FastMCP Discord**: Community support (link in docs)

## Checklist

Before deploying to FastMCP Cloud:

- [ ] `fastmcp.json` created and valid
- [ ] `requirements.txt` includes all dependencies
- [ ] Code pushed to GitHub main branch
- [ ] FastMCP Cloud project created
- [ ] `CATS_API_KEY` environment variable set
- [ ] Toolsets configured via `CATS_TOOLSETS`
- [ ] Health check endpoint accessible
- [ ] Logs showing successful startup
- [ ] MCP client connection tested
- [ ] Tools listing successfully

## Next Steps

After successful deployment:

1. **Monitor**: Watch logs and health checks
2. **Optimize**: Adjust toolsets based on usage
3. **Scale**: Add more toolsets as needed
4. **Iterate**: Use PR previews for testing
5. **Share**: Distribute URL to team members

---

**Congratulations!** Your CATS MCP Server is now running on FastMCP Cloud with automatic deployments, monitoring, and scalability.
