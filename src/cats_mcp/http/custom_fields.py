"""Account-specific custom field id -> name/type, resolved and cached.

CATS never puts a name next to a custom field's value. `GET
/candidates/{id}/custom_fields` - and the same field embedded on
`GET /candidates/{id}` - answers each row with only `{"id": ..., "value":
...}` (confirmed against the documented schema:
https://docs.catsone.com/api/v3/#candidates). An id on its own says nothing:
"359950: Message 1 Sent" is not readable by anyone who has not separately
memorised this account's field list. The name lives only in the definitions
endpoint, `GET /{resource}/custom_fields`, one call away and never joined by
CATS itself.

Definitions are account *configuration*, not account *records* - they change
when an administrator edits a setting, not when a recruiter does their job.
That is exactly the profile `observability.py` already calls cacheable
reference data, so this resolver caches them the same way: per account, never
globally, because a single process can serve more than one CATS account under
request-scoped credentials, and a ten-minute TTL, matching the reference-data
window used elsewhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from cats_mcp.credentials.base import CredentialProvider
from cats_mcp.http.correlation import get_logger
from cats_mcp.http.errors import CATSAPIError

logger = get_logger(__name__)

#: Matches the reference-data cache window in observability.py. Definitions
#: change rarely; ten minutes helps a batch of lookups without risking a real
#: edit going unnoticed for long.
REFERENCE_TTL_SECONDS = 600

#: id (as CATS sends it, a string key) -> {"name": ..., "type": ...}
FieldMap = dict[str, dict[str, str]]


def _account_key(api_key: str) -> str:
    """A stable per-account cache key that is not the secret itself.

    Same reasoning as http/site.py's UI domain cache: keyed on the credential,
    not on a caller-assigned label, so two tenants can never collide into
    sharing one account's field definitions.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


class CustomFieldResolver:
    """Resolves, and caches per account, id -> {name, type} for a resource."""

    def __init__(self, client_getter: Any, credential_provider: CredentialProvider) -> None:
        self._client_getter = client_getter
        self._credentials = credential_provider
        self._cache: dict[str, tuple[float, FieldMap]] = {}
        self._lock = asyncio.Lock()

    async def resolve(
        self, resource: str, context: Any | None = None
    ) -> tuple[FieldMap, int]:
        """id -> {name, type} for `resource` ("candidates" or "jobs").

        Returns the map and how many CATS requests it cost - 0 when served
        entirely from cache. Never raises: an unresolved name degrades to the
        bare id, which is the same information the caller had before asking.
        """
        try:
            credential = await self._credentials.resolve(context)
            key = f"{_account_key(credential.api_key)}:{resource}"
        except Exception:  # noqa: BLE001 - resolution failing here is not this call's problem
            key = f"unknown:{resource}"

        cached = self._cache.get(key)
        if cached and (time.monotonic() - cached[0]) < REFERENCE_TTL_SECONDS:
            return cached[1], 0

        async with self._lock:
            # Another call may have refreshed it while this one waited.
            cached = self._cache.get(key)
            if cached and (time.monotonic() - cached[0]) < REFERENCE_TTL_SECONDS:
                return cached[1], 0
            fields, used = await self._fetch(resource, context)
            self._cache[key] = (time.monotonic(), fields)
            return fields, used

    async def _fetch(self, resource: str, context: Any | None) -> tuple[FieldMap, int]:
        client = self._client_getter()
        out: FieldMap = {}
        requests_used = 0
        page = 1
        # Paginated defensively rather than assumed to fit one page. Silently
        # capping at 100 would repeat exactly the bug pagination injection
        # already fixed elsewhere for this same endpoint (see
        # registry/build.py's pagination_params_for) - and an unresolved name
        # here is not an error a caller would ever see, so a missing field
        # would go unnoticed rather than complained about.
        while True:
            try:
                payload = await client.request(
                    "GET",
                    f"/{resource}/custom_fields",
                    params={"per_page": 100, "page": page},
                    context=context,
                    max_attempts=1,
                )
            except CATSAPIError as exc:
                logger.warning(
                    "could not resolve %s custom field definitions: %s", resource, exc
                )
                break
            requests_used += 1
            embedded = (payload or {}).get("_embedded") or {}
            rows = embedded.get("custom_fields") or []
            for row in rows:
                if isinstance(row, dict) and row.get("id") is not None:
                    out[str(row["id"])] = {
                        "name": row.get("name") or row.get("title") or "",
                        "type": row.get("type") or "",
                    }
            links = (payload or {}).get("_links") or {}
            if not isinstance(links, dict) or "next" not in links:
                break
            page += 1
        return out, requests_used
