"""
Comprehensive test suite for all CATS MCP tools
Uses FastMCP v2 Client in-memory testing pattern
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp.client import Client


# Actual tool counts per toolset (verified by listing all 189 tool names)
TOTAL_EXPECTED_TOOLS = 189

# Key tools that MUST exist for each toolset (spot-check approach)
REQUIRED_TOOLS = {
    "candidates": [
        "list_candidates", "get_candidate", "create_candidate", "update_candidate",
        "delete_candidate", "search_candidates", "filter_candidates",
        "list_candidate_pipelines", "list_candidate_activities",
        "list_candidate_attachments", "list_candidate_custom_fields",
        "list_candidate_emails", "create_candidate_email", "update_candidate_email",
        "delete_candidate_email", "list_candidate_phones", "create_candidate_phone",
        "update_candidate_phone", "delete_candidate_phone",
        "list_candidate_tags", "attach_candidate_tags", "replace_candidate_tags",
        "delete_candidate_tag", "authorize_candidate",
        "list_candidate_work_history", "create_candidate_work_history",
        "get_candidate_work_history_item", "update_candidate_work_history_item",
        "delete_candidate_work_history_item",
        "list_candidate_lists", "get_candidate_list", "create_candidate_list",
        "delete_candidate_list", "list_candidate_list_items",
        "get_candidate_list_item", "create_candidate_list_items",
        "delete_candidate_list_item",
        "get_candidate_thumbnail", "change_candidate_thumbnail",
        "upload_candidate_attachment", "get_candidate_custom_field",
        "update_candidate_custom_field",
    ],
    "jobs": [
        "list_jobs", "get_job", "create_job", "update_job", "delete_job",
        "search_jobs", "filter_jobs",
        "list_job_pipelines", "list_job_candidates", "list_job_activities",
        "list_job_attachments", "list_job_custom_fields", "get_job_custom_field",
        "update_job_custom_fields", "update_job_custom_field",
        "list_job_statuses", "get_job_status", "change_job_status",
        "list_job_tags", "attach_job_tags", "delete_job_tag",
        "list_job_tasks",
        "list_job_lists", "get_job_list", "create_job_list",
        "delete_job_list", "list_job_list_items", "get_job_list_item",
        "create_job_list_items", "delete_job_list_item",
        "list_job_applications", "get_job_application",
        "list_job_application_fields",
    ],
    "pipelines": [
        "list_pipelines", "get_pipeline", "create_pipeline", "update_pipeline",
        "delete_pipeline", "filter_pipelines",
        "list_pipeline_workflows", "get_pipeline_workflow",
        "list_pipeline_workflow_statuses", "get_pipeline_workflow_status",
        "get_pipeline_statuses", "change_pipeline_status",
    ],
    "context": [
        "get_site", "get_me", "authorize_user",
    ],
    "tasks": [
        "list_tasks", "get_task", "create_task", "update_task", "delete_task",
    ],
    "companies": [
        "list_companies", "get_company", "create_company", "update_company",
        "delete_company", "search_companies", "filter_companies",
    ],
    "contacts": [
        "list_contacts", "get_contact", "create_contact", "update_contact",
        "delete_contact", "search_contacts", "filter_contacts",
    ],
    "activities": [
        "list_activities", "get_activity", "update_activity", "delete_activity",
        "search_activities", "filter_activities",
    ],
    "portals": [
        "list_portals", "get_portal", "list_portal_jobs",
        "submit_job_application", "publish_job_to_portal",
        "unpublish_job_from_portal", "get_portal_registration",
        "submit_portal_registration",
    ],
    "work_history": [
        "get_work_history", "update_work_history", "delete_work_history",
    ],
    "tags": [
        "list_tags", "get_tag",
    ],
    "webhooks": [
        "list_webhooks", "get_webhook", "create_webhook", "delete_webhook",
    ],
    "users": [
        "list_users", "get_user",
    ],
    "triggers": [
        "list_triggers", "get_trigger",
    ],
    "attachments": [
        "get_attachment", "delete_attachment", "download_attachment", "parse_resume",
    ],
    "backups": [
        "list_backups", "get_backup", "create_backup",
    ],
    "events": [
        "list_events",
    ],
}


@pytest.fixture
def mcp_server():
    """Return the FastMCP server instance with all tools loaded"""
    import server_all_tools
    return server_all_tools.mcp


@pytest.mark.asyncio
async def test_all_tools_loaded(mcp_server):
    """Verify all tools are registered"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        print(f"\n  Total tools loaded: {len(tools)}")
        assert len(tools) >= TOTAL_EXPECTED_TOOLS, (
            f"Expected at least {TOTAL_EXPECTED_TOOLS} tools, got {len(tools)}"
        )


@pytest.mark.asyncio
async def test_tool_names_are_unique(mcp_server):
    """Verify no duplicate tool names"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        duplicates = [name for name in tool_names if tool_names.count(name) > 1]
        assert len(duplicates) == 0, f"Duplicate tool names found: {set(duplicates)}"


@pytest.mark.asyncio
async def test_all_tools_have_descriptions(mcp_server):
    """Verify all tools have descriptions"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        missing = [t.name for t in tools if not t.description]
        assert len(missing) == 0, f"Tools missing descriptions: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize("toolset", list(REQUIRED_TOOLS.keys()))
async def test_toolset_tools_exist(mcp_server, toolset):
    """Test that each toolset's required tools are registered"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        required = REQUIRED_TOOLS[toolset]
        missing = [t for t in required if t not in tool_names]
        print(f"\n  {toolset}: {len(required)} required, {len(missing)} missing")
        assert len(missing) == 0, (
            f"Toolset '{toolset}' missing tools: {missing}"
        )


@pytest.mark.asyncio
async def test_candidate_crud_tools(mcp_server):
    """Verify full CRUD for candidates"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        crud = ["list_candidates", "get_candidate", "create_candidate",
                "update_candidate", "delete_candidate"]
        for tool in crud:
            assert tool in tool_names, f"Missing candidate CRUD tool: {tool}"


@pytest.mark.asyncio
async def test_job_crud_tools(mcp_server):
    """Verify full CRUD for jobs"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        crud = ["list_jobs", "get_job", "create_job", "update_job", "delete_job"]
        for tool in crud:
            assert tool in tool_names, f"Missing job CRUD tool: {tool}"


@pytest.mark.asyncio
async def test_search_tools_exist(mcp_server):
    """Verify search tools for major entities"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        search_tools = [
            "search_candidates", "search_jobs", "search_companies",
            "search_contacts", "search_activities"
        ]
        for tool in search_tools:
            assert tool in tool_names, f"Missing search tool: {tool}"


@pytest.mark.asyncio
async def test_filter_tools_exist(mcp_server):
    """Verify filter tools for major entities"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        filter_tools = [
            "filter_candidates", "filter_jobs", "filter_pipelines",
            "filter_companies", "filter_contacts", "filter_activities"
        ]
        for tool in filter_tools:
            assert tool in tool_names, f"Missing filter tool: {tool}"


@pytest.mark.asyncio
async def test_mock_api_call():
    """Test make_request with mocked HTTP response"""
    import server_all_tools

    # httpx response.json() is synchronous - use MagicMock not AsyncMock
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": "test"}
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}

    with patch.object(server_all_tools, 'CATS_API_KEY', 'test-key-123'):
        with patch('server_all_tools.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await server_all_tools.make_request("GET", "/test")
            assert result == {"data": "test"}


@pytest.mark.asyncio
async def test_make_request_missing_key():
    """Test make_request raises error without API key"""
    import server_all_tools

    with patch.object(server_all_tools, 'CATS_API_KEY', ''):
        with pytest.raises(server_all_tools.CATSAPIError) as exc_info:
            await server_all_tools.make_request("GET", "/test")
        assert "not configured" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_make_request_rate_limit_retry():
    """Test that make_request retries on 429 rate limit"""
    import server_all_tools

    # First call: 429 rate limit, second call: 200 OK
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    mock_response_429.headers = {}

    mock_response_ok = MagicMock()
    mock_response_ok.status_code = 200
    mock_response_ok.json.return_value = {"data": "success"}
    mock_response_ok.raise_for_status = MagicMock()
    mock_response_ok.headers = {}

    with patch.object(server_all_tools, 'CATS_API_KEY', 'test-key-123'):
        with patch('server_all_tools.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=[mock_response_429, mock_response_ok])
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch('server_all_tools.asyncio.sleep', new_callable=AsyncMock):
                result = await server_all_tools.make_request("GET", "/test")
                assert result == {"data": "success"}


@pytest.mark.asyncio
async def test_recruiting_tools_present(mcp_server):
    """Verify recruiting toolset tools are loaded (companies, contacts, activities, portals)"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        recruiting_tools = [
            "list_companies", "get_company", "search_companies",
            "list_contacts", "get_contact", "search_contacts",
            "list_activities", "get_activity",
            "list_portals", "get_portal",
            "get_work_history",
        ]
        for tool in recruiting_tools:
            assert tool in tool_names, f"Missing recruiting tool: {tool}"


@pytest.mark.asyncio
async def test_data_config_tools_present(mcp_server):
    """Verify data/config toolset tools are loaded"""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        data_tools = [
            "list_tags", "get_tag",
            "list_webhooks", "create_webhook",
            "list_users", "get_user",
            "list_triggers", "get_trigger",
            "get_attachment", "parse_resume",
            "list_backups", "create_backup",
            "list_events",
        ]
        for tool in data_tools:
            assert tool in tool_names, f"Missing data/config tool: {tool}"


if __name__ == "__main__":
    print(f"Expected total tools: {TOTAL_EXPECTED_TOOLS}")
    print(f"Required tool count: {sum(len(v) for v in REQUIRED_TOOLS.values())}")
