"""Documentation must not drift from the registry.

The audit found five different tool counts across README, server startup logs,
tests and design documents, four of them wrong, all hand-maintained. These tests
make that class of error impossible to reintroduce quietly.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

from cats_mcp.registry.catalog import REGISTRY

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_DOC = ROOT / "docs" / "TOOLS.md"
README = ROOT / "README.md"

COMPOSITE_COUNT = 5
STATUS_TOOL_COUNT = 1
TOTAL = len(REGISTRY) + COMPOSITE_COUNT + STATUS_TOOL_COUNT


def test_generated_tool_docs_are_current():
    """Fails when the registry changed but docs/TOOLS.md was not regenerated."""
    result = subprocess.run(
        [sys.executable, "scripts/generate_tool_docs.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_tools_doc_reports_the_registry_count():
    text = TOOLS_DOC.read_text(encoding="utf-8")
    assert f"**{TOTAL} tools total**" in text
    assert f"| **total** | **{len(REGISTRY)}** |" in text


async def test_documented_primitive_counts_match_the_running_server():
    """The doc's resource and prompt counts are hand-set constants; verify them
    against a real server rather than trusting that they were kept current."""
    import httpx2
    from fastmcp import Client

    import scripts.generate_tool_docs as gen  # noqa: PLC0415
    from cats_mcp.config import DiscoveryMode, Settings
    from cats_mcp.credentials.base import CATSCredential, CredentialProvider
    from cats_mcp.http.client import CATSClient
    from cats_mcp.server import create_server

    class Stub(CredentialProvider):
        async def resolve(self, context=None):
            return CATSCredential(api_key="k", base_url="https://api.catsone.com/v3")

        def describe(self):
            return "stub"

    settings = Settings(api_key="k", discovery_mode=DiscoveryMode.RAW)
    server = create_server(
        settings,
        credential_provider=Stub(),
        client=CATSClient(
            settings,
            Stub(),
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={})),
        ),
    )

    async with Client(server) as client:
        assert len(await client.list_resources()) == gen.RESOURCE_COUNT
        assert len(await client.list_resource_templates()) == gen.TEMPLATE_COUNT
        assert len(await client.list_prompts()) == gen.PROMPT_COUNT


def test_readme_quotes_no_stale_tool_counts():
    """The README previously claimed 228 tools; the real figure was 186."""
    text = README.read_text(encoding="utf-8")
    stale = {"228", "189", "186", "163", "164"}
    found = {n for n in re.findall(r"\b\d{3}\b", text) if n in stale}
    assert not found, (
        f"README contains stale tool counts {sorted(found)}. Counts belong in "
        f"docs/TOOLS.md, which is generated from the registry."
    )


def test_readme_points_at_the_generated_doc():
    text = README.read_text(encoding="utf-8")
    assert "docs/TOOLS.md" in text


def test_readme_does_not_reference_deleted_modules():
    """server_all_tools.py and the toolsets modules are archived."""
    text = README.read_text(encoding="utf-8")
    for stale in ("server_all_tools", "toolsets_default", "toolsets_recruiting", "toolsets_data"):
        assert stale not in text, f"README references archived module {stale}"


def test_fastmcp_json_points_at_the_real_entrypoint():
    """It previously started server_all_tools.py, which ignored CATS_TOOLSETS."""
    import json

    config = json.loads((ROOT / "fastmcp.json").read_text(encoding="utf-8"))
    assert config["source"]["path"] == "src/cats_mcp/app.py"
    assert config["source"]["entrypoint"] == "mcp"


def test_dependency_pins_agree_across_every_source():
    """pyproject, requirements.txt and fastmcp.json must not disagree.

    They previously named three different FastMCP floors.
    """
    import json

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    fastmcp_json = json.loads((ROOT / "fastmcp.json").read_text(encoding="utf-8"))

    def pins(text: str) -> dict[str, str]:
        return dict(re.findall(r"^\s*\"?([a-z0-9_-]+)==([0-9a-z.]+)\"?,?\s*$", text, re.MULTILINE))

    py_pins = pins(pyproject)
    req_pins = pins(requirements)
    json_pins = dict(
        p.split("==") for p in fastmcp_json["environment"]["dependencies"] if "==" in p
    )

    for package in ("fastmcp", "httpx2", "pydantic", "python-dotenv"):
        versions = {
            "pyproject.toml": py_pins.get(package),
            "requirements.txt": req_pins.get(package),
            "fastmcp.json": json_pins.get(package),
        }
        distinct = {v for v in versions.values() if v}
        assert len(distinct) == 1, f"{package} pinned inconsistently: {versions}"


def test_httpx_is_not_a_dependency():
    """FastMCP 4 uses httpx2 and does not ship httpx."""
    for name in ("pyproject.toml", "requirements.txt"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*\"?httpx==", text, re.MULTILINE), (
            f"{name} pins httpx; FastMCP 4 uses httpx2"
        )
