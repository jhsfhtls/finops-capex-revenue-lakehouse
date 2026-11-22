"""Stdio MCP server stub (`fcl-mcp`) with untrusted-data isolation (ADR-003)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence

from finops_lakehouse import __version__

_CLOSE_TAG_RE = re.compile(r"</\s*untrusted_data\s*>", re.IGNORECASE)


def _configure_stdio_utf8() -> None:
    """Prevent Windows cp1252 crashes on UTF-8 stdout/stderr output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def wrap_untrusted_data(text: str) -> str:
    """Wrap external untrusted text in explicit XML delimiters (OWASP LLM01)."""
    sanitized = _CLOSE_TAG_RE.sub("&lt;/untrusted_data&gt;", text)
    return f"<untrusted_data>{sanitized}</untrusted_data>"


def build_parser() -> argparse.ArgumentParser:
    """Build parser for the `fcl-mcp` CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="fcl-mcp",
        description="FinOps CAPEX/Revenue Lakehouse MCP server.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify MCP server metadata without opening a stdio loop.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """MCP entrypoint returning explicit exit codes."""
    _configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check:
        print(
            json.dumps(
                {
                    "server": "fcl-mcp",
                    "version": __version__,
                    "prompt_injection_delimiter": "untrusted_data",
                    "status": "FOUNDATION_READY",
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "server": "fcl-mcp",
                "version": __version__,
                "status": "WAITING_PHASE_07_TOOLS",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
