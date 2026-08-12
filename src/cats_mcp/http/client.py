"""The single CATS API client.

This is the only module in the package that imports an HTTP library. FastMCP 4
replaced `httpx` with `httpx2` and does not ship `httpx` at all; keeping the
dependency contained here is what made that a one-file change, and is what will
make the next one cheap too.

Replaces both previous `make_request` implementations, which diverged badly:
one had no retries at all, the other retried with `print()`-based logging;
neither honoured `Retry-After`; and both constructed a fresh `AsyncClient` per
call, so no connection was ever reused.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2

from cats_mcp.config import Settings
from cats_mcp.credentials.base import CATSCredential, CredentialProvider
from cats_mcp.http.correlation import get_logger, get_run_id, new_correlation_id
from cats_mcp.http.errors import CATSAPIError, normalize_http_error
from cats_mcp.http.ratelimit import (
    HEADER_RETRY_AFTER,
    RateLimitState,
    is_retryable_status,
    retry_delay,
)

logger = get_logger(__name__)

#: Responses with no body. CATS uses 204 for successful deletes.
_EMPTY_STATUSES = frozenset({204, 205})


class CATSClient:
    """Pooled, rate-limit-aware async client for the CATS API v3."""

    def __init__(
        self,
        settings: Settings,
        credential_provider: CredentialProvider,
        *,
        transport: Any | None = None,
    ) -> None:
        self._settings = settings
        self._credentials = credential_provider
        # `transport` is an injection point for tests, so the suite can exercise
        # retry and rate-limit behaviour without real network calls.
        self._transport = transport
        self._client: httpx2.AsyncClient | None = None
        self.rate_limit = RateLimitState()

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(self._settings.request_timeout),
            limits=httpx2.Limits(
                max_connections=self._settings.max_connections,
                max_keepalive_connections=self._settings.max_keepalive_connections,
            ),
            transport=self._transport,
            follow_redirects=True,
        )
        logger.debug("CATS HTTP client started")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.debug("CATS HTTP client closed")

    async def __aenter__(self) -> CATSClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- requests -----------------------------------------------------------

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        context: Any | None = None,
    ) -> Any:
        """Perform one CATS API call, with retries, and return the parsed body.

        Raises `CATSAPIError` on unrecoverable failure. Callers that surface
        results to a model should convert via `errors.to_tool_error`.
        """
        if self._client is None:
            # Tolerate use outside a lifespan (tests, scripts) rather than
            # failing obscurely on `None`.
            await self.start()
        assert self._client is not None

        credential = await self._credentials.resolve(context)
        correlation_id = get_run_id() or new_correlation_id()
        url = self._build_url(credential, endpoint)
        headers = {
            **credential.auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # `params` values of None would be serialised as the string "None".
        clean_params = {k: v for k, v in params.items() if v is not None} if params else None

        last_error: CATSAPIError | None = None
        attempts = max(1, self._settings.max_retries)

        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    params=clean_params,
                    json=json,
                )
            except httpx2.TimeoutException:
                # Must be caught before RequestError - it is a subclass.
                last_error = CATSAPIError(
                    f"CATS request to {endpoint} timed out after {self._settings.request_timeout}s",
                    endpoint=endpoint,
                    correlation_id=correlation_id,
                    retryable=True,
                )
            except httpx2.RequestError as exc:
                # Network-level failure: no response was received.
                last_error = CATSAPIError(
                    f"CATS request to {endpoint} failed to complete: {type(exc).__name__}",
                    endpoint=endpoint,
                    correlation_id=correlation_id,
                    retryable=True,
                )
            else:
                self.rate_limit.observe(response.headers)
                if self.rate_limit.is_nearly_exhausted():
                    logger.warning(
                        "CATS rate limit nearly exhausted account=%s remaining=%s "
                        "limit=%s request=%s",
                        credential.account_label,
                        self.rate_limit.remaining,
                        self.rate_limit.limit,
                        correlation_id,
                    )

                if response.status_code < 400:
                    logger.debug(
                        "CATS %s %s -> %s account=%s request=%s",
                        method.upper(),
                        endpoint,
                        response.status_code,
                        credential.account_label,
                        correlation_id,
                    )
                    return self._parse(response)

                if not is_retryable_status(response.status_code):
                    raise normalize_http_error(
                        response.status_code,
                        response.text,
                        endpoint=endpoint,
                        correlation_id=correlation_id,
                    )

                last_error = normalize_http_error(
                    response.status_code,
                    response.text,
                    endpoint=endpoint,
                    correlation_id=correlation_id,
                )
                if attempt < attempts - 1:
                    delay = retry_delay(
                        attempt,
                        retry_after=response.headers.get(HEADER_RETRY_AFTER),
                    )
                    logger.warning(
                        "CATS %s on %s; retrying in %.2fs (attempt %d/%d) request=%s",
                        response.status_code,
                        endpoint,
                        delay,
                        attempt + 1,
                        attempts,
                        correlation_id,
                    )
                    await asyncio.sleep(delay)
                continue

            # Reached only for the two transport-level failures above.
            if attempt < attempts - 1:
                delay = retry_delay(attempt)
                logger.warning(
                    "CATS transport failure on %s; retrying in %.2fs (attempt %d/%d) request=%s",
                    endpoint,
                    delay,
                    attempt + 1,
                    attempts,
                    correlation_id,
                )
                await asyncio.sleep(delay)

        raise last_error or CATSAPIError(
            f"CATS request to {endpoint} failed after {attempts} attempts",
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _build_url(credential: CATSCredential, endpoint: str) -> str:
        # Base URL comes from the credential, not from settings, so a
        # request-scoped provider can point different tenants at different hosts.
        return f"{credential.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    @staticmethod
    def _parse(response: httpx2.Response) -> Any:
        """Parse a successful response, tolerating empty and non-JSON bodies."""
        if response.status_code in _EMPTY_STATUSES or not response.content:
            return {"status": "success", "status_code": response.status_code}
        try:
            return response.json()
        except ValueError:
            # Binary or non-JSON payload (thumbnails, attachment downloads).
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            return {
                "status": "success",
                "status_code": response.status_code,
                "content_type": content_type,
                "content_length": len(response.content),
                "note": (
                    "Response body is not JSON. Use the dedicated download tool to "
                    "retrieve the file rather than reading it through this tool."
                ),
            }
