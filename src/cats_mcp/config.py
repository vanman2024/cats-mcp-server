"""Runtime configuration for the CATS MCP adapter.

Every setting is read from the environment. Nothing here knows about StaffHive,
recruiting workflows, or any particular consumer.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Horizon resolves `${VAR}` placeholders in fastmcp.json against the process
# environment, and FastMCP documents that "if a variable doesn't exist, the
# placeholder is preserved as-is". An unset Horizon variable therefore arrives
# as the literal string rather than as an error, which surfaces much later as a
# confusing 401 from CATS. Catch it at startup instead.
_UNRESOLVED_PLACEHOLDER = re.compile(r"^\$\{[^}]*\}$")


class DiscoveryMode(str, Enum):
    """How the tool catalog is exposed to the connected client.

    RAW     full authorized catalog, no transform. For orchestrators such as
            Mastra that run their own cross-server tool discovery. Protected:
            not the public default.
    SEARCH  BM25 search transform. The model sees `search_tools`, `call_tool`,
            and a small set of pinned tools. For direct MCP clients.
    CODE    Code Mode. Requires the `code-mode` extra. Experimental.
    """

    RAW = "raw"
    SEARCH = "search"
    CODE = "code"


class Transport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CATS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- CATS API -----------------------------------------------------------
    api_base_url: str = "https://api.catsone.com/v3"
    api_key: str = ""

    # --- Discovery ----------------------------------------------------------
    # SEARCH is the default because RAW must not be the public default: it
    # exposes the entire catalog to anyone who can reach the endpoint.
    discovery_mode: DiscoveryMode = DiscoveryMode.SEARCH
    search_max_results: int = 5

    # --- Toolsets -----------------------------------------------------------
    # Comma-separated, or "all". Honoured by every entrypoint. The previous
    # implementation accepted this variable and silently ignored it.
    toolsets: str = ""

    # --- Transport ----------------------------------------------------------
    transport: Transport = Transport.STDIO
    host: str = "0.0.0.0"
    port: int = 8000

    # --- HTTP client --------------------------------------------------------
    request_timeout: float = 30.0
    max_retries: int = 4
    max_connections: int = 20
    max_keepalive_connections: int = 10

    # --- Response shaping ---------------------------------------------------
    # Two page sizes, deliberately distinct. CATS allows up to 100 per request
    # and the standard rate limit is 500 requests/hour, so fetching wide costs
    # nothing extra against the budget while fetching narrow costs a request per
    # page. The model, meanwhile, wants a small page. Fetch wide, return narrow.
    api_page_size: int = Field(default=100, ge=1, le=100)
    model_page_size: int = Field(default=10, ge=1, le=100)

    # --- MCP authentication -------------------------------------------------
    # When unset the server refuses to start in HTTP transport unless
    # `allow_unauthenticated_http` is explicitly set. Local stdio use needs no
    # MCP-layer auth because the transport is already private to the user.
    auth_jwks_uri: str = ""
    auth_issuer: str = ""
    auth_audience: str = ""
    allow_unauthenticated_http: bool = False

    @field_validator("api_key", "auth_jwks_uri", "auth_issuer", "auth_audience")
    @classmethod
    def _reject_unresolved_placeholder(cls, value: str, info) -> str:
        if value and _UNRESOLVED_PLACEHOLDER.match(value.strip()):
            raise ValueError(
                f"{info.field_name} is the literal string {value!r}, which means a "
                f"`${{...}}` placeholder was never resolved. Set the variable in the "
                f"deployment environment (for Horizon: Settings -> Environment "
                f"Variables, then rebuild - changing a variable does not update a "
                f"running server)."
            )
        return value

    @property
    def requested_toolsets(self) -> set[str] | None:
        """Parsed CATS_TOOLSETS, or None to mean 'the default set'."""
        raw = self.toolsets.strip()
        if not raw:
            return None
        return {part.strip() for part in raw.split(",") if part.strip()}


def load_settings() -> Settings:
    return Settings()
