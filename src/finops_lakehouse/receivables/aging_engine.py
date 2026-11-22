"""Receivables aging engine: stg_faturas_recebiveis -> fct_receivables_aging (ADR-001/002)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    RATE_QUANT,
    ZERO_MONEY,
    format_money,
    to_decimal_money,
    to_decimal_rate,
)
from finops_lakehouse.core.storage import HybridStorageEngine
from finops_lakehouse.generators.receivables_invoices import (
    generate_roll_rate_snapshots,
    generate_synthetic_invoices,
)

AGING_BUCKETS: tuple[str, ...] = ("a_vencer", "0-30", "31-60", "61-90", "91-180", ">180")
ROLL_RATE_STATES: tuple[str, ...] = (*AGING_BUCKETS, "liquidado")

ZERO_RATE = Decimal("0.000000")
ONE_RATE = Decimal("1.000000")
HUNDRED_RATE = Decimal("100.000000")

DEFAULT_PD_BY_BUCKET: dict[str, Decimal] = {
    "a_vencer": Decimal("0.015000"),
    "0-30": Decimal("0.050000"),
    "31-60": Decimal("0.140000"),
    "61-90": Decimal("0.280000"),
    "91-180": Decimal("0.520000"),
    ">180": Decimal("0.780000"),
}

DEFAULT_LGD_BY_SEGMENT: dict[str, Decimal] = {
    "ENTERPRISE": Decimal("0.450000"),
    "MID_MARKET": Decimal("0.550000"),
    "SMB": Decimal("0.650000"),
    "PUBLIC_UTILITY_SYN": Decimal("0.350000"),
}

_URGENCY_BY_BUCKET: dict[str, Decimal] = {
    "a_vencer": Decimal("0.150000"),
    "0-30": Decimal("0.700000"),
    "31-60": Decimal("1.000000"),
    "61-90": Decimal("0.950000"),
    "91-180": Decimal("0.650000"),
    ">180": Decimal("0.350000"),
}

_ALLOWED_CURRENCIES = frozenset({"BRL", "USD", "EUR"})
_ALLOWED_STATUSES = frozenset({"OPEN", "PARTIAL", "PAID"})

STG_RECEIVABLES_SCHEMA = pa.schema(
    [
        ("id_fatura_masked", pa.string()),
        ("id_contrato_masked", pa.string()),
        ("cnpj_14", pa.string()),
        ("sigla_uf", pa.string()),
        ("segmento_cliente", pa.string()),
        ("competencia", pa.string()),
        ("data_emissao", pa.string()),
        ("data_vencimento", pa.string()),
        ("data_referencia", pa.string()),
        ("status_fatura", pa.string()),
        ("moeda", pa.string()),
        ("dias_atraso", pa.int32()),
        ("faixa_aging", pa.string()),
        ("valor_original", pa.decimal128(18, 4)),
        ("valor_pago", pa.decimal128(18, 4)),
        ("valor_em_aberto", pa.decimal128(18, 4)),
    ]
)

FCT_AGING_SCHEMA = pa.schema(
    [
        *STG_RECEIVABLES_SCHEMA,
        ("pd_aplicada", pa.decimal128(12, 6)),
        ("lgd_aplicada", pa.decimal128(12, 6)),
        ("valor_ecl", pa.decimal128(18, 4)),
        ("score_cobranca", pa.decimal128(12, 6)),
        ("prioridade_cobranca", pa.string()),
        ("ranking_cobranca", pa.int32()),
    ]
)


def classify_aging_bucket(dias_atraso: int) -> str:
    """Classify integer overdue days deterministically into the 6 canonical aging buckets."""
    if isinstance(dias_atraso, bool) or not isinstance(dias_atraso, int):
        raise TypeError("dias_atraso must be an integer.")
    if dias_atraso <= 0:
        return "a_vencer"
    if dias_atraso <= 30:
        return "0-30"
    if dias_atraso <= 60:
        return "31-60"
    if dias_atraso <= 90:
        return "61-90"
    if dias_atraso <= 180:
        return "91-180"
    return ">180"


def _require_text(inv: dict[str, Any], key: str, fat_id: str, *, upper: bool = False) -> str:
    raw = inv.get(key)
    if raw is None:
        raise ValueError(f"Invoice '{fat_id}': missing required column '{key}'.")
    text = str(raw).strip()
    if not text:
        raise ValueError(f"Invoice '{fat_id}': empty required column '{key}'.")
    return text.upper() if upper else text


def _rows_to_table(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    columns = {field.name: [row[field.name] for row in rows] for field in schema}
    return pa.table(columns, schema=schema)


def build_staging_invoices(
    raw_invoices: list[dict[str, Any]],
    *,
    as_of_date: str,
) -> list[dict[str, Any]]:
    """Normalize and validate raw invoices into `stg_faturas_recebiveis` rows."""
    if not raw_invoices:
        raise ValueError("raw_invoices cannot be empty.")

    ref_date = date.fromisoformat(as_of_date)
    seen_ids: set[str] = set()
    staged: list[dict[str, Any]] = []

    for idx, inv in enumerate(raw_invoices, 1):
        fat_id = str(inv.get("id_fatura_masked") or "").strip()
        if not fat_id:
            raise ValueError(f"Invoice {idx}: missing id_fatura_masked.")
        if fat_id in seen_ids:
            raise ValueError(f"Invoice {idx}: duplicate id_fatura_masked '{fat_id}'.")
        seen_ids.add(fat_id)

        cnpj = str(inv.get("cnpj_14") or "").strip()
        if len(cnpj) != 14 or not cnpj.isdigit():
            raise ValueError(f"Invoice '{fat_id}': cnpj_14 must be a 14-digit STRING.")

        moeda = str(inv.get("moeda") or "").strip().upper()
        if moeda not in _ALLOWED_CURRENCIES:
            raise ValueError(f"Invoice '{fat_id}': unsupported currency '{moeda}'.")

        status = str(inv.get("status_fatura") or "").strip().upper()
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"Invoice '{fat_id}': unsupported status_fatura '{status}'.")

        for money_key in ("valor_original", "valor_pago", "valor_em_aberto"):
            if money_key not in inv:
                raise ValueError(f"Invoice '{fat_id}': missing required column '{money_key}'.")

        val_orig = to_decimal_money(inv["valor_original"])
        val_pago = to_decimal_money(inv["valor_pago"])
        val_aberto = to_decimal_money(inv["valor_em_aberto"])

        if val_orig <= ZERO_MONEY:
            raise ValueError(f"Invoice '{fat_id}': valor_original must be > 0 ({val_orig}).")
        if val_pago < ZERO_MONEY or val_aberto < ZERO_MONEY:
            raise ValueError(f"Invoice '{fat_id}': negative payment/open amount.")
        if val_pago + val_aberto != val_orig:
            raise ValueError(
                f"Invoice '{fat_id}': valor_pago ({val_pago}) + valor_em_aberto ({val_aberto}) "
                f"!= valor_original ({val_orig})."
            )

        issue_dt = date.fromisoformat(_require_text(inv, "data_emissao", fat_id))
        due_dt = date.fromisoformat(_require_text(inv, "data_vencimento", fat_id))
        dias_atraso = (ref_date - due_dt).days

        sigla_uf = _require_text(inv, "sigla_uf", fat_id, upper=True)
        if len(sigla_uf) != 2 or not sigla_uf.isalpha():
            raise ValueError(f"Invoice '{fat_id}': sigla_uf must be a 2-letter UF code.")

        staged.append(
            {
                "id_fatura_masked": fat_id,
                "id_contrato_masked": _require_text(inv, "id_contrato_masked", fat_id),
                "cnpj_14": cnpj,
                "sigla_uf": sigla_uf,
                "segmento_cliente": _require_text(inv, "segmento_cliente", fat_id, upper=True),
                "competencia": _require_text(inv, "competencia", fat_id),
                "data_emissao": issue_dt.isoformat(),
                "data_vencimento": due_dt.isoformat(),
                "data_referencia": ref_date.isoformat(),
                "status_fatura": status,
                "moeda": moeda,
                "dias_atraso": dias_atraso,
                "faixa_aging": classify_aging_bucket(dias_atraso),
                "valor_original": val_orig,
                "valor_pago": val_pago,
                "valor_em_aberto": val_aberto,
            }
        )

    return staged


def compute_roll_rate_matrix(
    prev_invoices: list[dict[str, Any]],
    curr_invoices: list[dict[str, Any]],
) -> dict[str, dict[str, Decimal]]:
    """Compute exposure-weighted Markovian roll-rate matrix in DECIMAL(12,6)."""
    if not prev_invoices or not curr_invoices:
        raise ValueError("Both prev_invoices and curr_invoices must be non-empty.")

    curr_by_id = {row["id_fatura_masked"]: row for row in curr_invoices}
    totals_by_source: dict[str, Decimal] = {bucket: ZERO_MONEY for bucket in AGING_BUCKETS}
    flows: dict[str, dict[str, Decimal]] = {
        src: {dst: ZERO_MONEY for dst in ROLL_RATE_STATES}
        for src in AGING_BUCKETS
    }

    for prev in prev_invoices:
        prev_open = to_decimal_money(prev["valor_em_aberto"])
        if prev_open <= ZERO_MONEY:
            continue

        src_bucket = str(prev["faixa_aging"])
        if src_bucket not in flows:
            raise ValueError(f"Unknown source aging bucket: '{src_bucket}'.")

        totals_by_source[src_bucket] += prev_open
        curr = curr_by_id.get(prev["id_fatura_masked"])
        if curr is None:
            flows[src_bucket]["liquidado"] += prev_open
            continue

        curr_open = to_decimal_money(curr["valor_em_aberto"])
        if curr_open <= ZERO_MONEY:
            flows[src_bucket]["liquidado"] += prev_open
            continue

        retained = min(prev_open, curr_open)
        cured = prev_open - retained
        dst_bucket = str(curr["faixa_aging"])
        if dst_bucket not in flows[src_bucket]:
            raise ValueError(f"Unknown target aging bucket: '{dst_bucket}'.")

        flows[src_bucket][dst_bucket] += retained
        if cured > ZERO_MONEY:
            flows[src_bucket]["liquidado"] += cured

    matrix: dict[str, dict[str, Decimal]] = {}
    for src_bucket in AGING_BUCKETS:
        denom = totals_by_source[src_bucket]
        if denom == ZERO_MONEY:
            matrix[src_bucket] = {dst: ZERO_RATE for dst in ROLL_RATE_STATES}
            continue
        matrix[src_bucket] = {
            dst: (flows[src_bucket][dst] / denom).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
            for dst in ROLL_RATE_STATES
        }
    return matrix


def _priority_label(score: Decimal, open_amount: Decimal) -> str:
    if open_amount <= ZERO_MONEY:
        return "P4_MONITORAR"
    if score >= Decimal("70.000000"):
        return "P1_CRITICA"
    if score >= Decimal("45.000000"):
        return "P2_ALTA"
    if score >= Decimal("20.000000"):
        return "P3_MEDIA"
    return "P4_MONITORAR"


def _validated_rate_map(
    custom_map: dict[str, Decimal] | None,
    default_map: dict[str, Decimal],
    label: str,
) -> dict[str, Decimal]:
    source = default_map if custom_map is None else custom_map
    validated: dict[str, Decimal] = {}
    for key, val in source.items():
        rate = to_decimal_rate(val)
        if not (ZERO_RATE <= rate <= ONE_RATE):
            raise ValueError(f"{label} rate for '{key}' must be within [0, 1], got {rate}.")
        validated[key] = rate
    return validated


def score_and_rank_receivables(
    stg_invoices: list[dict[str, Any]],
    *,
    pd_by_bucket: dict[str, Decimal] | None = None,
    lgd_by_segment: dict[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    """Compute ECL and deterministic collection priority score/ranking for staged invoices."""
    if not stg_invoices:
        raise ValueError("stg_invoices cannot be empty.")

    pd_map = _validated_rate_map(pd_by_bucket, DEFAULT_PD_BY_BUCKET, "PD")
    lgd_map = _validated_rate_map(lgd_by_segment, DEFAULT_LGD_BY_SEGMENT, "LGD")

    max_open = max((row["valor_em_aberto"] for row in stg_invoices), default=ZERO_MONEY)
    scored: list[dict[str, Any]] = []

    for row in stg_invoices:
        bucket = row["faixa_aging"]
        segment = row["segmento_cliente"]
        if bucket not in _URGENCY_BY_BUCKET:
            raise ValueError(f"Unknown aging bucket '{bucket}'.")
        if bucket not in pd_map:
            raise ValueError(f"Missing PD rate for aging bucket '{bucket}'.")
        if segment not in lgd_map:
            raise ValueError(f"Missing LGD rate for customer segment '{segment}'.")

        pd_val = pd_map[bucket]
        lgd_val = lgd_map[segment]
        open_val: Decimal = row["valor_em_aberto"]

        ecl_val = (open_val * pd_val * lgd_val).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
        if open_val == ZERO_MONEY or max_open == ZERO_MONEY:
            score_val = ZERO_RATE
        else:
            exposure_norm = min(ONE_RATE, open_val / max_open)
            recoverability = ONE_RATE - pd_val
            urgency = _URGENCY_BY_BUCKET[bucket]
            raw_score = (
                exposure_norm * Decimal("45")
                + recoverability * Decimal("30")
                + urgency * Decimal("25")
            )
            score_val = min(HUNDRED_RATE, max(ZERO_RATE, raw_score)).quantize(
                RATE_QUANT, rounding=ROUND_HALF_EVEN
            )

        scored.append(
            {
                **row,
                "pd_aplicada": pd_val,
                "lgd_aplicada": lgd_val,
                "valor_ecl": ecl_val,
                "score_cobranca": score_val,
                "prioridade_cobranca": _priority_label(score_val, open_val),
            }
        )

    scored.sort(
        key=lambda r: (-r["score_cobranca"], -r["valor_em_aberto"], r["id_fatura_masked"])
    )
    ranked: list[dict[str, Any]] = []
    for idx, row in enumerate(scored, 1):
        ranked.append({**row, "ranking_cobranca": idx})
    return ranked


def summarize_aging_portfolio(fct_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize portfolio exposure and ECL per bucket and validate exact Decimal sum."""
    if not fct_rows:
        raise ValueError("fct_rows cannot be empty.")

    bucket_open: dict[str, Decimal] = {b: ZERO_MONEY for b in AGING_BUCKETS}
    bucket_ecl: dict[str, Decimal] = {b: ZERO_MONEY for b in AGING_BUCKETS}
    bucket_counts: dict[str, int] = {b: 0 for b in AGING_BUCKETS}

    total_original = ZERO_MONEY
    total_open = ZERO_MONEY
    total_ecl = ZERO_MONEY

    for row in fct_rows:
        bucket = row["faixa_aging"]
        if bucket not in bucket_open:
            raise ValueError(f"Unknown aging bucket in fact row: '{bucket}'.")
        open_val: Decimal = row["valor_em_aberto"]
        ecl_val: Decimal = row["valor_ecl"]

        bucket_open[bucket] += open_val
        bucket_ecl[bucket] += ecl_val
        bucket_counts[bucket] += 1
        total_original += row["valor_original"]
        total_open += open_val
        total_ecl += ecl_val

    sum_buckets = sum(bucket_open.values(), ZERO_MONEY)
    total_a_vencer = bucket_open["a_vencer"]
    total_vencido = sum((bucket_open[b] for b in AGING_BUCKETS if b != "a_vencer"), ZERO_MONEY)
    reconciliation_ok = (sum_buckets == total_open) and (total_a_vencer + total_vencido == total_open)

    buckets_summary: dict[str, dict[str, Any]] = {}
    for bucket in AGING_BUCKETS:
        pct = (
            ((bucket_open[bucket] / total_open) * Decimal("100")).quantize(
                RATE_QUANT, rounding=ROUND_HALF_EVEN
            )
            if total_open > ZERO_MONEY
            else ZERO_RATE
        )
        buckets_summary[bucket] = {
            "qtd_faturas": bucket_counts[bucket],
            "valor_em_aberto": format_money(bucket_open[bucket]),
            "valor_ecl": format_money(bucket_ecl[bucket]),
            "pct_carteira": f"{pct:.6f}",
        }

    return {
        "row_count": len(fct_rows),
        "total_original": format_money(total_original),
        "total_em_aberto": format_money(total_open),
        "total_a_vencer": format_money(total_a_vencer),
        "total_vencido": format_money(total_vencido),
        "total_ecl": format_money(total_ecl),
        "reconciliation_ok": reconciliation_ok,
        "buckets": buckets_summary,
    }


def materialize_receivables_aging(
    data_dir: Path,
    *,
    seed: int = 42,
    count: int = 4000,
    as_of_date: str = "2025-12-31",
) -> dict[str, Any]:
    """Generate, stage, score, and persist `stg_faturas_recebiveis` and `fct_receivables_aging`."""
    raw = generate_synthetic_invoices(seed=seed, count=count, as_of_date=as_of_date)
    stg_rows = build_staging_invoices(raw, as_of_date=as_of_date)
    fct_rows = score_and_rank_receivables(stg_rows)

    stg_table = _rows_to_table(stg_rows, STG_RECEIVABLES_SCHEMA)
    fct_table = _rows_to_table(fct_rows, FCT_AGING_SCHEMA)

    storage = HybridStorageEngine(data_dir)
    stg_manifest = storage.write_silver_parquet(
        "receivables_aging",
        "stg_faturas_recebiveis",
        stg_table,
        batch_id=f"stg_receivables_seed{seed}_{count}",
    )
    fct_manifest = storage.write_silver_parquet(
        "receivables_aging",
        "fct_receivables_aging",
        fct_table,
        batch_id=f"fct_receivables_seed{seed}_{count}",
    )

    curr_ref = date.fromisoformat(as_of_date)
    prev_as_of_date = (curr_ref.replace(day=1) - timedelta(days=1)).isoformat()
    prev_raw, curr_raw = generate_roll_rate_snapshots(
        seed=seed,
        count=min(count, 2000),
        prev_as_of_date=prev_as_of_date,
        curr_as_of_date=as_of_date,
    )
    prev_stg = build_staging_invoices(prev_raw, as_of_date=prev_as_of_date)
    curr_stg = build_staging_invoices(curr_raw, as_of_date=as_of_date)
    roll_rate = compute_roll_rate_matrix(prev_stg, curr_stg)

    portfolio_summary = summarize_aging_portfolio(fct_rows)
    return {
        "status": "ACCEPTED",
        "seed": seed,
        "as_of_date": as_of_date,
        "stg_manifest": stg_manifest,
        "fct_manifest": fct_manifest,
        "portfolio": portfolio_summary,
        "roll_rate_matrix": {
            src: {dst: f"{prob:.6f}" for dst, prob in row.items()}
            for src, row in roll_rate.items()
        },
    }
