"""Every path a deployment might be pointed at must load a working server.

This exists because of a real build failure. Horizon reads its entrypoint from
its own server configuration, **not** from `fastmcp.json`, so moving
`server_all_tools.py` into the archive broke the deployment even though the
repo's own config had been updated to the new path:

    Build failed: Couldn't find the entrypoint `server_all_tools.py`

Both historical root paths are therefore kept as thin re-export shims. Each also
bootstraps `src/` onto `sys.path`, because a build that installs only a
dependency file never installs this project as a package - without that, the
shim fails with a bare ModuleNotFoundError.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Every path a client config or deployment might name.
ENTRYPOINTS = [
    "server.py",  # stdio clients: Claude Desktop, Cursor
    "server_all_tools.py",  # the path the Horizon deployment was built against
    "src/cats_mcp/app.py",  # canonical; what fastmcp.json declares
]


@pytest.mark.parametrize("path", ENTRYPOINTS)
def test_entrypoint_file_exists(path):
    assert (ROOT / path).is_file(), f"{path} is referenced by a deployment or client config"


@pytest.mark.parametrize("path", ENTRYPOINTS)
def test_entrypoint_exposes_a_populated_server(path):
    """Load the file the way a runner does and confirm it yields real tools."""
    script = f"""
import asyncio, importlib.util, sys
spec = importlib.util.spec_from_file_location("entry", r"{ROOT / path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert hasattr(module, "mcp"), "no `mcp` object"
count = len(asyncio.run(module.mcp.list_tools()))
assert count > 0, "server exposes no tools"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **_clean_env(),
            "CATS_API_KEY": "test-key",
            "PYTHONIOENCODING": "utf-8",
        },
    )
    assert "OK" in result.stdout, result.stderr[-2000:]


@pytest.mark.parametrize("path", ["server.py", "server_all_tools.py"])
def test_root_shims_bootstrap_src_onto_the_path(path):
    """Asserted textually because the test environment has the package installed.

    A build that installs only requirements.txt does not, and that is the case
    that actually broke. Removing this bootstrap would pass every other test in
    the suite and still fail in production.
    """
    text = (ROOT / path).read_text(encoding="utf-8")
    assert 'parent / "src"' in text, f"{path} lost its src bootstrap"
    assert "sys.path.insert" in text, f"{path} lost its src bootstrap"


@pytest.mark.parametrize("path", ["server.py", "server_all_tools.py"])
def test_root_shims_are_shims_not_second_implementations(path):
    """The original problem was two divergent servers, not one file too few."""
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "from cats_mcp.app import" in text
    for duplicated in ("async def make_request", "@mcp.tool", "httpx.AsyncClient"):
        assert duplicated not in text, f"{path} contains its own implementation: {duplicated}"


def test_fastmcp_json_entrypoint_is_a_real_file():
    import json

    config = json.loads((ROOT / "fastmcp.json").read_text(encoding="utf-8"))
    declared = config["source"]["path"]
    assert (ROOT / declared).is_file(), f"fastmcp.json points at missing {declared}"


def _clean_env() -> dict[str, str]:
    """A minimal environment, so a stray CATS_* var cannot change the outcome."""
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT", "COMSPEC", "HOME", "USERPROFILE")
    return {k: v for k, v in os.environ.items() if k in keep}
