"""CLI entrypoint (`fcl`) delegating all financial domains to FinOpsLakehouse SDK."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from finops_lakehouse import __version__
from finops_lakehouse.calculators.career_parity import DEFAULT_ISS_MANAUS_SIMPLES
from finops_lakehouse.core.decimal_utils import ZERO_MONEY
from finops_lakehouse.core.shield import configure_stdio_utf8
from finops_lakehouse.finops_ai.pricing_catalog import (
    DEFAULT_BATCH_DISCOUNT_RATE,
    DEFAULT_CACHE_DISCOUNT_RATE,
)
from finops_lakehouse.sdk import FinOpsLakehouse


def get_status_payload(lakehouse: FinOpsLakehouse | None = None) -> dict[str, Any]:
    """Return foundational runtime and precision governance status."""
    client = lakehouse or FinOpsLakehouse()
    return client.get_status()


def _parse_cashflows_arg(raw: str) -> list[str]:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("monthly-cashflows cannot be empty.")
    if cleaned.startswith("["):
        loaded = json.loads(cleaned)
        if not isinstance(loaded, list) or not loaded:
            raise ValueError("monthly-cashflows JSON must be a non-empty list.")
        for item in loaded:
            if isinstance(item, (float, bool)):
                raise TypeError("IEEE 754 float/bool forbidden in monthly-cashflows.")
        return [str(item) for item in loaded]
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if not parts:
        raise ValueError("monthly-cashflows must contain at least one numeric amount.")
    return parts


def _build_common_parent() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override Medallion data directory (default: FCL_DATA_DIR or <repo>/data).",
    )
    common.add_argument(
        "--duckdb-path",
        type=Path,
        default=None,
        help="Override Gold DuckDB database path.",
    )
    common.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic synthetic seed (default: 42).",
    )
    common.add_argument("--json", action="store_true", help="Emit canonical JSON output.")
    return common


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for `fcl` across all financial domains."""
    common = _build_common_parent()
    parser = argparse.ArgumentParser(
        prog="fcl",
        description="FinOps, CAPEX/OPEX & Revenue Recovery Medallion Lakehouse CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", parents=[common], help="Display lakehouse runtime status.")

    bv = subparsers.add_parser(
        "budget-variance",
        parents=[common],
        help="Query CAPEX/OPEX budget vs realized variance.",
    )
    bv.add_argument("--competencia", type=str, default=None, help="Filter period YYYY-MM.")
    bv.add_argument("--centro-custo", type=str, default=None, help="Filter cost center code.")
    bv.add_argument("--tipo-despesa", type=str, default=None, help="Filter CAPEX or OPEX.")
    bv.add_argument("--limit", type=int, default=20, help="Max detail rows (1..1000).")

    ag = subparsers.add_parser(
        "aging-priority",
        parents=[common],
        help="Query receivables aging buckets, ECL, and collection ranking.",
    )
    ag.add_argument("--faixa-aging", type=str, default=None, help="Filter aging bucket.")
    ag.add_argument("--prioridade", type=str, default=None, help="Filter collection tier.")
    ag.add_argument("--limit", type=int, default=20, help="Max priority rows (1..1000).")

    b2b = subparsers.add_parser(
        "b2b-roi",
        parents=[common],
        help="Evaluate B2B deal economics (NPV, IRR, Payback, Margin Gate >= 65%%, COI).",
    )
    b2b.add_argument("--initial-investment", type=str, required=True, help="Decimal capex outlay.")
    b2b.add_argument(
        "--monthly-cashflows",
        type=str,
        required=True,
        help="Comma-separated monthly decimal cashflows or JSON list.",
    )
    b2b.add_argument("--contract-revenue", type=str, required=True, help="Total revenue (Decimal).")
    b2b.add_argument("--direct-delivery-cost", type=str, required=True, help="Direct cost (Decimal).")
    b2b.add_argument("--annual-discount-rate", type=str, default="0.150000", help="Hurdle rate.")
    b2b.add_argument("--monthly-inaction-cost", type=str, default=str(ZERO_MONEY))
    b2b.add_argument("--delay-months", type=int, default=3)
    b2b.add_argument("--target-gross-margin", type=str, default="0.650000")

    cp = subparsers.add_parser(
        "career-parity",
        parents=[common],
        help="Compare CLT vs PJ Simples Nacional (ISS Manaus) vs USD/EUR + 10k Monte Carlo.",
    )
    cp.add_argument("--clt-gross", type=str, required=True, help="Monthly CLT gross BRL.")
    cp.add_argument("--pj-gross", type=str, required=True, help="Monthly PJ gross BRL.")
    cp.add_argument("--contractor-fx", type=str, required=True, help="Monthly USD/EUR gross.")
    cp.add_argument("--fx-currency", type=str, default="USD", help="USD or EUR.")
    cp.add_argument("--vr-va", type=str, default="1200.0000", help="Monthly VR/VA BRL.")
    cp.add_argument("--health", type=str, default="800.0000", help="Monthly health benefit BRL.")
    cp.add_argument("--iss-rate", type=str, default=str(DEFAULT_ISS_MANAUS_SIMPLES))
    cp.add_argument("--pro-labore-ratio", type=str, default="0.280000")
    cp.add_argument("--n-scenarios", type=int, default=10_000)

    ai = subparsers.add_parser(
        "ai-finops",
        parents=[common],
        help="Summarize AI/Cloud FinOps consumption (POP_T_158) and optimization levers.",
    )
    ai.add_argument("--telemetry-path", type=Path, default=None, help="Optional JSONL telemetry path.")
    ai.add_argument("--cache-hit-ratio", type=str, default="0.400000")
    ai.add_argument("--cache-discount-rate", type=str, default=str(DEFAULT_CACHE_DISCOUNT_RATE))
    ai.add_argument("--batch-eligible-ratio", type=str, default="0.300000")
    ai.add_argument("--batch-discount-rate", type=str, default=str(DEFAULT_BATCH_DISCOUNT_RATE))
    ai.add_argument(
        "--disable-deepseek-offpeak",
        action="store_true",
        help="Disable DeepSeek Off-Peak UTC (-50%%) optimization simulation.",
    )
    return parser


def _dispatch_command(args: argparse.Namespace) -> dict[str, Any]:
    lakehouse = FinOpsLakehouse(
        data_dir=args.data_dir,
        duckdb_path=args.duckdb_path,
        seed=args.seed,
    )
    cmd = args.command
    if cmd == "status":
        return lakehouse.get_status()
    if cmd == "budget-variance":
        return lakehouse.budget_variance(
            competencia=args.competencia,
            cod_centro_custo=args.centro_custo,
            tipo_despesa=args.tipo_despesa,
            limit=args.limit,
        )
    if cmd == "aging-priority":
        return lakehouse.aging_priority(
            faixa_aging=args.faixa_aging,
            prioridade=args.prioridade,
            limit=args.limit,
        )
    if cmd == "b2b-roi":
        return lakehouse.b2b_roi(
            initial_investment=args.initial_investment,
            monthly_cashflows=_parse_cashflows_arg(args.monthly_cashflows),
            contract_revenue=args.contract_revenue,
            direct_delivery_cost=args.direct_delivery_cost,
            annual_discount_rate=args.annual_discount_rate,
            monthly_inaction_cost=args.monthly_inaction_cost,
            delay_months=args.delay_months,
            target_gross_margin=args.target_gross_margin,
        )
    if cmd == "career-parity":
        return lakehouse.career_parity(
            clt_gross_monthly=args.clt_gross,
            pj_gross_monthly=args.pj_gross,
            contractor_fx_monthly=args.contractor_fx,
            fx_currency=args.fx_currency,
            vr_va_monthly=args.vr_va,
            health_monthly=args.health,
            iss_rate=args.iss_rate,
            pro_labore_ratio=args.pro_labore_ratio,
            seed=args.seed,
            n_scenarios=args.n_scenarios,
        )
    if cmd == "ai-finops":
        return lakehouse.ai_finops(
            telemetry_path=args.telemetry_path,
            cache_hit_ratio=args.cache_hit_ratio,
            cache_discount_rate=args.cache_discount_rate,
            batch_eligible_ratio=args.batch_eligible_ratio,
            batch_discount_rate=args.batch_discount_rate,
            enable_deepseek_offpeak=not args.disable_deepseek_offpeak,
        )
    raise ValueError(f"Unsupported CLI command: {cmd}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning explicit exit codes."""
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        payload = _dispatch_command(args)
    except (TypeError, ValueError, OSError, json.JSONDecodeError, InvalidOperation) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if not args.json and args.command == "status":
        print(
            f"{payload['package']} v{payload['version']} | "
            f"{payload['monetary_precision']} ({payload['rounding_mode']}) | "
            f"status={payload['status']}"
        )
        return 0

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
