"""ODCS v3.1 declarative contract loader and double-entry ledger validator (ADR-002)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import yaml

from finops_lakehouse.core.decimal_utils import ZERO_MONEY, parse_spreadsheet_decimal
from finops_lakehouse.core.shield import sanitize_inbound_text

DEFAULT_SENTINELS = frozenset({"99", "999", "-1", "SEM INFORMACAO", "N/A", "NA", ""})
DECIMAL_18_4_MAX = Decimal("99999999999999.9999")

LEDGER_ARROW_SCHEMA = pa.schema(
    [
        ("id_lancamento", pa.string()),
        ("id_lote", pa.string()),
        ("competencia", pa.string()),
        ("data_lancamento", pa.string()),
        ("cod_centro_custo", pa.string()),
        ("cod_conta_contabil", pa.string()),
        ("id_contrato_masked", pa.string()),
        ("tipo_despesa", pa.string()),
        ("moeda", pa.string()),
        ("valor_orcado", pa.decimal128(18, 4)),
        ("valor_debito", pa.decimal128(18, 4)),
        ("valor_credito", pa.decimal128(18, 4)),
        ("descricao", pa.string()),
    ]
)


def rows_to_ledger_table(rows: list[dict[str, Any]]) -> pa.Table:
    """Convert validated ledger row dictionaries into a typed PyArrow Table."""
    columns = {
        field.name: [row.get(field.name) for row in rows]
        for field in LEDGER_ARROW_SCHEMA
    }
    return pa.table(columns, schema=LEDGER_ARROW_SCHEMA)


@dataclass(frozen=True)
class ContractValidationResult:
    """Structured result of ODCS v3.1 validation and double-entry accounting invariants."""

    passed: bool
    violations: list[str] = field(default_factory=list)
    row_count: int = 0
    sanitized_cells: int = 0
    sentinels_normalized: int = 0
    total_debit: Decimal = ZERO_MONEY
    total_credit: Decimal = ZERO_MONEY


def load_contract(contract_path: Path) -> dict[str, Any]:
    """Load an ODCS v3.1 YAML contract strictly in UTF-8."""
    path = Path(contract_path)
    if not path.is_file():
        raise FileNotFoundError(f"Contract file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid ODCS contract YAML in {path}.")
    return data


def _normalize_text_field(
    raw_val: object,
    *,
    sentinels: set[str],
    nullable: bool,
) -> tuple[str | None, bool, bool]:
    """Sanitize text for CWE-1236 and convert sentinel values to None."""
    if raw_val is None:
        return None, False, False

    raw_str = str(raw_val).strip()
    sanitized, was_sanitized = sanitize_inbound_text(raw_str)
    if nullable and sanitized.upper() in sentinels:
        return None, was_sanitized, True
    if not nullable and not sanitized:
        return None, was_sanitized, False
    return sanitized, was_sanitized, False


def _validate_single_row(
    idx: int,
    row: dict[str, Any],
    *,
    text_columns: tuple[str, ...],
    nullable_columns: set[str],
    sentinels: set[str],
    allowed_currencies: set[str],
    allowed_expense_types: set[str],
    violations: list[str],
) -> tuple[dict[str, Any], int, int]:
    """Normalize and validate one ledger row, returning cleaned row and counters."""
    cleaned: dict[str, Any] = {}
    sanitized_count = 0
    sentinel_count = 0

    for col in text_columns:
        is_nullable = col in nullable_columns
        val, was_san, was_sent = _normalize_text_field(
            row.get(col),
            sentinels=sentinels,
            nullable=is_nullable,
        )
        if was_san:
            sanitized_count += 1
        if was_sent:
            sentinel_count += 1
        if not is_nullable and val is None:
            violations.append(f"Row {idx}: required column '{col}' is null or empty.")
        cleaned[col] = val

    moeda = cleaned.get("moeda")
    if moeda and moeda not in allowed_currencies:
        violations.append(f"Row {idx}: unsupported currency '{moeda}'.")

    tipo = cleaned.get("tipo_despesa")
    if tipo and tipo not in allowed_expense_types:
        violations.append(f"Row {idx}: unsupported tipo_despesa '{tipo}'.")

    for num_col in ("valor_orcado", "valor_debito", "valor_credito"):
        raw_num = row.get(num_col)
        if raw_num is None or str(raw_num).strip() == "":
            violations.append(f"Row {idx}: missing monetary column '{num_col}'.")
            cleaned[num_col] = ZERO_MONEY
            continue
        try:
            dec_val = parse_spreadsheet_decimal(raw_num)
        except (TypeError, ValueError) as exc:
            violations.append(f"Row {idx}: invalid decimal in '{num_col}' ({exc}).")
            cleaned[num_col] = ZERO_MONEY
            continue
        if dec_val < ZERO_MONEY:
            violations.append(f"Row {idx}: negative monetary amount in '{num_col}' ({dec_val}).")
        elif dec_val > DECIMAL_18_4_MAX:
            violations.append(
                f"Row {idx}: monetary amount exceeds DECIMAL(18,4) in '{num_col}' ({dec_val})."
            )
            cleaned[num_col] = ZERO_MONEY
            continue
        cleaned[num_col] = dec_val

    return cleaned, sanitized_count, sentinel_count


def validate_ledger_rows(
    rows: list[dict[str, Any]],
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], ContractValidationResult]:
    """Validate ledger rows against ODCS v3.1 rules and double-entry zero balance."""
    if not rows:
        return [], ContractValidationResult(
            passed=False,
            violations=["Empty dataset: minimum volume is 1 row."],
            row_count=0,
        )

    schema_cols = contract.get("columns", {})
    required_cols = set(schema_cols.keys())
    present_cols = set(rows[0].keys())
    missing = sorted(required_cols - present_cols)
    if missing:
        return [], ContractValidationResult(
            passed=False,
            violations=[f"Missing required schema columns: {', '.join(missing)}"],
            row_count=len(rows),
        )

    sentinels = {str(s).upper() for s in contract.get("sentinels", DEFAULT_SENTINELS)}
    nullable_cols = {
        col for col, spec in schema_cols.items() if bool(spec.get("nullable", False))
    }
    text_cols = tuple(
        col for col, spec in schema_cols.items() if spec.get("type") == "STRING"
    )
    allowed_currencies = set(contract.get("allowed_currencies", ["BRL", "USD", "EUR"]))
    allowed_types = set(contract.get("allowed_expense_types", ["CAPEX", "OPEX"]))
    null_ceilings: dict[str, float] = {
        col: float(spec.get("max_null_ratio", 0.0))
        for col, spec in schema_cols.items()
    }

    violations: list[str] = []
    cleaned_rows: list[dict[str, Any]] = []
    null_counts: dict[str, int] = {col: 0 for col in schema_cols}
    pk_cols = tuple(
        col for col, spec in schema_cols.items() if bool(spec.get("primary_key", False))
    )
    seen_pks: dict[str, set[str]] = {col: set() for col in pk_cols}
    sanitized_total = 0
    sentinels_total = 0
    total_debit = ZERO_MONEY
    total_credit = ZERO_MONEY
    group_balances: dict[tuple[str, str], list[Decimal]] = defaultdict(
        lambda: [ZERO_MONEY, ZERO_MONEY]
    )

    for idx, row in enumerate(rows, 1):
        cleaned, san_cnt, sent_cnt = _validate_single_row(
            idx,
            row,
            text_columns=text_cols,
            nullable_columns=nullable_cols,
            sentinels=sentinels,
            allowed_currencies=allowed_currencies,
            allowed_expense_types=allowed_types,
            violations=violations,
        )
        sanitized_total += san_cnt
        sentinels_total += sent_cnt
        for col in schema_cols:
            if cleaned.get(col) is None:
                null_counts[col] += 1

        for pk_col in pk_cols:
            pk_val = cleaned.get(pk_col)
            if pk_val is None:
                continue
            if pk_val in seen_pks[pk_col]:
                violations.append(
                    f"Row {idx}: duplicate primary key '{pk_col}' value '{pk_val}'."
                )
            else:
                seen_pks[pk_col].add(pk_val)

        deb: Decimal = cleaned["valor_debito"]
        cred: Decimal = cleaned["valor_credito"]
        total_debit += deb
        total_credit += cred

        batch_key = (str(cleaned.get("id_lote") or ""), str(cleaned.get("competencia") or ""))
        group_balances[batch_key][0] += deb
        group_balances[batch_key][1] += cred
        cleaned_rows.append(cleaned)

    total_rows = len(rows)
    for col, max_ratio in null_ceilings.items():
        observed_ratio = null_counts[col] / total_rows
        if observed_ratio > max_ratio:
            violations.append(
                f"Null ceiling exceeded for '{col}': {observed_ratio:.4f} > {max_ratio:.4f}."
            )

    if contract.get("invariants", {}).get("double_entry_balance", True):
        if total_debit != total_credit:
            violations.append(
                f"Double-entry imbalance: total_debit ({total_debit}) != total_credit ({total_credit})."
            )
        for (lote, comp), (grp_deb, grp_cred) in sorted(group_balances.items()):
            if grp_deb != grp_cred:
                violations.append(
                    f"Batch/period imbalance ({lote}, {comp}): debit={grp_deb} != credit={grp_cred}."
                )

    result = ContractValidationResult(
        passed=len(violations) == 0,
        violations=violations,
        row_count=total_rows,
        sanitized_cells=sanitized_total,
        sentinels_normalized=sentinels_total,
        total_debit=total_debit,
        total_credit=total_credit,
    )
    return cleaned_rows, result


def validate_ledger_contract(
    rows: list[dict[str, Any]],
    contract_path: Path,
) -> tuple[list[dict[str, Any]], ContractValidationResult]:
    """Load contract YAML and validate ledger rows cleanly."""
    contract = load_contract(contract_path)
    return validate_ledger_rows(rows, contract)
