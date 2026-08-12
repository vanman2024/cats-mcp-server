"""Tests for the CATS HTTP client: retries, rate limits, errors, redaction.

These cover the behaviours the two previous `make_request` implementations got
wrong - no retries at all in one, no `Retry-After` handling in either, and no
connection reuse in both.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from cats_mcp.config import Settings
from cats_mcp.credentials.base import CATSCredential, CredentialError, CredentialProvider
from cats_mcp.credentials.env import EnvCredentialProvider
from cats_mcp.http.client import CATSClient
from cats_mcp.http.errors import CATSAPIError
from cats_mcp.http.ratelimit import (
    MAX_RETRY_SLEEP_SECONDS,
    RateLimitState,
    is_retryable_status,
    retry_delay,
)


class StubCredentials(CredentialProvider):
    def __init__(self, key: str = "test-key", label: str = "acme") -> None:
        self._key = key
        self._label = label

    async def resolve(self, context=None) -> CATSCredential:
        return CATSCredential(
            api_key=self._key,
            base_url="https://api.catsone.com/v3",
            account_label=self._label,
        )

    def describe(self) -> str:
        return "stub"


def make_settings(**overrides) -> Settings:
    defaults = dict(api_key="test-key", max_retries=4, request_timeout=5.0)
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Keep retry tests fast without weakening the retry logic under test."""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def client_with(handler, **settings_overrides) -> CATSClient:
    return CATSClient(
        make_settings(**settings_overrides),
        StubCredentials(),
        transport=httpx2.MockTransport(handler),
    )


# --- success paths ---------------------------------------------------------


async def test_returns_parsed_json():
    async with client_with(
        lambda request: httpx2.Response(200, json={"id": 7, "title": "Welder"})
    ) as client:
        assert await client.request("GET", "/jobs/7") == {"id": 7, "title": "Welder"}


async def test_sends_token_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx2.Response(200, json={})

    async with client_with(handler) as client:
        await client.request("GET", "/site")

    assert seen["auth"] == "Token test-key"


async def test_empty_204_response_is_not_an_error():
    async with client_with(lambda request: httpx2.Response(204)) as client:
        result = await client.request("DELETE", "/candidates/1")
    assert result["status"] == "success"
    assert result["status_code"] == 204


async def test_non_json_body_is_summarised_not_dumped():
    """Thumbnails and attachment downloads must not be inlined into a result."""

    async def handler(request):
        return httpx2.Response(
            200,
            content=b"\x89PNG\r\n" + b"x" * 5000,
            headers={"Content-Type": "image/png"},
        )

    async with client_with(handler) as client:
        result = await client.request("GET", "/candidates/1/thumbnail")

    assert result["content_type"] == "image/png"
    assert result["content_length"] == 5006
    assert "note" in result
    # The bytes themselves must not be in the payload handed to a model.
    assert "PNG" not in str(result)


async def test_none_valued_params_are_dropped():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx2.Response(200, json={})

    async with client_with(handler) as client:
        await client.request("GET", "/candidates", params={"page": 1, "sort": None})

    assert "page=1" in seen["url"]
    assert "sort" not in seen["url"]


# --- retries ---------------------------------------------------------------


async def test_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx2.Response(429, headers={"Retry-After": "1"})
        return httpx2.Response(200, json={"ok": True})

    async with client_with(handler) as client:
        assert await client.request("GET", "/candidates") == {"ok": True}

    assert calls["n"] == 3


async def test_retries_500_then_gives_up():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx2.Response(500, text="upstream boom")

    with pytest.raises(CATSAPIError) as excinfo:
        async with client_with(handler, max_retries=3) as client:
            await client.request("GET", "/candidates")

    assert calls["n"] == 3
    assert excinfo.value.status_code == 500


async def test_does_not_retry_client_errors():
    """Retrying a 404 cannot succeed and burns a 500/hour budget."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx2.Response(404, text="not found")

    with pytest.raises(CATSAPIError):
        async with client_with(handler) as client:
            await client.request("GET", "/candidates/999999")

    assert calls["n"] == 1


async def test_retries_timeouts():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx2.ConnectTimeout("too slow")
        return httpx2.Response(200, json={"ok": True})

    async with client_with(handler) as client:
        assert await client.request("GET", "/jobs") == {"ok": True}

    assert calls["n"] == 2


# --- rate limiting ---------------------------------------------------------


async def test_reads_rate_limit_from_headers_not_config():
    """The ceiling varies per account, so it must come from the response."""

    def handler(request):
        return httpx2.Response(
            200,
            json={},
            headers={"X-Rate-Limit-Limit": "1500", "X-Rate-Limit-Remaining": "1499"},
        )

    async with client_with(handler) as client:
        await client.request("GET", "/site")
        assert client.rate_limit.limit == 1500
        assert client.rate_limit.remaining == 1499


def test_rate_limit_defaults_to_the_conservative_standard():
    """500/hour is the CATS standard; assuming a raised account is unsafe."""
    assert RateLimitState().effective_limit == 500


def test_near_exhaustion_detection():
    state = RateLimitState(limit=500, remaining=10)
    assert state.is_nearly_exhausted()
    assert not RateLimitState(limit=500, remaining=400).is_nearly_exhausted()
    # Unknown remaining must not be reported as exhausted.
    assert not RateLimitState().is_nearly_exhausted()


def test_retry_after_header_wins_over_backoff():
    assert retry_delay(0, retry_after="30") == 30.0
    assert retry_delay(5, retry_after="2") == 2.0


def test_retry_after_is_capped():
    assert retry_delay(0, retry_after="99999") == MAX_RETRY_SLEEP_SECONDS


def test_backoff_is_jittered_and_bounded():
    """Full jitter is what de-correlates concurrent clients."""
    samples = {retry_delay(3) for _ in range(50)}
    assert len(samples) > 1, "backoff must be jittered, not fixed"
    assert all(0.0 <= s <= MAX_RETRY_SLEEP_SECONDS for s in samples)


def test_retryable_status_classification():
    assert is_retryable_status(429)
    assert is_retryable_status(503)
    assert not is_retryable_status(400)
    assert not is_retryable_status(404)


# --- errors and credentials ------------------------------------------------


async def test_error_body_is_truncated_before_reaching_the_model():
    def handler(request):
        return httpx2.Response(500, text="E" * 10_000)

    with pytest.raises(CATSAPIError) as excinfo:
        async with client_with(handler, max_retries=1) as client:
            await client.request("GET", "/candidates")

    assert len(str(excinfo.value)) < 1_000
    assert "truncated" in str(excinfo.value)


async def test_401_message_is_actionable():
    def handler(request):
        return httpx2.Response(401, text="bad token")

    with pytest.raises(CATSAPIError) as excinfo:
        async with client_with(handler) as client:
            await client.request("GET", "/site")

    assert "configuration problem" in str(excinfo.value)


async def test_credential_never_appears_in_error_text():
    def handler(request):
        return httpx2.Response(500, text="failure")

    with pytest.raises(CATSAPIError) as excinfo:
        async with client_with(handler, max_retries=1) as client:
            await client.request("GET", "/site")

    assert "test-key" not in str(excinfo.value)


async def test_missing_api_key_fails_with_a_clear_message():
    provider = EnvCredentialProvider(Settings(api_key=""))
    with pytest.raises(CredentialError) as excinfo:
        await provider.resolve()
    assert "CATS_API_KEY" in str(excinfo.value)


def test_unresolved_placeholder_is_rejected_at_startup():
    """Horizon leaves `${VAR}` literal when the variable is unset."""
    with pytest.raises(ValueError) as excinfo:
        Settings(api_key="${CATS_API_KEY}")
    assert "placeholder" in str(excinfo.value).lower()
