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

import pytest

from cats_mcp.registry.catalog import REGISTRY

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_DOC = ROOT / "docs" / "TOOLS.md"
README = ROOT / "README.md"

COMPOSITE_COUNT = 8
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


#: The complete set of documentation in the working tree. Superseded material
#: is not archived in-tree - git history is the archive.
LIVING_DOCS = [
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOYMENT.md",
    "docs/TOOLS.md",
    "docs/CREDENTIAL-SAFETY.md",
    # Diataxis set covering the id-identity work.
    "docs/RECORD-IDENTITY.md",  # explanation
    "docs/RESPONSE-SHAPING.md",  # reference
    "docs/LINKS.md",  # reference
    "docs/howto-check-a-list.md",  # how-to
    "docs/howto-search-by-location.md",  # how-to
]


@pytest.mark.parametrize("name", LIVING_DOCS)
def test_living_doc_exists(name):
    assert (ROOT / name).is_file()


@pytest.mark.parametrize("name", LIVING_DOCS)
def test_relative_links_resolve(name):
    """Consolidating docs moved files; a dead link is how that goes unnoticed."""
    doc = ROOT / name
    broken = []
    for text, target in re.findall(r"\[([^\]]+)\]\(([^)#]+)\)", doc.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (doc.parent / target).resolve().exists():
            broken.append(f"{text} -> {target}")
    assert not broken, f"{name} has dead links: {broken}"


def test_no_stray_markdown_at_the_repository_root():
    """Only README belongs at the root; the rest lives under docs/."""
    found = {p.name for p in ROOT.glob("*.md")}
    assert found == {"README.md"}, f"unexpected root docs: {sorted(found - {'README.md'})}"


def test_nothing_is_archived_in_the_working_tree():
    """Git history is the archive.

    The repository previously carried 32 files across two archive directories -
    superseded docs, four abandoned server variants, and 832KB of Newman results
    in which all 163 requests returned 401. None of it was reachable from the
    living documentation, and every version of it is still in git history.
    """
    stale = [d for d in (ROOT / "archive", ROOT / "docs" / "archive") if d.exists()]
    assert not stale, f"archive directories reappeared: {[str(d) for d in stale]}"


def test_docs_directory_holds_only_the_living_set():
    found = {f"docs/{p.name}" for p in (ROOT / "docs").glob("*.md")}
    expected = {d for d in LIVING_DOCS if d.startswith("docs/")}
    assert found == expected, f"unexpected docs: {sorted(found - expected)}"


@pytest.mark.parametrize("name", LIVING_DOCS)
def test_living_docs_do_not_reference_archived_modules(name):
    """The toolsets modules and the old cloud docs are gone."""
    text = (ROOT / name).read_text(encoding="utf-8")
    for stale in (
        "toolsets_default",
        "toolsets_recruiting",
        "toolsets_data",
        "response_helpers",
        "--list-toolsets",
        "FASTMCP_CLOUD_",
        "ENDPOINT_COVERAGE_REPORT",
    ):
        assert stale not in text, f"{name} references removed {stale}"


def test_env_example_covers_every_required_setting():
    """A reader copying .env.example should not have to guess."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for variable in ("CATS_API_KEY", "CATS_DISCOVERY_MODE", "CATS_AUTH_MODE", "CATS_TRANSPORT"):
        assert variable in text, f".env.example does not mention {variable}"


def test_env_example_contains_no_real_looking_credential():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("CATS_API_KEY="):
            value = line.split("=", 1)[1]
            assert "your_" in value or not value, f"suspicious value in .env.example: {value}"


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
