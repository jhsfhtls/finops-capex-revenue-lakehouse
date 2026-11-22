"""Deterministic Silver layer bootstrapper feeding the Phase 06 dbt-duckdb Gold layer."""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from finops_lakehouse.calculators.b2b_economics import evaluate_b2b_deal
from finops_lakehouse.core.contracts import (
    DECIMAL_18_4_MAX,
    ContractValidationResult,
    load_contract,
)
from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    ZERO_MONEY,
    require_non_negative_int,
    to_decimal_money,
    to_decimal_rate,
)
from finops_lakehouse.core.shield import default_repo_root, sanitize_inbound_text
from finops_lakehouse.core.storage import HybridStorageEngine, rows_to_arrow_table
from finops_lakehouse.finops_ai.pricing_catalog import (
    LLM_PRICING_CATALOG,
    TIERED_RATE_OVERRIDES,
)
from finops_lakehouse.finops_ai.telemetry_connector import (
    generate_synthetic_telemetry,
    materialize_ai_finops,
    redact_telemetry_text,
)
from finops_lakehouse.generators.capex_opex_ledger import (
    CANONICAL_COST_CENTERS,
    materialize_synthetic_ledger,
)
from finops_lakehouse.generators.receivables_invoices import CUSTOMER_SEGMENTS
from finops_lakehouse.receivables.aging_engine import materialize_receivables_aging

B2B_DEAL_ARROW_SCHEMA = pa.schema(
    [
        ("id_contrato_masked", pa.string()),
        ("id_cliente_masked", pa.string()),
        ("competencia", pa.string()),
        ("cod_centro_custo", pa.string()),
        ("segmento_cliente", pa.string()),
        ("moeda", pa.string()),
        ("initial_investment", pa.decimal128(18, 4)),
        ("horizon_months", pa.int32()),
        ("monthly_discount_rate", pa.decimal128(12, 6)),
        ("npv", pa.decimal128(18, 4)),
        ("irr_monthly", pa.decimal128(12, 6)),
        ("irr_annualized", pa.decimal128(12, 6)),
        ("payback_simple_months", pa.decimal128(12, 6)),
        ("payback_discounted_months", pa.decimal128(12, 6)),
        ("roi_pct", pa.decimal128(12, 6)),
        ("contract_revenue", pa.decimal128(18, 4)),
        ("direct_delivery_cost", pa.decimal128(18, 4)),
        ("gross_margin_ratio", pa.decimal128(12, 6)),
        ("gross_margin_pct", pa.decimal128(12, 6)),
        ("target_gross_margin_pct", pa.decimal128(12, 6)),
        ("margin_gate_passed", pa.bool_()),
        ("monthly_inaction_cost", pa.decimal128(18, 4)),
        ("delay_months", pa.int32()),
        ("cumulative_inaction_cost", pa.decimal128(18, 4)),
        ("annualized_inaction_cost", pa.decimal128(18, 4)),
        ("verdict", pa.string()),
    ]
)

LLM_MODEL_CATALOG_SCHEMA = pa.schema(
    [
        ("model_id", pa.string()),
        ("provider", pa.string()),
        ("tier", pa.string()),
        ("input_usd_per_m", pa.decimal128(12, 6)),
        ("output_usd_per_m", pa.decimal128(12, 6)),
        ("cliff_threshold_tokens", pa.int64()),
        ("cliff_input_usd_per_m", pa.decimal128(12, 6)),
        ("cliff_output_usd_per_m", pa.decimal128(12, 6)),
        ("has_billing_cliff", pa.bool_()),
    ]
)

_DEAL_COST_CENTERS: tuple[str, ...] = tuple(code for code, _ in CANONICAL_COST_CENTERS)


def default_b2b_contract_path() -> Path:
    """Return canonical ODCS v3.1 contract path for B2B deal economics."""
    return default_repo_root() / "catalog" / "contracts" / "b2b_deal_economics.contract.yaml"


def _optional_rate(value: str | None) -> Decimal | None:
    return to_decimal_rate(value) if value is not None else None


def _validate_single_b2b_row(
    idx: int,
    row: dict[str, Any],
    *,
    schema_cols: dict[str, dict[str, Any]],
    allowed_currencies: set[str],
    allowed_verdicts: set[str],
    invariants: dict[str, Any],
    violations: list[str],
) -> tuple[dict[str, Any], int]:
    cleaned: dict[str, Any] = {}
    sanitized_count = 0

    for col, spec in schema_cols.items():
        col_type = str(spec.get("type", ""))
        nullable = bool(spec.get("nullable", False))
        raw_val = row.get(col)

        if col_type == "STRING":
            if raw_val is None or not str(raw_val).strip():
                if not nullable:
                    violations.append(f"Row {idx}: required column '{col}' is null or empty.")
                cleaned[col] = None
            else:
                redacted = redact_telemetry_text(str(raw_val).strip())
                sanitized, was_san = sanitize_inbound_text(redacted)
                if was_san:
                    sanitized_count += 1
                cleaned[col] = sanitized
        elif col_type == "INTEGER":
            if isinstance(raw_val, bool) or not isinstance(raw_val, int) or raw_val < 0:
                violations.append(f"Row {idx}: column '{col}' must be non-negative integer.")
                cleaned[col] = 0
            else:
                cleaned[col] = raw_val
        elif col_type == "BOOLEAN":
            if not isinstance(raw_val, bool):
                violations.append(f"Row {idx}: column '{col}' must be boolean.")
                cleaned[col] = False
            else:
                cleaned[col] = raw_val
        elif col_type == "DECIMAL(18,4)":
            if raw_val is None:
                if not nullable:
                    violations.append(f"Row {idx}: missing monetary column '{col}'.")
                cleaned[col] = None if nullable else ZERO_MONEY
            else:
                try:
                    val_dec = to_decimal_money(raw_val)
                    if abs(val_dec) > DECIMAL_18_4_MAX:
                        violations.append(f"Row {idx}: out-of-range DECIMAL(18,4) in '{col}'.")
                    cleaned[col] = val_dec
                except (TypeError, ValueError) as exc:
                    violations.append(f"Row {idx}: invalid DECIMAL(18,4) in '{col}' ({exc}).")
                    cleaned[col] = ZERO_MONEY
        elif col_type == "DECIMAL(12,6)":
            if raw_val is None:
                if not nullable:
                    violations.append(f"Row {idx}: missing rate column '{col}'.")
                cleaned[col] = None
            else:
                try:
                    cleaned[col] = to_decimal_rate(raw_val)
                except (TypeError, ValueError) as exc:
                    violations.append(f"Row {idx}: invalid DECIMAL(12,6) in '{col}' ({exc}).")
                    cleaned[col] = Decimal("0.000000")

    if cleaned.get("moeda") and cleaned["moeda"] not in allowed_currencies:
        violations.append(f"Row {idx}: unsupported moeda '{cleaned['moeda']}'.")
    if cleaned.get("verdict") and cleaned["verdict"] not in allowed_verdicts:
        violations.append(f"Row {idx}: unsupported verdict '{cleaned['verdict']}'.")
    if invariants.get("positive_initial_investment", True) and (
        cleaned.get("initial_investment", ZERO_MONEY) <= ZERO_MONEY
    ):
        violations.append(f"Row {idx}: initial_investment must be strictly positive.")
    if invariants.get("positive_contract_revenue", True) and (
        cleaned.get("contract_revenue", ZERO_MONEY) <= ZERO_MONEY
    ):
        violations.append(f"Row {idx}: contract_revenue must be strictly positive.")
    if invariants.get("non_negative_direct_cost", True) and (
        cleaned.get("direct_delivery_cost", ZERO_MONEY) < ZERO_MONEY
    ):
        violations.append(f"Row {idx}: direct_delivery_cost cannot be negative.")
    if invariants.get("non_negative_inaction_cost", True) and (
        cleaned.get("monthly_inaction_cost", ZERO_MONEY) < ZERO_MONEY
    ):
        violations.append(f"Row {idx}: monthly_inaction_cost cannot be negative.")

    return cleaned, sanitized_count


def validate_b2b_deal_rows(
    rows: Sequence[dict[str, Any]],
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], ContractValidationResult]:
    """Validate B2B deal rows against ODCS v3.1 contract rules and PK uniqueness."""
    if not rows:
        return [], ContractValidationResult(
            passed=False,
            violations=["Empty dataset: minimum volume is 1 B2B deal row."],
            row_count=0,
        )
    if not isinstance(rows[0], dict):
        return [], ContractValidationResult(
            passed=False,
            violations=["Invalid row container: expected dict mappings."],
            row_count=len(rows),
        )

    schema_cols = contract.get("columns", {})
    missing = sorted(set(schema_cols.keys()) - set(rows[0].keys()))
    if missing:
        return [], ContractValidationResult(
            passed=False,
            violations=[f"Missing required schema columns: {', '.join(missing)}"],
            row_count=len(rows),
        )

    allowed_currencies = set(contract.get("allowed_currencies", ["BRL", "USD", "EUR"]))
    allowed_verdicts = set(contract.get("allowed_verdicts", ["GO", "REPRICE_MARGIN", "NO_GO"]))
    invariants = dict(contract.get("invariants", {}))
    pk_cols = tuple(col for col, spec in schema_cols.items() if bool(spec.get("primary_key", False)))
    seen_pks: dict[str, set[str]] = {col: set() for col in pk_cols}
    null_counts: dict[str, int] = {col: 0 for col in schema_cols}
    violations: list[str] = []
    cleaned_rows: list[dict[str, Any]] = []
    sanitized_total = 0

    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            violations.append(f"Row {idx}: expected dict mapping, got {type(row).__name__}.")
            continue
        cleaned, san_cnt = _validate_single_b2b_row(
            idx,
            row,
            schema_cols=schema_cols,
            allowed_currencies=allowed_currencies,
            allowed_verdicts=allowed_verdicts,
            invariants=invariants,
            violations=violations,
        )
        sanitized_total += san_cnt
        for col in schema_cols:
            if cleaned.get(col) is None:
                null_counts[col] += 1
        for pk_col in pk_cols:
            pk_val = cleaned.get(pk_col)
            if pk_val is None:
                continue
            if pk_val in seen_pks[pk_col]:
                violations.append(f"Row {idx}: duplicate primary key '{pk_col}' value '{pk_val}'.")
            else:
                seen_pks[pk_col].add(pk_val)
        cleaned_rows.append(cleaned)

    total_rows = len(rows)
    for col, spec in schema_cols.items():
        max_ratio = Decimal(str(spec.get("max_null_ratio", "0")))
        observed_ratio = Decimal(null_counts[col]) / Decimal(total_rows)
        if observed_ratio > max_ratio:
            violations.append(
                f"Null ratio exceeded for '{col}': {observed_ratio:.4f} > {max_ratio:.4f}."
            )

    return cleaned_rows, ContractValidationResult(
        passed=not violations,
        violations=violations,
        row_count=total_rows,
        sanitized_cells=sanitized_total,
    )


def generate_synthetic_b2b_deals(seed: int = 42, count: int = 36) -> list[dict[str, Any]]:
    """Generate deterministic B2B deal economics records evaluated by pure Decimal calculator."""
    valid_seed = require_non_negative_int(seed, "seed")
    valid_count = require_non_negative_int(count, "count")
    if valid_count <= 0:
        raise ValueError("count must be positive.")

    rng = random.Random(valid_seed)
    rows: list[dict[str, Any]] = []
    cents_div = Decimal("100")

    for idx in range(valid_count):
        competencia = f"2025-{(idx % 12) + 1:02d}"
        cost_center = _DEAL_COST_CENTERS[idx % len(_DEAL_COST_CENTERS)]
        segment = CUSTOMER_SEGMENTS[idx % len(CUSTOMER_SEGMENTS)]
        horizon = 12 if idx % 2 == 0 else 18
        initial_inv = (Decimal(rng.randint(25_000_00, 120_000_00)) / cents_div).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_EVEN
        )

        profile = idx % 6
        if profile == 4:
            monthly_cf = (initial_inv * Decimal("0.1450")).quantize(
                MONEY_QUANT, rounding=ROUND_HALF_EVEN
            )
            revenue = (monthly_cf * Decimal(horizon)).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
            cost = (revenue * Decimal("0.5200")).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
        elif profile == 5:
            monthly_cf = (initial_inv * Decimal("0.0400")).quantize(
                MONEY_QUANT, rounding=ROUND_HALF_EVEN
            )
            revenue = (monthly_cf * Decimal(horizon)).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
            cost = (revenue * Decimal("0.4500")).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
        else:
            factor = Decimal("0.1500") + Decimal(rng.randint(0, 400)) / Decimal("10000")
            monthly_cf = (initial_inv * factor).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
            revenue = (monthly_cf * Decimal(horizon)).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
            cost_ratio = Decimal("0.2200") + Decimal(rng.randint(0, 900)) / Decimal("10000")
            cost = (revenue * cost_ratio).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

        inaction = (revenue * Decimal("0.0450")).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
        metrics = evaluate_b2b_deal(
            initial_investment=initial_inv,
            monthly_cashflows=[monthly_cf for _ in range(horizon)],
            contract_revenue=revenue,
            direct_delivery_cost=cost,
            annual_discount_rate=Decimal("0.150000"),
            monthly_inaction_cost=inaction,
            delay_months=3,
            target_gross_margin=Decimal("0.650000"),
        )

        rows.append(
            {
                "id_contrato_masked": f"CTR-B2B-SYN-{valid_seed}-{idx + 1:04d}",
                "id_cliente_masked": f"CLI-SYN-{(idx % 12) + 1:03d}",
                "competencia": competencia,
                "cod_centro_custo": cost_center,
                "segmento_cliente": segment,
                "moeda": "BRL",
                "initial_investment": to_decimal_money(metrics["initial_investment"]),
                "horizon_months": int(metrics["horizon_months"]),
                "monthly_discount_rate": to_decimal_rate(metrics["monthly_discount_rate"]),
                "npv": to_decimal_money(metrics["npv"]),
                "irr_monthly": _optional_rate(metrics["irr_monthly"]),
                "irr_annualized": _optional_rate(metrics["irr_annualized"]),
                "payback_simple_months": _optional_rate(metrics["payback_simple_months"]),
                "payback_discounted_months": _optional_rate(metrics["payback_discounted_months"]),
                "roi_pct": to_decimal_rate(metrics["roi_pct"]),
                "contract_revenue": to_decimal_money(metrics["contract_revenue"]),
                "direct_delivery_cost": to_decimal_money(metrics["direct_delivery_cost"]),
                "gross_margin_ratio": to_decimal_rate(metrics["gross_margin_ratio"]),
                "gross_margin_pct": to_decimal_rate(metrics["gross_margin_pct"]),
                "target_gross_margin_pct": to_decimal_rate(metrics["target_gross_margin_pct"]),
                "margin_gate_passed": bool(metrics["margin_gate_passed"]),
                "monthly_inaction_cost": to_decimal_money(metrics["monthly_inaction_cost"]),
                "delay_months": int(metrics["delay_months"]),
                "cumulative_inaction_cost": to_decimal_money(metrics["cumulative_inaction_cost"]),
                "annualized_inaction_cost": to_decimal_money(metrics["annualized_inaction_cost"]),
                "verdict": str(metrics["verdict"]),
            }
        )

    return rows


def _redact_quarantine_row(row: Any) -> Any:
    if isinstance(row, str):
        sanitized, _ = sanitize_inbound_text(redact_telemetry_text(row))
        return sanitized
    if isinstance(row, dict):
        return {str(k): _redact_quarantine_row(v) for k, v in row.items()}
    if isinstance(row, (list, tuple)):
        return [_redact_quarantine_row(item) for item in row]
    return row


def materialize_b2b_deals(
    storage: HybridStorageEngine,
    rows: Sequence[dict[str, Any]],
    *,
    contract_path: Path | None = None,
    batch_id: str = "b2b_deals_batch",
) -> dict[str, Any]:
    """Validate B2B deal rows via ODCS v3.1 contract and persist or quarantine cleanly."""
    contract = load_contract(contract_path or default_b2b_contract_path())
    cleaned_rows, validation = validate_b2b_deal_rows(rows, contract)
    if not validation.passed:
        redacted_violations = [redact_telemetry_text(v) for v in validation.violations]
        sample_source = cleaned_rows if cleaned_rows else list(rows)
        redacted_samples = [_redact_quarantine_row(r) for r in sample_source[:25]]
        q_path = storage.write_quarantine(
            "b2b_deal_economics",
            batch_id,
            redacted_violations,
            redacted_samples,
        )
        return {
            "status": "QUARANTINED",
            "batch_id": batch_id,
            "violations": redacted_violations,
            "quarantine_path": str(q_path),
        }

    table = rows_to_arrow_table(cleaned_rows, B2B_DEAL_ARROW_SCHEMA)
    return storage.write_silver_parquet(
        "b2b_deal_economics",
        "b2b_deal_economics",
        table,
        batch_id=batch_id,
    )


def build_llm_model_catalog_table() -> pa.Table:
    """Build deterministic PyArrow Table for all 44 POP_T_158 v1.4.0 LLM models."""
    rows: list[dict[str, Any]] = []
    for model_id in sorted(LLM_PRICING_CATALOG):
        spec = LLM_PRICING_CATALOG[model_id]
        cliff = TIERED_RATE_OVERRIDES.get(model_id)
        rows.append(
            {
                "model_id": spec.model_id,
                "provider": spec.provider,
                "tier": spec.tier,
                "input_usd_per_m": spec.input_usd_per_m,
                "output_usd_per_m": spec.output_usd_per_m,
                "cliff_threshold_tokens": cliff[0] if cliff else None,
                "cliff_input_usd_per_m": cliff[1] if cliff else None,
                "cliff_output_usd_per_m": cliff[2] if cliff else None,
                "has_billing_cliff": cliff is not None,
            }
        )
    return rows_to_arrow_table(rows, LLM_MODEL_CATALOG_SCHEMA)


def materialize_silver_layer(
    data_dir: Path,
    *,
    seed: int = 42,
    b2b_deals_count: int = 36,
    ai_spans_count: int = 240,
) -> dict[str, Any]:
    """Materialize all deterministic synthetic Silver Parquet ZSTD datasets and Gold dir."""
    valid_seed = require_non_negative_int(seed, "seed")
    valid_b2b_count = require_non_negative_int(b2b_deals_count, "b2b_deals_count")
    valid_ai_count = require_non_negative_int(ai_spans_count, "ai_spans_count")
    if valid_b2b_count <= 0 or valid_ai_count <= 0:
        raise ValueError("b2b_deals_count and ai_spans_count must be positive integers.")

    resolved_data = Path(data_dir).resolve()
    resolved_data.mkdir(parents=True, exist_ok=True)
    (resolved_data / "gold").mkdir(parents=True, exist_ok=True)

    storage = HybridStorageEngine(resolved_data)

    ledger_manifest = materialize_synthetic_ledger(resolved_data, seed=valid_seed)
    receivables_manifest = materialize_receivables_aging(resolved_data, seed=valid_seed)

    b2b_rows = generate_synthetic_b2b_deals(seed=valid_seed, count=valid_b2b_count)
    b2b_manifest = materialize_b2b_deals(
        storage,
        b2b_rows,
        batch_id=f"b2b_deals_seed{valid_seed}_{valid_b2b_count}",
    )
    if b2b_manifest["status"] != "ACCEPTED":
        raise RuntimeError(
            f"Synthetic B2B deals failed ODCS gate: {b2b_manifest.get('violations')}"
        )

    ai_spans = generate_synthetic_telemetry(
        seed=valid_seed,
        num_spans=valid_ai_count,
        months=6,
        start_year=2025,
        start_month=7,
    )
    ai_mat = materialize_ai_finops(
        storage,
        ai_spans,
        batch_id=f"ai_finops_seed{valid_seed}_{valid_ai_count}",
    )
    if not ai_mat.passed:
        raise RuntimeError(f"Synthetic AI FinOps batch failed ODCS gate: {ai_mat.violations}")

    llm_catalog_table = build_llm_model_catalog_table()
    llm_catalog_manifest = storage.write_silver_parquet(
        "ai_cloud_finops",
        "llm_model_catalog",
        llm_catalog_table,
        batch_id="pop_t_158_v140_catalog",
    )

    return {
        "status": "READY",
        "seed": valid_seed,
        "data_dir": str(resolved_data),
        "silver_datasets": {
            "capex_opex_ledger": {
                "row_count": ledger_manifest["row_count"],
                "sha256": ledger_manifest["sha256"],
                "path": ledger_manifest["silver_path"],
            },
            "receivables_aging": {
                "row_count": receivables_manifest["fct_manifest"]["row_count"],
                "sha256": receivables_manifest["fct_manifest"]["sha256"],
                "path": receivables_manifest["fct_manifest"]["silver_path"],
            },
            "b2b_deal_economics": {
                "row_count": b2b_manifest["row_count"],
                "sha256": b2b_manifest["sha256"],
                "path": b2b_manifest["silver_path"],
            },
            "ai_finops_telemetry": {
                "row_count": ai_mat.row_count,
                "sha256": ai_mat.sha256,
                "path": str(ai_mat.silver_path),
            },
            "llm_model_catalog": {
                "row_count": llm_catalog_manifest["row_count"],
                "sha256": llm_catalog_manifest["sha256"],
                "path": llm_catalog_manifest["silver_path"],
            },
        },
    }
