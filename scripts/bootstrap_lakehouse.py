"""End-to-end deterministic lakehouse bootstrap script (Silver + dbt Gold <= 60s)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from finops_lakehouse.core.shield import configure_stdio_utf8
from finops_lakehouse.core.silver_bootstrap import materialize_silver_layer

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_lint_dbt_docs() -> Any:
    lint_path = REPO_ROOT / "scripts" / "lint_dbt_docs.py"
    spec = importlib.util.spec_from_file_location("lint_dbt_docs", lint_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load lint_dbt_docs.py from {lint_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_bootstrap(
    *,
    data_dir: Path | None = None,
    duckdb_path: Path | None = None,
    dbt_target_dir: Path | None = None,
    dbt_log_dir: Path | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Execute full Medallion bootstrap: Silver generation -> dbt build -> dbt docs -> lint."""
    started = time.perf_counter()
    resolved_data = (data_dir or (REPO_ROOT / "data")).resolve()
    resolved_duckdb = (duckdb_path or (resolved_data / "gold" / "finops_lakehouse.duckdb")).resolve()
    resolved_duckdb.parent.mkdir(parents=True, exist_ok=True)

    dbt_dir = REPO_ROOT / "dbt"
    resolved_target = (dbt_target_dir or (dbt_dir / "target")).resolve()
    resolved_logs = (dbt_log_dir or (dbt_dir / "logs")).resolve()

    silver_summary = materialize_silver_layer(resolved_data, seed=seed)

    env = os.environ.copy()
    env["FCL_DATA_DIR"] = resolved_data.as_posix()
    env["FCL_DUCKDB_PATH"] = resolved_duckdb.as_posix()
    env["DBT_PROFILES_DIR"] = dbt_dir.as_posix()

    common_flags = [
        "--project-dir",
        dbt_dir.as_posix(),
        "--profiles-dir",
        dbt_dir.as_posix(),
        "--target-path",
        resolved_target.as_posix(),
        "--log-path",
        resolved_logs.as_posix(),
    ]

    for subcmd in (["build", *common_flags], ["docs", "generate", *common_flags]):
        proc = subprocess.run(
            [sys.executable, "-m", "dbt.cli.main", *subcmd],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"dbt {' '.join(subcmd[:2])} failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )

    lint_mod = _load_lint_dbt_docs()
    manifest_path = resolved_target / "manifest.json"
    catalog_path = resolved_target / "catalog.json"
    lint_rc = lint_mod.evaluate_manifest(manifest_path, catalog_path=catalog_path)
    if lint_rc != 0:
        raise RuntimeError(f"lint_dbt_docs.py rejected documentation coverage (rc={lint_rc}).")

    elapsed = round(time.perf_counter() - started, 3)
    return {
        "status": "BOOTSTRAPPED",
        "seed": seed,
        "elapsed_seconds": elapsed,
        "within_60s_sla": elapsed <= 60.0,
        "data_dir": str(resolved_data),
        "duckdb_path": str(resolved_duckdb),
        "docs_lint": "APPROVED",
        "silver_datasets": silver_summary["silver_datasets"],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for end-to-end synthetic lakehouse bootstrap."""
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="Bootstrap synthetic Silver & dbt-duckdb Gold lakehouse in <= 60s."
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--duckdb-path", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    try:
        summary = run_bootstrap(
            data_dir=args.data_dir,
            duckdb_path=args.duckdb_path,
            seed=args.seed,
        )
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        print(f"[ERROR] Bootstrap failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
