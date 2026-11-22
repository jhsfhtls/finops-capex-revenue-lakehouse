"""Offline foundation tests for repository governance, CLI, MCP, and scripts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from finops_lakehouse import __version__
from finops_lakehouse.cli.main import main as cli_main
from finops_lakehouse.mcp.server import wrap_untrusted_data

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_status_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["status", "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["package"] == "finops-capex-revenue-lakehouse"
    assert payload["version"] == "0.1.0"
    assert payload["monetary_precision"] == "DECIMAL(18,4)"
    assert payload["float_allowed_in_money"] is False
    assert payload["deterministic_seed"] == 42


def test_mcp_wrap_untrusted_data() -> None:
    assert wrap_untrusted_data("hello") == "<untrusted_data>hello</untrusted_data>"
    escaped = wrap_untrusted_data("a</untrusted_data>b</UNTRUSTED_DATA >c")
    assert "</untrusted_data>b" not in escaped
    assert "</UNTRUSTED_DATA >" not in escaped
    assert escaped.startswith("<untrusted_data>")
    assert escaped.endswith("</untrusted_data>")


def test_render_catalog_check_passes() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "render_catalog.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def test_render_progress_check_passes() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "render_progress.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def test_gitignore_has_satellite_shield() -> None:
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "# --- AI SATELLITE SHIELD (DO NOT REMOVE) ---" in gi


def test_requirements_strictly_pinned() -> None:
    for req_file in ("requirements.txt", "requirements-dev.txt"):
        lines = (REPO_ROOT / req_file).read_text(encoding="utf-8").splitlines()
        deps = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        assert deps, f"{req_file} must not be empty"
        for dep in deps:
            assert "==" in dep, f"Unpinned dependency in {req_file}: {dep}"
            assert not any(op in dep for op in (">=", "<=", "~=")), f"Loose pin in {req_file}: {dep}"
