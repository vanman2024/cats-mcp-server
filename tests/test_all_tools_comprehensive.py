"""
Comprehensive test suite for all 163 CATS MCP tools
Uses FastMCP in-memory testing pattern
"""
import pytest
from unittest.mock import AsyncMock, patch
import httpx

# Import toolset counts for verification
EXPECTED_TOOL_COUNTS = {
    "candidates": 28,
    "jobs": 40,
    "pipelines": 13,
    "context": 3,
    "tasks": 5,
    "companies": 18,
    "contacts": 18,
    "activities": 6,
    "portals": 8,
    "work_history": 3,
    "tags": 2,
    "webhooks": 4,
    "users": 2,
    "triggers": 2,
    "attachments": 4,
    "backups": 3,
    "events": 5
}

TOTAL_EXPECTED_TOOLS = sum(EXPECTED_TOOL_COUNTS.values())  # 164


@pytest.fixture
async def mcp_client():
    """Create in-memory MCP client for testing with all tools loaded"""
    # Import server which loads all tools at import time
    import server_all_tools
    
    # Use FastMCP's test client
    async with server_all_tools.mcp.client() as client:
        yield client


@pytest.mark.asyncio
async def test_all_tools_loaded(mcp_client):
    """Verify all 164 tools are registered"""
    tools = await mcp_client.list_tools()
    tool_list = tools.tools
    
    print(f"\n✅ Total tools loaded: {len(tool_list)}")
    assert len(tool_list) == TOTAL_EXPECTED_TOOLS, f"Expected {TOTAL_EXPECTED_TOOLS} tools, got {len(tool_list)}"


@pytest.mark.asyncio
async def test_candidate_tools_exist(mcp_client):
    """Verify candidate tools are available"""
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    
    candidate_tools = [t for t in tool_names if 'candidate' in t.lower()]
    print(f"\n✅ Candidate tools found: {len(candidate_tools)}")
    assert len(candidate_tools) >= EXPECTED_TOOL_COUNTS["candidates"]


@pytest.mark.asyncio
async def test_job_tools_exist(mcp_client):
    """Verify job tools are available"""
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    
    job_tools = [t for t in tool_names if 'job' in t.lower()]
    print(f"\n✅ Job tools found: {len(job_tools)}")
    assert len(job_tools) >= EXPECTED_TOOL_COUNTS["jobs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("toolset,expected_count", [
    ("candidates", 28),
    ("jobs", 40),
    ("pipelines", 13),
    ("context", 3),
    ("tasks", 5),
])
async def test_default_toolsets_loaded(mcp_client, toolset, expected_count):
    """Test that default toolsets are loaded correctly"""
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    
    matching_tools = [t for t in tool_names if toolset in t.lower()]
    print(f"\n{toolset}: {len(matching_tools)} tools")
    assert len(matching_tools) >= expected_count


@pytest.mark.asyncio
async def test_mock_api_call():
    """Test make_request with mocked HTTP response"""
    import server_all_tools
    
    # Mock the HTTP client
    with patch('server_all_tools.httpx.AsyncClient') as mock_client:
        mock_response = AsyncMock()
        mock_response.json.return_value = {"data": "test"}
        mock_response.raise_for_status = AsyncMock()
        
        mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=mock_response)
        
        result = await server_all_tools.make_request("GET", "/test")
        assert result == {"data": "test"}


@pytest.mark.asyncio
async def test_list_tools_returns_descriptions(mcp_client):
    """Verify tools have descriptions"""
    tools = await mcp_client.list_tools()
    
    tools_with_descriptions = [t for t in tools.tools if t.description]
    print(f"\n✅ Tools with descriptions: {len(tools_with_descriptions)}/{len(tools.tools)}")
    assert len(tools_with_descriptions) > 150, "Most tools should have descriptions"


@pytest.mark.asyncio
async def test_recruiting_toolsets_loaded(mcp_client):
    """Test that recruiting toolsets are loaded"""
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    
    recruiting_keywords = ['company', 'companies', 'contact', 'activity', 'portal', 'work_history']
    recruiting_tools = [t for t in tool_names if any(kw in t.lower() for kw in recruiting_keywords)]
    
    print(f"\n✅ Recruiting tools found: {len(recruiting_tools)}")
    expected_recruiting = sum([EXPECTED_TOOL_COUNTS[k] for k in ["companies", "contacts", "activities", "portals", "work_history"]])
    assert len(recruiting_tools) >= expected_recruiting


@pytest.mark.asyncio
async def test_data_toolsets_loaded(mcp_client):
    """Test that data/config toolsets are loaded"""
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    
    data_keywords = ['tag', 'webhook', 'user', 'trigger', 'attachment', 'backup', 'event']
    data_tools = [t for t in tool_names if any(kw in t.lower() for kw in data_keywords)]
    
    print(f"\n✅ Data/config tools found: {len(data_tools)}")
    expected_data = sum([EXPECTED_TOOL_COUNTS[k] for k in ["tags", "webhooks", "users", "triggers", "attachments", "backups", "events"]])
    assert len(data_tools) >= expected_data


if __name__ == "__main__":
    print(f"Expected total tools: {TOTAL_EXPECTED_TOOLS}")
    print(f"Toolset breakdown: {EXPECTED_TOOL_COUNTS}")
