"""Core package acceptance with the optional MCP dependency genuinely unavailable to imports."""

import subprocess
import sys

BLOCK_MCP = """
import importlib.abc, sys
class NoMcp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mcp" or fullname.startswith("mcp."):
            raise ModuleNotFoundError("optional mcp deliberately unavailable", name=fullname)
sys.meta_path.insert(0, NoMcp())
"""


def test_core_cli_help_does_not_require_mcp() -> None:
    script = (
        BLOCK_MCP
        + """
from typer.testing import CliRunner
from nyx.cli.main import app
result = CliRunner().invoke(app, ["--help"])
assert result.exit_code == 0, result.output
assert "doctor" in result.output
assert "mcp" not in sys.modules
print("core-help-without-mcp-ok")
"""
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and owned negative-test script
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "core-help-without-mcp-ok" in result.stdout


def test_explicit_mcp_build_reports_typed_missing_extra() -> None:
    script = (
        BLOCK_MCP
        + """
from nyx.conversational.mcp_server import build_server
try:
    build_server()
except Exception as exc:
    assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE", type(exc).__name__
    assert "nyx[conversational]" in str(exc)
    print("typed-optional-refusal-ok")
else:
    raise AssertionError("MCP was deliberately unavailable")
"""
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and owned negative-test script
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "typed-optional-refusal-ok" in result.stdout


def test_explicit_mcp_entrypoint_refuses_without_traceback() -> None:
    script = (
        BLOCK_MCP
        + """
from nyx.conversational.mcp_server import main
main()
"""
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and owned negative-test script
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 2
    assert "DEPENDENCY_UNAVAILABLE" in result.stderr
    assert "Traceback" not in result.stderr
