"""Linter ensuring 100% column and model documentation coverage in the dbt project."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "dbt" / "target" / "manifest.json"
MODEL_PREFIX = "model.finops_capex_revenue_lakehouse."


def evaluate_manifest(manifest_path: Path) -> int:
    """Validate 100% documentation coverage for dbt models and columns."""
    if not manifest_path.exists():
        print(
            f"[ERROR] dbt manifest not found at {manifest_path}. Run 'dbt docs generate' first.",
            file=sys.stderr,
        )
        return 1

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})

    total_models = 0
    total_columns = 0
    undocumented_models: list[str] = []
    undocumented_sources: list[str] = []
    undocumented_columns: list[str] = []

    for unique_id, node in nodes.items():
        if not unique_id.startswith(MODEL_PREFIX):
            continue

        model_name = str(node.get("name", unique_id))
        total_models += 1

        if not str(node.get("description", "")).strip():
            undocumented_models.append(model_name)

        for col_name, col_meta in node.get("columns", {}).items():
            total_columns += 1
            if not str(col_meta.get("description", "")).strip():
                undocumented_columns.append(f"{model_name}.{col_name}")

    for unique_id, source in sources.items():
        source_label = f"{source.get('source_name', 'source')}.{source.get('name', unique_id)}"
        if not str(source.get("description", "")).strip():
            undocumented_sources.append(source_label)
        for col_name, col_meta in source.get("columns", {}).items():
            total_columns += 1
            if not str(col_meta.get("description", "")).strip():
                undocumented_columns.append(f"{source_label}.{col_name}")

    print("=================================================================")
    print("dbt Documentation & Metadata Governance Linter (ODCS v3.1 / ADR-002)")
    print("=================================================================")
    print(f"Total Models Evaluated:  {total_models}")
    print(f"Total Columns Evaluated: {total_columns}")
    print(f"Total Sources Evaluated: {len(sources)}")

    has_errors = False
    if undocumented_models:
        print(f"\n[FAIL] Found {len(undocumented_models)} undocumented models:")
        for model in undocumented_models:
            print(f"  - {model}")
        has_errors = True

    if undocumented_sources:
        print(f"\n[FAIL] Found {len(undocumented_sources)} undocumented sources:")
        for source_label in undocumented_sources:
            print(f"  - {source_label}")
        has_errors = True

    if undocumented_columns:
        print(f"\n[FAIL] Found {len(undocumented_columns)} undocumented columns:")
        for col in undocumented_columns:
            print(f"  - {col}")
        has_errors = True

    if has_errors:
        print("\nResult: REJECTED - 100% model and column documentation is mandatory.", file=sys.stderr)
        return 1

    print("\nResult: APPROVED - 100% Documentation Coverage across all models and columns!")
    print("=================================================================")
    return 0


def main() -> int:
    """CLI entrypoint for dbt documentation coverage linter."""
    parser = argparse.ArgumentParser(description="Validate 100% dbt model and column documentation.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Path to dbt manifest.json")
    args = parser.parse_args()
    return evaluate_manifest(args.manifest)


if __name__ == "__main__":
    sys.exit(main())
