"""Environment-backed credential provider - one CATS account per deployment.

This is the right provider for local development, for a dedicated deployment
per customer, and for a customer-hosted server. It is deliberately the default.
"""

from __future__ import annotations

from typing import Any

from cats_mcp.config import Settings
from cats_mcp.credentials.base import CATSCredential, CredentialError, CredentialProvider


class EnvCredentialProvider(CredentialProvider):
    """Resolves a single credential from `CATS_API_KEY` / `CATS_API_BASE_URL`."""

    def __init__(self, settings: Settings, account_label: str = "default") -> None:
        self._settings = settings
        self._account_label = account_label

    async def resolve(self, context: Any | None = None) -> CATSCredential:
        key = self._settings.api_key.strip()
        if not key:
            raise CredentialError(
                "CATS_API_KEY is not set. Set it in the environment (for a Horizon "
                "deployment: Settings -> Environment Variables, then rebuild - "
                "changing a variable does not update a running server)."
            )
        return CATSCredential(
            api_key=key,
            base_url=self._settings.api_base_url,
            account_label=self._account_label,
        )

    def describe(self) -> str:
        configured = "configured" if self._settings.api_key.strip() else "MISSING"
        return f"env ({self._account_label}, CATS_API_KEY {configured})"

    def validate_at_startup(self) -> None:
        """Fail fast rather than on the first tool call.

        A missing key otherwise surfaces as a 401 from CATS during an agent run,
        which reads like a permissions problem rather than a config problem.
        """
        if not self._settings.api_key.strip():
            raise CredentialError(
                "CATS_API_KEY is not set; the server would start but every tool "
                "would fail with a 401 from CATS."
            )
