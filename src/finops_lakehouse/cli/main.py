"""CLI entrypoint (`fcl`) for finops-capex-revenue-lakehouse."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from finops_lakehouse import __version__


def _configure_stdio_utf8() -> None:
    """Prevent Windows cp1252 crashes on UTF-8 stdout/stderr output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def get_status_payload() -> dict[str, Any]:
    """Return foundational runtime and precision governance status."""
    return {
        "package": "finops-capex-revenue-lakehouse",
        "version": __version__,
        "monetary_precision": "DECIMAL(18,4)",
        "rate_precision": "DECIMAL(12,6)",
        "float_allowed_in_money": False,
        "rounding_mode": "ROUND_HALF_EVEN",
        "deterministic_seed": 42,
        "status": "FOUNDATION_READY",
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for `fcl`."""
    parser = argparse.ArgumentParser(
        prog="fcl",
        description="FinOps, CAPEX/OPEX & Revenue Recovery Medallion Lakehouse CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    status_cmd = subparsers.add_parser("status", help="Display lakehouse foundation status.")
    status_cmd.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning explicit exit codes."""
    _configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "status":
        payload = get_status_payload()
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        print(
            f"{payload['package']} v{payload['version']} | "
            f"{payload['monetary_precision']} ({payload['rounding_mode']}) | "
            f"status={payload['status']}"
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
