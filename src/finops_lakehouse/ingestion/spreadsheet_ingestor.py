"""Safe CSV/XLSX ingestor and exporter enforcing ODCS v3.1, CWE-1236, and Client Path Shield."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import openpyxl

from finops_lakehouse.core.contracts import (
    load_contract,
    rows_to_ledger_table,
    validate_ledger_rows,
)
from finops_lakehouse.core.shield import (
    enforce_client_path_shield,
    sanitize_cell_for_export,
    validate_spreadsheet_file,
)
from finops_lakehouse.core.storage import HybridStorageEngine
from finops_lakehouse.generators.capex_opex_ledger import DEFAULT_CONTRACT_PATH


def _read_csv_rows(csv_path: Path) -> list[dict[str, Any]]:
    """Load CSV rows safely in UTF-8 (stripping optional BOM)."""
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        return [dict(row) for row in reader]


def _read_xlsx_rows(xlsx_path: Path) -> list[dict[str, Any]]:
    """Load XLSX rows safely in read-only mode, recovering unevaluated formulas for sanitization."""
    wb_values = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    wb_formulas = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=False)
    try:
        ws_val = wb_values.active
        ws_form = wb_formulas.active
        if ws_val is None or ws_form is None:
            return []
        val_iter = ws_val.iter_rows(values_only=True)
        form_iter = ws_form.iter_rows(values_only=True)
        headers_raw = next(val_iter, None)
        next(form_iter, None)
        if not headers_raw:
            return []
        headers = [str(h).strip() if h is not None else "" for h in headers_raw]
        records: list[dict[str, Any]] = []
        for val_row, form_row in zip(val_iter, form_iter, strict=False):
            merged = tuple(
                f_cell
                if (v_cell is None and isinstance(f_cell, str) and f_cell.startswith("="))
                else v_cell
                for v_cell, f_cell in zip(val_row, form_row, strict=False)
            )
            if all(v is None for v in merged):
                continue
            records.append(dict(zip(headers, merged, strict=False)))
        return records
    finally:
        wb_values.close()
        wb_formulas.close()


def ingest_ledger_file(
    input_path: Path,
    *,
    output_data_dir: Path,
    repo_root: Path | None = None,
    is_external_client_data: bool = False,
    contract_path: Path | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Validate, sanitize, and ingest a CSV/XLSX ledger into Silver Parquet or Quarantine."""
    enforce_client_path_shield(
        output_data_dir,
        repo_root,
        is_external_client_data=is_external_client_data,
    )

    source_path = Path(input_path)
    file_kind = validate_spreadsheet_file(source_path)
    raw_rows = _read_csv_rows(source_path) if file_kind == "csv" else _read_xlsx_rows(source_path)

    contract = load_contract(contract_path or DEFAULT_CONTRACT_PATH)
    cleaned_rows, validation = validate_ledger_rows(raw_rows, contract)

    resolved_batch_id = batch_id or f"ingest_{source_path.stem}"
    storage = HybridStorageEngine(output_data_dir)

    if not validation.passed:
        q_path = storage.write_quarantine(
            "capex_opex_ledger",
            resolved_batch_id,
            validation.violations,
            cleaned_rows,
        )
        return {
            "status": "QUARANTINED",
            "batch_id": resolved_batch_id,
            "row_count": validation.row_count,
            "sanitized_cells": validation.sanitized_cells,
            "sentinels_normalized": validation.sentinels_normalized,
            "violations": validation.violations,
            "quarantine_path": str(q_path),
        }

    table = rows_to_ledger_table(cleaned_rows)
    manifest = storage.write_silver_parquet(
        "capex_opex_ledger",
        "ledger_entries",
        table,
        batch_id=resolved_batch_id,
    )
    manifest["sanitized_cells"] = validation.sanitized_cells
    manifest["sentinels_normalized"] = validation.sentinels_normalized
    return manifest


def export_rows_to_csv_safe(
    rows: list[dict[str, Any]],
    output_path: Path,
    *,
    repo_root: Path | None = None,
    is_external_client_data: bool = False,
) -> Path:
    """Export rows to UTF-8 LF CSV with CWE-1236 formula neutralization and Path Shield."""
    target = enforce_client_path_shield(
        output_path,
        repo_root,
        is_external_client_data=is_external_client_data,
    )
    if not rows:
        raise ValueError("Cannot export empty row list to CSV.")

    target.parent.mkdir(parents=True, exist_ok=True)
    headers = list(rows[0].keys())
    safe_headers = [str(sanitize_cell_for_export(h)) for h in headers]

    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(safe_headers)
        for row in rows:
            writer.writerow([sanitize_cell_for_export(row.get(h)) for h in headers])
    return target


def export_rows_to_xlsx_safe(
    rows: list[dict[str, Any]],
    output_path: Path,
    *,
    repo_root: Path | None = None,
    is_external_client_data: bool = False,
) -> Path:
    """Export rows to XLSX with CWE-1236 formula neutralization and Client Path Shield."""
    target = enforce_client_path_shield(
        output_path,
        repo_root,
        is_external_client_data=is_external_client_data,
    )
    if not rows:
        raise ValueError("Cannot export empty row list to XLSX.")

    target.parent.mkdir(parents=True, exist_ok=True)
    headers = list(rows[0].keys())
    safe_headers = [str(sanitize_cell_for_export(h)) for h in headers]

    workbook = openpyxl.Workbook()
    try:
        sheet = workbook.active
        sheet.title = "ledger_export"
        sheet.append(safe_headers)
        for row in rows:
            sheet.append([sanitize_cell_for_export(row.get(h)) for h in headers])
        workbook.save(target)
    finally:
        workbook.close()
    return target
