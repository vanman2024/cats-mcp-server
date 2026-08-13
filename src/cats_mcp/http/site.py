"""The CATS web-UI domain for whichever account a credential belongs to.

`GET /site` returns the account's `subdomain`, and which account that resolves to
follows from the API key. So the UI domain never has to be configured: it can be
read from the same credential already in use.

That matters beyond saving a setting. `CATS_UI_BASE_URL` is a single value for
the whole process, so the moment two callers bring different CATS accounts -
which is exactly what request-scoped credentials are for - one of them gets
links pointing at the other company's CATS. Those links resolve, to a login page
or to a record that is not theirs. Deriving the domain per credential means the
question cannot arise.

An explicit `CATS_UI_BASE_URL` still wins: a vanity domain cannot be derived, and
some deployments would rather not spend the request.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from cats_mcp.credentials.base import CredentialProvider
from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)

#: CATS accounts are served from `<subdomain>.catsone.com`. Confirmed against a
#: live account: GET /site returns subdomain="acme" for
#: https://acme.catsone.com.
UI_DOMAIN_TEMPLATE = "https://{subdomain}.catsone.com"


def _cache_key(api_key: str) -> str:
    """A stable per-account key that is not the secret.

    Keyed on the credential rather than on `account_label`, which defaults to
    "default" and is set by whoever writes the provider. If two tenants ever
    shared a label they would silently share a UI domain, which is the
    cross-account version of the bug this whole module exists to avoid.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


class UIDomainResolver:
    """Resolves, and caches per account, the base URL of the CATS web UI."""

    def __init__(
        self,
        client_getter: Any,
        credential_provider: CredentialProvider,
        configured: str = "",
    ) -> None:
        self._client_getter = client_getter
        self._credentials = credential_provider
        self._configured = configured.rstrip("/")
        self._cache: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, context: Any | None = None) -> str:
        """The UI base URL for this request's account, or "" if unavailable.

        Never raises. A link is a convenience; failing a whole tool call because
        the domain lookup did not work would trade a missing URL for a missing
        answer.
        """
        if self._configured:
            return self._configured

        try:
            credential = await self._credentials.resolve(context)
        except Exception:  # noqa: BLE001 - a link is never worth failing a call
            return ""

        key = _cache_key(credential.api_key)
        if key in self._cache:
            return self._cache[key]

        async with self._lock:
            # Another request may have resolved it while we waited.
            if key in self._cache:
                return self._cache[key]
            domain = await self._fetch(context)
            # Cache the failure too. Otherwise every shaped response on a broken
            # or restricted account re-requests /site, turning one missing link
            # into a steady drain on a 500/hour budget.
            self._cache[key] = domain
            return domain

    async def _fetch(self, context: Any | None) -> str:
        try:
            site = await self._client_getter().request(
                "GET",
                "/site",
                context=context,
                # No retries: a link is optional, and backing off here would add
                # seconds of latency to a tool call that has already succeeded.
                max_attempts=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read /site for the UI domain: %s", exc)
            return ""

        subdomain = site.get("subdomain") if isinstance(site, dict) else None
        if not isinstance(subdomain, str) or not subdomain:
            logger.warning("GET /site returned no subdomain; UI links disabled")
            return ""

        domain = UI_DOMAIN_TEMPLATE.format(subdomain=subdomain)
        logger.info("UI links resolved to %s", domain)
        return domain
