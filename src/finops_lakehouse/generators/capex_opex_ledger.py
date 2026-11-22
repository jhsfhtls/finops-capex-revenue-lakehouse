"""Deterministic 24-month (~50k row) synthetic CAPEX/OPEX double-entry ledger generator."""

from __future__ import annotations

import random
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from finops_lakehouse.core.contracts import (
    LEDGER_ARROW_SCHEMA,
    load_contract,
    rows_to_ledger_table,
    validate_ledger_rows,
)
from finops_lakehouse.core.decimal_utils import MONEY_QUANT, ZERO_MONEY, format_money
from finops_lakehouse.core.storage import HybridStorageEngine

DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "catalog"
    / "contracts"
    / "capex_opex_ledger.contract.yaml"
)

_COST_CENTERS: tuple[tuple[str, str], ...] = (
    ("CC-0100-INFRA", "CAPEX"),
    ("CC-0200-CLOUD", "OPEX"),
    ("CC-0300-ENG", "CAPEX"),
    ("CC-0400-DATA", "OPEX"),
    ("CC-0500-OPS", "OPEX"),
    ("CC-0600-COM", "OPEX"),
)

_CAPEX_ACCOUNTS: tuple[str, ...] = ("1.02.01.001", "1.02.01.002", "1.02.02.001")
_OPEX_ACCOUNTS: tuple[str, ...] = ("4.01.01.001", "4.01.01.002", "4.01.02.001", "4.01.03.001")
_CREDIT_OFFSET_ACCOUNTS: tuple[str, ...] = ("1.01.01.001", "2.01.01.001")

# Monthly seasonality factors expressed in integer basis points (10000 = 1.0000x).
_SEASONALITY_BPS: tuple[int, ...] = (
    9200,
    9400,
    9800,
    10000,
    10200,
    10500,
    9900,
    10100,
    10600,
    11200,
    11800,
    12600,
)

_BPS_DIVISOR = Decimal("10000")
_CENTS_DIVISOR = Decimal("100")


def _month_competencias(start_year: int, start_month: int, months: int) -> list[tuple[int, int, str]]:
    """Build ordered (year, month, YYYY-MM) tuples without external calendar deps."""
    result: list[tuple[int, int, str]] = []
    for offset in range(months):
        total_months = (start_year * 12 + (start_month - 1)) + offset
        year = total_months // 12
        month = (total_months % 12) + 1
        result.append((year, month, f"{year:04d}-{month:02d}"))
    return result


def generate_synthetic_ledger_rows(
    seed: int = 42,
    months: int = 24,
    pairs_per_month: int = 1042,
) -> list[dict[str, Any]]:
    """Generate balanced synthetic CAPEX/OPEX double-entry ledger rows using pure Decimal/int."""
    if months <= 0 or pairs_per_month <= 0:
        raise ValueError("months and pairs_per_month must be positive integers.")

    rng = random.Random(seed)
    periods = _month_competencias(2024, 1, months)
    rows: list[dict[str, Any]] = []

    for year, month, competencia in periods:
        season_bps = _SEASONALITY_BPS[month - 1]
        lote_id = f"LOTE-{year:04d}{month:02d}"

        for pair_idx in range(1, pairs_per_month + 1):
            cost_center, default_type = _COST_CENTERS[(pair_idx - 1) % len(_COST_CENTERS)]
            tipo_despesa = "CAPEX" if (default_type == "CAPEX" or pair_idx % 5 == 0) else "OPEX"
            debit_account = (
                rng.choice(_CAPEX_ACCOUNTS)
                if tipo_despesa == "CAPEX"
                else rng.choice(_OPEX_ACCOUNTS)
            )
            credit_account = _CREDIT_OFFSET_ACCOUNTS[pair_idx % len(_CREDIT_OFFSET_ACCOUNTS)]
            day = ((pair_idx - 1) % 28) + 1
            data_lanc = f"{competencia}-{day:02d}"

            base_cents = rng.randint(150_00, 85_000_00)
            realized_amount = (
                (Decimal(base_cents) * Decimal(season_bps)) / (_CENTS_DIVISOR * _BPS_DIVISOR)
            ).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

            # Budget variance factor: 0.8800x to 1.1400x so both favorable & unfavorable gaps occur.
            variance_bps = rng.randint(8800, 11400)
            budget_amount = ((realized_amount * Decimal(variance_bps)) / _BPS_DIVISOR).quantize(
                MONEY_QUANT,
                rounding=ROUND_HALF_EVEN,
            )

            contrato_id = (
                None
                if pair_idx % 17 == 0
                else f"CTR-SYN-{(pair_idx % 90) + 10:04d}"
            )
            seq = f"{year:04d}{month:02d}-{pair_idx:05d}"

            # Debit leg (carries budget allocation for variance comparison)
            rows.append(
                {
                    "id_lancamento": f"LNC-{seq}-D",
                    "id_lote": lote_id,
                    "competencia": competencia,
                    "data_lancamento": data_lanc,
                    "cod_centro_custo": cost_center,
                    "cod_conta_contabil": debit_account,
                    "id_contrato_masked": contrato_id,
                    "tipo_despesa": tipo_despesa,
                    "moeda": "BRL",
                    "valor_orcado": budget_amount,
                    "valor_debito": realized_amount,
                    "valor_credito": ZERO_MONEY,
                    "descricao": f"Synthetic {tipo_despesa} appropriation {competencia}",
                }
            )
            # Symmetric credit leg (guarantees exact double-entry zero balance)
            rows.append(
                {
                    "id_lancamento": f"LNC-{seq}-C",
                    "id_lote": lote_id,
                    "competencia": competencia,
                    "data_lancamento": data_lanc,
                    "cod_centro_custo": cost_center,
                    "cod_conta_contabil": credit_account,
                    "id_contrato_masked": contrato_id,
                    "tipo_despesa": tipo_despesa,
                    "moeda": "BRL",
                    "valor_orcado": ZERO_MONEY,
                    "valor_debito": ZERO_MONEY,
                    "valor_credito": realized_amount,
                    "descricao": f"Synthetic {tipo_despesa} offset settlement {competencia}",
                }
            )

    return rows


def generate_synthetic_ledger(
    seed: int = 42,
    months: int = 24,
    pairs_per_month: int = 1042,
) -> tuple[pa.Table, dict[str, Any]]:
    """Generate a deterministic PyArrow Table and summary metadata for the synthetic ledger."""
    rows = generate_synthetic_ledger_rows(
        seed=seed,
        months=months,
        pairs_per_month=pairs_per_month,
    )
    table = rows_to_ledger_table(rows)
    assert table.schema == LEDGER_ARROW_SCHEMA

    total_debit = sum((r["valor_debito"] for r in rows), ZERO_MONEY)
    total_credit = sum((r["valor_credito"] for r in rows), ZERO_MONEY)
    total_budget = sum((r["valor_orcado"] for r in rows), ZERO_MONEY)
    sha256 = HybridStorageEngine.compute_table_sha256(table)

    metadata = {
        "seed": seed,
        "months": months,
        "row_count": table.num_rows,
        "sha256": sha256,
        "total_debit": format_money(total_debit),
        "total_credit": format_money(total_credit),
        "total_budget": format_money(total_budget),
        "double_entry_balanced": total_debit == total_credit,
    }
    return table, metadata


def materialize_synthetic_ledger(
    data_dir: Path,
    *,
    seed: int = 42,
    months: int = 24,
    pairs_per_month: int = 1042,
    contract_path: Path | None = None,
) -> dict[str, Any]:
    """Generate, ODCS-validate, and persist synthetic CAPEX/OPEX ledger to Silver Parquet."""
    rows = generate_synthetic_ledger_rows(
        seed=seed,
        months=months,
        pairs_per_month=pairs_per_month,
    )
    resolved_contract = contract_path or DEFAULT_CONTRACT_PATH
    contract = load_contract(resolved_contract)
    cleaned_rows, validation = validate_ledger_rows(rows, contract)

    storage = HybridStorageEngine(data_dir)
    batch_id = f"synthetic_capex_opex_seed{seed}_{months}m"
    if not validation.passed:
        q_path = storage.write_quarantine(
            "capex_opex_ledger",
            batch_id,
            validation.violations,
            cleaned_rows,
        )
        return {
            "status": "QUARANTINED",
            "batch_id": batch_id,
            "violations": validation.violations,
            "quarantine_path": str(q_path),
        }

    table = rows_to_ledger_table(cleaned_rows)
    manifest = storage.write_silver_parquet(
        "capex_opex_ledger",
        "ledger_entries",
        table,
        batch_id=batch_id,
    )
    manifest["seed"] = seed
    manifest["months"] = months
    manifest["total_debit"] = format_money(validation.total_debit)
    manifest["total_credit"] = format_money(validation.total_credit)
    return manifest
