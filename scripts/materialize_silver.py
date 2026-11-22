"""CLI entrypoint to materialize deterministic Silver Parquet ZSTD datasets before dbt build."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from finops_lakehouse.core.shield import configure_stdio_utf8, default_repo_root
from finops_lakehouse.core.silver_bootstrap import materialize_silver_layer


def _default_data_dir() -> Path:
    env_dir = os.environ.get("FCL_DATA_DIR", "").strip()
    return Path(env_dir) if env_dir else default_repo_root() / "data"


def main(argv: list[str] | None = None) -> int:
    """Materialize all synthetic Silver datasets deterministically."""
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="Materialize synthetic Silver Parquet ZSTD datasets for dbt-duckdb Gold layer."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_default_data_dir(),
        help="Target Medallion data directory (default: FCL_DATA_DIR or <repo>/data).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic synthetic seed (default: 42).",
    )
    args = parser.parse_args(argv)

    try:
        duckdb_env = os.environ.get("FCL_DUCKDB_PATH", "").strip()
        if duckdb_env:
            Path(duckdb_env).resolve().parent.mkdir(parents=True, exist_ok=True)
        result = materialize_silver_layer(args.data_dir, seed=args.seed)
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        print(f"[ERROR] Silver materialization failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
