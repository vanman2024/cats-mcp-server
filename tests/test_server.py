"""
Tests for CATS MCP Server (server.py)

Run with: pytest tests/test_server.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
try:
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
    HAS_RESPONSE_LIMITING = True
except ImportError:
    HAS_RESPONSE_LIMITING = False


def test_server_imports():
    """Test that server module can be imported and mcp instance exists"""
    import server
    assert server.mcp is not None
    assert server.mcp.name == "CATS API v3"


def test_server_has_required_constants():
    """Test that server has required configuration constants"""
    import server
    assert hasattr(server, 'CATS_API_BASE_URL')
    assert hasattr(server, 'CATS_API_KEY')
    assert hasattr(server, 'mcp')
    assert hasattr(server, 'CATSAPIError')
    assert hasattr(server, 'make_request')
    assert hasattr(server, 'load_toolsets')
    assert hasattr(server, 'DEFAULT_TOOLSETS')
    assert hasattr(server, 'ALL_TOOLSETS')


def test_default_toolsets_defined():
    """Test that default toolsets list is correct"""
    import server
    assert 'candidates' in server.DEFAULT_TOOLSETS
    assert 'jobs' in server.DEFAULT_TOOLSETS
    assert 'pipelines' in server.DEFAULT_TOOLSETS
    assert 'context' in server.DEFAULT_TOOLSETS
    assert 'tasks' in server.DEFAULT_TOOLSETS
    assert len(server.DEFAULT_TOOLSETS) == 5


def test_all_toolsets_defined():
    """Test that all toolsets are defined"""
    import server
    expected = [
        'candidates', 'jobs', 'pipelines', 'context', 'tasks',
        'companies', 'contacts', 'activities', 'portals', 'work_history',
        'tags', 'webhooks', 'users', 'triggers', 'attachments', 'backups', 'events'
    ]
    assert len(server.ALL_TOOLSETS) == 17
    for toolset in expected:
        assert toolset in server.ALL_TOOLSETS, f"Missing toolset: {toolset}"


@pytest.mark.asyncio
async def test_make_request_missing_api_key():
    """Test that make_request raises error when API key is missing"""
    import server

    with patch.object(server, 'CATS_API_KEY', ''):
        with pytest.raises(server.CATSAPIError) as exc_info:
            await server.make_request("GET", "/test")
        assert "not configured" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_make_request_success():
    """Test make_request with mocked successful HTTP response"""
    import server

    # httpx response.json() is synchronous - use MagicMock not AsyncMock
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"id": 1}]}
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {}

    with patch.object(server, 'CATS_API_KEY', 'test-key-123'):
        with patch('server.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await server.make_request("GET", "/candidates")
            assert result == {"data": [{"id": 1}]}
            mock_client.request.assert_called_once()


@pytest.mark.asyncio
async def test_make_request_http_error():
    """Test make_request raises CATSAPIError on HTTP errors"""
    import server
    import httpx

    mock_response = AsyncMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.headers = {}

    with patch.object(server, 'CATS_API_KEY', 'test-key-123'):
        with patch('server.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "Not Found",
                    request=httpx.Request("GET", "https://api.catsone.com/v3/test"),
                    response=httpx.Response(404, text="Not Found")
                )
            )
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(server.CATSAPIError) as exc_info:
                await server.make_request("GET", "/test")
            assert "404" in str(exc_info.value) or "error" in str(exc_info.value).lower()


def test_load_toolsets_function_exists():
    """Test that load_toolsets function exists and is callable"""
    import server
    assert callable(server.load_toolsets)


def test_cats_api_error_is_exception():
    """Test CATSAPIError is a proper exception"""
    import server
    error = server.CATSAPIError("test error")
    assert isinstance(error, Exception)
    assert str(error) == "test error"


@pytest.mark.skipif(not HAS_RESPONSE_LIMITING, reason="FastMCP v3 middleware not available")
def test_server_has_response_limiting_middleware():
    """Test that ResponseLimitingMiddleware is configured on the server"""
    import server
    limiting = [m for m in server.mcp.middleware if isinstance(m, ResponseLimitingMiddleware)]
    assert len(limiting) == 1, "Expected exactly one ResponseLimitingMiddleware"
    assert limiting[0].max_size == 100_000


@pytest.mark.skipif(not HAS_RESPONSE_LIMITING, reason="FastMCP v3 middleware not available")
def test_server_all_tools_has_response_limiting_middleware():
    """Test that ResponseLimitingMiddleware is configured on server_all_tools"""
    import server_all_tools
    limiting = [m for m in server_all_tools.mcp.middleware if isinstance(m, ResponseLimitingMiddleware)]
    assert len(limiting) == 1, "Expected exactly one ResponseLimitingMiddleware"
    assert limiting[0].max_size == 100_000
