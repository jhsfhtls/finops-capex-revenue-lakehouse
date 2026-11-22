"""Unified FinOpsLakehouse SDK serving identical Decimal results across all 4 interfaces."""

from __future__ import annotations

import os
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from finops_lakehouse import __version__
from finops_lakehouse.calculators.b2b_economics import evaluate_b2b_deal
from finops_lakehouse.calculators.career_parity import (
    DEFAULT_ISS_MANAUS_SIMPLES,
    compare_career_offers,
)
from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    RATE_QUANT,
    ZERO_MONEY,
    format_money,
    require_non_negative_int,
    to_decimal_money,
    to_decimal_rate,
)
from finops_lakehouse.core.shield import default_repo_root
from finops_lakehouse.finops_ai.pricing_catalog import (
    DEFAULT_BATCH_DISCOUNT_RATE,
    DEFAULT_CACHE_DISCOUNT_RATE,
    validate_unit_rate,
)
from finops_lakehouse.finops_ai.telemetry_connector import (
    generate_synthetic_telemetry,
    read_telemetry_jsonl,
    summarize_ai_finops,
)
from finops_lakehouse.gateway_bridge.macro_rates import load_macro_rates
from finops_lakehouse.generators.capex_opex_ledger import generate_synthetic_ledger_rows
from finops_lakehouse.generators.receivables_invoices import generate_synthetic_invoices
from finops_lakehouse.receivables.aging_engine import (
    AGING_BUCKETS,
    build_staging_invoices,
    score_and_rank_receivables,
    summarize_aging_portfolio,
)

_ALLOWED_EXPENSE_TYPES = frozenset({"CAPEX", "OPEX"})
_ALLOWED_PRIORITIES = frozenset({"P1_CRITICA", "P2_ALTA", "P3_MEDIA", "P4_MONITORAR"})
_ALLOWED_BUCKETS = frozenset(AGING_BUCKETS)
_ALLOWED_FX_CURRENCIES = frozenset({"USD", "EUR"})
_MAX_QUERY_LIMIT = 1000
_ZERO_RATE = Decimal("0.000000")


def _validate_positive_limit(limit: int, name: str = "limit") -> int:
    valid = require_non_negative_int(limit, name)
    if valid <= 0 or valid > _MAX_QUERY_LIMIT:
        raise ValueError(f"{name} must be between 1 and {_MAX_QUERY_LIMIT}, got {valid}.")
    return valid


def _clean_optional_filter(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Filter value must be a string or None, got {type(value).__name__}.")
    cleaned = value.strip()
    return cleaned if cleaned else None


def _as_decimal(val: Any, *, quant: Decimal = MONEY_QUANT) -> Decimal:
    if isinstance(val, (bool, float)):
        raise TypeError(
            f"IEEE 754 float/bool forbidden in decimal conversion: {type(val).__name__}."
        )
    if val is None:
        return Decimal("0").quantize(quant, rounding=ROUND_HALF_EVEN)
    try:
        if isinstance(val, Decimal):
            return val.quantize(quant, rounding=ROUND_HALF_EVEN)
        if isinstance(val, (int, str)):
            return Decimal(str(val).strip()).quantize(quant, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {val!r}") from exc
    raise TypeError(f"Unsupported value type for decimal conversion: {type(val).__name__}.")


def _compute_variacao_pct(variacao: Decimal, total_orcado: Decimal) -> Decimal:
    if total_orcado <= ZERO_MONEY:
        return _ZERO_RATE
    return ((variacao * Decimal("100")) / total_orcado).quantize(
        RATE_QUANT, rounding=ROUND_HALF_EVEN
    )


def _build_budget_variance_response(
    *,
    source_layer: str,
    competencia: str | None,
    cod_centro_custo: str | None,
    tipo_despesa: str | None,
    group_count: int,
    total_orcado: Decimal,
    total_debito: Decimal,
    total_credito: Decimal,
    total_realizado: Decimal,
    variacao: Decimal,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    variacao_pct = _compute_variacao_pct(variacao, total_orcado)
    return {
        "source_layer": source_layer,
        "filters": {
            "competencia": competencia,
            "cod_centro_custo": cod_centro_custo,
            "tipo_despesa": tipo_despesa,
        },
        "group_count": group_count,
        "returned_rows": len(rows),
        "total_orcado": format_money(total_orcado),
        "total_debito": format_money(total_debito),
        "total_credito": format_money(total_credito),
        "total_realizado": format_money(total_realizado),
        "variacao_orcamentaria": format_money(variacao),
        "variacao_pct": f"{variacao_pct:.6f}",
        "double_entry_balanced": total_debito == total_credito,
        "rows": rows,
    }


def _empty_aging_portfolio() -> dict[str, Any]:
    return {
        "row_count": 0,
        "total_original": "0.0000",
        "total_em_aberto": "0.0000",
        "total_a_vencer": "0.0000",
        "total_vencido": "0.0000",
        "total_ecl": "0.0000",
        "reconciliation_ok": True,
        "buckets": {
            b: {
                "qtd_faturas": 0,
                "valor_em_aberto": "0.0000",
                "valor_ecl": "0.0000",
                "pct_carteira": "0.000000",
            }
            for b in AGING_BUCKETS
        },
    }


class FinOpsLakehouse:
    """Single source of truth SDK consumed by CLI (`fcl`), FastAPI, MCP (`fcl-mcp`), and BI."""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        duckdb_path: Path | str | None = None,
        *,
        seed: int = 42,
    ) -> None:
        self.seed = require_non_negative_int(seed, "seed")
        if data_dir is not None:
            if isinstance(data_dir, str) and not data_dir.strip():
                raise ValueError("data_dir cannot be an empty string.")
            resolved_data = Path(data_dir).resolve()
        else:
            env_data = os.environ.get("FCL_DATA_DIR", "").strip()
            resolved_data = Path(env_data).resolve() if env_data else default_repo_root() / "data"
        self.data_dir = resolved_data

        if duckdb_path is not None:
            if isinstance(duckdb_path, str) and not duckdb_path.strip():
                raise ValueError("duckdb_path cannot be an empty string.")
            resolved_db = Path(duckdb_path).resolve()
        elif data_dir is not None:
            resolved_db = self.data_dir / "gold" / "finops_lakehouse.duckdb"
        else:
            env_db = os.environ.get("FCL_DUCKDB_PATH", "").strip()
            resolved_db = (
                Path(env_db).resolve()
                if env_db
                else self.data_dir / "gold" / "finops_lakehouse.duckdb"
            )
        self.duckdb_path = resolved_db

    def _has_gold_table(self, table_name: str) -> bool:
        if not self.duckdb_path.is_file():
            return False
        with duckdb.connect(str(self.duckdb_path), read_only=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name = ?",
                [table_name],
            ).fetchone()
        return bool(row and row[0] > 0)

    def get_status(self) -> dict[str, Any]:
        """Return runtime governance status and available Gold/Silver datasets."""
        gold_ready = self._has_gold_table("fct_capex_opex_variance")
        return {
            "package": "finops-capex-revenue-lakehouse",
            "version": __version__,
            "monetary_precision": "DECIMAL(18,4)",
            "rate_precision": "DECIMAL(12,6)",
            "float_allowed_in_money": False,
            "rounding_mode": "ROUND_HALF_EVEN",
            "deterministic_seed": self.seed,
            "gold_ready": gold_ready,
            "interfaces": ["sdk", "cli", "fastapi", "mcp"],
            "status": "FOUNDATION_READY",
        }

    def _query_budget_variance_gold(
        self,
        *,
        competencia: str | None,
        cod_centro_custo: str | None,
        tipo_despesa: str | None,
        limit: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if competencia is not None:
            clauses.append("competencia = ?")
            params.append(competencia)
        if cod_centro_custo is not None:
            clauses.append("cod_centro_custo = ?")
            params.append(cod_centro_custo)
        if tipo_despesa is not None:
            clauses.append("tipo_despesa = ?")
            params.append(tipo_despesa)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with duckdb.connect(str(self.duckdb_path), read_only=True) as conn:
            agg = conn.execute(
                f"SELECT COUNT(*), "
                f"COALESCE(SUM(valor_orcado_total), 0), "
                f"COALESCE(SUM(valor_debito_total), 0), "
                f"COALESCE(SUM(valor_credito_total), 0), "
                f"COALESCE(SUM(valor_realizado_total), 0), "
                f"COALESCE(SUM(variacao_orcamentaria), 0) "
                f"FROM fct_capex_opex_variance {where_sql}",
                params,
            ).fetchone()

            detail_rows = conn.execute(
                f"SELECT sk_capex_opex_variance, competencia, cod_centro_custo, "
                f"cod_conta_contabil, tipo_despesa, moeda, "
                f"valor_orcado_total, valor_debito_total, valor_credito_total, "
                f"valor_realizado_total, variacao_orcamentaria, variacao_pct, qtd_lancamentos "
                f"FROM fct_capex_opex_variance {where_sql} "
                f"ORDER BY competencia, cod_centro_custo, cod_conta_contabil, tipo_despesa, moeda "
                f"LIMIT ?",
                [*params, limit],
            ).fetchall()

        if agg is None:
            raise RuntimeError("DuckDB aggregate query returned no rows.")
        group_count = int(agg[0])
        total_orcado = _as_decimal(agg[1])
        total_debito = _as_decimal(agg[2])
        total_credito = _as_decimal(agg[3])
        total_realizado = _as_decimal(agg[4])
        variacao = _as_decimal(agg[5])

        formatted_rows = [
            {
                "sk_capex_opex_variance": str(r[0]),
                "competencia": str(r[1]),
                "cod_centro_custo": str(r[2]),
                "cod_conta_contabil": str(r[3]),
                "tipo_despesa": str(r[4]),
                "moeda": str(r[5]),
                "valor_orcado_total": format_money(_as_decimal(r[6])),
                "valor_debito_total": format_money(_as_decimal(r[7])),
                "valor_credito_total": format_money(_as_decimal(r[8])),
                "valor_realizado_total": format_money(_as_decimal(r[9])),
                "variacao_orcamentaria": format_money(_as_decimal(r[10])),
                "variacao_pct": f"{_as_decimal(r[11], quant=RATE_QUANT):.6f}",
                "qtd_lancamentos": int(r[12]),
            }
            for r in detail_rows
        ]
        return _build_budget_variance_response(
            source_layer="gold_duckdb",
            competencia=competencia,
            cod_centro_custo=cod_centro_custo,
            tipo_despesa=tipo_despesa,
            group_count=group_count,
            total_orcado=total_orcado,
            total_debito=total_debito,
            total_credito=total_credito,
            total_realizado=total_realizado,
            variacao=variacao,
            rows=formatted_rows,
        )

    def _query_budget_variance_fallback(
        self,
        *,
        competencia: str | None,
        cod_centro_custo: str | None,
        tipo_despesa: str | None,
        limit: int,
    ) -> dict[str, Any]:
        silver_path = self.data_dir / "capex_opex_ledger" / "silver" / "ledger_entries.parquet"
        if silver_path.is_file():
            raw_rows = pq.read_table(silver_path).to_pylist()
            source_layer = "silver_parquet"
        else:
            raw_rows = generate_synthetic_ledger_rows(seed=self.seed)
            source_layer = "synthetic_memory"

        grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in raw_rows:
            comp = str(row["competencia"])
            cc = str(row["cod_centro_custo"])
            tipo = str(row["tipo_despesa"])
            if competencia is not None and comp != competencia:
                continue
            if cod_centro_custo is not None and cc != cod_centro_custo:
                continue
            if tipo_despesa is not None and tipo != tipo_despesa:
                continue

            key = (comp, cc, str(row["cod_conta_contabil"]), tipo, str(row["moeda"]))
            bucket = grouped.setdefault(
                key,
                {
                    "valor_orcado_total": ZERO_MONEY,
                    "valor_debito_total": ZERO_MONEY,
                    "valor_credito_total": ZERO_MONEY,
                    "qtd_lancamentos": 0,
                },
            )
            bucket["valor_orcado_total"] += to_decimal_money(row["valor_orcado"])
            bucket["valor_debito_total"] += to_decimal_money(row["valor_debito"])
            bucket["valor_credito_total"] += to_decimal_money(row["valor_credito"])
            bucket["qtd_lancamentos"] += 1

        total_orcado = ZERO_MONEY
        total_debito = ZERO_MONEY
        total_credito = ZERO_MONEY
        ordered_rows: list[dict[str, Any]] = []
        for key in sorted(grouped):
            comp, cc, cta, tipo, moeda = key
            b = grouped[key]
            orc = b["valor_orcado_total"]
            deb = b["valor_debito_total"]
            cred = b["valor_credito_total"]
            var = (orc - deb).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
            var_pct = _compute_variacao_pct(var, orc)
            total_orcado += orc
            total_debito += deb
            total_credito += cred
            ordered_rows.append(
                {
                    "sk_capex_opex_variance": f"{comp}|{cc}|{cta}|{tipo}|{moeda}",
                    "competencia": comp,
                    "cod_centro_custo": cc,
                    "cod_conta_contabil": cta,
                    "tipo_despesa": tipo,
                    "moeda": moeda,
                    "valor_orcado_total": format_money(orc),
                    "valor_debito_total": format_money(deb),
                    "valor_credito_total": format_money(cred),
                    "valor_realizado_total": format_money(deb),
                    "variacao_orcamentaria": format_money(var),
                    "variacao_pct": f"{var_pct:.6f}",
                    "qtd_lancamentos": b["qtd_lancamentos"],
                }
            )

        total_var = (total_orcado - total_debito).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
        return _build_budget_variance_response(
            source_layer=source_layer,
            competencia=competencia,
            cod_centro_custo=cod_centro_custo,
            tipo_despesa=tipo_despesa,
            group_count=len(ordered_rows),
            total_orcado=total_orcado,
            total_debito=total_debito,
            total_credito=total_credito,
            total_realizado=total_debito,
            variacao=total_var,
            rows=ordered_rows[:limit],
        )

    def budget_variance(
        self,
        *,
        competencia: str | None = None,
        cod_centro_custo: str | None = None,
        tipo_despesa: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Compute deterministic CAPEX/OPEX budget vs realized variance summary & breakdown."""
        comp = _clean_optional_filter(competencia)
        cc = _clean_optional_filter(cod_centro_custo)
        tipo = _clean_optional_filter(tipo_despesa)
        if tipo is not None:
            tipo = tipo.upper()
            if tipo not in _ALLOWED_EXPENSE_TYPES:
                raise ValueError(
                    f"tipo_despesa must be one of {sorted(_ALLOWED_EXPENSE_TYPES)}, got {tipo!r}."
                )
        valid_limit = _validate_positive_limit(limit)

        if self._has_gold_table("fct_capex_opex_variance"):
            return self._query_budget_variance_gold(
                competencia=comp,
                cod_centro_custo=cc,
                tipo_despesa=tipo,
                limit=valid_limit,
            )
        return self._query_budget_variance_fallback(
            competencia=comp,
            cod_centro_custo=cc,
            tipo_despesa=tipo,
            limit=valid_limit,
        )

    def _query_aging_priority_gold(
        self,
        *,
        faixa_aging: str | None,
        prioridade: str | None,
        limit: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if faixa_aging is not None:
            clauses.append("faixa_aging = ?")
            params.append(faixa_aging)
        if prioridade is not None:
            clauses.append("prioridade_cobranca = ?")
            params.append(prioridade)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with duckdb.connect(str(self.duckdb_path), read_only=True) as conn:
            bucket_rows = conn.execute(
                f"SELECT faixa_aging, COUNT(*), "
                f"COALESCE(SUM(valor_original), 0), "
                f"COALESCE(SUM(valor_pago), 0), "
                f"COALESCE(SUM(valor_em_aberto), 0), "
                f"COALESCE(SUM(valor_ecl), 0) "
                f"FROM fct_receivables_aging {where_sql} "
                f"GROUP BY faixa_aging",
                params,
            ).fetchall()

            top_rows = conn.execute(
                f"SELECT id_fatura_masked, id_contrato_masked, segmento_cliente, sigla_uf, "
                f"faixa_aging, dias_atraso, valor_original, valor_pago, valor_em_aberto, "
                f"valor_ecl, score_cobranca, prioridade_cobranca, ranking_cobranca, is_vencido "
                f"FROM fct_receivables_aging {where_sql} "
                f"ORDER BY ranking_cobranca ASC, id_fatura_masked ASC "
                f"LIMIT ?",
                [*params, limit],
            ).fetchall()

        bucket_map = {
            str(r[0]): (
                int(r[1]),
                _as_decimal(r[2]),
                _as_decimal(r[3]),
                _as_decimal(r[4]),
                _as_decimal(r[5]),
            )
            for r in bucket_rows
        }
        row_count = sum(v[0] for v in bucket_map.values())
        total_orig = sum((v[1] for v in bucket_map.values()), ZERO_MONEY)
        total_pago = sum((v[2] for v in bucket_map.values()), ZERO_MONEY)
        total_open = sum((v[3] for v in bucket_map.values()), ZERO_MONEY)
        total_ecl = sum((v[4] for v in bucket_map.values()), ZERO_MONEY)
        total_a_vencer = bucket_map.get("a_vencer", (0, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY))[3]
        total_vencido = sum(
            (
                bucket_map[b][3]
                for b in AGING_BUCKETS
                if b != "a_vencer" and b in bucket_map
            ),
            ZERO_MONEY,
        )

        buckets_summary: dict[str, dict[str, Any]] = {}
        for b in AGING_BUCKETS:
            cnt, _, _, b_open, b_ecl = bucket_map.get(
                b, (0, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY)
            )
            pct = (
                ((b_open / total_open) * Decimal("100")).quantize(
                    RATE_QUANT, rounding=ROUND_HALF_EVEN
                )
                if total_open > ZERO_MONEY
                else _ZERO_RATE
            )
            buckets_summary[b] = {
                "qtd_faturas": cnt,
                "valor_em_aberto": format_money(b_open),
                "valor_ecl": format_money(b_ecl),
                "pct_carteira": f"{pct:.6f}",
            }

        top_invoices = [
            {
                "id_fatura_masked": str(r[0]),
                "id_contrato_masked": str(r[1]),
                "segmento_cliente": str(r[2]),
                "sigla_uf": str(r[3]),
                "faixa_aging": str(r[4]),
                "dias_atraso": int(r[5]),
                "valor_original": format_money(_as_decimal(r[6])),
                "valor_pago": format_money(_as_decimal(r[7])),
                "valor_em_aberto": format_money(_as_decimal(r[8])),
                "valor_ecl": format_money(_as_decimal(r[9])),
                "score_cobranca": f"{_as_decimal(r[10], quant=RATE_QUANT):.6f}",
                "prioridade_cobranca": str(r[11]),
                "ranking_cobranca": int(r[12]),
                "is_vencido": bool(r[13]),
            }
            for r in top_rows
        ]
        reconciliation_ok = (total_pago + total_open == total_orig) and (
            total_a_vencer + total_vencido == total_open
        )
        return {
            "source_layer": "gold_duckdb",
            "filters": {"faixa_aging": faixa_aging, "prioridade": prioridade},
            "row_count": row_count,
            "returned_rows": len(top_invoices),
            "total_original": format_money(total_orig),
            "total_pago": format_money(total_pago),
            "total_em_aberto": format_money(total_open),
            "total_a_vencer": format_money(total_a_vencer),
            "total_vencido": format_money(total_vencido),
            "total_ecl": format_money(total_ecl),
            "reconciliation_ok": reconciliation_ok,
            "buckets": buckets_summary,
            "top_priority_invoices": top_invoices,
        }

    def _query_aging_priority_fallback(
        self,
        *,
        faixa_aging: str | None,
        prioridade: str | None,
        limit: int,
    ) -> dict[str, Any]:
        silver_path = (
            self.data_dir / "receivables_aging" / "silver" / "fct_receivables_aging.parquet"
        )
        if silver_path.is_file():
            fct_rows = pq.read_table(silver_path).to_pylist()
            source_layer = "silver_parquet"
        else:
            raw = generate_synthetic_invoices(seed=self.seed)
            stg = build_staging_invoices(raw, as_of_date="2025-12-31")
            fct_rows = score_and_rank_receivables(stg)
            source_layer = "synthetic_memory"

        filtered = [
            r
            for r in fct_rows
            if (faixa_aging is None or str(r["faixa_aging"]) == faixa_aging)
            and (prioridade is None or str(r["prioridade_cobranca"]) == prioridade)
        ]
        if filtered:
            portfolio = summarize_aging_portfolio(filtered)
            total_pago = sum((to_decimal_money(r["valor_pago"]) for r in filtered), ZERO_MONEY)
        else:
            total_pago = ZERO_MONEY
            portfolio = _empty_aging_portfolio()

        ordered = sorted(
            filtered,
            key=lambda r: (int(r["ranking_cobranca"]), str(r["id_fatura_masked"])),
        )[:limit]
        top_invoices = [
            {
                "id_fatura_masked": str(r["id_fatura_masked"]),
                "id_contrato_masked": str(r["id_contrato_masked"]),
                "segmento_cliente": str(r["segmento_cliente"]),
                "sigla_uf": str(r["sigla_uf"]),
                "faixa_aging": str(r["faixa_aging"]),
                "dias_atraso": int(r["dias_atraso"]),
                "valor_original": format_money(to_decimal_money(r["valor_original"])),
                "valor_pago": format_money(to_decimal_money(r["valor_pago"])),
                "valor_em_aberto": format_money(to_decimal_money(r["valor_em_aberto"])),
                "valor_ecl": format_money(to_decimal_money(r["valor_ecl"])),
                "score_cobranca": f"{to_decimal_rate(r['score_cobranca']):.6f}",
                "prioridade_cobranca": str(r["prioridade_cobranca"]),
                "ranking_cobranca": int(r["ranking_cobranca"]),
                "is_vencido": int(r["dias_atraso"]) > 0
                and to_decimal_money(r["valor_em_aberto"]) > ZERO_MONEY,
            }
            for r in ordered
        ]
        reconciled = bool(portfolio["reconciliation_ok"]) and (
            total_pago + to_decimal_money(portfolio["total_em_aberto"])
            == to_decimal_money(portfolio["total_original"])
        )
        return {
            "source_layer": source_layer,
            "filters": {"faixa_aging": faixa_aging, "prioridade": prioridade},
            "row_count": portfolio["row_count"],
            "returned_rows": len(top_invoices),
            "total_original": portfolio["total_original"],
            "total_pago": format_money(total_pago),
            "total_em_aberto": portfolio["total_em_aberto"],
            "total_a_vencer": portfolio["total_a_vencer"],
            "total_vencido": portfolio["total_vencido"],
            "total_ecl": portfolio["total_ecl"],
            "reconciliation_ok": reconciled,
            "buckets": portfolio["buckets"],
            "top_priority_invoices": top_invoices,
        }

    def aging_priority(
        self,
        *,
        faixa_aging: str | None = None,
        prioridade: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return receivables aging buckets, ECL summary, and top priority collection queue."""
        faixa = _clean_optional_filter(faixa_aging)
        if faixa is not None and faixa not in _ALLOWED_BUCKETS:
            raise ValueError(
                f"faixa_aging must be one of {list(AGING_BUCKETS)}, got {faixa!r}."
            )
        prio = _clean_optional_filter(prioridade)
        if prio is not None:
            prio = prio.upper()
            if prio not in _ALLOWED_PRIORITIES:
                raise ValueError(
                    f"prioridade must be one of {sorted(_ALLOWED_PRIORITIES)}, got {prio!r}."
                )
        valid_limit = _validate_positive_limit(limit)

        if self._has_gold_table("fct_receivables_aging"):
            return self._query_aging_priority_gold(
                faixa_aging=faixa,
                prioridade=prio,
                limit=valid_limit,
            )
        return self._query_aging_priority_fallback(
            faixa_aging=faixa,
            prioridade=prio,
            limit=valid_limit,
        )

    def b2b_roi(
        self,
        *,
        initial_investment: Decimal | int | str,
        monthly_cashflows: Sequence[Decimal | int | str],
        contract_revenue: Decimal | int | str,
        direct_delivery_cost: Decimal | int | str,
        annual_discount_rate: Decimal | int | str = Decimal("0.150000"),
        monthly_inaction_cost: Decimal | int | str = ZERO_MONEY,
        delay_months: int = 3,
        target_gross_margin: Decimal | int | str = Decimal("0.650000"),
    ) -> dict[str, Any]:
        """Evaluate B2B deal ROI, NPV, IRR, Payback, Gross Margin Gate & Cost of Inaction."""
        return evaluate_b2b_deal(
            initial_investment=initial_investment,
            monthly_cashflows=monthly_cashflows,
            contract_revenue=contract_revenue,
            direct_delivery_cost=direct_delivery_cost,
            annual_discount_rate=annual_discount_rate,
            monthly_inaction_cost=monthly_inaction_cost,
            delay_months=delay_months,
            target_gross_margin=target_gross_margin,
        )

    def career_parity(
        self,
        *,
        clt_gross_monthly: Decimal | int | str,
        pj_gross_monthly: Decimal | int | str,
        contractor_fx_monthly: Decimal | int | str,
        fx_currency: str = "USD",
        vr_va_monthly: Decimal | int | str = Decimal("1200.0000"),
        health_monthly: Decimal | int | str = Decimal("800.0000"),
        iss_rate: Decimal | int | str = DEFAULT_ISS_MANAUS_SIMPLES,
        pro_labore_ratio: Decimal | int | str = Decimal("0.280000"),
        prefer_live_gateway: bool = False,
        seed: int | None = None,
        n_scenarios: int = 10_000,
    ) -> dict[str, Any]:
        """Compute CLT vs PJ Simples Nacional (ISS Manaus) vs USD/EUR parity + Monte Carlo."""
        if not isinstance(prefer_live_gateway, bool):
            raise TypeError("prefer_live_gateway must be a boolean.")
        if not isinstance(fx_currency, str):
            raise TypeError(f"fx_currency must be a string, got {type(fx_currency).__name__}.")
        currency = fx_currency.strip().upper()
        if currency not in _ALLOWED_FX_CURRENCIES:
            raise ValueError(
                f"fx_currency must be one of {sorted(_ALLOWED_FX_CURRENCIES)}, got {currency!r}."
            )
        valid_seed = self.seed if seed is None else require_non_negative_int(seed, "seed")
        valid_scenarios = require_non_negative_int(n_scenarios, "n_scenarios")
        if valid_scenarios <= 0 or valid_scenarios > 50_000:
            raise ValueError(f"n_scenarios must be between 1 and 50000, got {valid_scenarios}.")

        macro = load_macro_rates(prefer_live_gateway=prefer_live_gateway)
        result = compare_career_offers(
            clt_gross_monthly=clt_gross_monthly,
            pj_gross_monthly=pj_gross_monthly,
            contractor_fx_monthly=contractor_fx_monthly,
            fx_currency=currency,
            vr_va_monthly=vr_va_monthly,
            health_monthly=health_monthly,
            iss_rate=iss_rate,
            pro_labore_ratio=pro_labore_ratio,
            macro_rates=macro,
            seed=valid_seed,
            n_scenarios=valid_scenarios,
        )
        return {
            "macro_source": macro.source,
            "macro_reference_date": macro.reference_date,
            "ptax_usd_brl": f"{macro.ptax_usd_brl:.6f}",
            "ptax_eur_brl": f"{macro.ptax_eur_brl:.6f}",
            "selic_annual_pct": f"{macro.selic_annual_pct:.6f}",
            "ipca_12m_pct": f"{macro.ipca_12m_pct:.6f}",
            **result,
        }

    def ai_finops(
        self,
        *,
        telemetry_path: Path | str | None = None,
        cache_hit_ratio: Decimal | int | str = Decimal("0.400000"),
        cache_discount_rate: Decimal | int | str = DEFAULT_CACHE_DISCOUNT_RATE,
        batch_eligible_ratio: Decimal | int | str = Decimal("0.300000"),
        batch_discount_rate: Decimal | int | str = DEFAULT_BATCH_DISCOUNT_RATE,
        enable_deepseek_offpeak: bool = True,
    ) -> dict[str, Any]:
        """Summarize AI/Cloud FinOps consumption and cache/batch/off-peak savings waterfall."""
        hit_ratio = validate_unit_rate(cache_hit_ratio, "cache_hit_ratio")
        cache_disc = validate_unit_rate(cache_discount_rate, "cache_discount_rate")
        batch_ratio = validate_unit_rate(batch_eligible_ratio, "batch_eligible_ratio")
        batch_disc = validate_unit_rate(batch_discount_rate, "batch_discount_rate")
        if not isinstance(enable_deepseek_offpeak, bool):
            raise TypeError("enable_deepseek_offpeak must be a boolean.")

        if telemetry_path is not None:
            ingested = read_telemetry_jsonl(Path(telemetry_path))
            spans = ingested.spans
            if not spans:
                raise ValueError(
                    f"No valid telemetry spans found in '{telemetry_path}' "
                    f"(quarantined={ingested.quarantined_count})."
                )
            source_layer = "jsonl_file"
        elif self._has_gold_table("fct_ai_finops_consumption"):
            with duckdb.connect(str(self.duckdb_path), read_only=True) as conn:
                rows = conn.execute(
                    "SELECT id_lancamento, timestamp_utc, competencia, cod_centro_custo, "
                    "skill, operation, model, provider, tier, tokens_in, tokens_out, "
                    "cached_tokens_in, cost_usd, status "
                    "FROM fct_ai_finops_consumption ORDER BY id_lancamento"
                ).fetchall()
            spans = [
                {
                    "id_lancamento": str(r[0]),
                    "timestamp_utc": str(r[1]),
                    "competencia": str(r[2]),
                    "cod_centro_custo": str(r[3]),
                    "skill": str(r[4]),
                    "operation": str(r[5]),
                    "model": str(r[6]),
                    "provider": str(r[7]),
                    "tier": str(r[8]),
                    "tokens_in": int(r[9]),
                    "tokens_out": int(r[10]),
                    "cached_tokens_in": int(r[11]),
                    "cost_usd": _as_decimal(r[12]),
                    "status": str(r[13]),
                }
                for r in rows
            ]
            source_layer = "gold_duckdb"
        else:
            silver_path = (
                self.data_dir / "ai_cloud_finops" / "silver" / "ai_finops_telemetry.parquet"
            )
            if silver_path.is_file():
                spans = pq.read_table(silver_path).to_pylist()
                source_layer = "silver_parquet"
            else:
                spans = generate_synthetic_telemetry(seed=self.seed)
                source_layer = "synthetic_memory"

        summary = summarize_ai_finops(
            spans,
            cache_hit_ratio=hit_ratio,
            cache_discount_rate=cache_disc,
            batch_eligible_ratio=batch_ratio,
            batch_discount_rate=batch_disc,
            enable_deepseek_offpeak=enable_deepseek_offpeak,
        )
        return {
            "source_layer": source_layer,
            **summary.to_dict(),
        }
